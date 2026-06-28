"""Tests for the worst-account menu-bar summary (M2).

Covers the pure helpers (color thresholds, worst-account selection, pill title)
and the GET /api/menubar-summary endpoint against a real fixture DB.
"""
import pytest
from fastapi.testclient import TestClient

from jacked.service.menubar_summary import (
    compute_worst_account_summary,
    menubar_title,
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
# Worst-account selection
# ---------------------------------------------------------------------------


def test_picks_highest_utilization_account_and_class():
    accounts = [
        {"id": 1, "email": "low@x.com", "cached_usage_5h": 30, "cached_usage_7d": 40},
        {"id": 2, "email": "hot@x.com", "cached_usage_5h": 96, "cached_usage_7d": 78},
        {"id": 3, "email": "mid@x.com", "cached_usage_5h": 60, "cached_usage_7d": 72},
    ]
    s = compute_worst_account_summary(accounts)
    assert s["account_id"] == 2
    assert s["email"] == "hot@x.com"
    assert s["five_hour"] == 96.0
    assert s["seven_day"] == 78.0
    assert s["max_pct"] == 96.0
    assert s["color"] == "red"


def test_worst_can_be_driven_by_seven_day_window():
    accounts = [
        {"id": 1, "email": "a@x.com", "cached_usage_5h": 10, "cached_usage_7d": 85},
        {"id": 2, "email": "b@x.com", "cached_usage_5h": 50, "cached_usage_7d": 50},
    ]
    s = compute_worst_account_summary(accounts)
    assert s["account_id"] == 1, "max(5h,7d) ranks by the worse of the two windows"
    assert s["max_pct"] == 85.0
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
    s71 = compute_worst_account_summary([{"id": 1, "email": "a", "cached_usage_5h": 71, "cached_usage_7d": 0}])
    assert s71["color"] == "yellow"
    s90 = compute_worst_account_summary([{"id": 1, "email": "a", "cached_usage_5h": 90, "cached_usage_7d": 0}])
    assert s90["color"] == "red"


# ---------------------------------------------------------------------------
# Pill title
# ---------------------------------------------------------------------------


def test_menubar_title_formats_and_rounds():
    assert menubar_title(None) == "—"
    assert menubar_title({"five_hour": 96.0, "seven_day": 78.0, "color": "red"}) == "🔴 96%·78%"
    assert menubar_title({"five_hour": 40.4, "seven_day": 30.6, "color": "green"}) == "🟢 40%·31%"


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
                validation_status, cached_usage_5h, cached_usage_7d)
               VALUES (1, 'low@x.com', 'at1', 1900000000, 1, 0, 'valid', 30, 40)"""
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, expires_at, is_active, is_deleted,
                validation_status, cached_usage_5h, cached_usage_7d)
               VALUES (2, 'hot@x.com', 'at2', 1900000000, 1, 0, 'valid', 96, 78)"""
        )
    yield db
    db.close()


def test_endpoint_returns_worst_account(db):
    from jacked.api.main import app

    app.state.db = db
    try:
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.get("/api/menubar-summary")
        assert resp.status_code == 200
        data = resp.json()
        assert data["account_count"] == 2
        assert data["worst"]["email"] == "hot@x.com"
        assert data["worst"]["color"] == "red"
        assert data["worst"]["five_hour"] == 96.0
        assert data["worst"]["seven_day"] == 78.0
    finally:
        app.state.db = None


def test_endpoint_degraded_when_db_unavailable():
    from jacked.api.main import app

    app.state.db = None
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/menubar-summary")
    assert resp.status_code == 503
