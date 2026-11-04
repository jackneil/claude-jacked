"""`jacked grok add|list|switch|remove` and `jacked usage --provider grok`."""

import json
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from jacked import provider_cli
from jacked.cli import main
from jacked.grok.accounts import import_grok_account, slot_auth_path
from jacked.web.database import Database

KEY = "https://auth.x.ai::client"


def _auth(email, user_id, token="r"):
    return {KEY: {"key": "k", "refresh_token": token, "email": email, "user_id": user_id,
                  "expires_at": "2026-12-01T00:00:00Z"}}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / ".grok"
    monkeypatch.setenv("GROK_HOME", str(home))
    db_path = tmp_path / "jacked.db"
    monkeypatch.setattr(provider_cli, "provider_db", lambda: Database(str(db_path)))
    monkeypatch.setattr("jacked.cli._codex_db", lambda: Database(str(db_path)))
    monkeypatch.setattr("jacked.web.browser_launch.PROFILE_ROOT", tmp_path / "profiles")
    return home, db_path


def _db(db_path):
    return Database(str(db_path))


def test_add_imports_a_terminal_login(env):
    home, db_path = env
    _write(home / "auth.json", _auth("t@x.com", "u-t"))
    result = CliRunner().invoke(main, ["grok", "add"])
    assert result.exit_code == 0, result.output
    assert "t@x.com" in result.output
    db = _db(db_path)
    rows = [r for r in db.list_accounts() if r["provider"] == "grok"]
    assert [r["email"] for r in rows] == ["t@x.com"]
    assert db.get_active_account_id("grok") == rows[0]["id"]
    db.close()


def test_add_signs_in_apart_from_the_live_login(env, monkeypatch):
    home, db_path = env
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="live-a"))
    db = _db(db_path)
    a = import_grok_account(db, home=home, make_active=True)
    db.close()
    runs = []

    def _run(argv, env=None, check=False):
        runs.append(argv)
        _write(Path(env["GROK_HOME"]) / "auth.json", _auth("b@x.com", "u-b", token="b"))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr("jacked.findbin.find_bin", lambda name: "/opt/grok")
    monkeypatch.setattr(provider_cli.subprocess, "run", _run)
    result = CliRunner().invoke(main, ["grok", "add"])
    assert result.exit_code == 0, result.output
    assert runs[0][:2] == ["/opt/grok", "login"]
    assert runs[0][2] == "--leader-socket"
    live = json.loads((home / "auth.json").read_text())
    assert live[KEY]["refresh_token"] == "live-a"
    db = _db(db_path)
    assert db.get_active_account_id("grok") == a["id"]
    assert any(r["email"] == "b@x.com" for r in db.list_accounts())
    db.close()
    assert not (home / "jacked-accounts" / ".sign-in-cli").exists()


def test_add_with_no_login_refuses_when_nothing_new_is_live(env):
    result = CliRunner().invoke(main, ["grok", "add", "--no-login"])
    assert result.exit_code != 0
    assert "No new Grok login" in result.output


def test_list_switch_and_remove(env):
    home, db_path = env
    db = _db(db_path)
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="a"))
    a = import_grok_account(db, home=home, make_active=True)
    _write(home / "auth.json", _auth("b@x.com", "u-b", token="b"))
    b = import_grok_account(db, home=home)
    _write(home / "auth.json", _auth("a@x.com", "u-a", token="a"))
    db.close()
    runner = CliRunner()

    listed = runner.invoke(main, ["grok", "list"])
    assert listed.exit_code == 0
    assert "a@x.com" in listed.output and "b@x.com" in listed.output

    switched = runner.invoke(main, ["grok", "switch", str(b["id"])])
    assert switched.exit_code == 0, switched.output
    assert json.loads((home / "auth.json").read_text())[KEY]["email"] == "b@x.com"

    removed = runner.invoke(main, ["grok", "remove", str(a["id"]), "--yes"])
    assert removed.exit_code == 0, removed.output
    assert not slot_auth_path(a["id"], home).exists()
    db = _db(db_path)
    assert db.get_account(a["id"]) is None
    db.close()

    missing = runner.invoke(main, ["grok", "switch", "999"])
    assert missing.exit_code != 0 and "No Grok account" in missing.output


def test_usage_accepts_the_grok_provider(env, tmp_path, monkeypatch):
    """The generic account listing (`jacked usage`) knows the Grok provider."""
    from jacked import session_ancestry

    home, _ = env
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(session_ancestry, "calling_session_account", lambda db: None)
    db_path = tmp_path / ".claude" / "jacked.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = _db(db_path)
    _write(home / "auth.json", _auth("a@x.com", "u-a"))
    import_grok_account(db, home=home, make_active=True)
    db.create_account("c@x.com", "tok", 4102444800)
    db.close()
    result = CliRunner().invoke(main, ["usage", "--json", "--provider", "grok"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.output)["accounts"]
    assert [(r["provider"], r["email"]) for r in rows] == [("grok", "a@x.com")]
