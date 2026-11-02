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


def test_a_present_file_credential_is_imported_without_a_login(db, tmp_path, monkeypatch):
    home = _home(tmp_path, auth=_auth_json(), config=AUTO)
    monkeypatch.setattr(
        codex_accounts, "_run_codex_login", lambda *_a: pytest.fail("no login expected")
    )

    acct = add_codex_account(db, home=home, run_login=False)

    assert acct["email"] == "dev@example.com"
    assert 'cli_auth_credentials_store = "file"' in (home / "config.toml").read_text()
