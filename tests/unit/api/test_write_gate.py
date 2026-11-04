"""Every write route of the dashboard app runs the remote-access gate.

The gate is ``mutation_decision`` (``jacked.api.mutation_gate``): loopback
always may; a remote client may only when remote access is on and its
address is inside the enabled scope. Before this change only the account
switch, the upgrade, the swap settings and the settings table ran it. A
client that the gate refuses (remote access off while ``tailscale serve``
forwards from loopback, or a bind wider than the enabled scope) could still
write a hook into ``~/.claude/settings.json`` through
``PUT /api/claude-settings/raw``, which runs as a command at the next Claude
Code start, and could write project files, packs and analytics settings.

``jacked.api.write_gate`` attaches the gate to the whole app as one
dependency. The route table test below makes the class closed: a new write
route fails here until the gate covers it, or until it is listed in
``GATE_SKIPS`` with a reason and named in ``EXPECTED_SKIPS`` here.
"""

from __future__ import annotations

import inspect

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from jacked.api import write_gate
from jacked.api.main import app
from jacked.web.database import Database

TAILNET_IP = "100.116.47.72"
LAN_IP = "192.168.42.5"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}

# The write routes that the app-wide gate does not refuse, and the name that
# the route's own source must call to show that its own check is real.
EXPECTED_SKIPS = {
    # The route runs the gate itself, with copy for its own action.
    ("PUT", "/api/settings/swap-settings"): "mutation_decision",
    ("POST", "/api/settings/swap-pause"): "mutation_decision",
    ("POST", "/api/settings/swap-resume"): "mutation_decision",
    ("POST", "/api/upgrade"): "mutation_decision",
    ("PUT", "/api/settings/{key}"): "mutation_decision",
    ("DELETE", "/api/settings/{key}"): "mutation_decision",
    ("POST", "/api/auth/accounts/{account_id}/use"): "_credential_mutation_decision",
    # Loopback only, which is stricter than the gate.
    ("PUT", "/api/settings/remote-access"): "_is_loopback_client",
    ("POST", "/api/settings/remote-access/restart"): "_is_loopback_client",
    # The Codex import runs the gate; the Claude OAuth start only stores a
    # login, and the gate decides whether the flow may activate it.
    ("POST", "/api/auth/accounts/add"): "mutation_refusal",
    ("POST", "/api/auth/accounts/{account_id}/reauth"): "_credential_mutation_allowed",
    ("POST", "/api/auth/accounts/{account_id}/authorize-cc"): "_credential_mutation_allowed",
    ("POST", "/api/auth/flow/{flow_id}/code"): "",
    ("POST", "/api/auth/flow/{flow_id}/open"): "",
    # Reads usage or the profile; a refused caller renews no token.
    ("POST", "/api/auth/accounts/{account_id}/refresh-usage"): "allow_refresh",
    ("POST", "/api/auth/accounts/refresh-all-usage"): "allow_refresh",
    ("POST", "/api/auth/accounts/{account_id}/validate"): "allow_refresh",
}


def _write_routes():
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in sorted(set(route.methods) & UNSAFE):
                yield method, route


def _gated(route: APIRoute) -> bool:
    return any(
        dep.call is write_gate.require_mutation_access
        for dep in route.dependant.dependencies
    )


def test_every_write_route_is_gated_or_skipped_with_a_reason():
    found = {(method, route.path) for method, route in _write_routes()}
    ungated = sorted(
        (method, route.path) for method, route in _write_routes() if not _gated(route)
    )
    assert ungated == []
    assert set(write_gate.GATE_SKIPS) == set(EXPECTED_SKIPS)
    assert set(EXPECTED_SKIPS) <= found, "a skip names a route that does not exist"
    for key, reason in write_gate.GATE_SKIPS.items():
        assert reason.strip(), key
    endpoints = {(method, route.path): route.endpoint for method, route in _write_routes()}
    for key, evidence in EXPECTED_SKIPS.items():
        if evidence:
            assert evidence in inspect.getsource(endpoints[key]), key


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


def _remote(db, host, *, enabled, scope="tailscale"):
    db.set_setting("remote_access_enabled", "true" if enabled else "false")
    db.set_setting("remote_access_scope", scope)
    return TestClient(app, client=(host, 50000))


def test_every_gated_write_route_refuses_a_refused_client(db):
    """Sends each gated write as a refused client; none reaches its handler.

    The gate is a dependency, so it runs before the body and the path
    parameters are checked: an empty body still gets the 403.
    """
    test_every_write_route_is_gated_or_skipped_with_a_reason()
    remote = _remote(db, TAILNET_IP, enabled=False)
    for method, route in _write_routes():
        if (method, route.path) in EXPECTED_SKIPS:
            continue
        path = route.path_format
        for name in route.param_convertors:
            path = path.replace("{" + name + "}", "1")
        resp = remote.request(method, path)
        assert resp.status_code == 403, (method, route.path, resp.text)
        assert resp.json()["error"]["code"] == "REMOTE_ACCESS_OFF"


@pytest.fixture
def settings_writes(monkeypatch):
    from jacked.api.routes import features

    calls = []
    monkeypatch.setattr(features, "_write_settings_json", lambda data: calls.append(data))
    return calls


HOOK = {"content": {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "id"}]}]}},
        "confirm_overwrite": True}


@pytest.mark.parametrize(
    "host, enabled, code",
    [(TAILNET_IP, False, "REMOTE_ACCESS_OFF"), (LAN_IP, True, "OUTSIDE_SCOPE")],
)
def test_a_refused_client_cannot_write_settings_json(db, settings_writes, host, enabled, code):
    resp = _remote(db, host, enabled=enabled).put("/api/claude-settings/raw", json=HOOK)

    assert resp.status_code == 403, resp.text
    error = resp.json()["error"]
    assert error["code"] == code
    assert error["message"] == write_gate.MUTATION_DENIED_MESSAGES[code.lower()]
    assert "—" not in error["message"]
    assert resp.headers["cache-control"] == "no-store"
    assert settings_writes == []


def test_a_refused_client_cannot_set_a_settings_key(db, settings_writes):
    resp = _remote(db, TAILNET_IP, enabled=False).put(
        "/api/claude-settings/key/includeCoAuthoredBy", json={"value": False}
    )
    assert resp.status_code == 403
    assert settings_writes == []


def test_loopback_keeps_full_access(db, settings_writes):
    resp = TestClient(app).put("/api/claude-settings/raw", json=HOOK)
    assert resp.status_code == 200, resp.text
    assert settings_writes == [HOOK["content"]]


@pytest.mark.parametrize("host, scope", [(TAILNET_IP, "tailscale"), (LAN_IP, "all")])
def test_a_remote_client_inside_the_enabled_scope_keeps_access(db, settings_writes, host, scope):
    resp = _remote(db, host, enabled=True, scope=scope).put(
        "/api/claude-settings/raw", json=HOOK
    )
    assert resp.status_code == 200, resp.text
    assert settings_writes == [HOOK["content"]]


def test_a_refused_client_can_still_read(db):
    resp = _remote(db, TAILNET_IP, enabled=False).get("/api/settings/remote-access")
    assert resp.status_code == 200


def test_a_refused_codex_import_changes_nothing(db, monkeypatch):
    calls = []
    monkeypatch.setattr(
        "jacked.codex.accounts.add_codex_account",
        lambda *a, **k: calls.append("import") or {"id": 1, "email": "c@x.com"},
    )
    resp = _remote(db, TAILNET_IP, enabled=False).post("/api/auth/accounts/add?provider=codex")
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "REMOTE_ACCESS_OFF"
    assert calls == []
