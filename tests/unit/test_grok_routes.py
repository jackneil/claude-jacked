"""Dashboard routes for a Grok account: add, switch, re-auth, delete, CC auth."""

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.routes.auth import router
from jacked.grok.accounts import import_grok_account, slot_auth_path
from jacked.web.database import Database


def _auth(email, user_id, token="refresh-secret", expires="2026-11-04T00:00:00Z"):
    return {"https://auth.x.ai::client": {
        "key": "access-secret", "refresh_token": token, "expires_at": expires,
        "email": email, "user_id": user_id,
    }}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


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
def client(db):
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    return TestClient(app)


@pytest.fixture
def logins(monkeypatch):
    calls = []

    def _fake(provider, account, _db, **_kwargs):
        calls.append((provider, account.get("id"), account.get("email")))
        return {"profile_login": True, "provider": provider, "message": "opened"}

    monkeypatch.setattr("jacked.web.external_login.start_provider_login", _fake)
    return calls


def test_add_imports_the_signed_in_grok_login(client, db, home, logins):
    _write(home / "auth.json", _auth("g@x.com", "user-1"))
    resp = client.post("/api/auth/accounts/add?provider=grok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "grok" and body["imported"] is True
    row = db.get_account(body["account_id"])
    assert row["provider"] == "grok" and row["refresh_token"] in (None, "")
    assert logins == []


def test_add_without_a_login_opens_the_sign_in_in_a_pending_profile(client, home, logins):
    resp = client.post("/api/auth/accounts/add?provider=grok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile_login"] is True and body["needs_login"] is True
    assert logins == [("grok", "pending", "pending@grok.login")]


def test_add_from_a_remote_browser_does_not_start_a_sign_in_on_the_host(client, home, logins):
    resp = client.post("/api/auth/accounts/add?provider=grok&remote=true")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "GROK_LOGIN_REQUIRED"
    assert logins == []


def test_use_switches_the_live_grok_login(client, db, home):
    _write(home / "auth.json", _auth("a@x.com", "user-a", token="a"))
    a = import_grok_account(db, home=home, make_active=True)
    _write(home / "auth.json", _auth("b@x.com", "user-b", token="b"))
    b = import_grok_account(db, home=home)
    _write(home / "auth.json", _auth("a@x.com", "user-a", token="a"))

    resp = client.post(f"/api/auth/accounts/{b['id']}/use")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True and body["provider"] == "grok"
    live = json.loads((home / "auth.json").read_text())
    assert live["https://auth.x.ai::client"]["email"] == "b@x.com"
    assert db.get_active_account_id("grok") == b["id"]
    assert a["id"] != b["id"]


def test_reauth_of_the_live_account_opens_its_profile_sign_in(client, db, home, logins):
    _write(home / "auth.json", _auth("a@x.com", "user-a"))
    a = import_grok_account(db, home=home, make_active=True)
    resp = client.post(f"/api/auth/accounts/{a['id']}/reauth")
    assert resp.status_code == 200
    assert resp.json()["profile_login"] is True
    assert logins == [("grok", a["id"], "a@x.com")]


def test_reauth_never_signs_over_another_live_account(client, db, home, logins):
    """`grok login` replaces auth.json. With another account live, its rotated
    refresh token would be lost, so the re-auth is refused."""
    _write(home / "auth.json", _auth("a@x.com", "user-a"))
    a = import_grok_account(db, home=home)
    _write(home / "auth.json", _auth("b@x.com", "user-b"))
    import_grok_account(db, home=home, make_active=True)

    resp = client.post(f"/api/auth/accounts/{a['id']}/reauth")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "GROK_SIGNED_IN_AS_OTHER"
    assert logins == []


def test_reauth_from_a_remote_browser_is_refused(client, db, home, logins):
    _write(home / "auth.json", _auth("a@x.com", "user-a"))
    a = import_grok_account(db, home=home, make_active=True)
    resp = client.post(f"/api/auth/accounts/{a['id']}/reauth?remote=true")
    assert resp.status_code == 400
    assert logins == []


def test_codex_reauth_keeps_the_terminal_flow(client, db, logins):
    acct = db.create_account("r@x.com", "codex-managed", 4102444800,
                             provider="codex", organization_uuid="acct-R")
    resp = client.post(f"/api/auth/accounts/{acct['id']}/reauth")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CODEX_NOT_OAUTH"
    assert logins == []


def test_cc_auth_is_refused_for_a_grok_account(client, db, home):
    _write(home / "auth.json", _auth("a@x.com", "user-a"))
    a = import_grok_account(db, home=home)
    resp = client.post(f"/api/auth/accounts/{a['id']}/authorize-cc")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "NO_CC_TOKEN"


def test_delete_removes_the_grok_slot_and_active_pointer(client, db, home):
    """The slot holds a live refresh token. A deleted account must not leave it."""
    _write(home / "auth.json", _auth("a@x.com", "user-a"))
    a = import_grok_account(db, home=home, make_active=True)
    slot = slot_auth_path(a["id"], home)
    assert slot.exists()

    resp = client.delete(f"/api/auth/accounts/{a['id']}")
    assert resp.status_code == 200
    assert db.get_active_account_id("grok") is None
    assert not slot.parent.exists()
