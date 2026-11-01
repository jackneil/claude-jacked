"""Plan-aware windows in the pacing summary (jacked/service/usage_pacing.py)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from jacked import usage_plans as up
from jacked.service import usage_pacing as pacing

NOW = datetime(2026, 11, 1, 18, 0, tzinfo=timezone.utc)
CACHED = int(NOW.timestamp()) - 60

ULTRA_RAW = json.dumps({
    "five_hour": None,
    "limits": [
        {"kind": "weekly_all", "percent": 40},
        {"kind": "weekly_scoped", "percent": 61, "is_active": True,
         "scope": {"model": {"display_name": "Fable"}}},
    ],
})
MAX_RAW = json.dumps({
    "five_hour": {"utilization": 80.0, "resets_at": "2026-11-01T19:00:00+00:00"},
    "limits": [{"kind": "session", "percent": 80}, {"kind": "weekly_all", "percent": 50}],
})
MAX_WEEKLY_ONLY_RAW = json.dumps({"five_hour": None, "limits": [{"kind": "weekly_all"}]})


@pytest.fixture(autouse=True)
def _fresh_catalog(monkeypatch):
    monkeypatch.setattr(up, "_catalog_cache", None)
    monkeypatch.setattr(up, "_drift_logged", set())


def _ultra(**extra):
    row = {"id": 13, "email": "ultra@x.com", "provider": "claude", "is_active": 1,
           "validation_status": "valid", "subscription_type": "max",
           "rate_limit_tier": "default_claude_max_20x", "cached_usage_raw": ULTRA_RAW,
           "usage_cached_at": CACHED, "cached_usage_7d": 40.0,
           "cached_7d_resets_at": "2026-11-02T23:00:00+00:00"}
    row.update(extra)
    return row


def _max(**extra):
    row = {"id": 5, "email": "max@x.com", "provider": "claude", "is_active": 1,
           "validation_status": "valid", "subscription_type": "max",
           "rate_limit_tier": "default_claude_max_5x", "cached_usage_raw": MAX_RAW,
           "usage_cached_at": CACHED, "cached_usage_5h": 80.0,
           "cached_5h_resets_at": "2026-11-01T19:00:00+00:00", "cached_usage_7d": 50.0,
           "cached_7d_resets_at": "2026-11-02T05:00:00+00:00"}
    row.update(extra)
    return row


class TestAccountWindows:
    def test_ultra_has_only_a_weekly_window(self):
        windows = pacing.account_windows(_ultra(cached_usage_5h=99.0), NOW)
        assert [(kind, pct) for kind, pct, _ in windows] == [("7d", 40.0)]

    def test_max_has_both(self):
        windows = pacing.account_windows(_max(), NOW)
        assert [(kind, pct) for kind, pct, _ in windows] == [("5h", 80.0), ("7d", 50.0)]

    def test_worst_window(self):
        assert pacing.worst_window(_max(), NOW)[:2] == ("5h", 80.0)
        assert pacing.worst_window(_ultra(cached_usage_5h=99.0), NOW)[:2] == ("7d", 40.0)


class TestRecovery:
    def test_constrained_session_window(self):
        acct = _max(cached_usage_5h=95.0)
        assert pacing.account_recovery_at(acct, NOW).isoformat() == "2026-11-01T19:00:00+00:00"

    def test_latest_constrained_window_wins(self):
        acct = _max(cached_usage_5h=95.0, cached_usage_7d=99.0)
        assert pacing.account_recovery_at(acct, NOW).isoformat() == "2026-11-02T05:00:00+00:00"

    def test_unknowable_reset_gives_none(self):
        acct = _max(cached_usage_5h=95.0, cached_5h_resets_at=None)
        assert pacing.account_recovery_at(acct, NOW) is None

    def test_ultra_stale_session_value_never_constrains(self):
        acct = _ultra(cached_usage_5h=100.0, cached_5h_resets_at="2026-11-01T20:00:00+00:00")
        assert pacing.account_recovery_at(acct, NOW) is None

    def test_nothing_constrained(self):
        assert pacing.account_recovery_at(_max(), NOW) is None

    def test_recovery_names_the_window_that_frees_the_account(self):
        reset, kind = pacing.account_recovery(_max(cached_usage_5h=95.0), NOW)
        assert (reset.isoformat(), kind) == ("2026-11-01T19:00:00+00:00", "5h")
        reset, kind = pacing.account_recovery(_max(cached_usage_5h=95.0, cached_usage_7d=99.0), NOW)
        assert (reset.isoformat(), kind) == ("2026-11-02T05:00:00+00:00", "7d")
        # An Ultra account is freed by its weekly window, never a stale 5h one.
        ultra = _ultra(cached_usage_5h=100.0, cached_5h_resets_at="2026-11-01T20:00:00+00:00",
                       cached_usage_7d=96.0)
        assert pacing.account_recovery(ultra, NOW)[1] == "7d"


class TestSummary:
    def test_best_account_names_its_window(self):
        summary = pacing.compute_best_account_summary([_max(), _ultra()], now=NOW)
        assert summary["best_account_id"] == 13
        assert summary["best_account_worst_window_pct"] == 40.0
        assert summary["best_account_worst_window"] == "7d"
        assert summary["best_account_worst_window_resets_at"] == "2026-11-02T23:00:00+00:00"

    def test_session_window_can_be_the_worst(self):
        summary = pacing.compute_best_account_summary([_max()], now=NOW)
        assert summary["best_account_worst_window"] == "5h"
        assert summary["best_account_worst_window_resets_at"] == "2026-11-01T19:00:00+00:00"

    def test_pinned_fields_keep_their_meaning(self):
        summary = pacing.compute_best_account_summary([_max(cached_usage_5h=95.0)], now=NOW)
        assert summary["accounts_with_usage_data"] == 1
        assert summary["best_account_email"] == "max@x.com"
        assert summary["best_account_worst_window_pct"] == 95.0
        assert summary["best_account_cache_age_seconds"] == 60
        assert summary["pause_until"] == "2026-11-01T19:00:00+00:00"

    def test_plan_catalog_reports_drift(self):
        drifting = _max(cached_usage_raw=MAX_WEEKLY_ONLY_RAW)
        summary = pacing.compute_best_account_summary([drifting, _ultra()], now=NOW)
        assert summary["plan_catalog"]["verified_at"] == up.catalog().verified_at
        assert summary["plan_catalog"]["drift"] == [
            {"account_id": 5, "codes": ["session_missing"]}
        ]

    def test_empty_fleet(self):
        summary = pacing.compute_best_account_summary([], now=NOW)
        assert summary["best_account_worst_window"] is None
        assert summary["best_account_worst_window_resets_at"] is None
        assert summary["plan_catalog"]["drift"] == []
