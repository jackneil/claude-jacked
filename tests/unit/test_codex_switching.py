"""M6: guardrailed Codex account switching.

Covers the in-place active swap (capture the outgoing live auth.json FIRST,
write the target field-complete with no field loss, refuse a stale-snapshot
replay, set the per-provider active account, signal restart) and the
per-account CODEX_HOME slot/home + launch env.
"""

import base64
import json
import os

import pytest

from jacked.codex import switching as sw
from jacked.codex.switching import CodexSwapError
from jacked.web.database import Database
from tests._platform import requires_posix_file_modes


def _b64url(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).rstrip(b"=").decode()


def _jwt(account_id, email="dev@example.com"):
    h = _b64url({"alg": "RS256", "typ": "JWT"})
    p = _b64url({
        "email": email,
        "exp": 9999999999,
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "pro",
        },
    })
    s = base64.urlsafe_b64encode(b"sig").rstrip(b"=").decode()
    return f"{h}.{p}.{s}"


def _auth(account_id, email="dev@example.com", last_refresh="2026-06-27T00:00:00Z",
          api_key=None, extra=None):
    d = {
        "OPENAI_API_KEY": api_key,
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": _jwt(account_id, email),
            "access_token": f"access-{account_id}",
            "refresh_token": f"refresh-{account_id}",
            "account_id": account_id,
        },
        "last_refresh": last_refresh,
    }
    if extra:
        d.update(extra)
    return d


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


# --------------------------------------------------------------------------
# seed / slots
# --------------------------------------------------------------------------

def test_seed_codex_slot_captures_root(tmp_path):
    base = tmp_path / ".codex"
    _write(base / "auth.json", _auth("acct-live"))
    assert sw.seed_codex_slot(7, base) is True
    slot = json.loads(sw.codex_slot_auth_path(7, base).read_text())
    assert slot["tokens"]["account_id"] == "acct-live"


def test_seed_codex_slot_no_root_returns_false(tmp_path):
    base = tmp_path / ".codex"
    base.mkdir()
    assert sw.seed_codex_slot(7, base) is False


def test_swap_lock_uses_non_signalling_process_probe(tmp_path, monkeypatch):
    base = tmp_path / ".codex"
    lock_dir = base / ".jacked-codex-swap.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text(str(os.getpid()))
    observed = []
    monkeypatch.setattr(sw, "process_liveness", lambda pid: observed.append(pid) or True)
    monkeypatch.setattr(sw.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        sw.os,
        "kill",
        lambda *_args: pytest.fail("lock liveness checks must never signal a process"),
    )

    with sw._codex_swap_lock(base, retries=1) as acquired:
        assert acquired is False

    assert observed == [os.getpid()]


def test_swap_lock_preserves_owner_when_liveness_is_unknown(tmp_path, monkeypatch):
    base = tmp_path / ".codex"
    lock_dir = base / ".jacked-codex-swap.lock"
    lock_dir.mkdir(parents=True)
    (lock_dir / "pid").write_text("4242")
    monkeypatch.setattr(sw, "process_liveness", lambda _pid: None)
    monkeypatch.setattr(sw.time, "sleep", lambda _delay: None)

    with sw._codex_swap_lock(base, retries=1) as acquired:
        assert acquired is False

    assert lock_dir.exists()


# --------------------------------------------------------------------------
# swap — the guardrails
# --------------------------------------------------------------------------

def test_swap_field_complete_and_captures_outgoing(tmp_path, db):
    base = tmp_path / ".codex"
    # B is live; A is a stored slot. Both are known Codex accounts. Switch to A.
    a = db.create_account("a@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="acct-A")
    b = db.create_account("b@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="acct-B")
    b_live = _auth("acct-B", email="b@x.com", last_refresh="2026-06-28T10:00:00Z")
    a_slot = _auth("acct-A", email="a@x.com", last_refresh="2026-06-28T09:00:00Z",
                   api_key=None, extra={"custom_field": "keepme"})
    _write(base / "auth.json", b_live)
    _write(sw.codex_slot_auth_path(a["id"], base), a_slot)
    db.set_active_account_id(b["id"], provider="codex")

    result = sw.swap_codex_account(db, a["id"], base=base)

    # Root now holds A, field-complete (every key preserved, incl. the extra one).
    root = json.loads((base / "auth.json").read_text())
    assert root == a_slot
    assert root["custom_field"] == "keepme"
    assert root["auth_mode"] == "chatgpt"
    # Outgoing (B) was captured into its slot FIRST (resolved from the live file).
    b_captured = json.loads(sw.codex_slot_auth_path(b["id"], base).read_text())
    assert b_captured == b_live
    # Bookkeeping
    assert result.captured_outgoing is True
    assert result.restart_required is True
    assert result.outgoing_id == b["id"]
    assert db.get_active_account_id("codex") == a["id"]
    assert db.get_active_account_id("claude") is None  # provider-isolated


def test_swap_refuses_stale_replay_same_account(tmp_path, db):
    base = tmp_path / ".codex"
    # Live root is account A @ T2 (newer); the slot is account A @ T1 (older).
    live = _auth("acct-A", last_refresh="2026-06-28T12:00:00Z")
    stale_slot = _auth("acct-A", last_refresh="2026-06-28T08:00:00Z")
    _write(base / "auth.json", live)
    _write(sw.codex_slot_auth_path(1, base), stale_slot)
    db.set_active_account_id(1, provider="codex")

    with pytest.raises(CodexSwapError, match="stale"):
        sw.swap_codex_account(db, 1, base=base)
    # Root is untouched (still the newer live one).
    assert json.loads((base / "auth.json").read_text())["last_refresh"] == "2026-06-28T12:00:00Z"


def test_swap_missing_slot_raises(tmp_path, db):
    base = tmp_path / ".codex"
    _write(base / "auth.json", _auth("acct-B"))
    with pytest.raises(CodexSwapError):
        sw.swap_codex_account(db, 99, base=base)


def test_swap_refuses_incomplete_slot(tmp_path, db):
    base = tmp_path / ".codex"
    _write(base / "auth.json", _auth("acct-B"))
    # Slot missing auth_mode → would break Codex.
    _write(sw.codex_slot_auth_path(1, base), {"tokens": {"id_token": _jwt("acct-A")}})
    with pytest.raises(CodexSwapError, match="auth_mode"):
        sw.swap_codex_account(db, 1, base=base)


def test_swap_different_account_not_treated_as_stale(tmp_path, db):
    base = tmp_path / ".codex"
    # Live A @ T2; target B @ T1 — older, but DIFFERENT account → allowed.
    _write(base / "auth.json", _auth("acct-A", last_refresh="2026-06-28T12:00:00Z"))
    _write(sw.codex_slot_auth_path(1, base),
           _auth("acct-B", last_refresh="2026-06-28T08:00:00Z"))
    db.set_active_account_id(2, provider="codex")
    result = sw.swap_codex_account(db, 1, base=base)
    assert result.ok
    assert json.loads((base / "auth.json").read_text())["tokens"]["account_id"] == "acct-B"


def test_swap_captures_to_live_root_owner_not_tracked_pointer(tmp_path, db):
    """Hardening: capture the live tokens to the slot of the live auth.json's
    TRUE owner (e.g. after an out-of-band `codex login`), not jacked's possibly
    stale tracked pointer."""
    base = tmp_path / ".codex"
    a = db.create_account("a@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="acct-A")
    z = db.create_account("z@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="acct-Z")
    _write(base / "auth.json", _auth("acct-Z", email="z@x.com"))   # live root = Z
    _write(sw.codex_slot_auth_path(a["id"], base), _auth("acct-A", email="a@x.com"))
    db.set_active_account_id(999, provider="codex")                 # stale pointer

    result = sw.swap_codex_account(db, a["id"], base=base)
    # Z's live tokens were filed to Z's slot (its true owner), not slot 999.
    z_slot = json.loads(sw.codex_slot_auth_path(z["id"], base).read_text())
    assert z_slot["tokens"]["account_id"] == "acct-Z"
    assert not sw.codex_slot_auth_path(999, base).exists()
    assert result.outgoing_id == z["id"]


def test_swap_captures_empty_org_outgoing_by_email(tmp_path, db):
    """Capture-first must still fire when the outgoing account has an empty org
    sentinel (no chatgpt_account_id) and the tracked pointer is unset — resolved
    by EMAIL, not org. (Regression for the /dcr empty-org token-loss finding.)"""
    base = tmp_path / ".codex"
    o = db.create_account("o@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="")   # outgoing, empty org
    db.create_account("a@x.com", "t", 9999999999, provider="codex",
                      organization_uuid="acct-A")
    a_id = [x["id"] for x in db.list_accounts() if x["email"] == "a@x.com"][0]
    _write(base / "auth.json", _auth("", email="o@x.com"))  # live root = O, empty org
    _write(sw.codex_slot_auth_path(a_id, base), _auth("acct-A", email="a@x.com"))
    # NOTE: no set_active_account_id — the tracked pointer is None on purpose.

    result = sw.swap_codex_account(db, a_id, base=base)
    assert result.captured_outgoing is True
    assert result.outgoing_id == o["id"]
    o_slot = json.loads(sw.codex_slot_auth_path(o["id"], base).read_text())
    assert o_slot["tokens"]["account_id"] == ""  # the empty-org account's live tokens


def test_swap_skips_capture_for_unknown_live_account(tmp_path, db):
    """If the live root belongs to an account jacked doesn't know (out-of-band
    login), capture is SKIPPED — it must not clobber a tracked account's slot
    with the stranger's tokens. (Regression for the /dcr Wave-2 finding.)"""
    base = tmp_path / ".codex"
    a = db.create_account("a@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="acct-A")
    t = db.create_account("t@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="acct-T")
    t_good = _auth("acct-T", email="t@x.com")
    _write(sw.codex_slot_auth_path(t["id"], base), t_good)
    _write(sw.codex_slot_auth_path(a["id"], base), _auth("acct-A", email="a@x.com"))
    # Live root is a stranger not in the DB; the tracked pointer wrongly says T.
    _write(base / "auth.json", _auth("acct-STRANGER", email="stranger@x.com"))
    db.set_active_account_id(t["id"], provider="codex")

    result = sw.swap_codex_account(db, a["id"], base=base)
    assert result.captured_outgoing is False
    assert result.outgoing_id is None
    # T's slot is intact — NOT clobbered with the stranger's tokens.
    assert json.loads(sw.codex_slot_auth_path(t["id"], base).read_text()) == t_good


@requires_posix_file_modes
def test_accounts_container_locked_0700(tmp_path):
    """Hardening: the accounts/ container (created at umask by mkdir) is 0700."""
    import os
    import stat

    base = tmp_path / ".codex"
    _write(base / "auth.json", _auth("acct-X"))
    sw.seed_codex_slot(5, base)
    container = base / "accounts"
    assert stat.S_IMODE(os.stat(container).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(container / "5").st_mode) == 0o700


def test_swap_forces_file_storage(tmp_path, db):
    base = tmp_path / ".codex"
    (base).mkdir(parents=True)
    (base / "config.toml").write_text('cli_auth_credentials_store = "keyring"\n')
    _write(base / "auth.json", _auth("acct-B"))
    _write(sw.codex_slot_auth_path(1, base), _auth("acct-A"))
    db.set_active_account_id(2, provider="codex")
    sw.swap_codex_account(db, 1, base=base)
    from jacked.codex.credentials import credential_store_mode
    assert credential_store_mode(base) == "file"


# --------------------------------------------------------------------------
# per-account CODEX_HOME launch
# --------------------------------------------------------------------------

def test_prepare_account_home_and_launch_env(tmp_path):
    base = tmp_path / ".codex"
    _write(sw.codex_slot_auth_path(1, base), _auth("acct-A"))
    env = sw.build_codex_launch_env(1, base=base, env={})
    assert env["CODEX_HOME"] == str(sw.codex_account_home(1, base))
    # the per-account home has the account's auth.json + a file-store config
    assert sw.codex_slot_auth_path(1, base).exists()
    from jacked.codex.credentials import credential_store_mode
    assert credential_store_mode(sw.codex_account_home(1, base)) == "file"


def test_prepare_account_home_missing_creds_raises(tmp_path):
    base = tmp_path / ".codex"
    base.mkdir()
    with pytest.raises(CodexSwapError):
        sw.prepare_codex_account_home(5, base=base)


# --------------------------------------------------------------------------
# import seeds the slot (M3 + M6 integration)
# --------------------------------------------------------------------------

def test_import_seeds_codex_slot(tmp_path, db, monkeypatch):
    from jacked.codex.accounts import import_codex_account

    base = tmp_path / ".codex"
    _write(base / "auth.json", _auth("acct-X", email="x@example.com"))
    monkeypatch.setenv("CODEX_HOME", str(base))
    acct = import_codex_account(db)
    # the new account's slot now holds the live auth.json
    assert sw.codex_slot_auth_path(acct["id"], base).exists()
    slot = json.loads(sw.codex_slot_auth_path(acct["id"], base).read_text())
    assert slot["tokens"]["account_id"] == "acct-X"


# --------------------------------------------------------------------------
# /accounts/{id}/use route dispatches Codex to the guardrailed swap
# --------------------------------------------------------------------------

def test_use_account_route_swaps_codex(tmp_path, db, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from jacked.api.routes.auth import router

    base = tmp_path / ".codex"
    _write(base / "auth.json", _auth("acct-B", email="b@x.com"))
    monkeypatch.setenv("CODEX_HOME", str(base))

    # account 1 (A) is the target with a stored slot; account 2 (B) is live/active
    a = db.create_account("a@x.com", "atok", 9999999999, provider="codex",
                          organization_uuid="acct-A")
    _write(sw.codex_slot_auth_path(a["id"], base), _auth("acct-A", email="a@x.com"))
    b = db.create_account("b@x.com", "btok", 9999999999, provider="codex",
                          organization_uuid="acct-B")
    db.set_active_account_id(b["id"], provider="codex")

    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    client = TestClient(app)

    resp = client.post(f"/api/auth/accounts/{a['id']}/use")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "codex"
    assert body["restart_required"] is True
    assert db.get_active_account_id("codex") == a["id"]
    # root now holds account A
    root = json.loads((base / "auth.json").read_text())
    assert root["tokens"]["account_id"] == "acct-A"
