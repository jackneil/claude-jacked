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


# --- A disabled account is still a stored account (CRED-1) ---------------


def _second(home: Path, db, email="grace@example.com", user_id="user-2", token="grace"):
    data = _auth(email, "2026-10-03T00:00:00Z", token=token)
    data["https://auth.x.ai::client"]["user_id"] = user_id
    row = db.create_account(email=email, access_token="grok-managed", expires_at=4102444800,
                            refresh_token=None, organization_uuid=user_id, provider="grok")
    _write(slot_auth_path(row["id"], home), data)
    return row


def test_sync_still_copies_the_live_login_of_a_disabled_account(tmp_path):
    """Disabling an account must not stop the copy of its rotated login. A
    later switch back would replay the spent refresh token."""
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="first"))
    ada = import_grok_account(db, home=home, make_active=True)
    db.update_account(ada["id"], is_active=0)
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T01:00:00Z", token="rotated"))

    assert sync_active_slot_from_live(db, home=home) is True
    slot = json.loads(slot_auth_path(ada["id"], home).read_text(encoding="utf-8"))
    assert slot["https://auth.x.ai::client"]["refresh_token"] == "rotated"


def test_switch_captures_the_live_login_of_a_disabled_account(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="first"))
    ada = import_grok_account(db, home=home, make_active=True)
    grace = _second(home, db)
    db.update_account(ada["id"], is_active=0)
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T01:00:00Z", token="rotated"))

    result = swap_grok_account(db, grace["id"], home=home)

    assert result["outgoing_id"] == ada["id"] and result["captured_outgoing"] is True
    slot = json.loads(slot_auth_path(ada["id"], home).read_text(encoding="utf-8"))
    assert slot["https://auth.x.ai::client"]["refresh_token"] == "rotated"


def test_a_disabled_live_login_is_imported_again_by_add_account(tmp_path):
    """Add Account re-enables a disabled live login through the import, as
    before. The owner lookup reports it as not stored for that reason."""
    from jacked.grok.accounts import live_login_owner

    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z"))
    ada = import_grok_account(db, home=home, make_active=True)
    db.update_account(ada["id"], is_active=0)

    assert live_login_owner(db, home=home) == (True, None)


def test_switch_refuses_to_overwrite_a_live_login_that_jacked_does_not_know(tmp_path):
    """The live file holds the only copy of that login's rotated refresh token."""
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    grace = _second(home, db)
    stranger = _auth("t@example.com", "2026-10-03T00:00:00Z", token="terminal")
    stranger["https://auth.x.ai::client"]["user_id"] = "user-t"
    _write(home / "auth.json", stranger)

    with pytest.raises(GrokAccountError, match="Add this Grok login first"):
        swap_grok_account(db, grace["id"], home=home)

    live = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert live["https://auth.x.ai::client"]["refresh_token"] == "terminal"
    assert db.get_active_account_id(provider="grok") is None


def test_switch_overwrites_a_live_file_that_holds_no_usable_login(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    grace = _second(home, db)
    _write(home / "auth.json", {"https://auth.x.ai::client": {"email": "t@example.com"}})

    swap_grok_account(db, grace["id"], home=home)

    live = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    assert live["https://auth.x.ai::client"]["email"] == "grace@example.com"


# --- An unreadable live file is not "no login" (CRED-6) --------------------


@pytest.mark.parametrize("kind", ["partial", "symlink"])
def test_switch_refuses_when_the_live_login_cannot_be_read(tmp_path, kind):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    grace = _second(home, db)
    live_path = home / "auth.json"
    if kind == "partial":
        live_path.write_text('{"https://auth.x.ai::client": {"email": "ada', encoding="utf-8")
    else:
        real = tmp_path / "elsewhere.json"
        _write(real, _auth("ada@example.com", "2026-10-03T00:00:00Z"))
        live_path.symlink_to(real)
    before = live_path.read_bytes() if kind == "partial" else None

    with pytest.raises(GrokAccountError, match="could not be read"):
        swap_grok_account(db, grace["id"], home=home)

    if kind == "partial":
        assert live_path.read_bytes() == before
    else:
        assert live_path.is_symlink()
    assert db.get_active_account_id(provider="grok") is None


# --- The timer copy never moves a login backward (CRED-2) ------------------


def test_sync_never_puts_an_older_live_login_over_a_newer_slot(tmp_path):
    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z", token="first"))
    ada = import_grok_account(db, home=home, make_active=True)
    _write(slot_auth_path(ada["id"], home),
           _auth("ada@example.com", "2026-10-05T00:00:00Z", token="newer-slot"))

    assert sync_active_slot_from_live(db, home=home) is False
    slot = json.loads(slot_auth_path(ada["id"], home).read_text(encoding="utf-8"))
    assert slot["https://auth.x.ai::client"]["refresh_token"] == "newer-slot"


# --- The switch lock (CRED-5) ----------------------------------------------


def _lock(home: Path) -> Path:
    return home / "jacked-accounts" / ".switch.lockdir"


def test_a_lock_without_a_pid_file_is_not_broken_while_it_is_new(tmp_path, monkeypatch):
    """The holder makes the directory, then writes its pid. A lock seen between
    the two steps belongs to a live switch."""
    from jacked.grok import accounts as ga

    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z"))
    ada = import_grok_account(db, home=home)
    _lock(home).mkdir()
    monkeypatch.setattr(ga, "_LOCK_WAIT_SECONDS", 0.2)

    with pytest.raises(GrokAccountError, match="in progress"):
        swap_grok_account(db, ada["id"], home=home)
    assert _lock(home).exists()


def test_a_lock_without_a_pid_file_is_broken_when_it_is_old(tmp_path, monkeypatch):
    import os
    import time as _time

    from jacked.grok import accounts as ga

    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z"))
    ada = import_grok_account(db, home=home)
    _lock(home).mkdir()
    old = _time.time() - 60
    os.utime(_lock(home), (old, old))
    monkeypatch.setattr(ga, "_LOCK_WAIT_SECONDS", 1.0)

    swap_grok_account(db, ada["id"], home=home)
    assert not _lock(home).exists()


def test_the_lock_is_not_taken_when_its_pid_cannot_be_written(tmp_path, monkeypatch):
    from jacked import pid_lockdir

    home = tmp_path / "grok"
    db = Database(str(tmp_path / "jacked.db"))
    _write(home / "auth.json", _auth("ada@example.com", "2026-10-03T00:00:00Z"))
    ada = import_grok_account(db, home=home)

    def _fail(_path):
        raise OSError("disk full")

    monkeypatch.setattr(pid_lockdir, "_write_pid", _fail)
    with pytest.raises(GrokAccountError, match="switch lock"):
        swap_grok_account(db, ada["id"], home=home)
    assert not _lock(home).exists()
    assert db.get_active_account_id(provider="grok") is None


def test_a_holder_never_removes_a_lock_that_another_process_took(tmp_path):
    from jacked.grok import accounts as ga

    home = tmp_path / "grok"
    with ga._switch_lock(home):
        # Another process broke this lock and took it.
        (_lock(home) / "pid").write_text("999999", encoding="utf-8")
    assert _lock(home).exists()
