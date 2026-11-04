"""Dashboard routes for a Grok account: add, switch, re-auth, delete, CC auth."""

import json
import threading
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


class _Logins(list):
    """The sign-ins a test started. ``finish`` plays the CLI exit."""

    def __init__(self):
        super().__init__()
        self.hooks = []

    def finish(self, outcome="signed_in", data=None, home=None):
        prepare, on_exit = self.hooks[-1]
        env, args = prepare()
        if data is not None:
            _write(Path(env["GROK_HOME"]) / "auth.json", data)
        on_exit(outcome)
        return env, args


@pytest.fixture
def logins(monkeypatch):
    calls = _Logins()

    def _fake(provider, account, _db, **kwargs):
        calls.append((provider, account.get("id"), account.get("email")))
        calls.hooks.append((kwargs.get("prepare"), kwargs.get("on_exit")))
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
    # The imported login is the live one, so it is the active Grok account.
    # Without the pointer the 30-minute slot copy never runs for it.
    assert db.get_active_account_id("grok") == body["account_id"]
    assert logins == []


def test_add_without_a_login_opens_the_sign_in_in_a_pending_profile(client, db, home, logins):
    resp = client.post("/api/auth/accounts/add?provider=grok")
    assert resp.status_code == 200
    body = resp.json()
    assert body["profile_login"] is True and body["needs_login"] is True
    assert logins == [("grok", "pending", "pending@grok.login")]
    # After `grok login` exits 0, the new login is imported with no second click.
    logins.finish(data=_auth("n@x.com", "user-n"))
    rows = [r for r in db.list_accounts() if r["provider"] == "grok"]
    assert [r["email"] for r in rows] == ["n@x.com"]
    # Nobody was signed in, so the new login is the live and active one.
    live = json.loads((home / "auth.json").read_text())
    assert live["https://auth.x.ai::client"]["email"] == "n@x.com"
    assert db.get_active_account_id("grok") == rows[0]["id"]


def test_add_of_a_second_account_keeps_the_live_login(client, db, home, logins):
    _write(home / "auth.json", _auth("a@x.com", "user-a", token="live-a"))
    a = import_grok_account(db, home=home, make_active=True)

    resp = client.post("/api/auth/accounts/add?provider=grok")
    assert resp.status_code == 200 and resp.json()["needs_login"] is True
    logins.finish(data=_auth("b@x.com", "user-b", token="b"))

    live = json.loads((home / "auth.json").read_text())
    assert live["https://auth.x.ai::client"]["refresh_token"] == "live-a"
    assert db.get_active_account_id("grok") == a["id"]
    b = [r for r in db.list_accounts() if r["email"] == "b@x.com"][0]
    slot = json.loads(slot_auth_path(b["id"], home).read_text())
    assert slot["https://auth.x.ai::client"]["refresh_token"] == "b"


def _pushed_result(db, logins, data):
    """Run an Add Account sign-in to its end. Return the event the dashboard got.

    The broadcast runs on the server loop, apart from the thread that plays
    the CLI exit, so the test waits on an event and not on a sleep loop.
    """
    sent = []
    arrived = threading.Event()

    class _Registry:
        async def broadcast(self, topic, payload=None, source=None):
            sent.append((topic, payload))
            arrived.set()

    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    app.state.ws_registry = _Registry()
    with TestClient(app) as local:
        assert local.post("/api/auth/accounts/add?provider=grok").status_code == 200
        logins.finish(data=data)
        assert arrived.wait(timeout=10)
    assert sent[0][0] == "provider_login_finished"
    return sent[0][1]


def test_the_end_of_a_sign_in_is_pushed_to_the_dashboard(db, home, logins):
    """The watcher thread sends the result over the WebSocket registry."""
    payload = _pushed_result(db, logins, _auth("n@x.com", "user-n"))
    assert payload["ok"] is True and payload["email"] == "n@x.com" and payload["live"] is True
    assert "\u2014" not in payload["message"]


def test_a_storage_failure_after_the_sign_in_is_pushed_to_the_dashboard(
    db, home, logins, monkeypatch
):
    """An OSError after a good `grok login` used to escape the handler, so the
    dashboard waited for a result that never came."""
    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr("jacked.grok.login_import.store_signed_in_login", _boom)
    payload = _pushed_result(db, logins, _auth("n@x.com", "user-n"))
    assert payload["ok"] is False and payload["provider"] == "grok"
    assert "could not store the Grok sign-in" in payload["message"]
    assert "\u2014" not in payload["message"]


@pytest.mark.parametrize("outcome", ["failed", "timed_out", "no_browser"])
def test_a_sign_in_that_did_not_finish_changes_no_account(client, db, home, logins, outcome):
    assert client.post("/api/auth/accounts/add?provider=grok").status_code == 200
    env, _args = logins.finish(outcome, data=_auth("n@x.com", "user-n"))
    assert [r for r in db.list_accounts() if r["provider"] == "grok"] == []
    assert not (home / "auth.json").exists()
    assert not Path(env["GROK_HOME"]).exists()


def test_a_second_sign_in_while_one_runs_is_a_409(client, home, monkeypatch):
    from jacked.web.external_login import LoginInProgressError

    def _busy(*_args, **_kwargs):
        raise LoginInProgressError("A Grok sign-in is already open.")

    monkeypatch.setattr("jacked.web.external_login.start_provider_login", _busy)
    resp = client.post("/api/auth/accounts/add?provider=grok")
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "GROK_LOGIN_IN_PROGRESS"


def test_an_in_scope_remote_dashboard_never_starts_a_sign_in_on_the_host(db, home, logins):
    """Remote access on and inside the scope passes the gate, but the sign-in
    runs a CLI and a browser on the host, so a remote page gets the terminal step."""
    db.set_setting("remote_access_enabled", "true")
    db.set_setting("remote_access_scope", "tailscale")
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    remote = TestClient(app, client=("100.64.0.9", 5000))
    resp = remote.post("/api/auth/accounts/add?provider=grok")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "GROK_LOGIN_REQUIRED"
    _write(home / "auth.json", _auth("a@x.com", "user-a"))
    a = import_grok_account(db, home=home, make_active=True)
    resp = remote.post(f"/api/auth/accounts/{a['id']}/reauth")
    assert resp.status_code == 400
    assert logins == []


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


def test_reauth_of_a_non_live_account_signs_in_apart_from_the_live_login(client, db, home, logins):
    """The sign-in runs in an isolated GROK_HOME, so the live login of another
    account stays, and the re-auth is no longer refused with a 409."""
    _write(home / "auth.json", _auth("a@x.com", "user-a", token="old-a"))
    a = import_grok_account(db, home=home)
    _write(home / "auth.json", _auth("b@x.com", "user-b", token="live-b"))
    b = import_grok_account(db, home=home, make_active=True)

    resp = client.post(f"/api/auth/accounts/{a['id']}/reauth")
    assert resp.status_code == 200
    assert logins == [("grok", a["id"], "a@x.com")]
    env, args = logins.finish(data=_auth("a@x.com", "user-a", token="new-a"))

    assert Path(env["GROK_HOME"]) != home
    assert args == ("--leader-socket", str(Path(env["GROK_HOME"]) / "leader.sock"))
    live = json.loads((home / "auth.json").read_text())
    assert live["https://auth.x.ai::client"]["refresh_token"] == "live-b"
    slot = json.loads(slot_auth_path(a["id"], home).read_text())
    assert slot["https://auth.x.ai::client"]["refresh_token"] == "new-a"
    assert db.get_active_account_id("grok") == b["id"]
    assert not Path(env["GROK_HOME"]).exists()


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


@pytest.mark.parametrize("provider", ["grok", "codex"])
def test_delete_removes_the_slot_under_the_switch_lock(db, home, tmp_path, monkeypatch, provider):
    """The timer copy writes the slot under the switch lock. A delete that
    removes the slot outside that lock can lose the race, and the copy then
    writes the refresh token back after the row is gone."""
    import contextlib

    from jacked.api import provider_accounts

    held = {"now": False, "removed_inside": None}

    @contextlib.contextmanager
    def _recording_lock(*_args, **_kwargs):
        held["now"] = True
        try:
            yield True
        finally:
            held["now"] = False

    real_rmtree = provider_accounts.shutil.rmtree

    def _rmtree(path, *args, **kwargs):
        held["removed_inside"] = held["now"]
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(provider_accounts.shutil, "rmtree", _rmtree)
    if provider == "grok":
        monkeypatch.setattr("jacked.grok.accounts._switch_lock", _recording_lock)
        _write(home / "auth.json", _auth("a@x.com", "user-a"))
        account = import_grok_account(db, home=home, make_active=True)
        slot = slot_auth_path(account["id"], home).parent
    else:
        from jacked.codex import switching as sw

        monkeypatch.setenv("CODEX_HOME", str(tmp_path / ".codex"))
        monkeypatch.setattr(sw, "_codex_swap_lock", _recording_lock)
        account = db.create_account("c@x.com", "codex-managed", 4102444800,
                                    provider="codex", organization_uuid="acct-C")
        db.set_active_account_id(account["id"], provider="codex")
        slot = sw.codex_account_home(account["id"])
        _write(slot / "auth.json", {"auth_mode": "chatgpt"})

    db.delete_account(account["id"])
    provider_accounts.forget_slot(db, account)

    assert not slot.exists()
    assert held["removed_inside"] is True
    assert db.get_active_account_id(provider) is None


def test_each_dashboard_sign_in_gets_its_own_private_home(client, db, home, logins):
    """The result of one sign-in is stored from its own home, so the cleanup
    of one sign-in never removes the login of another."""
    assert client.post("/api/auth/accounts/add?provider=grok").status_code == 200
    first_prepare, first_exit = logins.hooks[-1]
    assert client.post("/api/auth/accounts/add?provider=grok").status_code == 200
    second_prepare, _second_exit = logins.hooks[-1]
    env1, _ = first_prepare()
    env2, _ = second_prepare()
    assert env1["GROK_HOME"] != env2["GROK_HOME"]
    _write(Path(env1["GROK_HOME"]) / "auth.json", _auth("n@x.com", "user-n"))

    first_exit("signed_in")

    assert [r["email"] for r in db.list_accounts() if r["provider"] == "grok"] == ["n@x.com"]
    assert not Path(env1["GROK_HOME"]).exists()
    assert Path(env2["GROK_HOME"]).exists()
