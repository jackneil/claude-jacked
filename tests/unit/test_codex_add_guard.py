"""`jacked codex add` checks the Codex store before it writes (#149i).

Forcing ``cli_auth_credentials_store = "file"`` makes Codex read
``auth.json``. When no file credential exists and no login follows (the web
route, or ``--no-login``), that write signs the user out of Codex and the
import then fails anyway. The add path now refuses first and leaves
``config.toml`` byte for byte as it was. With a login to run, or a file
credential present, it forces file storage as before.
"""

from __future__ import annotations

import json

import pytest

from jacked.codex import accounts as codex_accounts
from jacked.codex.accounts import CodexImportError, add_codex_account
from jacked.web.database import Database
from tests.unit.test_codex_add import _auth_json, _home

KEYRING = 'cli_auth_credentials_store = "keyring"\nmodel = "o3"\n'
AUTO = 'model = "o3"\n'


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


def _config_bytes(home):
    path = home / "config.toml"
    return path.read_bytes() if path.exists() else None


@pytest.mark.parametrize(
    ("config", "next_step"),
    [(KEYRING, 'cli_auth_credentials_store = "file"'), (AUTO, "codex login")],
    ids=["keyring", "auto-no-file"],
)
def test_no_login_refuses_before_any_write(db, tmp_path, config, next_step):
    home = _home(tmp_path, auth=None, config=config)
    before = _config_bytes(home)

    with pytest.raises(CodexImportError) as excinfo:
        add_codex_account(db, home=home, run_login=False)

    assert _config_bytes(home) == before
    assert not (home / "auth.json").exists()
    assert next_step in str(excinfo.value)  # the message names the next step
    assert db.list_accounts(include_inactive=True) == []


def test_a_login_that_follows_still_forces_file_storage(db, tmp_path):
    home = _home(tmp_path, auth=None, config=KEYRING)
    calls = []

    def login(login_home, _env):
        calls.append(login_home)
        (login_home / "auth.json").write_text(json.dumps(_auth_json()))

    acct = add_codex_account(db, home=home, run_login=True, login_runner=login)

    assert calls == [home]
    assert 'cli_auth_credentials_store = "file"' in (home / "config.toml").read_text()
    assert acct["email"] == "dev@example.com"


def test_a_missing_cli_refuses_before_the_store_flip(db, tmp_path, monkeypatch):
    """Without the `codex` CLI no login can write auth.json, so the store is
    not flipped to "file" first (that would sign the user out)."""
    home = _home(tmp_path, auth=None, config=KEYRING)
    before = _config_bytes(home)
    monkeypatch.setattr(codex_accounts, "find_bin", lambda name: None)
    monkeypatch.setattr(
        codex_accounts, "ensure_file_storage",
        lambda *_a, **_k: pytest.fail("the store must not be flipped without a CLI"),
    )

    with pytest.raises(CodexImportError, match="codex.*CLI"):
        add_codex_account(db, home=home, run_login=True)

    assert _config_bytes(home) == before


@pytest.mark.parametrize("config", [KEYRING, None], ids=["keyring-config", "no-config"])
def test_a_failed_login_restores_the_store(db, tmp_path, config):
    """`codex login` failed or was interrupted: config.toml goes back to what
    it was (or away, when it did not exist), so Codex does not read an
    absent auth.json."""
    home = _home(tmp_path, auth=None, config=config)
    before = _config_bytes(home)

    def login(_home, _env):
        raise CodexImportError("`codex login` failed (exit 1)")

    with pytest.raises(CodexImportError, match="exit 1"):
        add_codex_account(db, home=home, run_login=True, login_runner=login)

    assert _config_bytes(home) == before
    assert db.list_accounts(include_inactive=True) == []


def test_an_interrupted_login_restores_the_store_too(db, tmp_path):
    home = _home(tmp_path, auth=None, config=KEYRING)

    def login(_home, _env):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        add_codex_account(db, home=home, run_login=True, login_runner=login)

    assert _config_bytes(home) == KEYRING.encode()


def test_a_present_file_credential_is_imported_without_a_login(db, tmp_path, monkeypatch):
    home = _home(tmp_path, auth=_auth_json(), config=AUTO)
    monkeypatch.setattr(
        codex_accounts, "_run_codex_login", lambda *_a: pytest.fail("no login expected")
    )

    acct = add_codex_account(db, home=home, run_login=False)

    assert acct["email"] == "dev@example.com"
    assert 'cli_auth_credentials_store = "file"' in (home / "config.toml").read_text()


# --------------------------------------------------------------------------
# A keyring user with a leftover auth.json (DCR U-1)
#
# Codex keeps the live credential in the keyring, so a parseable auth.json
# there is a leftover: expired, spent, or another account. Forcing file
# storage would make Codex read it and replace the working keyring sign-in.
# With no login to follow, the add must refuse on the store verdict, not on
# "an identity parses".
# --------------------------------------------------------------------------

STALE_EMAIL = "stale@example.com"
FILE_STORE = 'cli_auth_credentials_store = "file"\n'


def test_no_login_refuses_a_leftover_file_under_the_keyring(db, tmp_path, monkeypatch):
    home = _home(tmp_path, auth=_auth_json(email=STALE_EMAIL), config=KEYRING)
    config_before = _config_bytes(home)
    auth_before = (home / "auth.json").read_bytes()
    monkeypatch.setattr(
        codex_accounts, "_run_codex_login", lambda *_a: pytest.fail("no login expected")
    )

    with pytest.raises(codex_accounts.CodexStoreUnswitchableError) as excinfo:
        add_codex_account(db, home=home, run_login=False)

    assert _config_bytes(home) == config_before
    assert (home / "auth.json").read_bytes() == auth_before
    assert db.list_accounts(include_inactive=True) == []
    reason = codex_accounts.detect_codex_account(home).reason
    assert str(excinfo.value) == reason
    assert 'cli_auth_credentials_store = "file"' in str(excinfo.value)


def test_the_store_refusal_is_a_codex_import_error_with_its_own_code(db, tmp_path):
    home = _home(tmp_path, auth=_auth_json(email=STALE_EMAIL), config=KEYRING)

    with pytest.raises(CodexImportError) as excinfo:
        add_codex_account(db, home=home, run_login=False)

    assert excinfo.value.code == "CODEX_STORE_UNSWITCHABLE"


@pytest.mark.parametrize(
    ("config", "code"),
    [
        (KEYRING, "CODEX_STORE_UNSWITCHABLE"),
        (AUTO, "CODEX_STORE_UNSWITCHABLE"),
        (FILE_STORE, "CODEX_LOGIN_REQUIRED"),
    ],
    ids=["keyring-no-file", "auto-no-file", "file-no-credential"],
)
def test_each_refusal_carries_the_code_of_its_fix(db, tmp_path, config, code):
    """A store jacked cannot manage needs a config change before the login;
    a file store with no credential needs only the login."""
    home = _home(tmp_path, auth=None, config=config)

    with pytest.raises(CodexImportError) as excinfo:
        add_codex_account(db, home=home, run_login=False)

    assert excinfo.value.code == code


def test_the_refusal_log_line_is_values_free(db, tmp_path, caplog):
    home = _home(tmp_path, auth=_auth_json(email=STALE_EMAIL), config=KEYRING)
    caplog.set_level("INFO", logger="jacked.codex.accounts")

    with pytest.raises(CodexImportError):
        add_codex_account(db, home=home, run_login=False)

    lines = [r.getMessage() for r in caplog.records if r.name == "jacked.codex.accounts"]
    assert any("store_mode=keyring" in line for line in lines), lines
    assert all(STALE_EMAIL not in line and "codex-access-token" not in line for line in lines)


# --------------------------------------------------------------------------
# The dashboard route gives each refusal its own code (DCR U-2)
#
# Every refusal used to come back as CODEX_LOGIN_REQUIRED, and the dashboard
# told a keyring user to run `codex login`. That login wrote the keyring
# again, so the next add was refused again: a loop.
# --------------------------------------------------------------------------


@pytest.fixture
def client(db):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from jacked.api.routes.auth import router

    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    return TestClient(app)


def test_route_names_a_store_refusal_and_passes_its_reason(client, db, tmp_path, monkeypatch):
    home = _home(tmp_path, auth=_auth_json(email=STALE_EMAIL), config=KEYRING)
    monkeypatch.setenv("CODEX_HOME", str(home))
    config_before = _config_bytes(home)

    resp = client.post("/api/auth/accounts/add?provider=codex")

    assert resp.status_code == 400
    error = resp.json()["error"]
    assert error["code"] == "CODEX_STORE_UNSWITCHABLE"
    assert error["message"] == codex_accounts.detect_codex_account(home).reason
    assert _config_bytes(home) == config_before
    assert db.list_accounts(include_inactive=True) == []


def test_route_names_auto_with_no_file_a_store_refusal(client, tmp_path, monkeypatch):
    home = _home(tmp_path, auth=None, config=AUTO)
    monkeypatch.setenv("CODEX_HOME", str(home))

    resp = client.post("/api/auth/accounts/add?provider=codex")

    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "CODEX_STORE_UNSWITCHABLE"


def test_route_keeps_login_required_for_a_file_store_with_no_credential(
    client, tmp_path, monkeypatch
):
    home = _home(tmp_path, auth=None, config=FILE_STORE)
    monkeypatch.setenv("CODEX_HOME", str(home))

    resp = client.post("/api/auth/accounts/add?provider=codex")

    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "CODEX_LOGIN_REQUIRED"
    assert body["needs_login"] is True
    assert body["command"] == "codex login"


def test_route_logs_the_refusal_code_without_values(client, tmp_path, monkeypatch, caplog):
    home = _home(tmp_path, auth=_auth_json(email=STALE_EMAIL), config=KEYRING)
    monkeypatch.setenv("CODEX_HOME", str(home))
    caplog.set_level("INFO", logger="jacked.api.routes.auth")

    client.post("/api/auth/accounts/add?provider=codex")

    lines = [r.getMessage() for r in caplog.records if r.name == "jacked.api.routes.auth"]
    assert any("CODEX_STORE_UNSWITCHABLE" in line for line in lines), lines
    assert all(STALE_EMAIL not in line for line in lines)


# --------------------------------------------------------------------------
# The CLI path signs in again over a leftover file (same root cause as U-1)
#
# `jacked codex add` (run_login=True) imported a parseable leftover auth.json
# under the keyring without a login. Codex then read that stale file once
# the store was "file". The CLI now treats a store jacked cannot switch like
# a missing sign-in: it sets the store to "file" first, then runs
# `codex login`, then imports what the login wrote.
# --------------------------------------------------------------------------

FRESH_EMAIL = "fresh@example.com"


def test_the_cli_signs_in_again_over_a_leftover_file_under_the_keyring(db, tmp_path):
    home = _home(tmp_path, auth=_auth_json(email=STALE_EMAIL), config=KEYRING)
    calls = []

    def login(login_home, _env):
        # The store is "file" before the login runs, so Codex writes auth.json.
        calls.append((login_home / "config.toml").read_text())
        (login_home / "auth.json").write_text(json.dumps(_auth_json(email=FRESH_EMAIL)))

    acct = add_codex_account(db, home=home, run_login=True, login_runner=login)

    assert len(calls) == 1
    assert 'cli_auth_credentials_store = "file"' in calls[0]
    assert acct["email"] == FRESH_EMAIL
    emails = [a["email"] for a in db.list_accounts(include_inactive=True)]
    assert emails == [FRESH_EMAIL], "the leftover account is never imported"


def test_the_cli_still_imports_a_switchable_store_without_a_login(db, tmp_path, monkeypatch):
    home = _home(tmp_path, auth=_auth_json(), config=AUTO)
    monkeypatch.setattr(
        codex_accounts, "_run_codex_login", lambda *_a: pytest.fail("no login expected")
    )

    acct = add_codex_account(db, home=home, run_login=True)

    assert acct["email"] == "dev@example.com"


def test_a_failed_cli_login_imports_nothing(db, tmp_path):
    home = _home(tmp_path, auth=_auth_json(email=STALE_EMAIL), config=KEYRING)

    def login(_home, _env):
        raise CodexImportError("`codex login` failed (exit 1)")

    with pytest.raises(CodexImportError):
        add_codex_account(db, home=home, run_login=True, login_runner=login)

    assert db.list_accounts(include_inactive=True) == []
