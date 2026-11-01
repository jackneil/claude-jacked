"""Bulk "Refresh all usage" with payloads that carry no 5-hour object.

A verified Ultra account answers ``"five_hour": null`` (2026-11-01 live
shape), and a Codex app-server result has no ``five_hour`` key at all. The
bulk pass used to parse ``usage_data.get("five_hour", {})``: the null raised
AttributeError, so every verified Ultra account was reported failed and its
card never got a "done" broadcast, and every Codex row reported a None 7d.
The pass now reports the values stored in the DB after the fetch.

Uses asyncio.run() wrappers (project convention: no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jacked.api.routes import auth as routes_auth
from jacked.web.auth import usage_cache_updates
from jacked.web.database import Database

ULTRA_PAYLOAD = {
    "five_hour": None,
    "seven_day": {"utilization": 33.0, "resets_at": "2026-11-02T22:59:59+00:00"},
    "seven_day_opus": None,
    "seven_day_sonnet": None,
    "limits": [
        {"kind": "weekly_all", "group": "weekly", "percent": 33,
         "resets_at": "2026-11-02T22:59:59+00:00", "scope": None, "is_active": False},
        {"kind": "weekly_scoped", "group": "weekly", "percent": 61,
         "resets_at": "2026-11-02T22:59:59+00:00", "is_active": True,
         "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
    ],
}

# The account/rateLimits/read result of a weekly-only ChatGPT Pro account.
CODEX_RESULT = {
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


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Fresh bulk-refresh locks, no real credential store, no real home."""
    routes_auth.reset_locks()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(routes_auth, "_get_active_account_id_cached", lambda: None)
    yield
    routes_auth.reset_locks()


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "jacked.db"))
    yield database
    database.close()


class _Registry:
    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    async def broadcast(self, event, payload=None, **kwargs):
        self.events.append((event, payload))


def _request(db, registry=None):
    request = MagicMock()
    request.app.state.db = db
    request.app.state.ws_registry = registry
    return request


def _storing_fetch(payload, **cache_updates):
    """A fetch_usage stand-in that stores like the real one, then returns
    the provider payload."""

    async def _fetch(account_id, db_arg, access_token=None, manual=False):
        updates = cache_updates or usage_cache_updates(payload)

        def _store():
            db_arg.update_account_usage_cache(account_id, raw=payload, **updates)

        await asyncio.to_thread(_store)
        return payload

    return _fetch


def _run(request, fetch):
    async def _go():
        with patch.object(routes_auth, "fetch_usage", side_effect=fetch):
            return await routes_auth.refresh_all_usage(request)

    return asyncio.run(_go())


def _ultra_account(db):
    acct = db.create_account(
        "ultra@x.com", "tok-u", int(time.time()) + 3600,
        subscription_type="max", rate_limit_tier="default_claude_max_20x",
    )
    # A 5h reading from before Anthropic verified the account.
    db.update_account_usage_cache(
        acct["id"], five_hour=67.0,
        five_hour_resets_at="2026-10-30T18:59:59+00:00", seven_day=30.0,
    )
    return acct


@pytest.mark.parametrize("with_ws", [True, False])
def test_null_five_hour_payload_is_a_success(db, with_ws):
    acct = _ultra_account(db)
    registry = _Registry() if with_ws else None

    resp = _run(_request(db, registry), _storing_fetch(ULTRA_PAYLOAD))

    assert (resp.refreshed, resp.failed) == (1, 0)
    row = resp.results[0]
    assert row["account_id"] == acct["id"]
    assert row["success"] is True
    assert "error" not in row
    # The values stored by the fetch: 7d read, the null 5h window cleared.
    assert row["cached_usage_7d"] == 33.0
    assert row["cached_usage_5h"] is None


def test_null_five_hour_payload_broadcasts_done_with_the_plan(db):
    acct = _ultra_account(db)
    registry = _Registry()

    _run(_request(db, registry), _storing_fetch(ULTRA_PAYLOAD))

    final = [p for e, p in registry.events
             if e == "usage_refresh_progress" and "progress" in p]
    assert len(final) == 1
    assert final[0]["account_id"] == acct["id"]
    assert final[0]["status"] == "done"
    data = final[0]["account_data"]
    assert data["windows"]["five_hour"]["state"] == "absent"
    assert data["plan"]["id"] == "ultra"
    assert data["usage"]["five_hour"] is None
    assert data["usage"]["seven_day"] == 33.0


@pytest.mark.parametrize("with_ws", [True, False])
def test_codex_result_reports_the_stored_seven_day(db, with_ws):
    acct = db.create_account(
        "cx@x.com", "tok-c", int(time.time()) + 3600,
        provider="codex", subscription_type="pro",
    )
    registry = _Registry() if with_ws else None
    # fetch_codex_usage normalizes the result and stores the 7d reading; the
    # raw result it returns has no five_hour key.
    fetch = _storing_fetch(
        CODEX_RESULT, seven_day=38.0,
        seven_day_resets_at="2026-11-08T05:38:34+00:00", clear_five_hour=True,
    )

    resp = _run(_request(db, registry), fetch)

    row = resp.results[0]
    assert row["account_id"] == acct["id"]
    assert row["success"] is True
    assert row["cached_usage_7d"] == 38.0
    assert row["cached_usage_5h"] is None


def test_cache_hit_reports_the_stored_values(db):
    acct = db.create_account("m@x.com", "tok-m", int(time.time()) + 3600,
                             subscription_type="max",
                             rate_limit_tier="default_claude_max_5x")
    db.update_account_usage_cache(acct["id"], five_hour=41.0, seven_day=23.0)

    async def cached(*args, **kwargs):
        return {"_cached": True}

    resp = _run(_request(db), cached)

    row = resp.results[0]
    assert row["success"] is True
    assert (row["cached_usage_5h"], row["cached_usage_7d"]) == (41.0, 23.0)


def test_a_failed_fetch_still_reports_its_error(db):
    acct = _ultra_account(db)
    db.record_account_error(acct["id"], "Usage API returned 500")

    async def failing(*args, **kwargs):
        return None

    registry = _Registry()
    resp = _run(_request(db, registry), failing)

    row = resp.results[0]
    assert row["success"] is False
    assert row["error"] == "Usage API returned 500"
    final = [p for e, p in registry.events
             if e == "usage_refresh_progress" and "progress" in p]
    assert final[0]["status"] == "failed"
