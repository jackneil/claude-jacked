"""Tests for the remote-access settings router (jacked/api/routes/settings_remote.py).

Driven through the real FastAPI app with a plain ``TestClient(app)`` that never
enters the context manager, so the lifespan (background sweeps, real home dir)
never runs. The only app.state a request path needs — ``db`` (GET/PUT) and
``ws_registry`` (POST restart) — is set per test and cleaned up.

The restart path is exercised WITHOUT actually replacing the process: the
handler is monkeypatched to a threading.Event setter and os.execv is patched to
prove neither the real tray nor a real execv is invoked.
"""

import threading

import pytest
from fastapi.testclient import TestClient

import jacked.api.routes.settings_remote as sr
from jacked.api.main import app
from jacked.service.bind import BindPlan, set_active_plan
from jacked.service.restart import set_restart_handler
from jacked.web.database import Database


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch):
    """Give every test a fresh restart lock and clear the global handler +
    active plan, so cross-test state never bleeds. Also strip JACKED_HOST so
    the 'unknown' effective address is deterministic."""
    monkeypatch.delenv("JACKED_HOST", raising=False)
    sr._restart_lock = threading.Lock()
    set_restart_handler(None)
    set_active_plan(None)
    yield
    sr._restart_lock = threading.Lock()
    set_restart_handler(None)
    set_active_plan(None)


@pytest.fixture
def db(tmp_path):
    """A throwaway file-backed settings DB wired onto app.state for the test.

    File-backed, not ``:memory:``: TestClient serves request handlers on a
    worker thread, and Database keeps a thread-local connection — a ``:memory:``
    DB is per-connection, so the handler thread would see a fresh, schema-less
    DB. A real file is shared across threads."""
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


@pytest.fixture
def client(db):
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------


def test_get_defaults_on_fresh_db(client):
    """Fresh DB: remote access off, default scope, effective mode 'unknown'
    (no BindPlan registered under TestClient)."""
    resp = client.get("/api/settings/remote-access")
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["scope"] == "tailscale"
    assert body["effective"]["mode"] == "unknown"
    assert body["effective"]["addresses"] == ["127.0.0.1"]
    assert body["effective"]["tailscale_ip"] is None
    assert body["effective"]["fallback_reason"] is None


def test_get_reflects_active_plan_exactly(client):
    """When a plan is registered, effective mirrors as_effective() verbatim."""
    plan = BindPlan(
        mode="tailscale",
        addresses=("127.0.0.1", "100.64.12.7"),
        port=8321,
        primary_host="100.64.12.7",
        tailscale_ip="100.64.12.7",
    )
    set_active_plan(plan)
    resp = client.get("/api/settings/remote-access")
    assert resp.status_code == 200
    assert resp.json()["effective"] == plan.as_effective()


# ---------------------------------------------------------------------------
# PUT
# ---------------------------------------------------------------------------


def test_put_persists_bare_strings_and_round_trips(client, db):
    resp = client.put(
        "/api/settings/remote-access",
        json={"enabled": True, "scope": "all"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is True
    assert body["scope"] == "all"

    # Persisted as the bare-string convention, not JSON-encoded.
    assert db.get_setting("remote_access_enabled") == "true"
    assert db.get_setting("remote_access_scope") == "all"

    # Round-trips through GET.
    got = client.get("/api/settings/remote-access").json()
    assert got["enabled"] is True
    assert got["scope"] == "all"


def test_put_false_persists_false(client, db):
    client.put("/api/settings/remote-access", json={"enabled": False, "scope": "tailscale"})
    assert db.get_setting("remote_access_enabled") == "false"
    assert db.get_setting("remote_access_scope") == "tailscale"


def test_put_invalid_scope_is_422(client):
    resp = client.put(
        "/api/settings/remote-access",
        json={"enabled": True, "scope": "everywhere"},
    )
    assert resp.status_code == 422


def test_put_missing_field_is_422(client):
    resp = client.put("/api/settings/remote-access", json={"enabled": True})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Protected-key guard (proves system.py's generic PUT/DELETE can't clobber)
# ---------------------------------------------------------------------------


def test_generic_put_on_protected_key_is_422(client):
    resp = client.put(
        "/api/settings/remote_access_enabled", json={"value": "true"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_KEY"


def test_generic_put_on_protected_scope_is_422(client):
    resp = client.put(
        "/api/settings/remote_access_scope", json={"value": "all"}
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_KEY"


def test_generic_delete_on_protected_key_is_422(client):
    resp = client.delete("/api/settings/remote_access_enabled")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_KEY"


# ---------------------------------------------------------------------------
# Route registration order — OUR router wins over system's catch-all PUT
# ---------------------------------------------------------------------------


def test_put_remote_access_reaches_our_router_not_catch_all(client):
    """The dedicated router is registered before system.router, so
    PUT /api/settings/remote-access hits us — proven by the effective-state
    shape, which the generic {"key", "value", "updated"} response can't produce."""
    resp = client.put(
        "/api/settings/remote-access",
        json={"enabled": False, "scope": "tailscale"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "effective" in body and "mode" in body["effective"]
    assert "updated" not in body  # the catch-all's tell-tale field


# ---------------------------------------------------------------------------
# POST restart
# ---------------------------------------------------------------------------


class _FakeWSRegistry:
    def __init__(self):
        self.calls = []

    async def broadcast(self, topic, payload=None, source=None):
        self.calls.append((topic, payload))


def test_restart_broadcasts_and_calls_handler_not_execv(client, monkeypatch):
    """POST returns started, broadcasts restart_started, and invokes the
    registered handler (tray in-process path) rather than os.execv."""
    ws = _FakeWSRegistry()
    prev_ws = getattr(app.state, "ws_registry", None)
    app.state.ws_registry = ws

    fired = threading.Event()
    set_restart_handler(lambda: fired.set())

    import jacked.service.restart as restart_mod
    execv_called = threading.Event()
    monkeypatch.setattr(
        restart_mod.os, "execv", lambda *a, **k: execv_called.set()
    )

    try:
        resp = client.post("/api/settings/remote-access/restart")
        assert resp.status_code == 200
        assert resp.json() == {"status": "started"}

        # Broadcast happened synchronously on the request.
        assert ws.calls, "expected a restart_started broadcast"
        assert ws.calls[0][0] == "restart_started"
        assert "message" in ws.calls[0][1]

        # Handler fires after the ~1.5s flush sleep; execv must NOT be called.
        assert fired.wait(timeout=4.0), "restart handler was not invoked"
        assert not execv_called.is_set(), "execv called despite a registered handler"
    finally:
        if prev_ws is not None:
            app.state.ws_registry = prev_ws
        else:
            try:
                del app.state.ws_registry
            except AttributeError:
                pass


def test_restart_returns_409_when_already_in_progress(client):
    """A held restart lock yields 409 without starting a second restart."""
    assert sr._restart_lock.acquire(blocking=False)
    try:
        resp = client.post("/api/settings/remote-access/restart")
        assert resp.status_code == 409
        assert "in progress" in resp.json()["detail"].lower()
    finally:
        sr._restart_lock.release()
