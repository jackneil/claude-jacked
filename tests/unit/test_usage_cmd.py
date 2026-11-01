"""Tests for `jacked usage` — the subscription-usage snapshot CLI.

The command powers autonomous-loop pacing (night-shift skill): loops read
--json to decide whether to pause until a rate-limit window resets. These
tests pin the JSON contract that skill depends on:

- accounts[] rows are field-ALLOWLISTED (token columns must never appear);
- summary.best_account_* considers only eligible accounts (active, not
  deleted, validation_status != "invalid") and applies staleness: a percent
  is stale headroom (-> effective 0) only when it PREDATES a turnover
  (usage_cached_at < resets_at <= now); a fresh percent beside an old
  reset row stays live;
- summary.pause_until is the earliest time some eligible account becomes
  WORKABLE: per account the LATEST future reset among its CONSTRAINED
  (>= 90% effective) windows, min'd across accounts -- an idle window's
  reset never sets the pause target (the wake/pause-treadmill bug), and
  one window resetting while the account's other window stays constrained
  does not either (the intra-account premature-wake bug);
- one hostile row (TEXT percent, naive timestamp) degrades, never crashes;
- windows are plan-aware: a plan without a 5-hour window (a verified Ultra,
  ChatGPT Pro) reports null 5h fields, and its worst window is the weekly
  one even when a stale 5h value lingers in the cache;
- ``selected`` comes from jacked's own database (desired account first),
  and ``in_use`` from the session tracker by process ancestry, never the
  credential store;
- ``--provider`` limits the rows AND the summary to one provider.
"""

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from jacked.cli import main
from jacked.web.database import Database

ROW_KEYS = {
    "id", "provider", "email", "subscription_type", "is_active",
    "validation_status", "usage_5h_pct", "usage_7d_pct",
    "resets_5h_at", "resets_7d_at", "cache_age_seconds",
    "plan", "plan_id", "session_window", "model_limits",
    "worst_window_pct", "worst_window", "worst_window_resets_at", "selected",
    "in_use",
}
MODEL_LIMIT_KEYS = {"label", "pct", "resets_at", "window_seconds"}


@pytest.fixture(autouse=True)
def _no_calling_session(monkeypatch):
    """No test walks the real process table: by default no tracked session
    runs the command. Tests of ``in_use`` set the ancestry themselves."""
    import jacked.session_ancestry as ancestry

    monkeypatch.setattr(ancestry, "ancestor_pids", lambda *a, **k: [])


def _seed_db(home: Path, accounts: list[dict]) -> list[int]:
    """Create the accounts and return their ids, in order."""
    db_path = home / ".claude" / "jacked.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(db_path))
    ids: list[int] = []
    try:
        for acct in accounts:
            row = db.create_account(
                email=acct["email"],
                access_token="tok-secret",
                expires_at=9999999999,
                refresh_token="ref-secret",
                organization_uuid=acct.get("organization_uuid", "org-" + acct["email"]),
                provider=acct.get("provider", "claude"),
            )
            ids.append(row["id"])
            updates = {
                k: v for k, v in acct.items()
                if k in {
                    "cached_usage_5h", "cached_usage_7d",
                    "cached_5h_resets_at", "cached_7d_resets_at",
                    "usage_cached_at", "subscription_type",
                    "validation_status", "is_active",
                    "rate_limit_tier", "cached_usage_raw", "profile_json",
                }
            }
            if updates:
                db.update_account(row["id"], **updates)
    finally:
        db.close()
    return ids


def _ultra_raw(resets_7d: str) -> str:
    """A verified Ultra payload (live shape, 2026-11-01): no session limit."""
    return json.dumps({
        "five_hour": None,
        "seven_day": {"utilization": 33.0, "resets_at": resets_7d},
        "limits": [
            {"kind": "weekly_all", "group": "weekly", "percent": 33,
             "resets_at": resets_7d, "scope": None, "is_active": False},
            {"kind": "weekly_scoped", "group": "weekly", "percent": 61,
             "resets_at": resets_7d, "is_active": True,
             "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
        ],
    })


def _max_raw(resets_5h: str, resets_7d: str) -> str:
    """A Max (formerly Max 5x) payload: session + weekly limits."""
    return json.dumps({
        "five_hour": {"utilization": 67.0, "resets_at": resets_5h},
        "seven_day": {"utilization": 23.0, "resets_at": resets_7d},
        "limits": [
            {"kind": "session", "group": "session", "percent": 67,
             "resets_at": resets_5h, "scope": None, "is_active": True},
            {"kind": "weekly_all", "group": "weekly", "percent": 23,
             "resets_at": resets_7d, "scope": None, "is_active": False},
        ],
    })


def _codex_pro_raw(resets_epoch: int) -> str:
    """A ChatGPT Pro Codex payload: one weekly primary window, no 5h window."""
    return json.dumps({"rateLimits": {
        "primary": {"usedPercent": 38, "windowDurationMins": 10080, "resetsAt": resets_epoch},
        "secondary": None,
        "planType": "pro",
    }})


def _forbid_credential_store(monkeypatch) -> list:
    """Make every credential-store read fail loudly; return the touch log."""
    import jacked.api.credential_helpers as helpers
    from jacked.credentials import file_store, macos_store

    touched: list = []

    def _boom(*_args, **_kwargs):
        touched.append(True)
        raise AssertionError("jacked usage must never read the credential store")

    monkeypatch.setattr(macos_store.MacOSCredentialStore, "read", _boom)
    monkeypatch.setattr(macos_store.SecurityCliBackend, "read", _boom)
    monkeypatch.setattr(file_store.FileCredentialStore, "read", _boom)
    monkeypatch.setattr(helpers, "read_platform_credentials", _boom)
    return touched


def _table_row(output: str, email: str) -> dict:
    """The human table's cells for *email*, keyed by column header."""
    header = next(
        line for line in output.splitlines() if "Plan" in line and "Model" in line
    )
    row = next(line for line in output.splitlines() if email in line)

    def cells(line: str) -> list[str]:
        return [c.strip() for c in re.split(r"[│┃|]", line)[1:-1]]

    return dict(zip(cells(header), cells(row)))


def _run_json(args=("usage", "--json")):
    result = CliRunner().invoke(main, list(args))
    assert result.exit_code == 0, result.output
    return result, json.loads(result.output)


def test_usage_json_no_db(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _, payload = _run_json()
    assert payload["available"] is False


def test_usage_json_contract_and_no_secrets(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    future_5h = (now + timedelta(hours=2)).isoformat()
    future_7d = (now + timedelta(days=3)).isoformat()
    past = (now - timedelta(hours=1)).isoformat()
    _seed_db(tmp_path, [
        {
            "email": "fresh@test.com",
            "subscription_type": "max",
            "validation_status": "valid",
            "cached_usage_5h": 10.0,
            "cached_usage_7d": 20.0,
            "cached_5h_resets_at": future_5h,
            "cached_7d_resets_at": future_7d,
            "usage_cached_at": int(now.timestamp()) - 60,
        },
        {
            # 5h reset is PAST but the 5% was cached AFTER it (now-120 >
            # now-3600), so it is post-turnover truth, NOT stale headroom --
            # and at 5% it is idle either way.
            # 7d at 91% with a future reset is the only CONSTRAINED window.
            "email": "burned@test.com",
            "subscription_type": "max",
            "validation_status": "valid",
            "cached_usage_5h": 5.0,
            "cached_usage_7d": 91.0,
            "cached_5h_resets_at": past,
            "cached_7d_resets_at": future_7d,
            "usage_cached_at": int(now.timestamp()) - 120,
        },
    ])

    result, payload = _run_json()
    assert payload["available"] is True
    assert len(payload["accounts"]) == 2
    for row in payload["accounts"]:
        # Allowlist tripwire: a refactor to a row-spread must fail loudly.
        assert set(row) == ROW_KEYS
    # Seeded token material must never reach stdout in either mode.
    assert "tok-secret" not in result.output
    assert "ref-secret" not in result.output

    by_email = {a["email"]: a for a in payload["accounts"]}
    assert by_email["burned@test.com"]["usage_7d_pct"] == 91.0
    assert by_email["fresh@test.com"]["cache_age_seconds"] >= 60

    summary = payload["summary"]
    assert summary["accounts_with_usage_data"] == 2
    assert summary["best_account_email"] == "fresh@test.com"
    assert summary["best_account_worst_window_pct"] == 20.0
    assert summary["best_account_cache_age_seconds"] >= 60
    # pause_until = the CONSTRAINED window's future reset (burned@ 7d),
    # NEVER an idle window's earlier reset (fresh@ 5h in 2h) -- that earlier
    # semantic caused a wake/pause treadmill in the consuming loop.
    assert summary["pause_until"] == future_7d


def test_invalid_account_excluded_from_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    future_5h = (now + timedelta(hours=4)).isoformat()
    future_7d = (now + timedelta(days=2)).isoformat()
    _seed_db(tmp_path, [
        # Revoked account keeps stale 0% forever; it must not mask exhaustion.
        {"email": "dead@test.com", "validation_status": "invalid",
         "cached_usage_5h": 0.0, "cached_usage_7d": 0.0},
        {"email": "live@test.com", "validation_status": "valid",
         "cached_usage_5h": 97.0, "cached_usage_7d": 98.0,
         "cached_5h_resets_at": future_5h, "cached_7d_resets_at": future_7d},
    ])
    _, payload = _run_json()
    summary = payload["summary"]
    assert summary["best_account_email"] == "live@test.com"
    assert summary["best_account_worst_window_pct"] == 98.0
    # Both live windows are constrained; the account is workable only once
    # the LATER one resets (at future_5h the 7d window is still at 98%).
    assert summary["pause_until"] == future_7d
    # The invalid row still appears in accounts[] (visibility), just not summary.
    assert {a["email"] for a in payload["accounts"]} == {"dead@test.com", "live@test.com"}


def test_stale_high_percent_is_headroom(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    past = (now - timedelta(minutes=30)).isoformat()
    future_7d = (now + timedelta(days=1)).isoformat()
    _seed_db(tmp_path, [
        # 95% on a window whose reset already passed = the window reset;
        # the account is actually ready to work (auto_swap selection rule).
        {"email": "ready@test.com", "validation_status": "valid",
         "cached_usage_5h": 95.0, "cached_usage_7d": 10.0,
         "cached_5h_resets_at": past, "cached_7d_resets_at": future_7d},
    ])
    _, payload = _run_json()
    summary = payload["summary"]
    assert summary["best_account_worst_window_pct"] == 10.0
    assert summary["pause_until"] is None  # nothing genuinely constrained


def test_hostile_rows_degrade_never_crash(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    naive = (now + timedelta(hours=1)).replace(tzinfo=None).isoformat()  # no offset
    db_path = tmp_path / ".claude" / "jacked.db"
    _seed_db(tmp_path, [
        {"email": "healthy@test.com", "validation_status": "valid",
         "cached_usage_5h": 30.0, "cached_usage_7d": 40.0},
    ])
    # Hostile shapes bypass the typed API: TEXT percent + naive timestamp.
    import sqlite3
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "INSERT INTO accounts (email, organization_uuid, access_token, expires_at,"
        " cached_usage_5h, cached_5h_resets_at, is_active)"
        " VALUES ('hostile@test.com', 'org-h', 'tok-secret', 9999999999, '91%', ?, 1)",
        (naive,),
    )
    conn.commit()
    conn.close()

    result, payload = _run_json()
    by_email = {a["email"]: a for a in payload["accounts"]}
    assert by_email["hostile@test.com"]["usage_5h_pct"] is None  # coerced, not crashed
    assert payload["summary"]["best_account_email"] == "healthy@test.com"
    # Table mode survives the same rows.
    table = CliRunner().invoke(main, ["usage"])
    assert table.exit_code == 0, table.output


def test_usage_json_no_cached_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_db(tmp_path, [{"email": "new@test.com"}])
    _, payload = _run_json()
    assert payload["available"] is True
    summary = payload["summary"]
    assert summary["accounts_with_usage_data"] == 0
    assert summary["best_account_email"] is None
    assert summary["best_account_worst_window_pct"] is None
    assert summary["pause_until"] is None


def test_include_inactive_and_codex_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_db(tmp_path, [
        {"email": "on@test.com", "cached_usage_5h": 10.0},
        {"email": "off@test.com", "cached_usage_5h": 20.0, "is_active": 0},
        {"email": "codex@test.com", "provider": "codex", "cached_usage_7d": 50.0},
    ])
    _, payload = _run_json()
    emails = {a["email"] for a in payload["accounts"]}
    assert "off@test.com" not in emails
    by_email = {a["email"]: a for a in payload["accounts"]}
    assert by_email["codex@test.com"]["provider"] == "codex"

    _, payload = _run_json(("usage", "--json", "--include-inactive"))
    by_email = {a["email"]: a for a in payload["accounts"]}
    assert by_email["off@test.com"]["is_active"] is False


def test_partial_window_data_still_counts(tmp_path, monkeypatch):
    """Documented semantic: an account missing one window is still summarized
    from the window it HAS (a Codex weekly-only account genuinely has no 5h
    window). Partial-write staleness is accepted; resets_at staleness is the
    guarded case."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_db(tmp_path, [
        {"email": "weekly@test.com", "validation_status": "valid",
         "cached_usage_7d": 3.0},
    ])
    _, payload = _run_json()
    assert payload["summary"]["best_account_email"] == "weekly@test.com"
    assert payload["summary"]["best_account_worst_window_pct"] == 3.0


def test_usage_human_table(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("COLUMNS", "300")  # keep rich from truncating cells
    _seed_db(tmp_path, [{
        "email": "fresh@test.com",
        "cached_usage_5h": 10.0,
        "cached_usage_7d": 20.0,
    }])
    result = CliRunner().invoke(main, ["usage"])
    assert result.exit_code == 0
    assert "fresh@test.com" in result.output
    assert "tok-secret" not in result.output


def test_fresh_percent_with_old_reset_row_is_not_stale(tmp_path, monkeypatch):
    """The Wave-2 CRITICAL inversion: percent and resets_at are written
    independently, so a FRESH 97% paired with the PREVIOUS window's past
    reset must stay 97% (staleness requires the percent to PREDATE the
    turnover: usage_cached_at < resets_at <= now)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    past_reset = (now - timedelta(hours=1)).isoformat()
    _seed_db(tmp_path, [
        {"email": "fresh97@test.com", "validation_status": "valid",
         "cached_usage_5h": 97.0,
         "cached_5h_resets_at": past_reset,
         # percent cached AFTER the reset passed -> it is post-turnover truth
         "usage_cached_at": int(now.timestamp()) - 60},
    ])
    _, payload = _run_json()
    assert payload["summary"]["best_account_worst_window_pct"] == 97.0


def test_percent_predating_turnover_is_stale_headroom(tmp_path, monkeypatch):
    """The LIVE staleness branch (usage_cached_at < resets_at <= now): a 95%
    cached BEFORE a reset that has since passed is the previous window's
    number -- the window turned over, the account is ready. This is the
    original stale-95%-blocks-a-ready-account bug; production rows always
    carry usage_cached_at, so THIS branch (not the no-stamp one) is the one
    that guards it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    reset = now - timedelta(minutes=30)
    _seed_db(tmp_path, [
        {"email": "turned@test.com", "validation_status": "valid",
         "cached_usage_5h": 95.0, "cached_usage_7d": 10.0,
         "cached_5h_resets_at": reset.isoformat(),
         "cached_7d_resets_at": (now + timedelta(days=1)).isoformat(),
         # percent cached BEFORE the reset -> it predates the turnover
         "usage_cached_at": int(reset.timestamp()) - 60},
    ])
    _, payload = _run_json()
    summary = payload["summary"]
    assert summary["best_account_worst_window_pct"] == 10.0
    assert summary["pause_until"] is None


def test_intra_account_earlier_reset_never_sets_wake(tmp_path, monkeypatch):
    """One window resetting while the SAME account's other window is still
    constrained does not make the account workable -- the earlier reset
    must not become pause_until (the loop would wake to a wall)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    cached = int(now.timestamp()) - 60
    in_8h = (now + timedelta(hours=8)).isoformat()
    _seed_db(tmp_path, [
        {"email": "best@test.com", "validation_status": "valid",
         "cached_usage_5h": 92.0, "cached_5h_resets_at": in_8h,
         "usage_cached_at": cached},
        {"email": "b@test.com", "validation_status": "valid",
         "cached_usage_5h": 99.0,
         "cached_5h_resets_at": (now + timedelta(minutes=20)).isoformat(),
         "cached_usage_7d": 99.0,
         "cached_7d_resets_at": (now + timedelta(days=4)).isoformat(),
         "usage_cached_at": cached},
    ])
    _, payload = _run_json()
    # b@'s 5h reset (+20m) frees nothing (its 7d wall stands until +4d);
    # the earliest genuine workability is best@'s single window at +8h.
    assert payload["summary"]["pause_until"] == in_8h


def test_constrained_without_reset_time_yields_null_pause_until(tmp_path, monkeypatch):
    """A constrained window whose row carries no resets_at produces a
    constrained best pct WITH a null pause_until — consumers must read that
    as 'pause time unknown', so the contract deliberately allows it."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    _seed_db(tmp_path, [
        {"email": "walled@test.com", "validation_status": "valid",
         "cached_usage_5h": 97.0,
         "usage_cached_at": int(now.timestamp()) - 30},
    ])
    _, payload = _run_json()
    assert payload["summary"]["best_account_worst_window_pct"] == 97.0
    assert payload["summary"]["pause_until"] is None


def test_include_inactive_rows_visible_but_never_best(tmp_path, monkeypatch):
    """--include-inactive shows disabled rows, but a disabled account (with
    its frozen stale cache) must never be crowned best_account (SQLite hands
    back 0, not False — the truthiness bug found in Wave 2)."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _seed_db(tmp_path, [
        {"email": "disabled@test.com", "is_active": 0, "validation_status": "valid",
         "cached_usage_5h": 0.0},
        {"email": "only@test.com", "validation_status": "valid",
         "cached_usage_5h": 60.0},
    ])
    _, payload = _run_json(("usage", "--json", "--include-inactive"))
    assert {a["email"] for a in payload["accounts"]} == {"disabled@test.com", "only@test.com"}
    assert payload["summary"]["best_account_email"] == "only@test.com"


def test_unavailable_payload_shape_is_pinned(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _, payload = _run_json()
    assert set(payload) == {"available", "reason"}


def test_usage_pacing_doctests_run():
    """CI runs bare pytest with testpaths=["tests"] and no --doctest-modules,
    so the truth-table doctests in usage_pacing would otherwise never execute.
    Run them here explicitly."""
    import doctest

    from jacked.service import usage_pacing

    results = doctest.testmod(usage_pacing)
    assert results.attempted > 0
    assert results.failed == 0


def test_table_constrained_line_wording(tmp_path, monkeypatch):
    """The human table's reset line must not claim the fleet is exhausted
    when only one account's window is constrained."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("COLUMNS", "300")
    now = datetime.now(timezone.utc)
    future = (now + timedelta(hours=3)).isoformat()
    _seed_db(tmp_path, [
        {"email": "idle@test.com", "validation_status": "valid",
         "cached_usage_5h": 5.0, "usage_cached_at": int(now.timestamp())},
        {"email": "burned@test.com", "validation_status": "valid",
         "cached_usage_5h": 99.0, "cached_5h_resets_at": future,
         "usage_cached_at": int(now.timestamp())},
    ])
    result = CliRunner().invoke(main, ["usage"])
    assert result.exit_code == 0
    assert "Earliest constrained-window reset" in result.output
    assert "All eligible accounts constrained" not in result.output


# --------------------------------------------------------------------------- #
# Plan-aware windows, active account, provider filter
# --------------------------------------------------------------------------- #


def test_verified_ultra_row_reports_no_5h_window(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    future_5h = (now + timedelta(hours=2)).isoformat()
    future_7d = (now + timedelta(days=3)).isoformat()
    _seed_db(tmp_path, [{
        "email": "ultra@test.com", "validation_status": "valid",
        "subscription_type": "max", "rate_limit_tier": "default_claude_max_20x",
        "cached_usage_raw": _ultra_raw(future_7d),
        # A stale 5h reading from before verification, HIGHER than the 7d
        # one: if anything still counted it, the worst window would be "5h".
        "cached_usage_5h": 88.0, "cached_5h_resets_at": future_5h,
        "cached_usage_7d": 33.0, "cached_7d_resets_at": future_7d,
        "usage_cached_at": int(now.timestamp()) - 60,
    }])
    result, payload = _run_json()
    row = payload["accounts"][0]
    assert set(row) == ROW_KEYS
    assert row["plan"] == "Ultra"
    assert row["plan_id"] == "ultra"
    assert row["session_window"] == "absent"
    assert row["usage_5h_pct"] is None
    assert row["resets_5h_at"] is None
    assert row["usage_7d_pct"] == 33.0
    assert row["worst_window"] == "7d"
    assert row["worst_window_pct"] == 33.0
    assert row["worst_window_resets_at"] == datetime.fromisoformat(future_7d).isoformat()
    assert row["model_limits"] == [
        {"label": "Fable", "pct": 61.0, "resets_at": future_7d, "window_seconds": 604800},
    ]
    assert row["selected"] is False
    assert row["in_use"] is False
    assert payload["summary"]["best_account_worst_window"] == "7d"
    assert "tok-secret" not in result.output


def test_max_row_worst_window_is_the_higher_5h(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    future_5h = (now + timedelta(hours=2)).isoformat()
    future_7d = (now + timedelta(days=3)).isoformat()
    _seed_db(tmp_path, [{
        "email": "max@test.com", "validation_status": "valid",
        "subscription_type": "max", "rate_limit_tier": "default_claude_max_5x",
        "cached_usage_raw": _max_raw(future_5h, future_7d),
        "cached_usage_5h": 67.0, "cached_5h_resets_at": future_5h,
        "cached_usage_7d": 23.0, "cached_7d_resets_at": future_7d,
        "usage_cached_at": int(now.timestamp()) - 60,
    }])
    _, payload = _run_json()
    row = payload["accounts"][0]
    assert (row["plan"], row["plan_id"], row["session_window"]) == ("Max", "max", "present")
    assert row["usage_5h_pct"] == 67.0
    assert row["resets_5h_at"] == future_5h
    assert row["worst_window"] == "5h"
    assert row["worst_window_pct"] == 67.0
    assert row["worst_window_resets_at"] == datetime.fromisoformat(future_5h).isoformat()
    assert row["model_limits"] == []


def test_codex_pro_row_reports_no_5h_window(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    reset = now + timedelta(days=4)
    _seed_db(tmp_path, [{
        "email": "codex@test.com", "provider": "codex", "validation_status": "valid",
        "subscription_type": "pro",
        "cached_usage_raw": _codex_pro_raw(int(reset.timestamp())),
        "cached_usage_7d": 38.0, "cached_7d_resets_at": reset.isoformat(),
        "usage_cached_at": int(now.timestamp()) - 60,
    }])
    _, payload = _run_json()
    row = payload["accounts"][0]
    assert row["provider"] == "codex"
    assert (row["plan"], row["plan_id"], row["session_window"]) == ("Pro", "pro", "absent")
    assert row["usage_5h_pct"] is None
    assert row["worst_window"] == "7d"
    assert row["worst_window_pct"] == 38.0


def test_model_limit_entries_are_allowlisted_and_coerced(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    future_7d = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    raw = json.loads(_ultra_raw(future_7d))
    # Extra keys in the provider entry must never reach the row.
    raw["limits"][1]["secret_ish"] = "should-not-leak"
    _seed_db(tmp_path, [{
        "email": "u@test.com", "subscription_type": "max",
        "rate_limit_tier": "default_claude_max_20x",
        "cached_usage_raw": json.dumps(raw),
    }])
    result, payload = _run_json()
    (limit,) = payload["accounts"][0]["model_limits"]
    assert set(limit) == MODEL_LIMIT_KEYS
    assert isinstance(limit["pct"], float)
    assert "should-not-leak" not in result.output


def test_selected_is_read_from_jacked_database_once_per_provider(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ids = _seed_db(tmp_path, [
        {"email": "a@test.com", "validation_status": "valid", "cached_usage_7d": 10.0},
        {"email": "b@test.com", "validation_status": "valid", "cached_usage_7d": 20.0},
        {"email": "c@test.com", "provider": "codex", "validation_status": "valid",
         "cached_usage_7d": 30.0},
    ])
    calls: list[str] = []

    def fake_selected(self, provider="claude"):
        calls.append(provider)
        return {"claude": ids[1], "codex": ids[2]}.get(provider)

    monkeypatch.setattr(Database, "get_selected_account_id", fake_selected)
    touched = _forbid_credential_store(monkeypatch)
    _, payload = _run_json()
    by_email = {a["email"]: a for a in payload["accounts"]}
    assert by_email["a@test.com"]["selected"] is False
    assert by_email["b@test.com"]["selected"] is True
    assert by_email["c@test.com"]["selected"] is True
    assert sorted(calls) == ["claude", "codex"]  # one read per provider
    assert touched == []


def test_selected_prefers_the_desired_account(tmp_path, monkeypatch):
    """The common macOS switch outcome (observed_target_unfenced) writes
    only desired_account_id and leaves active_account_id behind: the
    selected row is the desired account, not the stale active one."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ids = _seed_db(tmp_path, [
        {"email": "a@test.com"}, {"email": "b@test.com"},
        {"email": "c@test.com", "provider": "codex"},
    ])
    db = Database(str(tmp_path / ".claude" / "jacked.db"))
    try:
        db.set_active_account_id(ids[0], provider="claude")
        db.set_setting("desired_account_id", str(ids[1]))
        db.set_active_account_id(ids[2], provider="codex")
    finally:
        db.close()
    touched = _forbid_credential_store(monkeypatch)
    _, payload = _run_json()
    selected = {a["email"]: a["selected"] for a in payload["accounts"]}
    assert selected == {"a@test.com": False, "b@test.com": True, "c@test.com": True}
    assert touched == []


def test_in_use_is_the_calling_sessions_account(tmp_path, monkeypatch):
    """The command descends from a tracked Claude Code process: that
    session's account is in_use, whatever account is selected."""
    import jacked.session_ancestry as ancestry

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ids = _seed_db(tmp_path, [{"email": "a@test.com"}, {"email": "b@test.com"}])
    db = Database(str(tmp_path / ".claude" / "jacked.db"))
    try:
        db.set_setting("desired_account_id", str(ids[0]))
        db.record_session_account("old", account_id=ids[0], email="a@test.com",
                                  detection_method="session_start", pid=4100)
        db.end_session_account("old")
        db.record_session_account("s-1", account_id=ids[1], email="b@test.com",
                                  detection_method="session_start", pid=4100)
    finally:
        db.close()
    monkeypatch.setattr(ancestry, "ancestor_pids", lambda *a, **k: [5000, 4100, 300])
    touched = _forbid_credential_store(monkeypatch)
    _, payload = _run_json()
    by_email = {a["email"]: a for a in payload["accounts"]}
    assert by_email["b@test.com"]["in_use"] is True
    assert by_email["a@test.com"]["in_use"] is False
    assert by_email["a@test.com"]["selected"] is True
    assert touched == []


def test_no_tracked_session_marks_nothing_in_use(tmp_path, monkeypatch):
    import jacked.session_ancestry as ancestry

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    ids = _seed_db(tmp_path, [{"email": "a@test.com"}])
    db = Database(str(tmp_path / ".claude" / "jacked.db"))
    try:
        db.record_session_account("s-1", account_id=ids[0], email="a@test.com",
                                  detection_method="session_start", pid=4100)
    finally:
        db.close()
    monkeypatch.setattr(ancestry, "ancestor_pids", lambda *a, **k: [7777])
    _, payload = _run_json()
    assert [a["in_use"] for a in payload["accounts"]] == [False]


def test_provider_filter_limits_rows_and_summary(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    now = datetime.now(timezone.utc)
    cached = int(now.timestamp()) - 60
    _seed_db(tmp_path, [
        {"email": "claude@test.com", "validation_status": "valid",
         "cached_usage_7d": 80.0, "usage_cached_at": cached},
        {"email": "codex@test.com", "provider": "codex", "validation_status": "valid",
         "cached_usage_7d": 10.0, "usage_cached_at": cached},
    ])
    _, payload = _run_json()
    assert payload["provider"] is None
    assert {a["email"] for a in payload["accounts"]} == {"claude@test.com", "codex@test.com"}
    assert payload["summary"]["best_account_email"] == "codex@test.com"

    _, payload = _run_json(("usage", "--json", "--provider", "claude"))
    assert payload["provider"] == "claude"
    assert {a["email"] for a in payload["accounts"]} == {"claude@test.com"}
    assert payload["summary"]["best_account_email"] == "claude@test.com"
    assert payload["summary"]["best_account_worst_window_pct"] == 80.0
    assert payload["summary"]["accounts_with_usage_data"] == 1

    _, payload = _run_json(("usage", "--json", "--provider", "codex"))
    assert payload["provider"] == "codex"
    assert {a["email"] for a in payload["accounts"]} == {"codex@test.com"}
    assert payload["summary"]["best_account_email"] == "codex@test.com"

    _, payload = _run_json(("usage", "--json", "--provider", "Claude"))
    assert payload["provider"] == "claude"
    assert {a["email"] for a in payload["accounts"]} == {"claude@test.com"}

    bad = CliRunner().invoke(main, ["usage", "--json", "--provider", "gemini"])
    assert bad.exit_code == 2


def test_table_shows_plan_none_and_model_columns(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("COLUMNS", "300")
    now = datetime.now(timezone.utc)
    future_5h = (now + timedelta(hours=2)).isoformat()
    future_7d = (now + timedelta(days=3)).isoformat()
    cached = int(now.timestamp()) - 60
    _seed_db(tmp_path, [
        {"email": "ultra@test.com", "validation_status": "valid",
         "subscription_type": "max", "rate_limit_tier": "default_claude_max_20x",
         "cached_usage_raw": _ultra_raw(future_7d),
         "cached_usage_5h": 88.0, "cached_5h_resets_at": future_5h,
         "cached_usage_7d": 33.0, "cached_7d_resets_at": future_7d,
         "usage_cached_at": cached},
        {"email": "max@test.com", "validation_status": "valid",
         "subscription_type": "max", "rate_limit_tier": "default_claude_max_5x",
         "cached_usage_raw": _max_raw(future_5h, future_7d),
         "cached_usage_5h": 67.0, "cached_5h_resets_at": future_5h,
         "cached_usage_7d": 23.0, "cached_7d_resets_at": future_7d,
         "usage_cached_at": cached},
        {"email": "bare@test.com", "cached_usage_5h": 5.0},
    ])
    result = CliRunner().invoke(main, ["usage"])
    assert result.exit_code == 0, result.output

    ultra = _table_row(result.output, "ultra@test.com")
    assert ultra["Plan"] == "Ultra"
    assert ultra["5h %"] == "none"
    assert ultra["5h resets (UTC)"] == "none"
    assert ultra["7d %"] == "33%"
    assert ultra["Model"] == "Fable 61%"

    max_row = _table_row(result.output, "max@test.com")
    assert max_row["Plan"] == "Max"
    assert max_row["5h %"] == "67%"
    assert max_row["5h resets (UTC)"] == future_5h
    assert max_row["Model"] == "-"

    bare = _table_row(result.output, "bare@test.com")
    assert bare["Plan"] == "?"
    assert bare["5h %"] == "5%"


def test_table_prints_provider_names_as_text(tmp_path, monkeypatch):
    """Plan and model names come from the provider. Rich must print them as
    text: read as markup, "[beta]" vanishes from the cell and "[/x]" stops
    the whole table with an error."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("COLUMNS", "300")
    future_7d = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    raw = json.loads(_ultra_raw(future_7d))
    raw["limits"][1]["scope"]["model"]["display_name"] = "Fable [/x]"
    _seed_db(tmp_path, [{
        "email": "u@test.com", "subscription_type": "max",
        "rate_limit_tier": "default_claude_max_20x",
        "cached_usage_raw": json.dumps(raw),
        "profile_json": json.dumps({"organization": {"plan_display_name": "Ultra [beta]"}}),
    }])
    result = CliRunner().invoke(main, ["usage"])
    assert result.exit_code == 0, result.output
    row = _table_row(result.output, "u@test.com")
    assert row["Plan"] == "Ultra [beta]"
    assert row["Model"] == "Fable [/x] 61%"
