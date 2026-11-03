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
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import jacked.api.routes.settings_remote as sr
from jacked.api.main import app
from jacked.api.routes.settings_swap import SWAP_SETTING_KEYS
from jacked.service.bind import BindPlan, set_active_plan
from jacked.service.restart import set_restart_handler
from jacked.web.database import SELECTED_ACCOUNT_KEYS, Database


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
    # A test may leave its restart thread sleeping (the ~1.5 s flush). Wait for
    # it while this test's handler and patches are still in place: if it woke
    # after teardown it would find no handler and call the real os.execv.
    for thread in threading.enumerate():
        if thread.name == "jacked-remote-access-restart":
            thread.join(timeout=10)
            assert not thread.is_alive(), "remote-access restart thread did not finish"
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


# Every key that a dedicated endpoint owns: the remote-access pair (a security
# decision with its own restart flow), every auto-swap / window keeper
# setting (PUT /api/settings/swap-settings validates them as one set), and
# the credential-selection pointers. The authority guard, the identity watcher
# and the OAuth flow act on the pointers, so only the switch path (its
# mutation gate, switch audit and rate limit) may move them. The 5-hour rules
# toggle and each pointer are named on their own too, so the test still fails
# if one drops out of its source tuple.
_DEDICATED_KEYS = list(dict.fromkeys([
    "remote_access_enabled",
    "remote_access_scope",
    "auto_swap_5h_rules_enabled",
    *SWAP_SETTING_KEYS,
    "desired_account_id",
    "active_account_id",
    "active_account_id_codex",
    *SELECTED_ACCOUNT_KEYS,
    Database.active_account_setting_key("codex"),
]))


@pytest.mark.parametrize("key", _DEDICATED_KEYS)
def test_generic_put_on_protected_key_is_422(client, db, key):
    resp = client.put(f"/api/settings/{key}", json={"value": "true"})
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_KEY"
    assert db.get_setting(key) is None


@pytest.mark.parametrize("key", _DEDICATED_KEYS)
def test_generic_delete_on_protected_key_is_422(client, db, key):
    db.set_setting(key, "kept")
    resp = client.delete(f"/api/settings/{key}")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_KEY"
    assert db.get_setting(key) == "kept"


def test_generic_delete_removes_a_stale_retired_7d_row(client, db):
    """No endpoint owns the retired ``auto_swap_7d_threshold`` now, so the
    generic DELETE must not answer "Use the dedicated endpoint". It deletes
    a row that an old jacked wrote after the database opened."""
    key = "auto_swap_7d_threshold"
    assert key not in _DEDICATED_KEYS
    db.set_setting(key, "85")
    resp = client.delete(f"/api/settings/{key}")
    assert resp.status_code == 200
    assert resp.json() == {"key": key, "deleted": True}
    assert db.get_setting(key) is None


@pytest.mark.parametrize("method", ["put", "delete"])
def test_the_pointer_of_a_new_provider_is_protected_too(client, db, method):
    """Each provider has its own active-account pointer
    (``Database.active_account_setting_key``). The guard follows the shape of
    that key, so the pointer of a provider added later is protected the day
    the provider is added."""
    key = Database.active_account_setting_key("nextprovider")
    db.set_setting(key, "7")
    if method == "put":
        resp = client.put(f"/api/settings/{key}", json={"value": "9"})
    else:
        resp = client.delete(f"/api/settings/{key}")
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "PROTECTED_KEY"
    assert db.get_setting(key) == "7"


def test_a_key_that_only_looks_like_a_pointer_is_not_protected(client, db):
    """The guard matches the pointer keys, not every key that starts with
    the same letters."""
    resp = client.put("/api/settings/active_account_ids_note", json={"value": "x"})
    assert resp.status_code == 200
    assert db.get_setting("active_account_ids_note") == '"x"'


@pytest.mark.parametrize("key", Database.RETIRED_SETTING_KEYS)
def test_generic_put_on_a_retired_key_is_422(client, db, key):
    """A retired key is deleted when the database opens. The generic PUT must
    not write it again in between: no code reads it, so a write there only
    looks like a setting that works."""
    resp = client.put(f"/api/settings/{key}", json={"value": 70})
    assert resp.status_code == 422
    body = resp.json()
    assert body["error"]["code"] == "RETIRED_KEY"
    assert body["error"]["message"] == f"jacked no longer uses the setting '{key}'."
    assert "—" not in body["error"]["message"]
    assert db.get_setting(key) is None


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
        payload = ws.calls[0][1]
        assert "message" in payload
        # Enriched with the just-saved settings so clients can detect a lockout.
        # No PUT preceded this restart, so the DB defaults apply.
        assert payload["enabled"] is False
        assert payload["scope"] == "tailscale"

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


def test_restart_payload_mirrors_saved_settings(client, monkeypatch):
    """After a PUT persists enabled+all, the restart_started payload carries
    those exact values so a remote client can decide to poll or go terminal."""
    ws = _FakeWSRegistry()
    prev_ws = getattr(app.state, "ws_registry", None)
    app.state.ws_registry = ws

    # In-process handler so the daemon thread returns without touching execv.
    # Signal an Event so we can wait for the handler to run BEFORE teardown
    # resets it (otherwise the ~1.5s-delayed thread would race the reset and
    # fall through to a real os.execv). Patch execv too as a belt-and-braces.
    fired = threading.Event()
    set_restart_handler(lambda: fired.set())

    import jacked.service.restart as restart_mod
    monkeypatch.setattr(restart_mod.os, "execv", lambda *a, **k: None)

    try:
        client.put(
            "/api/settings/remote-access",
            json={"enabled": True, "scope": "all"},
        )
        resp = client.post("/api/settings/remote-access/restart")
        assert resp.status_code == 200

        assert ws.calls, "expected a restart_started broadcast"
        topic, payload = ws.calls[0]
        assert topic == "restart_started"
        assert payload["enabled"] is True
        assert payload["scope"] == "all"

        # Let the delayed restart handler run before teardown resets it.
        assert fired.wait(timeout=4.0), "restart handler was not invoked"
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


def test_restart_lock_released_if_body_raises_before_thread_starts(db, monkeypatch):
    """If anything between lock.acquire() and a successful Thread.start() throws
    (a sqlite 'database is locked' on the payload read, thread exhaustion), the
    lock MUST be released — otherwise every future restart returns 409 forever
    and the GUI toggle silently wedges until the process restarts."""
    # Force the post-acquire body to raise: make the payload DB read blow up.
    monkeypatch.setattr(
        sr, "_read_enabled_scope",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")),
    )
    # raise_server_exceptions=False so the raised error becomes a 500 response
    # instead of propagating into the test (the default re-raises).
    strict_client = TestClient(app, raise_server_exceptions=False)
    assert not sr._restart_lock.locked()
    resp = strict_client.post("/api/settings/remote-access/restart")
    assert resp.status_code == 500
    assert not sr._restart_lock.locked(), "restart lock leaked on a failed body"
    # And a subsequent restart is not permanently 409-wedged.
    monkeypatch.undo()
    set_restart_handler(lambda: None)
    import jacked.service.restart as restart_mod
    monkeypatch.setattr(restart_mod.os, "execv", lambda *a, **k: None)
    resp2 = strict_client.post("/api/settings/remote-access/restart")
    assert resp2.status_code == 200


def test_restart_lock_is_reacquirable_after_handler_path_completes(client):
    """The in-process (handler) restart path must RELEASE the lock so a later
    apply can run. Regression pin for the daemon-thread finally-release."""
    fired = threading.Event()
    set_restart_handler(lambda: fired.set())

    resp = client.post("/api/settings/remote-access/restart")
    assert resp.status_code == 200
    assert fired.wait(timeout=4.0), "handler was not invoked"
    # The daemon thread's finally must have released the lock; prove it is free.
    acquired = sr._restart_lock.acquire(blocking=True, timeout=2.0)
    assert acquired, "restart lock was not released after the handler path"
    sr._restart_lock.release()


# ---------------------------------------------------------------------------
# Middleware coverage on the two new security-critical routes.
#
# The HostValidationMiddleware (jacked/api/security.py) is app-level, so it
# guards these routes, but a future refactor (sub-app mount, exemption list)
# could silently uncover them. Pin the guarantees on the routes themselves, and
# specifically that the widest bind (JACKED_HOST=0.0.0.0) never loosens them.
# ---------------------------------------------------------------------------


def test_put_remote_access_foreign_origin_is_csrf_403(client):
    """A cross-site PUT (foreign Origin) to flip the bind must be blocked."""
    resp = client.put(
        "/api/settings/remote-access",
        json={"enabled": True, "scope": "all"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_post_restart_foreign_origin_is_csrf_403(client):
    """A cross-site POST to trigger a restart must be blocked."""
    resp = client.post(
        "/api/settings/remote-access/restart",
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_post_restart_origin_null_is_csrf_403(client):
    """`Origin: null` (redirect-laundered / no-referrer cross-site POST) blocked."""
    resp = client.post(
        "/api/settings/remote-access/restart",
        headers={"Origin": "null"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


def test_put_remote_access_rebinding_host_is_421(client):
    """A registrable multi-label Host (DNS-rebinding shape) must be refused."""
    resp = client.put(
        "/api/settings/remote-access",
        json={"enabled": True, "scope": "all"},
        headers={"Host": "attacker.example:8321"},
    )
    assert resp.status_code == 421
    assert resp.json()["error"]["code"] == "UNTRUSTED_HOST"


def test_csrf_guard_holds_even_when_bound_all_interfaces(client, monkeypatch):
    """The widest bind must never loosen the CSRF guard: with JACKED_HOST set to
    0.0.0.0 (all-interfaces), a foreign-Origin PUT is still 403. Pins that the
    guard is bind-independent (build_allowed_origins ignores its host arg)."""
    monkeypatch.setenv("JACKED_HOST", "0.0.0.0")
    resp = client.put(
        "/api/settings/remote-access",
        json={"enabled": True, "scope": "all"},
        headers={"Origin": "http://evil.example"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "CSRF_ORIGIN"


# ---------------------------------------------------------------------------
# The setting is a live authorization bit: only the host may flip it
#
# remote_access_enabled/scope now decide, per request, who may switch
# credentials. A remote peer that could PUT them would grant itself credential
# control just by reaching the port, and "turn it off" would not revoke
# anything until the socket is rebound by a restart.
# ---------------------------------------------------------------------------

TAILNET_IP = "100.116.47.72"
LAN_IP = "192.168.42.5"

_SETTINGS_LOCAL_ONLY_MESSAGE = (
    "Remote access settings can only be changed from the machine running "
    "jacked. Open the dashboard on the host to change them."
)


@pytest.mark.parametrize("host", [TAILNET_IP, LAN_IP])
def test_put_remote_access_is_rejected_from_a_remote_client(db, host):
    remote = TestClient(app, client=(host, 1))

    resp = remote.put(
        "/api/settings/remote-access",
        json={"enabled": True, "scope": "all"},
    )

    assert resp.status_code == 403
    body = resp.json()
    assert body["error"]["code"] == "REMOTE_ACCESS_SETTINGS_LOCAL_ONLY"
    assert body["error"]["message"] == _SETTINGS_LOCAL_ONLY_MESSAGE
    assert "—" not in body["error"]["message"]
    # And nothing was written.
    assert db.get_setting("remote_access_enabled") is None
    assert db.get_setting("remote_access_scope") is None


def test_put_remote_access_cannot_be_flipped_off_remotely_either(db):
    """Not just escalation: a remote peer must not be able to disable the
    setting out from under the host either."""
    db.set_setting("remote_access_enabled", "true")
    db.set_setting("remote_access_scope", "tailscale")
    remote = TestClient(app, client=(TAILNET_IP, 1))

    resp = remote.put(
        "/api/settings/remote-access",
        json={"enabled": False, "scope": "tailscale"},
    )

    assert resp.status_code == 403
    assert db.get_setting("remote_access_enabled") == "true"


def test_restart_is_rejected_from_a_remote_client(db, monkeypatch):
    called = threading.Event()
    set_restart_handler(lambda: called.set())
    import jacked.service.restart as restart_mod

    execv_called = threading.Event()
    monkeypatch.setattr(
        restart_mod.os, "execv", lambda *a, **k: execv_called.set()
    )
    remote = TestClient(app, client=(TAILNET_IP, 1))

    resp = remote.post("/api/settings/remote-access/restart")

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "REMOTE_ACCESS_SETTINGS_LOCAL_ONLY"
    assert not called.wait(timeout=2.5), "a remote client restarted the service"
    assert not execv_called.is_set()
    # The 403 returns before the lock is taken, so a later local apply works.
    assert not sr._restart_lock.locked()


def test_local_put_and_restart_are_unchanged(client, db, monkeypatch):
    """The loopback path keeps working exactly as before the gate."""
    fired = threading.Event()
    set_restart_handler(lambda: fired.set())
    import jacked.service.restart as restart_mod

    execv_called = threading.Event()
    monkeypatch.setattr(
        restart_mod.os, "execv", lambda *a, **k: execv_called.set()
    )

    put = client.put(
        "/api/settings/remote-access",
        json={"enabled": True, "scope": "all"},
    )
    assert put.status_code == 200
    assert put.json()["editable"] is True
    assert db.get_setting("remote_access_enabled") == "true"
    assert db.get_setting("remote_access_scope") == "all"

    restart = client.post("/api/settings/remote-access/restart")
    assert restart.status_code == 200
    assert restart.json() == {"status": "started"}
    # Wait out the handler thread INSIDE the patched window: the restart runs
    # after a ~1.5s flush sleep, and letting the test end first would leave a
    # real execv to re-exec the pytest process.
    assert fired.wait(timeout=4.0), "restart handler was not invoked"
    assert not execv_called.is_set()


def test_get_reports_editable_true_for_loopback(client):
    body = client.get("/api/settings/remote-access").json()
    assert body["editable"] is True


@pytest.mark.parametrize("host", [TAILNET_IP, LAN_IP])
def test_get_reports_editable_false_for_a_remote_client(db, host):
    remote = TestClient(app, client=(host, 1))
    body = remote.get("/api/settings/remote-access").json()
    assert body["editable"] is False
    # The rest of the payload is still readable: a remote viewer may SEE the
    # policy that applies to it, it just may not change it.
    assert body["enabled"] is False
    assert body["scope"] == "tailscale"


# ---------------------------------------------------------------------------
# The mutation gate on the generic settings endpoint and on the swap routes
#
# The generic PUT/DELETE /api/settings/{key} writes every key that no
# dedicated endpoint owns (the color theme, the OAuth browser mode, the raw
# table on the Settings page). PUT /api/settings/swap-settings,
# POST /swap-pause and POST /swap-resume turn auto-swap on and off and set or
# clear the hold after a manual switch. All five run the gate of an account
# switch: loopback always may; a remote client may only while remote access
# is on and its address is inside the scope.
# ---------------------------------------------------------------------------

_OUTSIDE_SCOPE_MESSAGE = (
    "This browser's address is outside the remote access scope. Widen the "
    "scope under Settings > Advanced on the host machine."
)
_SETTINGS_DENIED = {
    "remote_access_off": (
        "Changing settings from this browser is off. Turn on remote access "
        "under Settings > Advanced on the host machine."
    ),
    "outside_scope": _OUTSIDE_SCOPE_MESSAGE,
}
_SWAP_DENIED = {
    "remote_access_off": (
        "Changing auto-swap from this browser is off. Turn on remote access "
        "under Settings > Advanced on the host machine."
    ),
    "outside_scope": _OUTSIDE_SCOPE_MESSAGE,
}
_STORED_PAUSE = "2999-01-01T00:00:00+00:00"
_SWAP_ROUTES = ("swap-settings", "swap-pause", "swap-resume")


@pytest.fixture
def sweep_wakes(monkeypatch):
    """The swap-settings PUT wakes the sweep loop: record the wake instead of
    setting the module's asyncio.Event."""
    import jacked.api.usage_monitor as usage_monitor

    calls = []
    monkeypatch.setattr(usage_monitor, "_sweep_wake", SimpleNamespace(set=lambda: calls.append(1)))
    return calls


def _turn_on_remote_access(db, scope="tailscale"):
    db.set_setting("remote_access_enabled", "true")
    db.set_setting("remote_access_scope", scope)


def _generic_write(http, method):
    if method == "put":
        return http.put("/api/settings/color_theme", json={"value": "light"})
    return http.delete("/api/settings/color_theme")


def _generic_written(db, method):
    return db.get_setting("color_theme") == ('"light"' if method == "put" else None)


def _swap_write(http, route):
    if route == "swap-settings":
        return http.put("/api/settings/swap-settings", json={"auto_swap_enabled": True})
    if route == "swap-pause":
        return http.post("/api/settings/swap-pause?minutes=30")
    return http.post("/api/settings/swap-resume")


def _swap_written(db, route):
    if route == "swap-settings":
        return db.get_setting("auto_swap_enabled") == "true"
    if route == "swap-pause":
        return db.get_setting("auto_swap_paused_until") not in (None, "", _STORED_PAUSE)
    return db.get_setting("auto_swap_paused_until") == ""


def _assert_denied(resp, code, message):
    assert resp.status_code == 403
    assert resp.json() == {"error": {"message": message, "code": code}}
    assert resp.headers["cache-control"] == "no-store"
    assert "—" not in message


@pytest.mark.parametrize("method", ["put", "delete"])
@pytest.mark.parametrize("host", [TAILNET_IP, LAN_IP])
def test_generic_settings_write_is_refused_while_remote_access_is_off(db, method, host):
    db.set_setting("color_theme", '"dark"')
    resp = _generic_write(TestClient(app, client=(host, 1)), method)
    _assert_denied(resp, "REMOTE_ACCESS_OFF", _SETTINGS_DENIED["remote_access_off"])
    assert db.get_setting("color_theme") == '"dark"'


@pytest.mark.parametrize("method", ["put", "delete"])
def test_generic_settings_write_is_refused_outside_the_scope(db, method):
    _turn_on_remote_access(db)
    db.set_setting("color_theme", '"dark"')
    resp = _generic_write(TestClient(app, client=(LAN_IP, 1)), method)
    _assert_denied(resp, "OUTSIDE_SCOPE", _SETTINGS_DENIED["outside_scope"])
    assert db.get_setting("color_theme") == '"dark"'


@pytest.mark.parametrize("method", ["put", "delete"])
def test_generic_settings_write_works_inside_the_scope(db, method):
    _turn_on_remote_access(db)
    db.set_setting("color_theme", '"dark"')
    resp = _generic_write(TestClient(app, client=(TAILNET_IP, 1)), method)
    assert resp.status_code == 200
    assert _generic_written(db, method)


@pytest.mark.parametrize("method", ["put", "delete"])
def test_generic_settings_write_from_loopback_is_unchanged(client, db, method):
    db.set_setting("color_theme", '"dark"')
    resp = _generic_write(client, method)
    assert resp.status_code == 200
    assert _generic_written(db, method)


@pytest.mark.parametrize("method", ["put", "delete"])
def test_the_gate_runs_before_the_protected_key_check(db, method):
    """A refused client gets the 403 for a protected key too, so it learns
    nothing about the key, and the pointer stays as it was."""
    db.set_setting("desired_account_id", "5")
    remote = TestClient(app, client=(TAILNET_IP, 1))
    if method == "put":
        resp = remote.put("/api/settings/desired_account_id", json={"value": "9"})
    else:
        resp = remote.delete("/api/settings/desired_account_id")
    _assert_denied(resp, "REMOTE_ACCESS_OFF", _SETTINGS_DENIED["remote_access_off"])
    assert db.get_setting("desired_account_id") == "5"


@pytest.mark.parametrize("route", _SWAP_ROUTES)
@pytest.mark.parametrize("host", [TAILNET_IP, LAN_IP])
def test_swap_writes_are_refused_while_remote_access_is_off(db, sweep_wakes, route, host):
    db.set_setting("auto_swap_paused_until", _STORED_PAUSE)
    resp = _swap_write(TestClient(app, client=(host, 1)), route)
    _assert_denied(resp, "REMOTE_ACCESS_OFF", _SWAP_DENIED["remote_access_off"])
    # Nothing changed: auto-swap stays off, the hold stays, the loop sleeps on.
    assert db.get_setting("auto_swap_enabled") is None
    assert db.get_setting("auto_swap_paused_until") == _STORED_PAUSE
    assert sweep_wakes == []


@pytest.mark.parametrize("route", _SWAP_ROUTES)
def test_swap_writes_are_refused_outside_the_scope(db, sweep_wakes, route):
    _turn_on_remote_access(db)
    db.set_setting("auto_swap_paused_until", _STORED_PAUSE)
    resp = _swap_write(TestClient(app, client=(LAN_IP, 1)), route)
    _assert_denied(resp, "OUTSIDE_SCOPE", _SWAP_DENIED["outside_scope"])
    assert db.get_setting("auto_swap_enabled") is None
    assert db.get_setting("auto_swap_paused_until") == _STORED_PAUSE


@pytest.mark.parametrize("route", _SWAP_ROUTES)
@pytest.mark.parametrize("who", ["loopback", "tailnet_in_scope"])
def test_swap_writes_work_from_loopback_and_inside_the_scope(db, sweep_wakes, route, who):
    if who == "loopback":
        http = TestClient(app)
    else:
        _turn_on_remote_access(db)
        http = TestClient(app, client=(TAILNET_IP, 1))
    db.set_setting("auto_swap_paused_until", _STORED_PAUSE)
    resp = _swap_write(http, route)
    assert resp.status_code == 200
    assert _swap_written(db, route)
