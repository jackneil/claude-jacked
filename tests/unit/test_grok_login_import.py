"""A Grok sign-in in an isolated GROK_HOME, stored for one account."""

import json
import sys
from pathlib import Path

import pytest

from jacked.grok import login_import as li
from jacked.grok.accounts import GrokAccountError, import_grok_account, slot_auth_path
from jacked.web.database import Database

KEY = "https://auth.x.ai::client"


def _auth(email, user_id, token="r"):
    return {KEY: {"key": "k", "refresh_token": token, "email": email, "user_id": user_id,
                  "expires_at": "2026-12-01T00:00:00Z"}}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def _token(path: Path) -> str:
    return json.loads(path.read_text())[KEY]["refresh_token"]


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr("jacked.web.browser_launch.PROFILE_ROOT", tmp_path / "profiles")
    return tmp_path / ".grok"


def test_the_sign_in_home_is_private_and_carries_the_grok_settings(home):
    _write(home / "config.toml", {})
    (home / "config.toml").write_text('[grok_com_config.oidc]\nissuer = "x"\n')
    env, args = li.prepare_login_env(home)
    target = Path(env["GROK_HOME"])
    assert target == li.login_dir(home)
    assert (target / "config.toml").read_text().startswith("[grok_com_config.oidc]")
    assert args == ("--leader-socket", str(target / "leader.sock"))
    if sys.platform != "win32":
        assert target.stat().st_mode & 0o077 == 0


def test_a_new_sign_in_home_starts_empty(home):
    env, _ = li.prepare_login_env(home)
    _write(Path(env["GROK_HOME"]) / "auth.json", _auth("old@x.com", "u-old"))
    env, _ = li.prepare_login_env(home)
    assert not (Path(env["GROK_HOME"]) / "auth.json").exists()


def test_re_auth_of_the_live_account_replaces_the_live_login(db, home):
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="old"))
    a = import_grok_account(db, home=home, make_active=True)
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("a@x.com", "u-a", token="new"))

    payload = li.finish_login(db, "signed_in", target_id=a["id"], home=home)

    assert payload["ok"] is True and payload["live"] is True
    assert _token(home / "auth.json") == "new"
    assert _token(slot_auth_path(a["id"], home)) == "new"
    assert not li.login_dir(home).exists()


def test_a_sign_in_as_another_account_never_changes_the_target(db, home):
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="old"))
    a = import_grok_account(db, home=home, make_active=True)
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("b@x.com", "u-b", token="b"))

    payload = li.finish_login(db, "signed_in", target_id=a["id"], home=home)

    assert payload["ok"] is False
    assert "b@x.com" in payload["message"] and "a@x.com" in payload["message"]
    assert _token(slot_auth_path(a["id"], home)) == "old"
    assert _token(home / "auth.json") == "old"
    assert not li.login_dir(home).exists()


def test_an_unstored_terminal_login_is_never_overwritten(db, home):
    """A usable live login that jacked does not know holds the only copy of
    its rotated refresh token, so a new account is stored beside it."""
    _write(home / "auth.json", _auth("t@x.com", "u-t", token="terminal"))
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("n@x.com", "u-n", token="n"))

    stored = li.store_signed_in_login(db, home=home)

    assert stored["created"] is True and stored["live"] is False
    assert _token(home / "auth.json") == "terminal"
    assert db.get_active_account_id("grok") is None


def test_adding_a_known_account_again_updates_its_row(db, home):
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="old"))
    a = import_grok_account(db, home=home, make_active=True)
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("a@x.com", "u-a", token="new"))

    stored = li.store_signed_in_login(db, home=home)

    assert stored["created"] is False and stored["account"]["id"] == a["id"]
    assert len([r for r in db.list_accounts() if r["provider"] == "grok"]) == 1


def test_a_sign_in_that_wrote_no_login_is_refused(db, home):
    li.prepare_login_env(home)
    with pytest.raises(GrokAccountError):
        li.store_signed_in_login(db, home=home)


def test_a_sign_in_refuses_when_the_live_login_cannot_be_read(db, home):
    """A partial live file is not "nobody signed in". Writing over it can lose
    the only copy of a rotated refresh token."""
    (home).mkdir(parents=True, exist_ok=True)
    (home / "auth.json").write_text('{"x": {"email": "t@', encoding="utf-8")
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("n@x.com", "u-n", token="n"))

    with pytest.raises(GrokAccountError, match="could not be read"):
        li.store_signed_in_login(db, home=home)

    assert (home / "auth.json").read_text(encoding="utf-8") == '{"x": {"email": "t@'
    assert [r for r in db.list_accounts(include_inactive=True) if r["provider"] == "grok"] == []


def test_a_storage_failure_is_reported_to_the_dashboard(db, home, monkeypatch):
    """An OSError or a database error after a good `grok login` must still
    reach the dashboard, and the private home must still go."""
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("n@x.com", "u-n"))

    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(li, "store_signed_in_login", _boom)
    payload = li.finish_login(db, "signed_in", home=home)

    assert payload["ok"] is False and payload["provider"] == "grok"
    assert payload["message"] == (
        "jacked could not store the Grok sign-in. Check the jacked log, "
        "then try the sign-in again."
    )
    assert not li.login_dir(home).exists()


def test_each_sign_in_has_its_own_private_home(home):
    """Two sign-ins never remove the private home of each other."""
    first = li.new_login_name()
    second = li.new_login_name()
    assert first != second
    env1, _ = li.prepare_login_env(home, first)
    _write(Path(env1["GROK_HOME"]) / "auth.json", _auth("a@x.com", "u-a"))
    env2, _ = li.prepare_login_env(home, second)
    assert (Path(env1["GROK_HOME"]) / "auth.json").exists()
    assert Path(env1["GROK_HOME"]) != Path(env2["GROK_HOME"])
    li.discard_login_dir(home, second)
    assert (Path(env1["GROK_HOME"]) / "auth.json").exists()


def test_a_sign_in_home_left_by_a_crash_is_removed_later(home):
    import os
    import time

    stale = li.login_dir(home, li.new_login_name())
    stale.mkdir(parents=True)
    (stale / "auth.json").write_text("{}")
    old = time.time() - 2 * 24 * 3600
    os.utime(stale, (old, old))
    fresh = li.login_dir(home, li.new_login_name(li.CLI))
    fresh.mkdir(parents=True)

    li.prepare_login_env(home, li.new_login_name())

    assert not stale.exists()
    assert fresh.exists()


def test_add_account_enables_a_disabled_account_again(db, home):
    """Add Account puts the account back in the pool, as the import does."""
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="old"))
    a = import_grok_account(db, home=home, make_active=True)
    db.update_account(a["id"], is_active=0)
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("a@x.com", "u-a", token="new"))

    stored = li.store_signed_in_login(db, home=home)

    assert stored["account"]["id"] == a["id"] and stored["created"] is False
    assert db.get_account(a["id"])["is_active"]
    assert _token(slot_auth_path(a["id"], home)) == "new"


def test_re_auth_keeps_a_disabled_account_disabled(db, home):
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="old"))
    a = import_grok_account(db, home=home, make_active=True)
    db.update_account(a["id"], is_active=0)
    li.prepare_login_env(home)
    _write(li.login_dir(home) / "auth.json", _auth("a@x.com", "u-a", token="new"))

    li.store_signed_in_login(db, target_id=a["id"], home=home)

    assert not db.get_account(a["id"])["is_active"]
