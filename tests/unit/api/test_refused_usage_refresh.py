"""A client that the gate refuses can read usage, but it renews no token.

``POST /accounts/{id}/refresh-usage``, ``/refresh-all-usage`` and
``/accounts/{id}/validate`` are not refused by the app-wide gate
(``jacked.api.write_gate.GATE_SKIPS``): they read the usage or the profile.
On a 401 the usage fetch and the validation used to renew the token
(``_try_refresh_primary_token``) and import the live login
(``reconcile_credentials_from_live_store``). For a refused caller the route
now passes ``allow_refresh=False``: the 401 is reported, and no token is
renewed, imported or marked as refused.

No test reaches the network, the Keychain or a real credential store.
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from jacked.api.main import app
from jacked.web import auth
from jacked.web.database import Database

TAILNET_IP = "100.116.47.72"


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "jacked.db"))
    prev = getattr(app.state, "db", None)
    app.state.db = database
    yield database
    if prev is not None:
        app.state.db = prev
    else:
        try:
            del app.state.db
        except AttributeError:
            pass
    database.close()


@pytest.fixture
def renewals(monkeypatch):
    """Record every renewal and live import; the network answers 401."""
    calls = []

    async def _renew(account_id, db, stale_token=None):
        calls.append("renew")
        return "at-new"

    def _reconcile(account_id, db):
        calls.append("reconcile")
        return False

    resp = MagicMock(status_code=401, headers={})
    resp.json.return_value = {}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    monkeypatch.setattr(auth, "_try_refresh_primary_token", _renew)
    monkeypatch.setattr(auth.httpx, "AsyncClient", lambda *a, **k: client)
    monkeypatch.setattr(
        "jacked.api.credential_helpers.reconcile_credentials_from_live_store", _reconcile
    )
    monkeypatch.setattr(
        "jacked.api.credential_helpers.read_fresh_active_token", lambda account_id: None
    )
    auth._account_usage_state.clear()
    yield calls
    auth._account_usage_state.clear()


def _account(db):
    acct = db.create_account("u@example.com", "at-1", int(time.time()) + 3600, refresh_token="rt-1")
    db.update_account(acct["id"], validation_status="valid")
    return acct["id"]


def _refused(db):
    db.set_setting("remote_access_enabled", "false")
    return TestClient(app, client=(TAILNET_IP, 50000))


def test_refresh_usage_for_a_refused_caller_renews_nothing(db, renewals):
    account_id = _account(db)
    resp = _refused(db).post(f"/api/auth/accounts/{account_id}/refresh-usage")

    assert resp.status_code == 502, resp.text
    assert renewals == []
    row = db.get_account(account_id)
    assert (row["access_token"], row["refresh_token"]) == ("at-1", "rt-1")
    assert row["validation_status"] == "valid"


def test_refresh_usage_from_loopback_still_renews(db, renewals):
    account_id = _account(db)
    TestClient(app).post(f"/api/auth/accounts/{account_id}/refresh-usage")
    assert "renew" in renewals


def test_validate_for_a_refused_caller_renews_nothing(db, renewals):
    account_id = _account(db)
    resp = _refused(db).post(f"/api/auth/accounts/{account_id}/validate")

    assert resp.status_code == 200, resp.text
    assert resp.json()["valid"] is False
    assert renewals == []
    row = db.get_account(account_id)
    assert (row["access_token"], row["refresh_token"]) == ("at-1", "rt-1")
    assert row["validation_status"] != "invalid"


def test_validate_from_loopback_still_renews(db, renewals):
    account_id = _account(db)
    TestClient(app).post(f"/api/auth/accounts/{account_id}/validate")
    assert "renew" in renewals


def test_bulk_refresh_for_a_refused_caller_renews_nothing(db, renewals, monkeypatch):
    from jacked.api.routes import auth as routes_auth

    monkeypatch.setattr(routes_auth, "_last_bulk_completed_at", 0.0)
    monkeypatch.setattr(routes_auth, "_get_active_account_id_cached", lambda: None)
    _account(db)
    resp = _refused(db).post("/api/auth/accounts/refresh-all-usage")

    assert resp.status_code == 200, resp.text
    assert renewals == []


def test_fetch_usage_without_refresh_keeps_the_codex_root(db, monkeypatch):
    """The Codex app-server can renew the root's token, so it is not started."""
    import asyncio

    acct = db.create_account("c@example.com", "sentinel", int(time.time()) + 3600, provider="codex")
    started = []

    async def _fetch(*a, **k):
        started.append("codex")
        return {}

    monkeypatch.setattr("jacked.codex.usage.fetch_codex_usage", _fetch)
    monkeypatch.setattr("jacked.codex.usage.live_codex_account_id", lambda db: acct["id"])
    auth._account_usage_state.clear()
    result = asyncio.run(auth.fetch_usage(acct["id"], db, manual=True, allow_refresh=False))
    assert result == {"_cached": True}
    assert started == []
