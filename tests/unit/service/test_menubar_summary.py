"""Tests for the menu-bar usage summary.

Covers the pure helpers (color thresholds, single-account summary, active- and
worst-account selection, pill title) and the GET /api/menubar-summary endpoint
against a real fixture DB with the active-account resolver patched for
determinism.
"""
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from jacked.service.menubar_summary import (
    compute_active_account_summary,
    compute_worst_account_summary,
    menubar_title,
    summarize_account,
    usage_color_class,
)


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
    assert menubar_title(None) == "—"
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
        return ActiveCredentialResponse(account_id=account_id, email=None)

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
