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
        rec = pacing.account_recovery(_max(cached_usage_5h=95.0), NOW)
        assert (rec.at.isoformat(), rec.window, rec.label) == (
            "2026-11-01T19:00:00+00:00", "5h", None)
        rec = pacing.account_recovery(_max(cached_usage_5h=95.0, cached_usage_7d=99.0), NOW)
        assert (rec.at.isoformat(), rec.window, rec.label) == (
            "2026-11-02T05:00:00+00:00", "7d", None)
        # An Ultra account is freed by its weekly window, never a stale 5h one.
        ultra = _ultra(cached_usage_5h=100.0, cached_5h_resets_at="2026-11-01T20:00:00+00:00",
                       cached_usage_7d=96.0)
        assert pacing.account_recovery(ultra, NOW).window == "7d"


WEEKLY_RESET = "2026-11-02T23:00:00+00:00"  # the weekly reset of _ultra()
FABLE_RESET = "2026-11-04T23:00:00+00:00"


def _model_raw(pct, resets, *, weekly=40, binding="Fable"):
    """A no-session payload with a Fable limit at *pct*. *binding* names the
    limit the provider flags is_active (a Sonnet limit at 20% when it is
    "Sonnet")."""
    limits = [{"kind": "weekly_all", "percent": weekly, "resets_at": WEEKLY_RESET},
              {"kind": "weekly_scoped", "percent": pct, "is_active": binding == "Fable",
               "resets_at": resets, "scope": {"model": {"display_name": "Fable"}}}]
    if binding == "Sonnet":
        limits.append({"kind": "weekly_scoped", "percent": 20, "is_active": True,
                       "resets_at": resets, "scope": {"model": {"display_name": "Sonnet"}}})
    return json.dumps({"five_hour": None, "limits": limits})


class TestModelRecovery:
    """F4: a binding model limit at or above the plan's weekly_critical_pct
    walls the account (auto-swap rule 2b), so it is a constrained window of
    kind "model" whose label names the model."""

    def test_a_walled_binding_model_decides_the_recovery(self):
        acct = _ultra(cached_usage_raw=_model_raw(99, FABLE_RESET))
        rec = pacing.account_recovery(acct, NOW)
        assert (rec.at.isoformat(), rec.window, rec.label) == (FABLE_RESET, "model", "Fable")
        assert pacing.account_recovery_at(acct, NOW).isoformat() == FABLE_RESET

    def test_the_model_threshold_is_the_plan_field(self):
        """97 is above the 90 pacing threshold but under the 98 wall: the
        account still serves Fable, so the limit holds nothing back."""
        assert pacing.account_recovery(_ultra(cached_usage_raw=_model_raw(97, FABLE_RESET)), NOW) is None
        assert pacing.account_recovery(_ultra(cached_usage_raw=_model_raw(98, FABLE_RESET)), NOW).window == "model"

    def test_a_catalog_edit_moves_the_model_threshold(self, tmp_path, monkeypatch):
        (tmp_path / up.CATALOG_FILE).write_text(json.dumps({
            "version": 1, "verified_at": "2026-11-01",
            "providers": {"claude": {"subscriptions": {"max": {"label": "Max", "tiers": {
                "default_claude_max_20x": {"id": "ultra", "label": "Ultra",
                                           "weekly_critical_pct": 95},
            }}}}},
        }), encoding="utf-8")
        monkeypatch.setattr(up, "_catalog_cache", up.load_catalog(tmp_path))
        rec = pacing.account_recovery(_ultra(cached_usage_raw=_model_raw(96, FABLE_RESET)), NOW)
        assert (rec.window, rec.label) == ("model", "Fable")

    def test_only_the_binding_model_counts(self):
        """The provider flags Sonnet (20%) as the binding limit: the Fable
        limit at 99% is not the account's wall (the rule 2b selection)."""
        acct = _ultra(cached_usage_raw=_model_raw(99, FABLE_RESET, binding="Sonnet"))
        assert pacing.account_recovery(acct, NOW) is None

    def test_the_latest_constrained_limit_decides(self):
        walled_week = _ultra(cached_usage_7d=95.0,
                             cached_usage_raw=_model_raw(99, FABLE_RESET, weekly=95))
        rec = pacing.account_recovery(walled_week, NOW)
        assert (rec.at.isoformat(), rec.window, rec.label) == (FABLE_RESET, "model", "Fable")
        early_model = _ultra(cached_usage_7d=95.0, cached_usage_raw=_model_raw(
            99, "2026-11-02T06:00:00+00:00", weekly=95))
        rec = pacing.account_recovery(early_model, NOW)
        assert (rec.at.isoformat(), rec.window, rec.label) == (WEEKLY_RESET, "7d", None)

    def test_on_a_tie_the_model_is_named(self):
        """The Fable limit and the weekly window often share a reset; the
        banner then names the model, the narrower wall."""
        acct = _ultra(cached_usage_7d=95.0, cached_usage_raw=_model_raw(99, WEEKLY_RESET, weekly=95))
        rec = pacing.account_recovery(acct, NOW)
        assert (rec.at.isoformat(), rec.window, rec.label) == (WEEKLY_RESET, "model", "Fable")

    def test_an_unknowable_model_reset_gives_none(self):
        for resets in (None, "not-a-date", 1793780314):
            acct = _ultra(cached_usage_raw=_model_raw(99, resets))
            assert pacing.account_recovery(acct, NOW) is None, resets

    def test_a_model_reading_from_before_its_reset_holds_nothing(self):
        """The Fable limit reset at 17:30 and the reading is from 17:00, so it
        predates the turnover (the staleness rule of every window): the
        weekly window decides. The same reading taken after 17:30 is a
        constrained limit whose reset passed, so the time is unknowable."""
        before = int(datetime(2026, 11, 1, 17, 0, tzinfo=timezone.utc).timestamp())
        raw = _model_raw(99, "2026-11-01T17:30:00+00:00", weekly=95)
        stale = _ultra(usage_cached_at=before, cached_usage_7d=95.0, cached_usage_raw=raw)
        rec = pacing.account_recovery(stale, NOW)
        assert (rec.at.isoformat(), rec.window) == (WEEKLY_RESET, "7d")
        fresh = _ultra(cached_usage_7d=95.0, cached_usage_raw=raw)  # cached at 17:59
        assert pacing.account_recovery(fresh, NOW) is None

    def test_pause_until_waits_out_a_model_wall(self):
        """The pacing summary uses the same recovery: an account walled on its
        binding model is not workable before that limit resets."""
        acct = _ultra(cached_usage_7d=95.0, cached_usage_raw=_model_raw(99, FABLE_RESET, weekly=95))
        summary = pacing.compute_best_account_summary([acct], now=NOW)
        assert summary["best_account_worst_window_pct"] == 95.0
        assert summary["pause_until"] == FABLE_RESET


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
