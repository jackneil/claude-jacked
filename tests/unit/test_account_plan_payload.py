"""The account API reads every usage window through the plan catalog.

GET /api/auth/accounts (and every WebSocket payload built from
``_account_to_response``) carries ``plan`` and ``windows``, and
``usage.five_hour`` is null, not 0, for an account that has no 5-hour window.
The payload fixtures copy the live shapes of 2026-11-01: a verified Ultra
account answers ``five_hour: null`` with a ``limits`` list that holds only
weekly entries; a Max 5x account lists a ``session`` limit; a ChatGPT Pro
Codex account reports one primary window of 10080 minutes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from unittest.mock import MagicMock

import pytest

from jacked import usage_plans
from jacked.api.routes import auth as routes_auth
from jacked.api.routes.auth import _account_to_response, _build_account_usage
from jacked.service import menubar_summary


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    """The bundled catalog, an empty drift log and an empty failure log."""
    monkeypatch.setattr(usage_plans, "_catalog_cache", None)
    monkeypatch.setattr(usage_plans, "_drift_logged", set())
    monkeypatch.setattr(menubar_summary, "_VIEW_FAILURES_LOGGED", set())


ULTRA_RAW = {
    "five_hour": None,
    "seven_day": {"utilization": 33.0, "resets_at": "2026-11-02T22:59:59+00:00"},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 33,
         "resets_at": "2026-11-02T22:59:59+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 61,
         "resets_at": "2026-11-02T22:59:59+00:00", "is_active": True,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}

# An Ultra account that Anthropic has not verified yet: a temporary 5h limit.
UNVERIFIED_ULTRA_RAW = {
    "five_hour": {"utilization": 12.0, "resets_at": "2026-11-01T18:59:59+00:00"},
    "seven_day": {"utilization": 4.0, "resets_at": "2026-11-07T04:59:59+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 12,
         "resets_at": "2026-11-01T18:59:59+00:00", "scope": None, "is_active": True},
        {"kind": "weekly_all", "group": "weekly", "percent": 4,
         "resets_at": "2026-11-07T04:59:59+00:00", "scope": None, "is_active": False},
    ],
}

# Max 5x with a session limit and no 5h window open right now.
MAX_IDLE_RAW = {
    "five_hour": None,
    "seven_day": {"utilization": 23.0, "resets_at": "2026-11-02T04:59:59+00:00"},
    "limits": [
        {"kind": "session", "group": "session", "percent": 0,
         "resets_at": None, "scope": None, "is_active": False},
        {"kind": "weekly_all", "group": "weekly", "percent": 23,
         "resets_at": "2026-11-02T04:59:59+00:00", "scope": None, "is_active": True},
    ],
}

CODEX_PRO_RAW = {
    "ordinaryUsageAllowed": True,
    "rateLimits": {
        "limitId": "codex",
        "primary": {"usedPercent": 38, "windowDurationMins": 10080,
                    "resetsAt": 1793780314},
        "secondary": None,
        "planType": "pro",
        "rateLimitReachedType": None,
        "spendControlReached": False,
    },
}

ULTRA_PROFILE = json.dumps(
    {"organization": {"organization_type": "claude_max", "plan_display_name": "Ultra"}}
)


def _row(**fields):
    row = {
        "id": 7,
        "provider": "claude",
        "email": "u@x.com",
        "expires_at": int(time.time()) + 3600,
        "priority": 1,
    }
    row.update(fields)
    if isinstance(row.get("cached_usage_raw"), dict):
        row["cached_usage_raw"] = json.dumps(row["cached_usage_raw"])
    return row


def _verified_ultra(**extra):
    return _row(
        subscription_type="max",
        rate_limit_tier="default_claude_max_20x",
        profile_json=ULTRA_PROFILE,
        # A stale 5h reading from before Anthropic verified the account.
        cached_usage_5h=67.0,
        cached_5h_resets_at="2026-10-30T18:59:59+00:00",
        cached_usage_7d=33.0,
        cached_7d_resets_at="2026-11-02T22:59:59+00:00",
        cached_usage_raw=ULTRA_RAW,
        **extra,
    )


# --------------------------------------------------------------------------- #
# plan + windows on the account payload
# --------------------------------------------------------------------------- #


class TestPlanAndWindows:
    def test_verified_ultra_has_no_five_hour_window(self):
        resp = _account_to_response(_verified_ultra())

        assert resp.plan is not None
        assert resp.plan.model_dump() == {
            "id": "ultra",
            "label": "Ultra",
            "formerly": "Max 20x",
            "label_source": "provider",
            "drift": [],
            "catalog_verified_at": "2026-11-01",
        }
        five = resp.windows.five_hour
        assert (five.state, five.temporary, five.seconds) == ("absent", False, 5 * 3600)
        assert five.note.startswith("Verified Ultra accounts have no 5-hour limit")
        seven = resp.windows.seven_day
        assert (seven.state, seven.temporary, seven.seconds, seven.note) == (
            "present", False, 7 * 86400, None,
        )
        # The stale cached reading is never shown as a live 5h value.
        assert resp.usage.five_hour is None
        assert resp.usage.seven_day == 33.0

    def test_payload_is_json_ready_for_the_websocket(self):
        dumped = _account_to_response(_verified_ultra()).model_dump()
        text = json.dumps(dumped)  # the WS registry sends model_dump() as JSON
        assert '"plan"' in text and '"windows"' in text
        assert dumped["windows"]["five_hour"]["state"] == "absent"
        assert dumped["usage"]["five_hour"] is None

    def test_unverified_ultra_window_is_temporary(self):
        row = _row(
            subscription_type="max", rate_limit_tier="default_claude_max_20x",
            profile_json=ULTRA_PROFILE, cached_usage_5h=12.0, cached_usage_7d=4.0,
            cached_usage_raw=UNVERIFIED_ULTRA_RAW,
        )
        resp = _account_to_response(row)
        five = resp.windows.five_hour
        assert (five.state, five.temporary) == ("present", True)
        assert "temporary 5-hour limit" in five.note
        assert resp.usage.five_hour == 12.0

    def test_idle_max_reads_zero_not_null(self):
        row = _row(
            subscription_type="max", rate_limit_tier="default_claude_max_5x",
            cached_usage_5h=None, cached_usage_7d=23.0, cached_usage_raw=MAX_IDLE_RAW,
        )
        resp = _account_to_response(row)
        assert (resp.plan.id, resp.plan.label, resp.plan.formerly) == ("max", "Max", "Max 5x")
        assert resp.plan.label_source == "catalog"
        assert resp.windows.five_hour.state == "present"
        assert resp.usage.five_hour == 0

    def test_codex_pro_has_no_five_hour_window(self):
        row = _row(
            provider="codex", subscription_type="pro",
            cached_usage_5h=40.0, cached_usage_7d=38.0, cached_usage_raw=CODEX_PRO_RAW,
        )
        resp = _account_to_response(row)
        assert (resp.plan.id, resp.plan.label) == ("pro", "Pro")
        assert resp.windows.five_hour.state == "absent"
        assert "ChatGPT Pro" in resp.windows.five_hour.note
        assert resp.windows.seven_day.seconds == 10080 * 60
        assert resp.usage.five_hour is None
        assert resp.usage.seven_day == 38.0

    def test_drift_reaches_the_payload(self):
        # A Max 5x payload that lists its limits without a session entry: the
        # catalog says Max always has the window, so it stays present (idle)
        # and the contradiction is reported.
        weekly_only = {"five_hour": None,
                       "limits": [{"kind": "weekly_all", "percent": 10}]}
        row = _row(subscription_type="max", rate_limit_tier="default_claude_max_5x",
                   cached_usage_7d=10.0, cached_usage_raw=weekly_only)
        resp = _account_to_response(row)
        assert resp.plan.drift == ["session_missing"]
        assert resp.usage.five_hour == 0

    def test_a_row_without_usage_still_carries_plan_and_windows(self):
        resp = _account_to_response(_row(subscription_type="team"))
        assert resp.usage is None
        assert resp.plan.label == "Team"
        assert resp.windows.five_hour.state == "present"


# --------------------------------------------------------------------------- #
# usage.five_hour: the plan-aware rule
# --------------------------------------------------------------------------- #


class TestFiveHourRule:
    def test_verified_ultra_drops_a_stale_cached_reading(self):
        usage = _build_account_usage(_verified_ultra())
        assert usage.five_hour is None
        # The reset of a window the account does not have is stale too.
        assert usage.five_hour_resets_at is None
        assert usage.seven_day_resets_at == "2026-11-02T22:59:59+00:00"

    def test_present_window_keeps_its_reset(self):
        row = _row(subscription_type="max", rate_limit_tier="default_claude_max_5x",
                   cached_usage_5h=41.0, cached_5h_resets_at="2026-11-01T18:59:59+00:00",
                   cached_usage_7d=23.0, cached_usage_raw=MAX_IDLE_RAW)
        usage = _build_account_usage(row)
        assert (usage.five_hour, usage.five_hour_resets_at) == (
            41.0, "2026-11-01T18:59:59+00:00",
        )

    def test_max_with_a_session_limit_and_no_reading_is_zero(self):
        row = _row(subscription_type="max", rate_limit_tier="default_claude_max_5x",
                   cached_usage_5h=None, cached_usage_7d=23.0,
                   cached_usage_raw=MAX_IDLE_RAW)
        assert _build_account_usage(row).five_hour == 0

    def test_codex_pro_is_null(self):
        row = _row(provider="codex", subscription_type="pro",
                   cached_usage_5h=None, cached_usage_7d=38.0,
                   cached_usage_raw=CODEX_PRO_RAW)
        usage = _build_account_usage(row)
        assert usage.five_hour is None
        assert usage.seven_day == 38.0

    def test_a_passed_view_is_used_without_resolving_again(self, monkeypatch):
        row = _verified_ultra()
        view = usage_plans.account_view(row)
        calls = []
        real = usage_plans.account_view

        def _counting(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(usage_plans, "account_view", _counting)
        assert _build_account_usage(row, view).five_hour is None
        assert calls == []

    def test_the_response_resolves_each_row_once(self, monkeypatch):
        calls = []
        real = usage_plans.account_view

        def _counting(*args, **kwargs):
            calls.append(args)
            return real(*args, **kwargs)

        monkeypatch.setattr(usage_plans, "account_view", _counting)
        _account_to_response(_verified_ultra())
        assert len(calls) == 1

    def test_seven_day_keeps_its_rule(self):
        claude = _row(subscription_type="max", rate_limit_tier="default_claude_max_5x",
                      cached_usage_5h=10.0, cached_usage_7d=None,
                      cached_usage_raw=MAX_IDLE_RAW)
        assert _build_account_usage(claude).seven_day == 0
        codex = _row(provider="codex", subscription_type="plus",
                     cached_usage_5h=10.0, cached_usage_7d=None)
        assert _build_account_usage(codex).seven_day is None

    def test_no_cached_reading_returns_none(self):
        assert _build_account_usage(_row(subscription_type="max",
                                         rate_limit_tier="default_claude_max_20x")) is None


# --------------------------------------------------------------------------- #
# A resolver error never fails the account list
# --------------------------------------------------------------------------- #


# The shared resolver guard (menubar_summary.safe_account_view) logs here.
_SUMMARY_LOGGER = "jacked.service.menubar_summary"


def _raising_resolver(monkeypatch):
    calls = {"n": 0}

    def _boom(row, *args, **kwargs):
        calls["n"] += 1
        raise RuntimeError("resolver exploded on payload shape")

    monkeypatch.setattr(usage_plans, "account_view", _boom)
    return calls


class TestResolverFailure:
    def test_response_falls_back_to_cached_readings(self, monkeypatch, caplog):
        _raising_resolver(monkeypatch)
        row = _verified_ultra()
        with caplog.at_level(logging.WARNING, logger=_SUMMARY_LOGGER):
            resp = _account_to_response(row)

        assert resp.plan is None
        assert resp.windows is None
        # No plan facts: the cached readings, a null 5h stays null.
        assert resp.usage.five_hour == 67.0
        assert resp.usage.seven_day == 33.0
        # The per-model list still renders, with provider window lengths.
        assert resp.usage.binding_model.label == "Fable"
        assert resp.usage.binding_model.window_seconds == 7 * 86400

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        text = warnings[0].getMessage()
        assert "account 7" in text and "RuntimeError" in text
        # Neither the payload nor the exception text reaches the log.
        assert "exploded" not in text and "Fable" not in text

    def test_null_cached_five_hour_stays_null(self, monkeypatch):
        _raising_resolver(monkeypatch)
        row = _row(subscription_type="max", rate_limit_tier="default_claude_max_5x",
                   cached_usage_5h=None, cached_usage_7d=30.0)
        assert _account_to_response(row).usage.five_hour is None

    def test_warning_is_logged_once_per_account_and_type(self, monkeypatch, caplog):
        _raising_resolver(monkeypatch)
        with caplog.at_level(logging.DEBUG, logger=_SUMMARY_LOGGER):
            for _ in range(3):
                _account_to_response(_verified_ultra())
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert any(r.levelno == logging.DEBUG and r.exc_info for r in caplog.records)

    def test_list_route_serves_every_account(self, monkeypatch, tmp_path):
        """GET /api/auth/accounts answers 200 with a row the resolver fails on."""
        from jacked.web.database import Database

        db = Database(str(tmp_path / "jacked.db"))
        good = db.create_account("good@x.com", "tok-a", int(time.time()) + 3600,
                                 subscription_type="team")
        bad = db.create_account("bad@x.com", "tok-b", int(time.time()) + 3600,
                                provider="codex", subscription_type="pro")
        db.update_account_usage_cache(good["id"], five_hour=5.0, seven_day=6.0)
        # A payload shape the Codex window parser does not expect.
        db.update_account_usage_cache(
            bad["id"], seven_day=9.0,
            raw={"rateLimits": {"primary": "unexpected", "secondary": None}},
        )
        monkeypatch.setattr(routes_auth, "_get_active_account_id_cached", lambda: None)
        request = MagicMock()
        request.app.state.db = db
        try:
            accounts = asyncio.run(routes_auth.list_accounts(request))
        finally:
            db.close()

        by_email = {a.email: a for a in accounts}
        assert by_email["good@x.com"].plan.label == "Team"
        assert by_email["good@x.com"].usage.five_hour == 5.0
        assert by_email["bad@x.com"].plan is None
        assert by_email["bad@x.com"].windows is None
        assert by_email["bad@x.com"].usage.seven_day == 9.0


# --------------------------------------------------------------------------- #
# Per-model window lengths
# --------------------------------------------------------------------------- #


class TestModelWindowSeconds:
    def test_every_model_entry_carries_its_window_length(self):
        usage = _build_account_usage(_verified_ultra())
        assert usage.per_model["fable"].window_seconds == 7 * 86400
        assert usage.binding_model.window_seconds == 7 * 86400

    def test_the_plan_reaches_the_per_model_parser(self, monkeypatch):
        seen = []
        real = menubar_summary.parse_per_model

        def _spy(raw, plan=None):
            seen.append(plan)
            return real(raw, plan)

        monkeypatch.setattr(menubar_summary, "parse_per_model", _spy)
        _build_account_usage(_verified_ultra())
        assert [p.id for p in seen] == ["ultra"]

    def test_codex_named_limit_uses_its_own_duration(self):
        raw = dict(CODEX_PRO_RAW)
        raw["rateLimitsByLimitId"] = {
            "codex_bengalfox": {
                "limitName": "GPT-5.3-Codex-Spark",
                "primary": {"usedPercent": 7, "windowDurationMins": 300},
            },
        }
        row = _row(provider="codex", subscription_type="pro",
                   cached_usage_7d=38.0, cached_usage_raw=raw)
        usage = _build_account_usage(row)
        assert usage.per_model["codex_bengalfox"].window_seconds == 300 * 60
