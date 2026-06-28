"""M3: add / import a Codex account (force file storage, codex login, persist).

Covers import_codex_account (persist provider='codex', re-add updates not
duplicates), ensure_file_storage (idempotent config write), add_codex_account
orchestration (forces file + drives login), and the POST /accounts/add
?provider=codex route (import vs needs_login).
"""

import base64
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.routes.auth import router
from jacked.codex import credentials as cc
from jacked.codex.accounts import (
    CodexImportError,
    add_codex_account,
    import_codex_account,
)
from jacked.web.database import Database


def _b64url(obj: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(payload: dict) -> str:
    h = _b64url({"alg": "RS256", "typ": "JWT"})
    s = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{h}.{_b64url(payload)}.{s}"


def _auth_json(email="dev@example.com", plan="pro", account_id="acct-xyz"):
    return {
        "OPENAI_API_KEY": None,
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _jwt({
                "email": email,
                "exp": 9999999999,
                "https://api.openai.com/auth": {
                    "chatgpt_plan_type": plan,
                    "chatgpt_account_id": account_id,
                    "chatgpt_user_id": "u-1",
                },
            }),
            "access_token": "codex-access-token",
            "refresh_token": "codex-refresh-token",
            "account_id": account_id,
        },
        "last_refresh": "2026-06-27T21:18:58Z",
    }


def _home(tmp_path, auth=None, config=None):
    home = tmp_path / ".codex"
    home.mkdir(parents=True, exist_ok=True)
    if auth is not None:
        (home / "auth.json").write_text(json.dumps(auth))
    if config is not None:
        (home / "config.toml").write_text(config)
    return home


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


# --------------------------------------------------------------------------
# import_codex_account
# --------------------------------------------------------------------------

def test_import_persists_codex_row(db, tmp_path):
    home = _home(tmp_path, auth=_auth_json())
    acct = import_codex_account(db, home=home)
    assert acct["provider"] == "codex"
    assert acct["email"] == "dev@example.com"
    assert acct["subscription_type"] == "pro"
    assert acct["organization_uuid"] == "acct-xyz"
    assert acct["refresh_token"] is None  # jacked never stores a Codex refresh token


def test_reimport_updates_no_duplicate(db, tmp_path):
    home = _home(tmp_path, auth=_auth_json())
    first = import_codex_account(db, home=home)
    second = import_codex_account(db, home=home)
    assert first["id"] == second["id"]
    codex_rows = [a for a in db.list_accounts() if a["provider"] == "codex"]
    assert len(codex_rows) == 1


def test_import_does_not_store_live_token_at_rest(db, tmp_path):
    """Hardening: the live Codex token must not be written into jacked.db — it's
    never read back (switching uses auth.json, usage uses app-server)."""
    home = _home(tmp_path, auth=_auth_json())
    acct = import_codex_account(db, home=home)
    assert acct["access_token"] == "codex-managed"
    assert "codex-access-token" not in json.dumps(acct)  # the real token is absent


def test_import_marks_account_valid(db, tmp_path):
    """A freshly-imported (signed-in) Codex account is valid — not left 'unknown'
    where the Anthropic validator would falsely mark it invalid."""
    home = _home(tmp_path, auth=_auth_json())
    acct = import_codex_account(db, home=home)
    assert acct["validation_status"] == "valid"


def test_validate_codex_uses_signed_in_state_not_anthropic(db, tmp_path, monkeypatch):
    """validate_account on a Codex account must NOT hit Anthropic (which would
    401 on the sentinel token) — it validates by the slot being signed in."""
    import asyncio

    from jacked.codex.switching import codex_slot_auth_path
    from jacked.web.auth import validate_account

    base = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(base))
    acct = db.create_account("v@x.com", "codex-managed", 9999999999,
                             provider="codex", organization_uuid="acct-V")
    db.update_account(acct["id"], validation_status="invalid")  # the bug state
    slot = codex_slot_auth_path(acct["id"], base)
    slot.parent.mkdir(parents=True, exist_ok=True)
    slot.write_text(json.dumps(_auth_json(email="v@x.com")))

    res = asyncio.run(validate_account(acct["id"], db))
    assert res["valid"] is True
    assert db.get_account(acct["id"])["validation_status"] == "valid"


def test_validate_codex_signed_out_is_invalid(db, tmp_path, monkeypatch):
    import asyncio

    from jacked.web.auth import validate_account

    base = tmp_path / ".codex"
    base.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(base))
    acct = db.create_account("o@x.com", "codex-managed", 9999999999,
                             provider="codex", organization_uuid="acct-O")
    res = asyncio.run(validate_account(acct["id"], db))  # no slot -> signed out
    assert res["valid"] is False
    assert db.get_account(acct["id"])["validation_status"] == "invalid"


def test_import_raises_without_identity(db, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    with pytest.raises(CodexImportError):
        import_codex_account(db, home=home)


def test_codex_and_claude_same_email_coexist(db, tmp_path):
    db.create_account("dev@example.com", "claude-tok", 9999999999, provider="claude")
    home = _home(tmp_path, auth=_auth_json(email="dev@example.com"))
    import_codex_account(db, home=home)
    providers = sorted(
        a["provider"] for a in db.list_accounts() if a["email"] == "dev@example.com"
    )
    assert providers == ["claude", "codex"]


def test_make_active_sets_codex_active_setting(db, tmp_path):
    home = _home(tmp_path, auth=_auth_json())
    acct = import_codex_account(db, home=home, make_active=True)
    assert db.get_active_account_id("codex") == acct["id"]
    assert db.get_active_account_id("claude") is None  # provider-isolated


# --------------------------------------------------------------------------
# ensure_file_storage
# --------------------------------------------------------------------------

def test_ensure_file_storage_writes_key_when_no_config(tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    changed = cc.ensure_file_storage(home)
    assert changed is True
    assert cc.credential_store_mode(home) == "file"


def test_ensure_file_storage_replaces_keyring_preserving_content(tmp_path):
    home = _home(
        tmp_path,
        config='model = "gpt-5.5"\ncli_auth_credentials_store = "keyring"\n',
    )
    changed = cc.ensure_file_storage(home)
    assert changed is True
    text = (home / "config.toml").read_text()
    assert cc.credential_store_mode(home) == "file"
    assert 'model = "gpt-5.5"' in text
    assert '"keyring"' not in text


def test_ensure_file_storage_idempotent_when_already_file(tmp_path):
    home = _home(tmp_path, config='cli_auth_credentials_store = "file"\n')
    assert cc.ensure_file_storage(home) is False


def test_ensure_file_storage_stays_top_level(tmp_path):
    home = _home(tmp_path, config='[tui]\ntheme = "dark"\n')
    cc.ensure_file_storage(home)
    lines = (home / "config.toml").read_text().splitlines()
    key_idx = next(i for i, ln in enumerate(lines) if "cli_auth_credentials_store" in ln)
    table_idx = next(i for i, ln in enumerate(lines) if ln.strip().startswith("["))
    assert key_idx < table_idx  # key precedes the [tui] table → stays top-level


# --------------------------------------------------------------------------
# add_codex_account orchestration
# --------------------------------------------------------------------------

def test_add_forces_file_storage_and_imports(db, tmp_path):
    home = _home(
        tmp_path, auth=_auth_json(), config='cli_auth_credentials_store = "keyring"\n'
    )
    acct = add_codex_account(db, home=home, run_login=False)
    assert acct["provider"] == "codex"
    assert cc.credential_store_mode(home) == "file"  # forced


def test_add_runs_login_when_absent(db, tmp_path):
    home = tmp_path / ".codex"
    home.mkdir()
    calls = []

    def fake_login(h, env):
        calls.append(h)
        (h / "auth.json").write_text(json.dumps(_auth_json(email="new@example.com")))

    acct = add_codex_account(
        db, home=home, run_login=True, login_runner=fake_login
    )
    assert calls == [home]  # login was driven
    assert acct["email"] == "new@example.com"
    assert acct["provider"] == "codex"


def test_add_does_not_run_login_when_present(db, tmp_path):
    home = _home(tmp_path, auth=_auth_json())
    calls = []
    add_codex_account(
        db, home=home, run_login=True, login_runner=lambda h, e: calls.append(h)
    )
    assert calls == []  # already logged in → no login driven


# --------------------------------------------------------------------------
# POST /accounts/add?provider=codex route
# --------------------------------------------------------------------------

@pytest.fixture
def client(db):
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    return TestClient(app)


def test_route_imports_codex_account(client, db, tmp_path, monkeypatch):
    home = _home(tmp_path, auth=_auth_json(email="route@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(home))
    resp = client.post("/api/auth/accounts/add?provider=codex")
    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "codex"
    assert body["email"] == "route@example.com"
    assert body["plan"] == "pro"
    codex = [a for a in db.list_accounts() if a["provider"] == "codex"]
    assert len(codex) == 1


def test_reauth_route_rejects_codex(client, db):
    """POST /accounts/{id}/reauth must not start a Claude OAuth flow for Codex."""
    acct = db.create_account("r@x.com", "codex-managed", 4102444800,
                             provider="codex", organization_uuid="acct-R")
    resp = client.post(f"/api/auth/accounts/{acct['id']}/reauth")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CODEX_NOT_OAUTH"


def test_cc_auth_route_rejects_codex(client, db):
    """POST /accounts/{id}/authorize-cc — Codex has no Claude Code token."""
    acct = db.create_account("c@x.com", "codex-managed", 4102444800,
                             provider="codex", organization_uuid="acct-C")
    resp = client.post(f"/api/auth/accounts/{acct['id']}/authorize-cc")
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CODEX_NO_CC"


def test_delete_route_removes_codex_slot_and_active_pointer(client, db, tmp_path, monkeypatch):
    """Deleting a Codex account clears its active pointer + removes its slot dir."""
    from jacked.codex.switching import codex_account_home

    base = tmp_path / ".codex"
    monkeypatch.setenv("CODEX_HOME", str(base))
    acct = db.create_account("d@x.com", "codex-managed", 4102444800,
                             provider="codex", organization_uuid="acct-D")
    db.set_active_account_id(acct["id"], provider="codex")
    slot = codex_account_home(acct["id"])
    slot.mkdir(parents=True, exist_ok=True)
    (slot / "auth.json").write_text("{}")

    resp = client.delete(f"/api/auth/accounts/{acct['id']}")
    assert resp.status_code == 200
    assert db.get_active_account_id("codex") is None  # pointer cleared
    assert not slot.exists()  # slot removed


def test_codex_never_refreshes_against_anthropic(db):
    """Explicit defense-in-depth: a Codex row never qualifies for token refresh."""
    from jacked.web.auth import should_refresh, should_refresh_cc

    codex = {"provider": "codex", "refresh_token": "x", "expires_at": 0,
             "cc_refresh_token": "y", "cc_expires_at": 0}
    assert should_refresh(codex) is False
    assert should_refresh_cc(codex) is False


def test_route_needs_login_when_absent(client, tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    resp = client.post("/api/auth/accounts/add?provider=codex")
    assert resp.status_code == 400
    body = resp.json()
    assert body["needs_login"] is True
    assert body["command"] == "codex login"
