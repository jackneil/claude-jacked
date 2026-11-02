"""Tests for the menu-bar usage summary.

Covers the pure helpers (color thresholds, single-account summary, active- and
worst-account selection, pill title) and the GET /api/menubar-summary endpoint
against a real fixture DB with the active-account resolver patched for
determinism.
"""
import json
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from jacked.service.menubar_summary import (
    compute_active_account_summary,
    compute_worst_account_summary,
    summarize_account,
    usage_color_class,
)
from jacked.service.pill_text import TOOLTIP_MAX_UNITS, menubar_title, tray_tooltip


# ---------------------------------------------------------------------------
# Color thresholds (mirror of JS usageColorClass: <71 green, 71-89 yellow, >=90 red)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pct,expected",
    [
        (0, "green"), (50, "green"), (70.9, "green"),
        (71, "yellow"), (80, "yellow"), (89.9, "yellow"),
        (90, "red"), (95, "red"), (100, "red"), (150, "red"),
        (None, "green"),
    ],
)
def test_usage_color_class_thresholds(pct, expected):
    assert usage_color_class(pct) == expected


# ---------------------------------------------------------------------------
# Single-account summary
# ---------------------------------------------------------------------------


def test_summarize_account():
    s = summarize_account(
        {"id": 1, "email": "a@x.com", "cached_usage_5h": 37, "cached_usage_7d": 87}
    )
    assert s["account_id"] == 1
    assert s["five_hour"] == 37.0 and s["seven_day"] == 87.0
    assert s["max_pct"] == 87.0
    assert s["color"] == "yellow"


def test_summarize_account_none_without_usage():
    assert summarize_account({"id": 1, "email": "a@x.com"}) is None
    assert summarize_account({}) is None


# ---------------------------------------------------------------------------
# The 5h slot follows the account's plan (jacked.usage_plans)
# ---------------------------------------------------------------------------

# Live shape (2026-11-01): a verified Ultra account answers five_hour: null
# and lists only weekly limits.
_ULTRA_RAW = json.dumps({
    "five_hour": None,
    "seven_day": {"utilization": 33.0, "resets_at": "2026-11-02T22:59:59+00:00"},
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 33},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 61, "is_active": True,
         "scope": {"model": {"display_name": "Fable"}}},
    ],
})


@pytest.fixture
def _fresh_plan_state(monkeypatch):
    from jacked import usage_plans
    from jacked.service import menubar_summary

    monkeypatch.setattr(usage_plans, "_catalog_cache", None)
    monkeypatch.setattr(usage_plans, "_drift_logged", set())
    monkeypatch.setattr(menubar_summary, "_VIEW_FAILURES_LOGGED", set())


def _ultra(**extra):
    row = {"id": 3, "email": "u@x.com", "subscription_type": "max",
           "rate_limit_tier": "default_claude_max_20x",
           "cached_usage_5h": 67.0,  # stale, from before verification
           "cached_usage_7d": 33.0, "cached_usage_raw": _ULTRA_RAW}
    row.update(extra)
    return row


@pytest.mark.usefixtures("_fresh_plan_state")
class TestPlanAwareFiveHour:
    def test_verified_ultra_has_no_5h_slot(self):
        s = summarize_account(_ultra())
        assert s["five_hour"] is None
        assert s["seven_day"] == 33.0
        # The worst limit is the weekly Fable limit (61%); the stale 67% in
        # the 5h cache is not a reading.
        assert s["model_cap"]["label"] == "Fable"
        assert s["max_pct"] == 61.0
        assert s["color"] == "green"
        assert menubar_title(s) == "7d 33%·F61%"

    def test_idle_max_reads_zero(self):
        raw = json.dumps({"five_hour": None, "limits": [
            {"kind": "session", "percent": 0}, {"kind": "weekly_all", "percent": 23}]})
        s = summarize_account({"id": 4, "subscription_type": "max",
                               "rate_limit_tier": "default_claude_max_5x",
                               "cached_usage_5h": None, "cached_usage_7d": 23,
                               "cached_usage_raw": raw})
        assert s["five_hour"] == 0.0
        assert menubar_title(s) == "0%·23%"

    def test_rounding_is_kept(self):
        raw = json.dumps({"limits": [{"kind": "session", "percent": 41}]})
        s = summarize_account({"id": 5, "subscription_type": "max",
                               "rate_limit_tier": "default_claude_max_5x",
                               "cached_usage_5h": 41.26, "cached_usage_7d": 9.04,
                               "cached_usage_raw": raw})
        assert (s["five_hour"], s["seven_day"]) == (41.3, 9.0)

    def test_no_reading_in_either_slot_is_no_summary(self):
        # The Codex payload lists every window the account has, and it lists
        # none: the stale 5h value is no reading, and Codex never turns a
        # null 7d into 0%. Nothing is left to show (and max() of nothing
        # must not raise).
        raw = json.dumps({"rateLimits": {"primary": None, "secondary": None}})
        assert summarize_account({"id": 9, "provider": "codex",
                                  "subscription_type": "pro",
                                  "cached_usage_5h": 40.0,
                                  "cached_usage_7d": None,
                                  "cached_usage_raw": raw}) is None

    def test_a_cached_five_hour_reading_without_a_payload_is_kept(self):
        # No payload to read: the cached 5h reading is prior evidence of a
        # window, and a partial read must never erase a live window.
        s = summarize_account({"id": 9, "provider": "codex",
                               "subscription_type": "pro",
                               "cached_usage_5h": 40.0, "cached_usage_7d": None})
        assert (s["five_hour"], s["seven_day"], s["max_pct"]) == (40.0, None, 40.0)

    def test_worst_ignores_a_stale_5h_on_a_plan_without_the_window(self):
        # The Ultra row's real worst is its Fable limit (61%); a stale 99% in
        # its 5h cache would beat busy's 70% if it still counted.
        busy = {"id": 1, "email": "busy@x.com", "cached_usage_5h": 70, "cached_usage_7d": 40}
        worst = compute_worst_account_summary([_ultra(cached_usage_5h=99.0), busy])
        assert worst["email"] == "busy@x.com"

    def test_resolver_error_falls_back_to_the_cached_readings(self, monkeypatch, caplog):
        import logging

        from jacked import usage_plans

        def _boom(*args, **kwargs):
            raise RuntimeError("unexpected payload shape")

        monkeypatch.setattr(usage_plans, "account_view", _boom)
        with caplog.at_level(logging.WARNING, logger="jacked.service.menubar_summary"):
            s = summarize_account(_ultra())
            worst = compute_worst_account_summary([_ultra(), _ultra(id=8)])
        assert (s["five_hour"], s["seven_day"]) == (67.0, 33.0)
        assert worst is not None
        assert summarize_account(_ultra(cached_usage_5h=None))["five_hour"] is None
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 2  # once per account
        assert all("RuntimeError" in w and "unexpected" not in w for w in warnings)


# ---------------------------------------------------------------------------
# Pill text and tooltip for every plan in the shipped catalog
# ---------------------------------------------------------------------------


def _catalog_variants():
    """(provider, subscription, tier) for every plan the shipped catalog names,
    plus each provider's default (an unknown plan). A plan added to the catalog
    joins this walk without a test edit."""
    from jacked import usage_plans

    out = []
    for provider, pcat in usage_plans.catalog().providers.items():
        out.append((provider, "", ""))
        for sub, entry in pcat.subscriptions.items():
            out.append((provider, sub, ""))
            out.extend((provider, sub, tier) for tier in entry.tiers)
    return out


def _variant_row(provider, sub, tier, session_reported):
    """A row for one catalog plan: 5h 12% (when the payload reports a session
    window), 7d 34%, and for Claude a weekly Fable limit at 56%."""
    if provider == "claude":
        limits = [{"kind": "weekly_all", "percent": 34},
                  {"kind": "weekly_scoped", "percent": 56, "is_active": True,
                   "scope": {"model": {"display_name": "Fable"}}}]
        if session_reported:
            limits.insert(0, {"kind": "session", "percent": 12})
        raw = {"five_hour": {"utilization": 12.0} if session_reported else None,
               "limits": limits}
    else:
        week = {"usedPercent": 34, "windowDurationMins": 10080, "resetsAt": 4102444800}
        five = {"usedPercent": 12, "windowDurationMins": 300, "resetsAt": 4102444800}
        raw = {"ordinaryUsageAllowed": True,
               "rateLimits": ({"primary": five, "secondary": week} if session_reported
                              else {"primary": week, "secondary": None})}
    return {"id": 1, "provider": provider, "subscription_type": sub or None,
            "rate_limit_tier": tier or None,
            "cached_usage_5h": 12.0 if session_reported else None,
            "cached_usage_7d": 34.0, "cached_usage_raw": json.dumps(raw)}


_VARIANTS = [
    pytest.param(provider, sub, tier, reported,
                 id=f"{provider}-{sub or 'default'}-{tier or 'any'}-{'5h' if reported else 'no5h'}")
    for provider, sub, tier in _catalog_variants()
    for reported in (True, False)
]


@pytest.mark.usefixtures("_fresh_plan_state")
class TestPillForEveryCatalogPlan:
    """Every plan and provider jacked knows renders a pill with no dead slot."""

    @pytest.mark.parametrize("provider,sub,tier,reported", _VARIANTS)
    def test_pill_shows_only_the_limits_the_account_has(self, provider, sub, tier, reported):
        s = summarize_account(_variant_row(provider, sub, tier, reported))
        title = menubar_title(s)
        slots = title.split("·")
        assert "-" not in slots, title  # never a dead slot
        if s["five_hour"] is None:
            # The weekly number carries its tag, so it cannot read as 5h.
            assert slots[0] == "7d 34%", title
        else:
            assert slots[:2] == [f"{round(s['five_hour'])}%", "34%"], title
        if provider == "claude":
            assert slots[-1] == "F56%", title
            assert s["max_pct"] == 56.0 and s["color"] == "green"
        else:
            assert s["model_cap"] is None
        tip = tray_tooltip(s)
        assert ("5h " in tip) is (s["five_hour"] is not None), tip
        assert "7d 34%" in tip
        assert ("Fable 56%" in tip) is (provider == "claude"), tip

    def test_the_walk_covers_both_shapes_of_the_pill(self):
        # Guard against a catalog change that leaves only one branch tested.
        shapes = set()
        for provider, sub, tier in _catalog_variants():
            for reported in (True, False):
                s = summarize_account(_variant_row(provider, sub, tier, reported))
                shapes.add((provider, s["five_hour"] is None))
        assert shapes == {("claude", True), ("claude", False),
                          ("codex", True), ("codex", False)}


@pytest.mark.usefixtures("_fresh_plan_state")
class TestModelLimitInThePill:
    def _ultra13(self):
        # Live shape (2026-11-02, acct 13): weekly 74%, weekly Fable 100%.
        raw = json.dumps({"five_hour": None, "limits": [
            {"kind": "weekly_all", "percent": 74},
            {"kind": "weekly_scoped", "percent": 100, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}}]})
        return _ultra(cached_usage_7d=74.0, cached_usage_raw=raw)

    def test_a_capped_fable_limit_turns_the_pill_red(self):
        s = summarize_account(self._ultra13())
        assert menubar_title(s) == "7d 74%·F100%"
        assert (s["max_pct"], s["color"]) == (100.0, "red")
        assert tray_tooltip(s) == "Jacked · Claude · 7d 74% · Fable 100%"

    def test_max_shows_all_three_limits(self):
        raw = json.dumps({"limits": [
            {"kind": "session", "percent": 5}, {"kind": "weekly_all", "percent": 11},
            {"kind": "weekly_scoped", "percent": 18, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}}]})
        s = summarize_account({"id": 5, "subscription_type": "max",
                               "rate_limit_tier": "default_claude_max_5x",
                               "cached_usage_5h": 5.0, "cached_usage_7d": 11.0,
                               "cached_usage_raw": raw})
        assert menubar_title(s) == "5%·11%·F18%"
        assert s["max_pct"] == 18.0

    def test_the_pill_shows_the_limit_the_dashboard_shows(self):
        # The provider marks Fable active: the pill shows Fable (as the
        # dashboard does), and the tint follows the shown limit only.
        raw = json.dumps({"five_hour": None, "limits": [
            {"kind": "weekly_all", "percent": 20},
            {"kind": "weekly_scoped", "percent": 40, "is_active": True,
             "scope": {"model": {"display_name": "Fable"}}},
            {"kind": "weekly_scoped", "percent": 88, "is_active": False,
             "scope": {"model": {"display_name": "Sonnet"}}}]})
        s = summarize_account(_ultra(cached_usage_7d=20.0, cached_usage_raw=raw))
        assert menubar_title(s) == "7d 20%·F40%"
        assert s["color"] == "green"

    def test_a_codex_model_limit_is_tagged_by_its_last_word(self):
        raw = json.dumps({"ordinaryUsageAllowed": True,
                          "rateLimits": {"primary": {"usedPercent": 5, "windowDurationMins": 300},
                                         "secondary": {"usedPercent": 40,
                                                       "windowDurationMins": 10080}},
                          "rateLimitsByLimitId": {
                              "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark",
                                                  "secondary": {"usedPercent": 91}}}})
        s = summarize_account({"id": 14, "provider": "codex", "subscription_type": "plus",
                               "cached_usage_5h": 5.0, "cached_usage_7d": 40.0,
                               "cached_usage_raw": raw})
        assert menubar_title(s) == "5%·40%·S91%"
        assert s["color"] == "red"
        assert tray_tooltip(s) == "Jacked · Codex · 5h 5% · 7d 40% · GPT-5.3-Codex-Spark 91%"

    def test_an_unreadable_payload_has_no_model_limit(self):
        s = summarize_account({"id": 1, "cached_usage_5h": 10, "cached_usage_7d": 20,
                               "cached_usage_raw": "not json"})
        assert s["model_cap"] is None
        assert menubar_title(s) == "10%·20%"

    def test_a_model_limit_failure_does_not_stop_the_pill(self, monkeypatch):
        from jacked.service import menubar_summary

        def _boom(*args, **kwargs):
            raise RuntimeError("bad payload")

        monkeypatch.setattr(menubar_summary, "binding_model_compact", _boom)
        s = summarize_account(self._ultra13())
        assert s["model_cap"] is None
        assert menubar_title(s) == "7d 74%"


def test_pill_rounds_half_up_like_the_dashboard():
    # usage.js uses Math.round: 40.5 shows 41%. Python's round() gives 40.
    assert menubar_title({"five_hour": 40.5, "seven_day": 2.5}) == "41%·3%"


class TestTrayTooltip:
    def test_no_data_and_unknown_provider(self):
        assert tray_tooltip(None) == "Jacked"
        assert tray_tooltip({"provider": "grok", "seven_day": 9.0}) == "Jacked · Grok · 7d 9%"

    def test_a_long_label_is_cut_to_the_windows_limit(self):
        cap = {"label": "X" * 300, "utilization": 50.0}
        tip = tray_tooltip({"provider": "claude", "five_hour": 1.0, "seven_day": 2.0,
                            "model_cap": cap})
        assert len(tip.encode("utf-16-le")) // 2 == TOOLTIP_MAX_UNITS
        assert tip.endswith("…") and tip.startswith("Jacked · Claude · 5h 1% · 7d 2% · X")

    def test_the_limit_counts_utf16_units_not_characters(self):
        # An emoji is two UTF-16 units: a cut by characters would overflow.
        cap = {"label": "\U0001F600" * 100, "utilization": 50.0}
        tip = tray_tooltip({"provider": "claude", "seven_day": 2.0, "model_cap": cap})
        assert len(tip.encode("utf-16-le")) // 2 <= TOOLTIP_MAX_UNITS


# ---------------------------------------------------------------------------
# Active-account selection (what the pill shows)
# ---------------------------------------------------------------------------


def _fleet():
    return [
        {"id": 1, "email": "me@x.com", "cached_usage_5h": 37, "cached_usage_7d": 87},
        {"id": 5, "email": "idle@x.com", "cached_usage_5h": 0, "cached_usage_7d": 100},
    ]


def test_active_summary_tracks_the_active_account_not_the_worst():
    # The fleet's WORST is idle@x.com at 100% 7d, but the pill must show the
    # ACTIVE account (me@x.com) — this is the exact bug the redesign fixes.
    s = compute_active_account_summary(_fleet(), 1)
    assert s["email"] == "me@x.com"
    assert s["five_hour"] == 37.0 and s["seven_day"] == 87.0
    assert s["color"] == "yellow"
    # ...whereas worst-of-all would have picked the idle 100% account.
    assert compute_worst_account_summary(_fleet())["email"] == "idle@x.com"


def test_active_summary_none_when_no_active_id():
    assert compute_active_account_summary(_fleet(), None) is None


def test_active_summary_none_when_active_not_found():
    assert compute_active_account_summary(_fleet(), 999) is None


def test_active_summary_none_when_active_disabled_or_deleted():
    accts = [
        {"id": 1, "email": "a@x.com", "is_active": False, "cached_usage_5h": 50, "cached_usage_7d": 50},
        {"id": 2, "email": "b@x.com", "is_deleted": True, "cached_usage_5h": 50, "cached_usage_7d": 50},
    ]
    assert compute_active_account_summary(accts, 1) is None
    assert compute_active_account_summary(accts, 2) is None


def test_active_summary_none_when_active_has_no_usage():
    accts = [{"id": 1, "email": "a@x.com", "cached_usage_5h": None, "cached_usage_7d": None}]
    assert compute_active_account_summary(accts, 1) is None


# ---------------------------------------------------------------------------
# Worst-account selection (fleet-wide glance, retained)
# ---------------------------------------------------------------------------


def test_picks_highest_utilization_account_and_class():
    accounts = [
        {"id": 1, "email": "low@x.com", "cached_usage_5h": 30, "cached_usage_7d": 40},
        {"id": 2, "email": "hot@x.com", "cached_usage_5h": 96, "cached_usage_7d": 78},
        {"id": 3, "email": "mid@x.com", "cached_usage_5h": 60, "cached_usage_7d": 72},
    ]
    s = compute_worst_account_summary(accounts)
    assert s["account_id"] == 2
    assert s["max_pct"] == 96.0
    assert s["color"] == "red"


def test_worst_can_be_driven_by_seven_day_window():
    accounts = [
        {"id": 1, "email": "a@x.com", "cached_usage_5h": 10, "cached_usage_7d": 85},
        {"id": 2, "email": "b@x.com", "cached_usage_5h": 50, "cached_usage_7d": 50},
    ]
    s = compute_worst_account_summary(accounts)
    assert s["account_id"] == 1, "max(5h,7d) ranks by the worse of the two windows"
    assert s["color"] == "yellow"


def test_skips_disabled_deleted_and_usageless_accounts():
    accounts = [
        {"id": 1, "email": "disabled@x.com", "is_active": False, "cached_usage_5h": 99, "cached_usage_7d": 99},
        {"id": 2, "email": "deleted@x.com", "is_deleted": True, "cached_usage_5h": 98, "cached_usage_7d": 98},
        {"id": 3, "email": "nousage@x.com", "cached_usage_5h": None, "cached_usage_7d": None},
        {"id": 4, "email": "real@x.com", "cached_usage_5h": 20, "cached_usage_7d": 25},
    ]
    s = compute_worst_account_summary(accounts)
    assert s["account_id"] == 4, "disabled/deleted/usageless accounts must not win"
    assert s["color"] == "green"


def test_returns_none_when_no_usable_accounts():
    assert compute_worst_account_summary([]) is None
    assert compute_worst_account_summary([{"id": 1, "email": "x", "cached_usage_5h": None, "cached_usage_7d": None}]) is None


def test_boundary_exactly_71_and_90():
    assert compute_worst_account_summary([{"id": 1, "email": "a", "cached_usage_5h": 71, "cached_usage_7d": 0}])["color"] == "yellow"
    assert compute_worst_account_summary([{"id": 1, "email": "a", "cached_usage_5h": 90, "cached_usage_7d": 0}])["color"] == "red"


# ---------------------------------------------------------------------------
# Pill title — just the %, the colored "J" icon carries the color
# ---------------------------------------------------------------------------


def test_menubar_title_formats_and_rounds():
    assert menubar_title(None) == "-"
    assert menubar_title({"five_hour": 96.0, "seven_day": 78.0, "color": "red"}) == "96%·78%"
    assert menubar_title({"five_hour": 40.4, "seven_day": 30.6, "color": "green"}) == "40%·31%"


def test_menubar_title_has_no_color_glyph():
    # The color now lives in the icon, not the text — no emoji in the title.
    title = menubar_title({"five_hour": 96.0, "seven_day": 78.0, "color": "red"})
    assert "🔴" not in title and "🟡" not in title and "🟢" not in title


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@pytest.fixture
def db(tmp_path):
    from jacked.web.database import Database

    db = Database(str(tmp_path / "test.db"))
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, expires_at, is_active, is_deleted,
                validation_status, cached_usage_5h, cached_usage_7d, usage_cached_at)
               VALUES (1, 'low@x.com', 'at1', 1900000000, 1, 0, 'valid', 37, 87, 1700000000)"""
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, expires_at, is_active, is_deleted,
                validation_status, cached_usage_5h, cached_usage_7d, usage_cached_at)
               VALUES (2, 'hot@x.com', 'at2', 1900000000, 1, 0, 'valid', 96, 78, 1700000000)"""
        )
    yield db
    db.close()


def _patch_active(account_id):
    """Patch the active-credential resolver the endpoint reuses, for determinism
    (the real one reads the host's ~/.claude credentials)."""
    from jacked.api.routes.auth import ActiveCredentialResponse

    async def _fake(_request):
        # allow_credential_activation is required on the model (a forgotten
        # kwarg must be a construction error, not a silently-enabled button).
        # The menu-bar summary ignores it; True is the loopback answer.
        return ActiveCredentialResponse(
            account_id=account_id, email=None, allow_credential_activation=True
        )

    return mock.patch("jacked.api.routes.auth.get_active_credential", _fake)


def test_endpoint_returns_active_account(db):
    from jacked.api.main import app

    app.state.db = db
    try:
        with _patch_active(1):  # active = low@x.com (37/87)
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/menubar-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["account_count"] == 2
        assert data["active_account_id"] == 1
        assert data["active"]["email"] == "low@x.com"
        assert data["active"]["five_hour"] == 37.0
        assert data["active"]["seven_day"] == 87.0
        assert data["active"]["color"] == "yellow"
        # worst is still reported (the fleet-wide glance) — hot@x.com at 96%.
        assert data["worst"]["email"] == "hot@x.com"
        assert data["worst"]["color"] == "red"
    finally:
        app.state.db = None


def test_endpoint_active_null_when_no_active_account(db):
    from jacked.api.main import app

    app.state.db = db
    try:
        with _patch_active(None):
            client = TestClient(app, raise_server_exceptions=False)
            resp = client.get("/api/menubar-summary")
        data = resp.json()
        assert data["active"] is None
        assert data["account_count"] == 2
    finally:
        app.state.db = None


def _clear_poll_state(app):
    for attr in ("active_poll_account_id", "active_poll_at", "active_poll_interval", "active_poll_tier"):
        try:
            setattr(app.state, attr, None)
        except Exception:
            pass


def test_endpoint_next_refresh_from_published_poll_schedule(db):
    """The active account's next_refresh_at uses the REAL schedule the poll loop
    published to app.state (last poll time + interval)."""
    import time as _t
    from jacked.api.main import app

    app.state.db = db
    now = int(_t.time())
    app.state.active_poll_account_id = 1
    app.state.active_poll_at = now
    app.state.active_poll_interval = 300
    app.state.active_poll_tier = "normal"
    try:
        with _patch_active(1):
            client = TestClient(app, raise_server_exceptions=False)
            data = client.get("/api/menubar-summary").json()
        assert data["active"]["next_refresh_at"] == now + 300
        assert data["active"]["poll_interval"] == 300
        assert data["active"]["poll_tier"] == "normal"
    finally:
        _clear_poll_state(app)
        app.state.db = None


def test_endpoint_next_refresh_falls_back_to_tier_estimate(db):
    """With no published schedule, next_refresh_at = usage_cached_at + the real
    tier interval (compute_urgency_tier), never a hardcoded guess."""
    from jacked.api.main import app

    app.state.db = db
    _clear_poll_state(app)
    try:
        with _patch_active(1):
            client = TestClient(app, raise_server_exceptions=False)
            data = client.get("/api/menubar-summary").json()
        a = data["active"]
        assert a["next_refresh_at"] is not None
        assert a["poll_interval"] and a["poll_interval"] > 0
        # internal consistency: next = cached_at(1700000000) + the computed interval
        assert a["next_refresh_at"] == 1700000000 + a["poll_interval"]
    finally:
        app.state.db = None


def test_endpoint_degraded_when_db_unavailable():
    from jacked.api.main import app

    app.state.db = None
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/menubar-summary")
    assert resp.status_code == 503


def test_endpoint_worst_codex_keeps_unreported_window_null(db):
    """A blocked weekly-only Codex account: no 5h number, red, blocked."""
    import json
    import time

    from jacked.api.main import app

    raw = {"ordinaryUsageAllowed": False,
           "rateLimits": {"rateLimitReachedType": "rate_limit_reached"},
           "rateLimitUpsell": {"reset_at": int(time.time()) + 86400}}
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, provider, email, access_token, expires_at, is_active, is_deleted,
                validation_status, cached_usage_5h, cached_usage_7d, usage_cached_at,
                cached_usage_raw)
               VALUES (9, 'codex', 'cx@x.com', 'at9', 1900000000, 1, 0, 'valid',
                       NULL, 100, ?, ?)""",
            (int(time.time()), json.dumps(raw)),
        )
    app.state.db = db
    try:
        with _patch_active(1):
            resp = TestClient(app, raise_server_exceptions=False).get("/api/menubar-summary")
        worst = resp.json()["worst"]
        assert worst["email"] == "cx@x.com"
        assert worst["five_hour"] is None
        assert worst["blocked"] is True
        assert worst["color"] == "red"
        assert menubar_title(worst) == "7d 100%"
    finally:
        app.state.db = None


def test_endpoint_survives_a_payload_the_resolver_cannot_read(db, _fresh_plan_state, monkeypatch):
    """One account the plan resolver fails on must not take the pill down
    for the whole fleet."""
    import time

    import jacked.usage_plans as usage_plans
    from jacked.api.main import app

    real_view = usage_plans.account_view

    def failing_view(row, *args, **kwargs):
        if row.get("email") == "junk@x.com":
            raise RuntimeError("resolver failure")
        return real_view(row, *args, **kwargs)

    monkeypatch.setattr(usage_plans, "account_view", failing_view)
    junk = {"rateLimits": {"primary": "unexpected", "secondary": None}}
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, provider, email, access_token, expires_at, is_active, is_deleted,
                validation_status, cached_usage_5h, cached_usage_7d, usage_cached_at,
                cached_usage_raw)
               VALUES (10, 'codex', 'junk@x.com', 'at10', 1900000000, 1, 0, 'valid',
                       NULL, 12, ?, ?)""",
            (int(time.time()), json.dumps(junk)),
        )
    app.state.db = db
    try:
        with _patch_active(10):
            resp = TestClient(app, raise_server_exceptions=False).get("/api/menubar-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["active"]["email"] == "junk@x.com"
        assert data["active"]["five_hour"] is None
        assert data["active"]["seven_day"] == 12.0
        assert data["worst"]["email"] == "hot@x.com"
    finally:
        app.state.db = None
