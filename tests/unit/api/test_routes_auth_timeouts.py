"""Tests for route-level timeout guards on single-account auth endpoints,
the bulk-refresh 429 envelope / finally-block state reset, and the bulk
pass-throttle (2026-06 usage-fetch storm fix).

Uses asyncio.run() wrappers (project convention — no pytest-asyncio)."""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Reset bulk-refresh module state before each test (see
    test_bulk_refresh_timeout.py for the cross-loop Lock rationale) and
    redirect Path.home() so no test touches real credential stores."""
    from jacked.api.routes import auth as routes_auth
    routes_auth.reset_locks()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield
    routes_auth.reset_locks()


async def _hang(*args, **kwargs):
    await asyncio.sleep(3600)


def _body(resp):
    return json.loads(resp.body)


def test_refresh_usage_timeout_returns_504_and_resets_status(monkeypatch):
    """A wedged fetch_usage must yield a deterministic 504 with the
    standard envelope AND write validation_status='unknown' + last_error
    in the same call (mirrors the bulk route's timeout handling)."""
    from jacked.api.routes import auth as routes_auth

    db = MagicMock()
    db.get_account.return_value = {"id": 1, "access_token": "at_db"}
    request = MagicMock()
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)
    monkeypatch.setattr(routes_auth, "_SINGLE_USAGE_TIMEOUT", 0.2)

    async def _run():
        with (
            patch(
                "jacked.api.credential_helpers.read_fresh_active_token",
                return_value=None,
            ),
            patch.object(routes_auth, "fetch_usage", side_effect=_hang),
        ):
            return await routes_auth.refresh_usage(1, request)

    resp = asyncio.run(_run())
    assert resp.status_code == 504
    body = _body(resp)
    assert body["error"]["code"] == "REFRESH_TIMEOUT"
    assert (
        body["error"]["message"]
        == "Usage fetch timed out — server may be recovering a wedged refresh"
    )

    call = db.update_account.call_args
    assert call.args == (1,)
    assert call.kwargs["validation_status"] == "unknown"
    assert "timed out" in call.kwargs["last_error"].lower()
    assert call.kwargs["last_error_at"]


def test_refresh_token_timeout_returns_504(monkeypatch):
    """A wedged refresh_account_token must yield a 504, not a hung request."""
    from jacked.api.routes import auth as routes_auth

    db = MagicMock()
    db.get_account.return_value = {"id": 1, "refresh_token": "rt_db"}
    request = MagicMock()
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)
    monkeypatch.setattr(routes_auth, "_TOKEN_REFRESH_TIMEOUT", 0.2)

    async def _run():
        with patch.object(routes_auth, "refresh_account_token", side_effect=_hang):
            return await routes_auth.refresh_token(1, request)

    resp = asyncio.run(_run())
    assert resp.status_code == 504
    body = _body(resp)
    assert body["error"]["code"] == "REFRESH_TIMEOUT"
    assert "timed out" in body["error"]["message"].lower()


def test_validate_timeout_returns_504(monkeypatch):
    """A wedged validate_account must yield a 504, not a hung request."""
    from jacked.api.routes import auth as routes_auth

    db = MagicMock()
    db.get_account.return_value = {"id": 1}
    request = MagicMock()
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)
    monkeypatch.setattr(routes_auth, "_VALIDATE_TIMEOUT", 0.2)

    async def _run():
        with patch.object(routes_auth, "validate_account", side_effect=_hang):
            return await routes_auth.validate_token(1, request)

    resp = asyncio.run(_run())
    assert resp.status_code == 504
    body = _body(resp)
    assert body["error"]["code"] == "VALIDATE_TIMEOUT"
    assert "timed out" in body["error"]["message"].lower()


def test_bulk_refresh_429_uses_standard_envelope(monkeypatch):
    """The in-progress rejection must use the standard error envelope,
    not the legacy {'detail': ...} shape."""
    from jacked.api.routes import auth as routes_auth

    db = MagicMock()
    request = MagicMock()
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    async def _run():
        await routes_auth._bulk_refresh_lock.acquire()
        routes_auth._bulk_refresh_acquired_at = time.time()
        try:
            return await routes_auth.refresh_all_usage(request)
        finally:
            routes_auth._bulk_refresh_lock.release()

    resp = asyncio.run(_run())
    assert resp.status_code == 429
    body = _body(resp)
    assert body["error"]["code"] == "REFRESH_IN_PROGRESS"
    assert body["error"]["message"] == "Usage refresh already in progress"


def test_bulk_refresh_finally_resets_acquired_at(monkeypatch):
    """When the owning task clears the slot, the staleness clock must be
    reset too — otherwise a finished run keeps aging toward force-reset."""
    from jacked.api.routes import auth as routes_auth

    db = MagicMock()
    db.list_accounts.return_value = [{"id": 1, "email": "a@x"}]
    db.get_account.return_value = {"id": 1, "last_error": None}
    request = MagicMock()
    request.app.state.ws_registry = None
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    async def fake_fetch(*args, **kwargs):
        return {"five_hour": {"utilization": 0.0},
                "seven_day": {"utilization": 0.0}}

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            return await routes_auth.refresh_all_usage(request)

    resp = asyncio.run(_run())
    assert resp.refreshed == 1
    assert routes_auth._bulk_refresh_task is None
    assert routes_auth._bulk_refresh_acquired_at == 0.0


# ---------------------------------------------------------------------------
# Bulk pass-throttle: a second pass within 30s of the previous pass's
# completion must be served from the DB without touching the Anthropic API
# (2026-06 incident: a runaway dashboard tab drove ~25 fetches/min for hours).
# ---------------------------------------------------------------------------


def _throttle_db():
    db = MagicMock()
    db.list_accounts.return_value = [
        {"id": 1, "email": "a@x", "cached_usage_5h": 12.5, "cached_usage_7d": 40.0},
    ]
    # Full row: the real (non-throttled) pass feeds this through
    # _account_to_response for the WS progress payload.
    db.get_account.return_value = {
        "id": 1,
        "email": "a@x",
        "expires_at": int(time.time()) + 3600,
        "last_error": None,
    }
    return db


def test_bulk_refresh_throttled_within_30s_fetches_once(monkeypatch):
    """Two bulk passes back-to-back: only the first may call fetch_usage.
    The second returns success=True per account with current DB values."""
    from jacked.api.routes import auth as routes_auth

    db = _throttle_db()
    request = MagicMock()
    request.app.state.ws_registry = None
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    calls = {"n": 0}

    async def fake_fetch(*args, **kwargs):
        calls["n"] += 1
        return {"five_hour": {"utilization": 12.5},
                "seven_day": {"utilization": 40.0}}

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            first = await routes_auth.refresh_all_usage(request)
            second = await routes_auth.refresh_all_usage(request)
        return first, second

    first, second = asyncio.run(_run())
    assert calls["n"] == 1, "second bulk pass within 30s must not hit the API"
    assert first.refreshed == 1
    assert first.throttled == 0
    assert second.refreshed == 1
    assert second.failed == 0
    assert second.throttled == 1
    assert second.results[0]["account_id"] == 1
    assert second.results[0]["success"] is True
    assert second.results[0]["cached_usage_5h"] == 12.5
    assert second.results[0]["cached_usage_7d"] == 40.0


def test_bulk_refresh_not_throttled_after_interval(monkeypatch):
    """A pass that completed longer than the throttle window ago does not
    block a fresh API pass."""
    from jacked.api.routes import auth as routes_auth

    db = _throttle_db()
    request = MagicMock()
    request.app.state.ws_registry = None
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)
    routes_auth._last_bulk_completed_at = (
        time.time() - routes_auth._BULK_PASS_MIN_INTERVAL - 1.0
    )

    calls = {"n": 0}

    async def fake_fetch(*args, **kwargs):
        calls["n"] += 1
        return {"five_hour": {"utilization": 12.5},
                "seven_day": {"utilization": 40.0}}

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            return await routes_auth.refresh_all_usage(request)

    resp = asyncio.run(_run())
    assert calls["n"] == 1
    assert resp.refreshed == 1


def test_bulk_refresh_throttled_pass_sends_no_ws_events(monkeypatch):
    """The throttled pass must not broadcast ANY WS events — otherwise other
    tabs enter a 'refreshing' state that no progress/done event resolves."""
    from jacked.api.routes import auth as routes_auth

    db = _throttle_db()
    ws = MagicMock()
    ws.broadcast = AsyncMock()
    request = MagicMock()
    request.app.state.ws_registry = ws
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    async def fake_fetch(*args, **kwargs):
        return {"five_hour": {"utilization": 12.5},
                "seven_day": {"utilization": 40.0}}

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            await routes_auth.refresh_all_usage(request)
            after_first = ws.broadcast.call_count
            await routes_auth.refresh_all_usage(request)
        return after_first, ws.broadcast.call_count

    after_first, after_second = asyncio.run(_run())
    assert after_first > 0  # real pass broadcasts started + progress
    assert after_second == after_first  # throttled pass broadcasts nothing


def test_reset_locks_clears_bulk_completion_throttle():
    """reset_locks (lifespan/tray restart) must clear the throttle clock so
    a fresh process never starts in a throttled state."""
    from jacked.api.routes import auth as routes_auth

    routes_auth._last_bulk_completed_at = time.time()
    routes_auth.reset_locks()
    assert routes_auth._last_bulk_completed_at == 0.0


def _multi_account_db(n=5):
    db = MagicMock()
    db.list_accounts.return_value = [
        {"id": i, "email": f"a{i}@x", "cached_usage_5h": 10.0, "cached_usage_7d": 20.0}
        for i in range(1, n + 1)
    ]
    db.get_account.side_effect = lambda aid: {
        "id": aid,
        "email": f"a{aid}@x",
        "expires_at": int(time.time()) + 3600,
        "last_error": None,
    }
    return db


def test_bulk_refresh_fetches_accounts_concurrently(monkeypatch):
    """Per-account fetches must overlap: the usage-API limit is
    per-account/per-token, so a 5-account pass with a 0.3s fetch should
    finish in ~one fetch duration, not five serialized ones."""
    from jacked.api.routes import auth as routes_auth

    db = _multi_account_db(5)
    request = MagicMock()
    request.app.state.ws_registry = None
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    in_flight = {"now": 0, "peak": 0}

    async def slow_fetch(*args, **kwargs):
        in_flight["now"] += 1
        in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        await asyncio.sleep(0.3)
        in_flight["now"] -= 1
        return {"five_hour": {"utilization": 1.0},
                "seven_day": {"utilization": 2.0}}

    async def _run():
        start = time.monotonic()
        with patch.object(routes_auth, "fetch_usage", side_effect=slow_fetch):
            resp = await routes_auth.refresh_all_usage(request)
        return resp, time.monotonic() - start

    resp, elapsed = asyncio.run(_run())
    assert resp.refreshed == 5
    assert resp.failed == 0
    assert in_flight["peak"] >= 2, "fetches never overlapped — pass is serialized"
    assert elapsed < 1.2, f"5x0.3s pass took {elapsed:.2f}s — looks serialized"


def test_bulk_refresh_progress_counts_completions(monkeypatch):
    """WS done/failed events must carry a monotonically increasing
    completion count ending at total, so the 'Refreshing N/M' button label
    stays meaningful under concurrency."""
    from jacked.api.routes import auth as routes_auth

    db = _multi_account_db(4)
    request = MagicMock()
    events = []

    class FakeWS:
        async def broadcast(self, event, payload=None, **kw):
            events.append((event, payload))

    request.app.state.ws_registry = FakeWS()
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    async def fake_fetch(*args, **kwargs):
        await asyncio.sleep(0.01)
        return {"five_hour": {"utilization": 1.0},
                "seven_day": {"utilization": 2.0}}

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            return await routes_auth.refresh_all_usage(request)

    resp = asyncio.run(_run())
    assert resp.refreshed == 4
    done = [p for e, p in events
            if e == "usage_refresh_progress" and p["status"] == "done"]
    assert len(done) == 4
    counts = [p["progress"] for p in done]
    assert counts == sorted(counts), f"completion counts not monotonic: {counts}"
    assert counts[-1] == 4
    assert {p["account_id"] for p in done} == {1, 2, 3, 4}


def test_bulk_refresh_one_account_error_does_not_strand_siblings(monkeypatch):
    """A per-account internal error (e.g. sqlite OperationalError on the
    update) must become a failed result row — never propagate out of the
    gather, which would release the bulk lock with siblings still in
    flight."""
    from jacked.api.routes import auth as routes_auth

    db = _multi_account_db(3)
    request = MagicMock()
    request.app.state.ws_registry = None
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    async def fetch_or_boom(account_id, *args, **kwargs):
        await asyncio.sleep(0.01)
        if account_id == 2:
            raise RuntimeError("synthetic db explosion")
        return {"five_hour": {"utilization": 1.0},
                "seven_day": {"utilization": 2.0}}

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=fetch_or_boom):
            return await routes_auth.refresh_all_usage(request)

    resp = asyncio.run(_run())
    assert resp.refreshed == 2
    assert resp.failed == 1
    bad = next(r for r in resp.results if r["account_id"] == 2)
    assert bad["success"] is False
    assert "synthetic db explosion" in (bad["error"] or "")
    from jacked.api.routes.auth import _bulk_refresh_lock  # noqa: F401
    assert not routes_auth._bulk_refresh_lock.locked()
