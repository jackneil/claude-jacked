"""Tests for route-level timeout guards on single-account auth endpoints
and the bulk-refresh 429 envelope / finally-block state reset.

Uses asyncio.run() wrappers (project convention — no pytest-asyncio)."""
import asyncio
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

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
