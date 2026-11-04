"""Grok accounts: import identity, never store the refresh token, refuse a stale replay."""

import json
from pathlib import Path

import pytest

from jacked.grok.accounts import (
    GrokAccountError,
    import_grok_account,
    slot_auth_path,
    swap_grok_account,
    sync_active_slot_from_live,
)
from jacked.web.database import Database


def _auth(email: str, expires: str, token: str = "refresh-secret") -> dict:
    return {
        "https://auth.x.ai::client": {
            "key": "access-secret",
            "refresh_token": token,
            "expires_at": expires,
            "email": email,
            "user_id": "user-1",
        }
    }


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_import_stores_identity_but_not_the_refresh_token(tmp_path):
    home = tmp_path / "grok"
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z"))
    db = Database(str(tmp_path / "jacked.db"))

    account = import_grok_account(db, home=home)

    assert account["provider"] == "grok"
    assert account["email"] == "ada@example.com"
    assert account["refresh_token"] in (None, "")
    assert account["access_token"] == "grok-managed"
    slot = json.loads(slot_auth_path(account["id"], home).read_text(encoding="utf-8"))
    assert slot["https://auth.x.ai::client"]["refresh_token"] == "refresh-secret"


def test_import_without_a_login_fails(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    with pytest.raises(GrokAccountError):
        import_grok_account(db, home=tmp_path / "empty")


def test_switch_captures_the_outgoing_login_before_it_writes(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="ada-new"))
    ada = import_grok_account(db, home=home, make_active=True)

    # A second account whose slot is a different person.
    grace_auth = _auth("grace@example.com", "2026-10-03T00:00:00Z", token="grace")
    grace_auth["https://auth.x.ai::client"]["user_id"] = "user-2"
    _write(home / "auth.json", grace_auth)
    grace = import_grok_account(db, home=home)

    # Live file is still grace (import copied it but did not change who is live
    # except the second import overwrote the live file with grace). Put ada
    # back as live with a rotated token, then switch to grace.
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-04T00:00:00Z", token="ada-rotated"))
    swap_grok_account(db, grace["id"], home=home)

    captured = json.loads(slot_auth_path(ada["id"], home).read_text(encoding="utf-8"))
    assert captured["https://auth.x.ai::client"]["refresh_token"] == "ada-rotated"
    live = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert live["https://auth.x.ai::client"]["email"] == "grace@example.com"
    row = db.get_account(ada["id"])
    assert row["refresh_token"] in (None, "")


def test_sync_copies_a_rotated_live_file_without_refreshing(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="first"))
    account = import_grok_account(db, home=home, make_active=True)
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T01:00:00Z", token="rotated"))

    assert sync_active_slot_from_live(db, home=home) is True
    slot = json.loads(slot_auth_path(account["id"], home).read_text(encoding="utf-8"))
    assert slot["https://auth.x.ai::client"]["refresh_token"] == "rotated"
    assert db.get_account(account["id"])["refresh_token"] in (None, "")
    assert sync_active_slot_from_live(db, home=home) is False


def test_switch_refuses_a_different_refresh_token_when_expiry_does_not_move(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="old"))
    ada = import_grok_account(db, home=home)
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="rotated"))

    with pytest.raises(GrokAccountError, match="not the live one"):
        swap_grok_account(db, ada["id"], home=home)

    live = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert live["https://auth.x.ai::client"]["refresh_token"] == "rotated"


def test_switch_refuses_an_older_copy_of_the_same_login(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="old"))
    ada = import_grok_account(db, home=home)
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-05T00:00:00Z", token="newer"))

    with pytest.raises(GrokAccountError, match="not the live one"):
        swap_grok_account(db, ada["id"], home=home)

    live = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert live["https://auth.x.ai::client"]["refresh_token"] == "newer"


def test_a_lock_left_by_a_dead_switch_is_broken(tmp_path, monkeypatch):
    """A crash inside the switch leaves the lock directory behind. Without a
    stale-lock rule every later switch waits out its deadline and fails."""
    from jacked.grok import accounts as ga

    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z"))
    ada = import_grok_account(db, home=home)
    lock = home / "jacked-accounts" / ".switch.lockdir"
    lock.mkdir()
    (lock / "pid").write_text("4242")
    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: False)
    monkeypatch.setattr(ga, "_LOCK_WAIT_SECONDS", 1.0)

    swap_grok_account(db, ada["id"], home=home)

    assert not lock.exists()


def test_sync_waits_for_a_switch_in_progress(tmp_path, monkeypatch):
    """The timer copy must not interleave with a switch: an older read written
    after the switch captured a newer file loses the rotated token."""
    from jacked.grok import accounts as ga

    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="first"))
    import_grok_account(db, home=home, make_active=True)
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T01:00:00Z", token="rotated"))
    lock = home / "jacked-accounts" / ".switch.lockdir"
    lock.mkdir()
    (lock / "pid").write_text("4242")
    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: True)
    monkeypatch.setattr(ga, "_LOCK_WAIT_SECONDS", 0.2)

    with pytest.raises(GrokAccountError, match="in progress"):
        sync_active_slot_from_live(db, home=home)


def test_switch_reports_success_like_the_codex_switch(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z"))
    ada = import_grok_account(db, home=home)
    result = swap_grok_account(db, ada["id"], home=home)
    assert result["success"] is True
    assert result["provider"] == "grok"
    assert db.get_active_account_id(provider="grok") == ada["id"]
