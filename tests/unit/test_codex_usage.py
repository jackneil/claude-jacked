"""M4: Codex usage via `codex app-server` JSON-RPC.

Covers the normalizer (against a RECORDED real app-server response), the
DB cache write (5h/7d percents + ISO reset times land in the same columns the
Anthropic path uses), a real subprocess round-trip through a fake app-server
that speaks the newline-delimited JSON-RPC protocol, and the web.auth.fetch_usage
provider DISPATCH (codex accounts route to the Codex leaf — and only the
account live in the shared root is polled; non-active ones keep their cache).
"""

import asyncio
import base64
import json
import os
from datetime import datetime, timezone

import pytest

from jacked.codex import usage as cu
from jacked.web.database import Database
from tests._platform import write_python_shim

# Verbatim result captured from a live `codex app-server` 0.142.3
# account/rateLimits/read (token values were never in this payload).
RECORDED_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 2, "windowDurationMins": 300, "resetsAt": 1782683811},
        "secondary": {"usedPercent": 26, "windowDurationMins": 10080, "resetsAt": 1783199300},
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "planType": "pro",
        "rateLimitReachedType": None,
    },
    "rateLimitsByLimitId": {
        "codex": {"primary": {"usedPercent": 2}, "planType": "pro"},
        "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark", "primary": {"usedPercent": 0}},
    },
    "rateLimitResetCredits": {"availableCount": 0},
}

# Verbatim result captured live 2026-07-23: the weekly-only shape. secondary
# is null and the lone primary IS the weekly window (windowDurationMins
# 10080). Mapping positionally here filed the weekly percent under "5h" and
# left a dead 100% frozen in the "7d" cache (#weekly-only regression).
WEEKLY_ONLY_RESULT = {
    "rateLimits": {
        "limitId": "codex",
        "limitName": None,
        "primary": {"usedPercent": 23, "windowDurationMins": 10080, "resetsAt": 1785266094},
        "secondary": None,
        "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
        "individualLimit": None,
        "planType": "pro",
        "rateLimitReachedType": None,
    },
    "rateLimitsByLimitId": {
        "codex": {"limitId": "codex", "primary": {"usedPercent": 23}, "planType": "pro"},
    },
    "rateLimitResetCredits": {"availableCount": 0, "credits": []},
}


# --------------------------------------------------------------------------
# Normalizer
# --------------------------------------------------------------------------

def test_normalize_maps_primary_to_5h_secondary_to_7d():
    norm = cu.normalize_rate_limits(RECORDED_RESULT)
    assert norm["five_hour"]["utilization"] == 2
    assert norm["seven_day"]["utilization"] == 26
    assert norm["plan_type"] == "pro"


def test_normalize_converts_epoch_resets_to_iso():
    norm = cu.normalize_rate_limits(RECORDED_RESULT)
    five = norm["five_hour"]["resets_at"]
    # ISO string that parses back to the original epoch
    assert datetime.fromisoformat(five) == datetime.fromtimestamp(
        1782683811, tz=timezone.utc
    )


def test_normalize_weekly_only_primary_lands_in_7d():
    # windowDurationMins wins over position: a lone weekly primary must land
    # in seven_day, leaving five_hour empty — not the other way around.
    norm = cu.normalize_rate_limits(WEEKLY_ONLY_RESULT)
    assert norm["seven_day"]["utilization"] == 23
    assert norm["seven_day"]["reported"] is True
    assert datetime.fromisoformat(norm["seven_day"]["resets_at"]) == datetime.fromtimestamp(
        1785266094, tz=timezone.utc
    )
    assert norm["five_hour"]["utilization"] is None
    assert norm["five_hour"]["resets_at"] is None
    assert norm["five_hour"]["reported"] is False
    # Explicit null secondary + recognized duration = authoritative shape.
    assert norm["windows_complete"] is True


def test_normalize_positional_fallback_without_durations():
    # Old payloads without windowDurationMins keep the legacy mapping — but
    # an un-understood shape is NOT authoritative (must never drive a clear).
    norm = cu.normalize_rate_limits({"rateLimits": {
        "primary": {"usedPercent": 4, "resetsAt": 1782683811},
        "secondary": {"usedPercent": 40, "resetsAt": 1783199300},
    }})
    assert norm["five_hour"]["utilization"] == 4
    assert norm["seven_day"]["utilization"] == 40
    assert norm["windows_complete"] is False


def test_normalize_drops_second_window_for_same_slot(caplog):
    # Two weekly-duration windows must not misfile one under "5h": the first
    # keeps the seven_day slot, the second is dropped — LOUDLY (a drop means
    # codex changed shape; DEBUG would be invisible in the running server).
    with caplog.at_level("WARNING", logger="jacked.codex.usage"):
        norm = cu.normalize_rate_limits({"rateLimits": {
            "primary": {"usedPercent": 11, "windowDurationMins": 10080, "resetsAt": 1785266094},
            "secondary": {"usedPercent": 99, "windowDurationMins": 20160, "resetsAt": 1785266094},
        }})
    assert norm["seven_day"]["utilization"] == 11
    assert norm["five_hour"]["utilization"] is None
    assert norm["windows_complete"] is False  # a drop vetoes clearing
    assert any("dropping codex rate-limit window" in r.message for r in caplog.records)


def test_normalize_fallback_logs_warning(caplog):
    # A present window without a usable duration is the "shape changed"
    # signal — it must warn at default log level, not debug.
    cu._warned_shapes.clear()  # per-process throttle: isolate from other tests
    with caplog.at_level("WARNING", logger="jacked.codex.usage"):
        cu.normalize_rate_limits({"rateLimits": {
            "primary": {"usedPercent": 4, "resetsAt": 1782683811},
            "secondary": None,
        }})
    assert any("no usable windowDurationMins" in r.message for r in caplog.records)


@pytest.mark.parametrize("bad_dur", [True, False, "10080", float("nan"), -5, 0, None, {}])
def test_normalize_unusable_durations_fall_back_positional(bad_dur):
    # JSON true is an int to Python (True < 1440 would classify as 5h); NaN
    # compares False everywhere; strings/objects/negatives are garbage. All
    # must take the positional fallback, never duration classification.
    norm = cu.normalize_rate_limits({"rateLimits": {
        "primary": {"usedPercent": 7, "windowDurationMins": bad_dur, "resetsAt": 1782683811},
        "secondary": None,
    }})
    assert norm["five_hour"]["utilization"] == 7  # positional: primary → 5h
    assert norm["seven_day"]["utilization"] is None
    assert norm["windows_complete"] is False


def test_normalize_duration_boundary_and_float():
    # Exactly 1440 (one day) is NOT the short window; float durations count.
    norm = cu.normalize_rate_limits({"rateLimits": {
        "primary": {"usedPercent": 9, "windowDurationMins": 1440, "resetsAt": 1785266094},
        "secondary": {"usedPercent": 3, "windowDurationMins": 300.0, "resetsAt": 1782683811},
    }})
    assert norm["seven_day"]["utilization"] == 9
    assert norm["five_hour"]["utilization"] == 3
    assert norm["windows_complete"] is True


def test_normalize_secondary_only_classified_by_duration():
    # primary null, secondary present: duration classification, not position.
    norm = cu.normalize_rate_limits({"rateLimits": {
        "primary": None,
        "secondary": {"usedPercent": 55, "windowDurationMins": 300, "resetsAt": 1782683811},
    }})
    assert norm["five_hour"]["utilization"] == 55
    assert norm["seven_day"]["utilization"] is None
    assert norm["seven_day"]["reported"] is False
    assert norm["windows_complete"] is True


def test_normalize_partial_payload_is_not_authoritative():
    # secondary KEY entirely absent (not an explicit null): possibly a
    # truncated/degraded read — never authoritative, never clears.
    norm = cu.normalize_rate_limits({"rateLimits": {
        "primary": {"usedPercent": 2, "windowDurationMins": 300, "resetsAt": 1782683811},
    }})
    assert norm["five_hour"]["utilization"] == 2
    assert norm["windows_complete"] is False


def test_normalize_handles_missing_windows():
    norm = cu.normalize_rate_limits({"rateLimits": {}})
    assert norm["five_hour"]["utilization"] is None
    assert norm["five_hour"]["resets_at"] is None
    assert norm["seven_day"]["utilization"] is None


def test_normalize_surfaces_credits_and_reset_credits():
    norm = cu.normalize_rate_limits(RECORDED_RESULT)
    assert norm["credits"]["balance"] == "0"
    assert norm["reset_credits"]["availableCount"] == 0
    assert "codex_bengalfox" in norm["by_limit"]


def test_epoch_to_iso_none_and_garbage():
    assert cu._epoch_to_iso(None) is None
    assert cu._epoch_to_iso("not-a-number") is None


# --------------------------------------------------------------------------
# fetch_codex_usage -> DB cache (mocked driver)
# --------------------------------------------------------------------------

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


def test_fetch_codex_usage_writes_cache(db, tmp_path, monkeypatch):
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")

    async def fake_read(**kwargs):
        return RECORDED_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    # home=tmp keeps the swap lock out of the real ~/.codex
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is not None

    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] == 2
    assert row["cached_usage_7d"] == 26
    assert row["cached_5h_resets_at"] is not None
    assert row["cached_7d_resets_at"] is not None
    assert row["usage_cached_at"] is not None


def test_fetch_weekly_only_clears_stale_5h_and_updates_7d(db, tmp_path, monkeypatch):
    """The weekly-only regression: a dead window's cache must be CLEARED.

    Seed the cache as the old positional bug left it (weekly percent under
    "5h", a frozen 100% under "7d"), then fetch the weekly-only payload: the
    real weekly usage must land in 7d and the 5h columns must go NULL — not
    keep serving the stale reading to the dashboard and auto-swap.
    """
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    db.update_account_usage_cache(
        acct["id"],
        five_hour=23.0,
        seven_day=100.0,
        five_hour_resets_at="2026-07-28T19:14:54+00:00",
        seven_day_resets_at="2026-07-18T06:04:44+00:00",
    )

    async def fake_read(**kwargs):
        return WEEKLY_ONLY_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is not None

    row = db.get_account(acct["id"])
    assert row["cached_usage_7d"] == 23
    assert row["cached_7d_resets_at"] is not None
    assert row["cached_usage_5h"] is None
    assert row["cached_5h_resets_at"] is None


def test_fetch_5h_only_clears_stale_7d_and_updates_5h(db, tmp_path, monkeypatch):
    """Mirror of the weekly-only case: a 5h-only authoritative payload must
    clear the dead 7d column and file the 5h percent correctly."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    db.update_account_usage_cache(acct["id"], five_hour=50.0, seven_day=90.0)

    async def fake_read(**kwargs):
        return {"rateLimits": {
            "primary": {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 1782683811},
            "secondary": None,
            "planType": "pro",
        }}

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is not None

    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] == 12
    assert row["cached_usage_7d"] is None
    assert row["cached_7d_resets_at"] is None


def test_fetch_partial_read_preserves_other_window(db, tmp_path, monkeypatch):
    """A degraded read (secondary KEY absent, not an explicit null) must NOT
    clear the other window — 'absent from this read' is not 'gone forever'."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    db.update_account_usage_cache(
        acct["id"], seven_day=85.0, seven_day_resets_at="2026-07-28T19:14:54+00:00"
    )

    async def fake_read(**kwargs):
        return {"rateLimits": {
            "primary": {"usedPercent": 5, "windowDurationMins": 300, "resetsAt": 1782683811},
            "planType": "pro",
        }}

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is not None

    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] == 5
    assert row["cached_usage_7d"] == 85.0  # preserved, not destroyed
    assert row["cached_7d_resets_at"] == "2026-07-28T19:14:54+00:00"


def test_fetch_unrecognized_shape_never_clears(db, tmp_path, monkeypatch):
    """A weekly-only account whose duration field got renamed falls back to
    positional (weekly% lands under 5h — wrong but visible via WARNING); the
    un-understood shape must NOT also clear the 7d column."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    db.update_account_usage_cache(acct["id"], seven_day=23.0)

    async def fake_read(**kwargs):
        return {"rateLimits": {
            "primary": {"usedPercent": 31, "windowDurationMinutes": 10080, "resetsAt": 1785266094},
            "secondary": None,
            "planType": "pro",
        }}

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is not None

    row = db.get_account(acct["id"])
    assert row["cached_usage_7d"] == 23.0  # fallback shape may never clear


def test_fetch_all_none_read_preserves_seeded_cache(db, tmp_path, monkeypatch):
    """The degraded-read invariant, pinned directly: an empty payload must
    leave previously cached usage untouched."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    db.update_account_usage_cache(acct["id"], five_hour=23.0, seven_day=42.0)

    async def empty(**kwargs):
        return {"rateLimits": {}}

    monkeypatch.setattr(cu, "read_codex_rate_limits", empty)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is None
    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] == 23.0
    assert row["cached_usage_7d"] == 42.0


def test_fetch_proceeds_when_root_identity_matches(db, tmp_path, monkeypatch):
    """The PRODUCTION path of the TOCTOU guard: the real ~/.codex always has
    an auth.json matching the polled account, so the re-check must match and
    PROCEED to read + write. A regression in the identity comparison would
    make every real poll skip silently while auth-less test rigs stay green —
    this test pins the proceed branch."""
    home = tmp_path / ".codex"
    home.mkdir()
    acct = db.create_account(
        "dev@example.com", "tok", cu.time.time() + 99999, provider="codex"
    )
    (home / "auth.json").write_text(json.dumps(_root_auth("", "dev@example.com")))

    called = []

    async def fake_read(**kwargs):
        called.append(1)
        return WEEKLY_ONLY_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=home))
    assert result is not None
    assert called == [1]
    row = db.get_account(acct["id"])
    assert row["cached_usage_7d"] == 23


def test_fetch_resets_at_only_travels_with_its_percent(db, tmp_path, monkeypatch):
    """A window carrying resetsAt but no usedPercent must not pair a fresh
    reset time with the preserved stale percent."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    db.update_account_usage_cache(
        acct["id"], five_hour=40.0, five_hour_resets_at="2026-07-20T00:00:00+00:00"
    )

    async def fake_read(**kwargs):
        return {"rateLimits": {
            # 5h window reported, but degraded: resetsAt without usedPercent
            "primary": {"windowDurationMins": 300, "resetsAt": 1785266094},
            "secondary": {"usedPercent": 9, "windowDurationMins": 10080, "resetsAt": 1785266094},
            "planType": "pro",
        }}

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is not None
    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] == 40.0  # stale percent preserved...
    assert row["cached_5h_resets_at"] == "2026-07-20T00:00:00+00:00"  # ...with ITS reset time
    assert row["cached_usage_7d"] == 9


def test_fallback_warning_throttles_to_debug_on_repeat(caplog):
    """The steady-state fallback (old codex client) warns once per shape per
    process, then drops to DEBUG — WARNING must stay signal, not noise."""
    cu._warned_shapes.clear()
    payload = {"rateLimits": {
        "primary": {"usedPercent": 4, "resetsAt": 1782683811},
        "secondary": None,
    }}
    with caplog.at_level("DEBUG", logger="jacked.codex.usage"):
        cu.normalize_rate_limits(payload)
        cu.normalize_rate_limits(payload)
    warnings = [r for r in caplog.records
                if r.levelname == "WARNING" and "windowDurationMins" in r.message]
    assert len(warnings) == 1


def test_update_usage_cache_none_still_preserves_without_clear(db):
    """Partial updates (the Anthropic path) keep their None-skips: only an
    explicit clear flag nulls a column."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999)
    db.update_account_usage_cache(acct["id"], five_hour=10.0, seven_day=20.0)
    db.update_account_usage_cache(acct["id"], five_hour=11.0)  # 7d untouched
    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] == 11.0
    assert row["cached_usage_7d"] == 20.0
    db.update_account_usage_cache(acct["id"], seven_day=21.0, clear_five_hour=True)
    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] is None
    assert row["cached_usage_7d"] == 21.0


def test_update_usage_cache_clear_beats_passed_values(db):
    """clear_* wins over a value AND a resets_at passed for the same window —
    percent and resets_at are nulled together, never left half-set."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999)
    db.update_account_usage_cache(
        acct["id"], five_hour=10.0, five_hour_resets_at="2026-07-28T00:00:00+00:00"
    )
    db.update_account_usage_cache(
        acct["id"],
        five_hour=99.0,
        five_hour_resets_at="2026-07-29T00:00:00+00:00",
        clear_five_hour=True,
    )
    row = db.get_account(acct["id"])
    assert row["cached_usage_5h"] is None
    assert row["cached_5h_resets_at"] is None
    # And the seven-day twin — the symmetric guard must hold on both windows.
    db.update_account_usage_cache(
        acct["id"], seven_day=20.0, seven_day_resets_at="2026-07-28T00:00:00+00:00"
    )
    db.update_account_usage_cache(
        acct["id"],
        seven_day=88.0,
        seven_day_resets_at="2026-07-30T00:00:00+00:00",
        clear_seven_day=True,
    )
    row = db.get_account(acct["id"])
    assert row["cached_usage_7d"] is None
    assert row["cached_7d_resets_at"] is None


def test_fetch_skips_write_when_root_swapped_under_gate(db, tmp_path, monkeypatch):
    """TOCTOU guard: the live-account gate runs outside the swap lock, so a
    swap can land account B in the root before the poll takes the lock. The
    in-lock identity re-check must skip the read — B's usage (or B's window
    shape clearing columns) must never be written under A's id."""
    home = tmp_path / ".codex"
    home.mkdir()
    acct_a = db.create_account(
        "dev-a@example.com", "tok", cu.time.time() + 99999, provider="codex"
    )
    db.create_account(
        "dev-b@example.com", "tok", cu.time.time() + 99999, provider="codex"
    )
    db.update_account_usage_cache(acct_a["id"], five_hour=40.0, seven_day=60.0)
    # The root now holds B (the swap completed after the caller's gate).
    (home / "auth.json").write_text(json.dumps(_root_auth("", "dev-b@example.com")))

    called = []

    async def fake_read(**kwargs):
        called.append(1)
        return WEEKLY_ONLY_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    result = asyncio.run(cu.fetch_codex_usage(acct_a["id"], db, home=home))
    assert result == {"_cached": True}
    assert called == []  # never even spawned the app-server
    row = db.get_account(acct_a["id"])
    assert row["cached_usage_5h"] == 40.0  # A's cache untouched
    assert row["cached_usage_7d"] == 60.0


def test_fetch_codex_usage_records_error_on_failure(db, tmp_path, monkeypatch):
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")

    async def boom(**kwargs):
        raise cu.CodexUsageError("app-server exploded")

    monkeypatch.setattr(cu, "read_codex_rate_limits", boom)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is None
    row = db.get_account(acct["id"])
    assert row["last_error"] == "app-server exploded"
    assert row["consecutive_failures"] == 1


def test_fetch_codex_usage_all_none_is_soft_failure(db, tmp_path, monkeypatch):
    """An empty/partial app-server read must NOT stamp the account fresh+valid."""
    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")

    async def empty(**kwargs):
        return {"rateLimits": {}}  # no primary/secondary -> all None

    monkeypatch.setattr(cu, "read_codex_rate_limits", empty)
    result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert result is None
    row = db.get_account(acct["id"])
    # SOFT failure: recorded, but does NOT increment consecutive_failures (would
    # starve a healthy-but-empty/new account out of auto-swap fallback).
    assert row["consecutive_failures"] == 0
    assert row["last_error"] is not None
    assert row["cached_usage_5h"] is None  # not stamped with empty data


def test_fetch_codex_usage_skips_when_swap_locked(db, tmp_path, monkeypatch):
    """If a swap holds the lock, the poll skips (returns cached) — no app-server."""
    from jacked.codex.switching import _codex_swap_lock

    acct = db.create_account("dev@example.com", "tok", cu.time.time() + 99999, provider="codex")
    home = tmp_path / ".codex"
    home.mkdir()
    called = []

    async def fake_read(**kwargs):
        called.append(1)
        return RECORDED_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    # Hold the swap lock, then poll — it must skip without spawning app-server.
    with _codex_swap_lock(home):
        result = asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=home))
    assert result == {"_cached": True}
    assert called == []


# --------------------------------------------------------------------------
# Real subprocess round-trip through a fake app-server
# --------------------------------------------------------------------------

_FAKE_CODEX = """import sys, json, os
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    msg = json.loads(line)
    mid = msg.get("id")
    method = msg.get("method")
    if method == "initialize":
        print(json.dumps({"id": mid, "result": {"userAgent": "fake-codex"}}), flush=True)
        # emit a notification too — the reader must skip it
        print(json.dumps({"method": "remoteControl/status/changed", "params": {}}), flush=True)
    elif method == "account/rateLimits/read":
        print(json.dumps({"id": mid, "result": {"rateLimits": {
            "primary": {"usedPercent": 7, "windowDurationMins": 300, "resetsAt": 1782683811},
            "secondary": {"usedPercent": 33, "windowDurationMins": 10080, "resetsAt": 1783199300},
            "planType": "pro"}}}), flush=True)
    # 'initialized' notification: no response
exit_file = os.environ.get("JACKED_TEST_CHILD_EXIT_FILE")
if exit_file:
    with open(exit_file, "w", encoding="utf-8") as marker:
        marker.write(str(os.getpid()))
"""


def _write_fake_codex(tmp_path):
    # Runs on this interpreter so the fake works even when `python3` is not on
    # PATH, and via a .cmd launcher on Windows, which has no shebang support
    # (exec'ing a shebang script there raises WinError 193). Shared with the
    # pack tests' fake npx so the two cannot drift.
    return str(write_python_shim(tmp_path, "codex_fake", _FAKE_CODEX))


def test_read_rate_limits_real_subprocess_roundtrip(tmp_path):
    fake = _write_fake_codex(tmp_path)
    home = tmp_path / ".codex"
    home.mkdir()
    exit_file = tmp_path / "fake-exited"
    run_env = dict(os.environ)
    run_env["JACKED_TEST_CHILD_EXIT_FILE"] = str(exit_file)
    result = asyncio.run(
        cu.read_codex_rate_limits(home=home, codex_bin=fake, env=run_env)
    )
    norm = cu.normalize_rate_limits(result)
    assert norm["five_hour"]["utilization"] == 7
    assert norm["seven_day"]["utilization"] == 33
    assert exit_file.read_text(encoding="utf-8").isdigit()


def test_read_rate_limits_missing_binary_raises(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    with pytest.raises(cu.CodexUsageError):
        asyncio.run(
            cu.read_codex_rate_limits(home=home, codex_bin=str(tmp_path / "nope"))
        )


def _b64url(o):
    return base64.urlsafe_b64encode(json.dumps(o).encode()).rstrip(b"=").decode()


def _root_auth(account_id, email):
    payload = {"email": email, "exp": 9999999999,
               "https://api.openai.com/auth": {"chatgpt_account_id": account_id}}
    sig = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    jwt = f"{_b64url({'alg': 'RS256'})}.{_b64url(payload)}.{sig}"
    return {
        "OPENAI_API_KEY": None, "auth_mode": "chatgpt",
        "tokens": {"id_token": jwt, "access_token": "a", "refresh_token": "r",
                   "account_id": account_id},
        "last_refresh": "2026-06-27T00:00:00Z",
    }


def test_fetch_usage_dispatches_codex_active_only(db, tmp_path, monkeypatch):
    """web.auth.fetch_usage routes a codex account to the Codex leaf, but ONLY
    the account live in the shared root — non-active ones return cached."""
    from jacked.web import auth as webauth

    webauth._account_usage_state.clear()  # avoid cross-test pacing residue
    home = tmp_path / ".codex"
    home.mkdir()
    a = db.create_account("a@x.com", "codex-managed", 9999999999,
                          provider="codex", organization_uuid="acct-A")
    b = db.create_account("b@x.com", "codex-managed", 9999999999,
                          provider="codex", organization_uuid="acct-B")
    (home / "auth.json").write_text(json.dumps(_root_auth("acct-A", "a@x.com")))
    monkeypatch.setenv("CODEX_HOME", str(home))

    called = []

    async def fake_fetch(account_id, db_, state=None):
        called.append(account_id)
        return {"ok": True}

    monkeypatch.setattr("jacked.codex.usage.fetch_codex_usage", fake_fetch)

    # Non-active B → cached sentinel, NOT delegated.
    assert asyncio.run(webauth.fetch_usage(b["id"], db)) == {"_cached": True}
    assert b["id"] not in called
    # Active A (live in the root) → delegated to the Codex leaf.
    assert asyncio.run(webauth.fetch_usage(a["id"], db)) == {"ok": True}
    assert called == [a["id"]]


def test_read_rate_limits_passes_codex_home_env(tmp_path):
    # A fake that echoes CODEX_HOME back as the plan, proving env wiring.
    script = write_python_shim(
        tmp_path,
        "echo_home",
        "import sys, json, os\n"
        "for line in sys.stdin:\n"
        "    line=line.strip()\n"
        "    if not line: continue\n"
        "    m=json.loads(line); mid=m.get('id')\n"
        "    if m.get('method')=='initialize':\n"
        "        print(json.dumps({'id':mid,'result':{}}), flush=True)\n"
        "    elif m.get('method')=='account/rateLimits/read':\n"
        "        print(json.dumps({'id':mid,'result':{'rateLimits':{'planType':os.environ.get('CODEX_HOME','')}}}), flush=True)\n",
    )
    home = tmp_path / "my-codex-home"
    home.mkdir()
    result = asyncio.run(cu.read_codex_rate_limits(home=home, codex_bin=str(script)))
    assert result["rateLimits"]["planType"] == str(home)


def test_fetch_codex_usage_syncs_plan_change(db, tmp_path, monkeypatch):
    """A ChatGPT plan change (Plus -> Pro) must update subscription_type on
    the next usage fetch — the plan is otherwise only captured at add time."""
    acct = db.create_account(
        "dev@example.com", "tok", cu.time.time() + 99999,
        provider="codex", subscription_type="plus",
    )

    async def fake_read(**kwargs):
        return RECORDED_RESULT  # carries planType "pro"

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert db.get_account(acct["id"])["subscription_type"] == "pro"


def test_fetch_codex_usage_plan_unchanged_no_write(db, tmp_path, monkeypatch):
    acct = db.create_account(
        "dev@example.com", "tok", cu.time.time() + 99999,
        provider="codex", subscription_type="pro",
    )

    async def fake_read(**kwargs):
        return RECORDED_RESULT

    monkeypatch.setattr(cu, "read_codex_rate_limits", fake_read)
    asyncio.run(cu.fetch_codex_usage(acct["id"], db, home=tmp_path / ".codex"))
    assert db.get_account(acct["id"])["subscription_type"] == "pro"
