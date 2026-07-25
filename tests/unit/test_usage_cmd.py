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
- one hostile row (TEXT percent, naive timestamp) degrades, never crashes.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from click.testing import CliRunner

from jacked.cli import main
from jacked.web.database import Database

ROW_KEYS = {
    "id", "provider", "email", "subscription_type", "is_active",
    "validation_status", "usage_5h_pct", "usage_7d_pct",
    "resets_5h_at", "resets_7d_at", "cache_age_seconds",
}


def _seed_db(home: Path, accounts: list[dict]) -> None:
    db_path = home / ".claude" / "jacked.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = Database(str(db_path))
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
            updates = {
                k: v for k, v in acct.items()
                if k in {
                    "cached_usage_5h", "cached_usage_7d",
                    "cached_5h_resets_at", "cached_7d_resets_at",
                    "usage_cached_at", "subscription_type",
                    "validation_status", "is_active",
                }
            }
            if updates:
                db.update_account(row["id"], **updates)
    finally:
        db.close()


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
