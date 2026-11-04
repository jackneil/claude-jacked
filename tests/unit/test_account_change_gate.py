"""Every account route that changes credentials runs the remote-access gate.

The gate is ``_credential_mutation_decision`` (``jacked.api.mutation_gate``):
loopback always may; a remote client may only when remote access is on and
its address is inside the scope. ``/use`` ran it before this change. Add
(Codex, Grok), Grok re-auth, delete, edit, reorder and token refresh did
not, so a remote page could start a sign-in on the host, import a login, or
delete stored credentials while remote access was off.

The app-wide write gate (``jacked.api.write_gate``) now refuses every
unsafe route; ``tests/unit/api/test_write_gate.py`` keeps that set closed.
This file proves the account routes through the same wiring as the app.
"""

import json
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from jacked.api.routes.auth import router
from jacked.api.write_gate import MutationRefused, mutation_refused_handler, require_mutation_access
from jacked.web.database import Database

TAILNET_IP = "100.116.47.72"
LAN_IP = "192.168.42.5"

@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / ".grok"
    monkeypatch.setenv("GROK_HOME", str(h))
    monkeypatch.setattr("jacked.web.browser_launch.PROFILE_ROOT", tmp_path / "profiles")
    return h


@pytest.fixture
def side_effects(monkeypatch):
    """Record every host action a refused request must never reach."""
    calls = []
    monkeypatch.setattr(
        "jacked.web.external_login.start_provider_login",
        lambda *a, **k: calls.append("grok-login") or {"profile_login": True},
    )
    monkeypatch.setattr(
        "jacked.codex.accounts.add_codex_account",
        lambda *a, **k: calls.append("codex-import") or {"id": 1, "email": "c@x.com"},
    )
    monkeypatch.setattr(
        "jacked.api.routes.auth.refresh_account_token",
        lambda *a, **k: calls.append("refresh"),
    )
    return calls


def _app(db):
    # The same wiring as jacked.api.main: the gate runs before every write.
    app = FastAPI(dependencies=[Depends(require_mutation_access)])
    app.add_exception_handler(MutationRefused, mutation_refused_handler)
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    return app


def _grok_account(db, home):
    data = {"https://auth.x.ai::client": {
        "key": "k", "refresh_token": "r", "email": "g@x.com", "user_id": "u",
        "expires_at": "2026-12-01T00:00:00Z",
    }}
    home.mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text(json.dumps(data), encoding="utf-8")
    from jacked.grok.accounts import import_grok_account

    return import_grok_account(db, home=home, make_active=True)


def _send(http, method, path):
    if method == "PATCH":
        return http.patch(path, json={"display_name": "x"})
    if method == "DELETE":
        return http.delete(path)
    if path.endswith("/reorder"):
        return http.post(path, json={"order": [1]})
    return http.post(path)


CASES = [
    ("POST", "/accounts/add?provider=codex"),
    ("POST", "/accounts/add?provider=grok"),
    ("POST", "/accounts/{id}/reauth"),
    ("PATCH", "/accounts/{id}"),
    ("DELETE", "/accounts/{id}"),
    ("POST", "/accounts/reorder"),
    ("POST", "/accounts/{id}/refresh"),
    ("POST", "/accounts/{id}/use"),
]


@pytest.mark.parametrize("method, path", CASES)
@pytest.mark.parametrize("host, enabled", [(TAILNET_IP, False), (LAN_IP, True)])
def test_a_refused_remote_client_changes_nothing(
    db, home, side_effects, method, path, host, enabled
):
    acct = _grok_account(db, home)
    db.set_setting("remote_access_enabled", "true" if enabled else "false")
    db.set_setting("remote_access_scope", "tailscale")
    slot = Path(home / "jacked-accounts" / str(acct["id"]) / "auth.json")
    before = db.get_account(acct["id"])

    remote = TestClient(_app(db), client=(host, 5000))
    resp = _send(remote, method, "/api/auth" + path.replace("{id}", str(acct["id"])))

    assert resp.status_code == 403, resp.text
    error = resp.json()["error"]
    # /use keeps its own code; every other route answers with the reason.
    assert error["code"] in {"CREDENTIAL_MUTATION_LOCAL_ONLY", "REMOTE_ACCESS_OFF", "OUTSIDE_SCOPE"}
    assert "—" not in error["message"]
    assert resp.headers["cache-control"] == "no-store"
    assert side_effects == []
    assert db.get_account(acct["id"]) == before
    assert slot.exists()


@pytest.mark.parametrize("method, path", [c for c in CASES if c[0] != "POST" or "use" not in c[1]])
def test_loopback_is_never_refused(db, home, side_effects, method, path):
    acct = _grok_account(db, home)
    local = TestClient(_app(db))
    resp = _send(local, method, "/api/auth" + path.replace("{id}", str(acct["id"])))
    assert resp.status_code != 403, resp.text


def test_an_in_scope_remote_client_may_delete(db, home, side_effects):
    acct = _grok_account(db, home)
    db.set_setting("remote_access_enabled", "true")
    db.set_setting("remote_access_scope", "tailscale")
    remote = TestClient(_app(db), client=(TAILNET_IP, 5000))
    resp = remote.delete(f"/api/auth/accounts/{acct['id']}")
    assert resp.status_code == 200
    assert db.get_account(acct["id"]) is None


def test_claude_add_from_a_refused_remote_client_still_only_stores(db, monkeypatch):
    """The documented exemption: no 403, and the flow gets no activation right."""
    started = []

    class _Flow:
        def __init__(self, _db, **kwargs):
            started.append(kwargs)

        async def start(self):
            return {"flow_id": "f"}

    monkeypatch.setattr("jacked.api.routes.auth.OAuthFlow", _Flow)
    remote = TestClient(_app(db), client=(TAILNET_IP, 5000))
    assert remote.post("/api/auth/accounts/add").status_code == 200
    assert started[0]["allow_credential_activation"] is False
