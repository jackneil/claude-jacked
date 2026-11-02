"""#149i: a refused Codex swap changes nothing on disk.

``swap_codex_account`` used to force ``cli_auth_credentials_store = "file"``
into config.toml before it checked anything that could refuse the swap. So a
keyring user, or a swap that a later guard refused, still got a rewritten
config. These tests pin the order: every refusal runs first, and a refusal
leaves config.toml (bytes and mtime) and every auth.json unchanged.

They also pin the stale-replay guard for a snapshot whose age is not provable:
an equal or a missing ``last_refresh`` with a different refresh token is
refused, because the replay can put a used refresh token over the live one.
"""

import json
import os
from contextlib import contextmanager

import pytest

from jacked.codex import swap_guards as guards
from jacked.codex import switching as sw
from jacked.codex.credentials import credential_store_mode
from jacked.codex.switching import CodexSwapError
from jacked.web.database import Database
from tests.unit.test_codex_switching import _auth, _write

# A fixed past mtime, so any rewrite of config.toml moves it.
_OLD_NS = 1_000_000_000 * 1_000_000_000
_T1 = "2026-06-28T08:00:00Z"
_T2 = "2026-06-28T12:00:00Z"


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "jacked.db"))
    yield d
    d.close()


def _config(base, mode):
    """Write a config.toml that sets ``mode``, pin its mtime, return its path."""
    cfg = base / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(f'model = "gpt-5"\ncli_auth_credentials_store = "{mode}"\n')
    os.utime(cfg, ns=(_OLD_NS, _OLD_NS))
    return cfg


def _snapshot(path):
    """``(bytes, mtime_ns)`` of ``path``, or None when the file does not exist."""
    if not path.exists():
        return None
    return path.read_bytes(), path.stat().st_mtime_ns


def _blob(account, refresh_token, last_refresh):
    """An auth.json for ``account``; ``last_refresh=None`` drops the key."""
    data = _auth(account)
    data["tokens"]["refresh_token"] = refresh_token
    if last_refresh is None:
        del data["last_refresh"]
    else:
        data["last_refresh"] = last_refresh
    return data


@contextmanager
def _busy_lock(_base, retries=340):
    yield False


# --------------------------------------------------------------------------
# Part A: the store mode is checked before any write
# --------------------------------------------------------------------------


@pytest.mark.parametrize("root_present", [True, False], ids=["root", "no-root"])
def test_keyring_store_refuses_and_leaves_config_untouched(tmp_path, db, root_present):
    base = tmp_path / ".codex"
    cfg = _config(base, "keyring")
    if root_present:
        _write(base / "auth.json", _auth("acct-B"))
    _write(sw.codex_slot_auth_path(1, base), _auth("acct-A"))
    before, root_before = _snapshot(cfg), _snapshot(base / "auth.json")

    with pytest.raises(CodexSwapError, match="OS keyring"):
        sw.swap_codex_account(db, 1, base=base)

    assert _snapshot(cfg) == before
    assert credential_store_mode(base) == "keyring"
    assert _snapshot(base / "auth.json") == root_before
    assert db.get_active_account_id("codex") is None


@pytest.mark.parametrize("root", ["absent", "no-credential", "corrupt"])
@pytest.mark.parametrize("explicit", [True, False], ids=["auto-key", "no-config"])
def test_auto_store_without_a_file_credential_refuses(tmp_path, db, root, explicit):
    """Under "auto" with no credential in auth.json, the live credential can
    be in the OS keyring. Forcing "file" would switch the user's store."""
    base = tmp_path / ".codex"
    base.mkdir(parents=True)
    cfg = _config(base, "auto") if explicit else base / "config.toml"
    if root == "no-credential":
        _write(base / "auth.json", {"auth_mode": "chatgpt"})
    elif root == "corrupt":
        (base / "auth.json").write_text("{not json")
    _write(sw.codex_slot_auth_path(1, base), _auth("acct-A"))
    before, root_before = _snapshot(cfg), _snapshot(base / "auth.json")

    with pytest.raises(CodexSwapError, match='store mode "auto"'):
        sw.swap_codex_account(db, 1, base=base)

    assert _snapshot(cfg) == before  # None stays None: no config.toml appears
    assert _snapshot(base / "auth.json") == root_before


def test_file_store_without_a_live_root_still_switches(tmp_path, db):
    """"file" is what jacked switches; a logged-out root is not a refusal."""
    base = tmp_path / ".codex"
    cfg = _config(base, "file")
    _write(sw.codex_slot_auth_path(1, base), _auth("acct-A"))
    before = _snapshot(cfg)

    result = sw.swap_codex_account(db, 1, base=base)

    assert result.ok
    assert json.loads((base / "auth.json").read_text())["tokens"]["account_id"] == "acct-A"
    assert _snapshot(cfg) == before  # already "file": nothing to rewrite


@pytest.mark.parametrize(
    "refusal",
    ["missing_slot", "incomplete_slot", "lock_busy", "older_snapshot", "not_provably_newer"],
)
def test_every_refusal_runs_before_config_is_rewritten(tmp_path, db, monkeypatch, refusal):
    """"auto" with a live auth.json passes the store check, so the swap would
    force "file". A later refusal must still leave config.toml as it was."""
    base = tmp_path / ".codex"
    cfg = _config(base, "auto")
    live = _blob("acct-A", "refresh-live", _T2)
    _write(base / "auth.json", live)
    slot = sw.codex_slot_auth_path(1, base)
    if refusal == "incomplete_slot":
        _write(slot, {"tokens": {"refresh_token": "x"}})
    elif refusal == "lock_busy":
        _write(slot, _auth("acct-B"))
        monkeypatch.setattr(sw, "_codex_swap_lock", _busy_lock)
    elif refusal == "older_snapshot":
        _write(slot, _blob("acct-A", "refresh-old", _T1))
    elif refusal == "not_provably_newer":
        _write(slot, _blob("acct-A", "refresh-other", _T2))
    before, root_before = _snapshot(cfg), _snapshot(base / "auth.json")

    with pytest.raises(CodexSwapError):
        sw.swap_codex_account(db, 1, base=base)

    assert _snapshot(cfg) == before
    assert credential_store_mode(base) == "auto"
    assert _snapshot(base / "auth.json") == root_before


@pytest.mark.parametrize("content", ["{not json", "", "[1, 2]"], ids=["corrupt", "empty", "not-a-dict"])
def test_an_unreadable_live_root_refuses_and_writes_nothing(tmp_path, db, content):
    """A root that exists but does not parse (a Codex write in progress, or a
    damaged file) leaves no tokens to save and no time to check. Writing over
    it blind could lose a rotated token, so the swap stops."""
    base = tmp_path / ".codex"
    cfg = _config(base, "file")
    (base / "auth.json").write_text(content)
    _write(sw.codex_slot_auth_path(1, base), _auth("acct-A"))
    before, root_before = _snapshot(cfg), _snapshot(base / "auth.json")

    with pytest.raises(CodexSwapError, match="not readable"):
        sw.swap_codex_account(db, 1, base=base)

    assert _snapshot(cfg) == before
    assert _snapshot(base / "auth.json") == root_before
    assert db.get_active_account_id("codex") is None


def test_refusals_log_why_without_values(tmp_path, db, caplog):
    base = tmp_path / ".codex"
    _config(base, "keyring")
    _write(base / "auth.json", _auth("acct-B", email="who@example.com"))
    _write(sw.codex_slot_auth_path(7, base), _auth("acct-A", email="me@example.com"))

    with caplog.at_level("WARNING", logger="jacked.codex.switching"):
        with pytest.raises(CodexSwapError):
            sw.swap_codex_account(db, 7, base=base)

    assert "Codex swap to account 7 refused: store_mode=keyring" in caplog.text
    for value in ("who@example.com", "me@example.com", "refresh-acct-A", "access-acct-B"):
        assert value not in caplog.text


# --------------------------------------------------------------------------
# Part B: a snapshot that is not provably newer cannot replace a different token
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_ts, live_ts",
    [(_T2, _T2), (None, _T2), (_T2, None), (None, None), ("not a time", _T2)],
    ids=["equal", "target-missing", "live-missing", "both-missing", "target-unparseable"],
)
def test_not_provably_newer_snapshot_with_another_token_is_refused(target_ts, live_ts):
    live = _blob("acct-A", "refresh-live", live_ts)
    target = _blob("acct-A", "refresh-other", target_ts)

    with pytest.raises(CodexSwapError, match="not provably newer"):
        guards.refuse_stale_replay(target, live)


def test_callers_catch_the_class_the_rules_raise():
    """cli.py and the /use route import CodexSwapError from switching."""
    assert sw.CodexSwapError is guards.CodexSwapError is CodexSwapError


def test_strictly_older_snapshot_is_still_refused_as_stale():
    live = _blob("acct-A", "refresh-live", _T2)
    with pytest.raises(CodexSwapError, match="stale"):
        guards.refuse_stale_replay(_blob("acct-A", "refresh-old", _T1), live)


@pytest.mark.parametrize(
    "target, live",
    [
        (_blob("acct-A", "refresh-new", _T2), _blob("acct-A", "refresh-live", _T1)),
        (_blob("acct-A", "refresh-live", _T2), _blob("acct-A", "refresh-live", _T2)),
        (_blob("acct-A", "refresh-live", None), _blob("acct-A", "refresh-live", _T2)),
        (_blob("acct-B", "refresh-b", _T2), _blob("acct-A", "refresh-live", _T2)),
        (_blob("acct-A", "refresh-new", None), None),
    ],
    ids=["strictly-newer", "same-token", "same-token-no-time", "other-account", "no-live"],
)
def test_replay_guard_allows_a_safe_write(target, live):
    guards.refuse_stale_replay(target, live)


def test_mixed_naive_and_aware_timestamps_compare_without_crashing():
    """A hand-written naive timestamp is read as UTC, never a TypeError."""
    live = _blob("acct-A", "refresh-live", "2026-06-28T08:00:00")
    guards.refuse_stale_replay(_blob("acct-A", "refresh-new", _T2), live)
    with pytest.raises(CodexSwapError, match="stale"):
        guards.refuse_stale_replay(_blob("acct-A", "refresh-old", "2026-06-28T07:00:00Z"), live)


def test_swapping_to_the_live_account_from_its_own_snapshot_still_works(tmp_path, db):
    """A slot seeded from the live root is the same snapshot: not a replay."""
    base = tmp_path / ".codex"
    a = db.create_account("a@x.com", "t", 9999999999, provider="codex",
                          organization_uuid="acct-A")
    live = _auth("acct-A", email="a@x.com", last_refresh=_T2)
    _write(base / "auth.json", live)
    assert sw.seed_codex_slot(a["id"], base) is True

    result = sw.swap_codex_account(db, a["id"], base=base)

    assert result.ok
    assert json.loads((base / "auth.json").read_text()) == live


def test_not_provably_newer_refusal_logs_why_without_values(tmp_path, db, caplog):
    base = tmp_path / ".codex"
    live = _blob("acct-A", "refresh-live-secret", _T2)
    _write(base / "auth.json", live)
    _write(sw.codex_slot_auth_path(3, base), _blob("acct-A", "refresh-other-secret", _T2))

    with caplog.at_level("WARNING", logger="jacked.codex.switching"):
        with pytest.raises(CodexSwapError, match="not provably newer"):
            sw.swap_codex_account(db, 3, base=base)

    assert "Codex swap to account 3 refused: snapshot_not_provably_newer" in caplog.text
    assert "last_refresh=equal" in caplog.text
    for value in ("refresh-live-secret", "refresh-other-secret", "dev@example.com", _T2):
        assert value not in caplog.text
