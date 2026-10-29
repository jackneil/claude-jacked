"""jacked.web.auth and jacked.web.oauth run their jacked.db work off the loop (#162).

Each test records ``threading.get_ident()`` inside a fake DB method and
compares it with the event loop's thread. The cc lineage tests also prove
the read-check-write runs under ``CC_LINEAGE_WRITE_LOCK``, which keeps an
OAuth store of a new cc lineage out of the gap now that both sides run in
worker threads.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx

from jacked.web import auth, oauth
from jacked.web.oauth import CC_LINEAGE_WRITE_LOCK, OAuthFlow


class _RecordingDB:
    """Records the thread of every call, by method name."""

    def __init__(self, account: dict | None = None):
        self.account = account
        self.threads: dict[str, list[int]] = {}
        self.lock_held: dict[str, list[bool]] = {}

    def _note(self, name: str) -> None:
        self.threads.setdefault(name, []).append(threading.get_ident())
        self.lock_held.setdefault(name, []).append(CC_LINEAGE_WRITE_LOCK.locked())

    def get_account(self, account_id):
        self._note("get_account")
        return dict(self.account) if self.account else None

    def update_account(self, account_id, **fields):
        self._note("update_account")
        return dict(self.account or {}, **fields)

    def list_accounts(self, include_inactive=False):
        self._note("list_accounts")
        return []

    def list_stuck_checking_accounts(self, threshold_seconds=120):
        self._note("list_stuck_checking_accounts")
        return [{"id": 7, "updated_at": None}]

    def reset_stuck_checking(self, account_id, threshold_seconds=120, reason=""):
        self._note("reset_stuck_checking")
        return 1

    def update_account_usage_cache(self, account_id, **fields):
        self._note("update_account_usage_cache")

    def clear_account_errors(self, account_id):
        self._note("clear_account_errors")

    def record_account_error(self, account_id, message, increment_failures=True):
        self._note("record_account_error")


async def _drain_background_tasks():
    """Cancel tasks the code under test spawned, including their cleanup sleep."""
    current = asyncio.current_task()
    for _ in range(10):
        pending = [t for t in asyncio.all_tasks() if t is not current and not t.done()]
        if not pending:
            return
        for task in pending:
            task.cancel()
        await asyncio.sleep(0)


def _run(coro_fn):
    """Run ``coro_fn()`` and return (its result, the loop thread id)."""

    async def main():
        try:
            return await coro_fn(), threading.get_ident()
        finally:
            await _drain_background_tasks()

    return asyncio.run(main())


def _patch_http(monkeypatch, handler) -> None:
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        return real(*args, transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(auth.httpx, "AsyncClient", factory)


# ---------------------------------------------------------------------------
# Background loops
# ---------------------------------------------------------------------------

def test_refresh_all_expiring_tokens_lists_accounts_off_the_loop(monkeypatch):
    from jacked.api import credential_helpers

    monkeypatch.setattr(credential_helpers, "read_active_account_id", lambda: None)
    monkeypatch.setattr(credential_helpers, "live_holder_account_ids", lambda db: set())
    db = _RecordingDB()

    counts, loop_tid = _run(lambda: auth.refresh_all_expiring_tokens(db=db))

    assert counts["checked"] == 0
    assert db.threads["list_accounts"] and loop_tid not in db.threads["list_accounts"]


def test_heal_invalid_accounts_opens_and_lists_the_db_off_the_loop(monkeypatch):
    db = _RecordingDB()
    opened: list[int] = []

    def fake_database():
        opened.append(threading.get_ident())
        return db

    monkeypatch.setattr(auth, "Database", fake_database)

    result, loop_tid = _run(auth.heal_invalid_accounts)

    assert result["checked"] == 0
    assert opened and loop_tid not in opened
    assert loop_tid not in db.threads["list_accounts"]


def test_stuck_checking_watchdog_scans_and_resets_in_one_worker_hop():
    db = _RecordingDB()

    reset, loop_tid = _run(lambda: auth.reset_stale_checking_accounts(db))

    assert reset == 1
    scan = db.threads["list_stuck_checking_accounts"]
    assert loop_tid not in scan
    assert db.threads["reset_stuck_checking"] == scan


# ---------------------------------------------------------------------------
# Async helpers
# ---------------------------------------------------------------------------

def test_fetch_usage_reads_and_stores_off_the_loop(monkeypatch):
    account_id = 9101
    db = _RecordingDB({
        "id": account_id, "provider": "claude", "access_token": "tok",
        "usage_cached_at": 0,
    })
    _patch_http(monkeypatch, lambda request: httpx.Response(
        200, json={"five_hour": {"utilization": 5, "resets_at": "R"}},
    ))
    # Skip the piggybacked profile fetch; start with clean pacing state.
    monkeypatch.setitem(auth._profile_refreshed_at, account_id, time.time())
    auth._account_usage_state.pop(account_id, None)
    try:
        data, loop_tid = _run(lambda: auth.fetch_usage(account_id, db))
    finally:
        auth._account_usage_state.pop(account_id, None)

    assert data["five_hour"]["utilization"] == 5
    for name in ("get_account", "update_account_usage_cache", "clear_account_errors"):
        assert loop_tid not in db.threads[name], name
    assert db.threads["update_account_usage_cache"] == db.threads["clear_account_errors"]


def test_fetch_usage_records_an_http_error_off_the_loop(monkeypatch):
    account_id = 9102
    db = _RecordingDB({
        "id": account_id, "provider": "claude", "access_token": "tok",
        "usage_cached_at": 0,
    })
    _patch_http(monkeypatch, lambda request: httpx.Response(500))
    auth._account_usage_state.pop(account_id, None)
    try:
        data, loop_tid = _run(lambda: auth.fetch_usage(account_id, db))
    finally:
        auth._account_usage_state.pop(account_id, None)

    assert data is None
    assert loop_tid not in db.threads["record_account_error"]


def test_cc_chain_cleanup_rereads_and_writes_under_the_lineage_lock(monkeypatch):
    monkeypatch.setattr(auth, "_live_recovery_payload", lambda account_id, account: None)
    db = _RecordingDB({"id": 1, "cc_refresh_token": "dead", "cc_access_token": "old"})

    result, loop_tid = _run(lambda: auth._cc_chain_died(1, db, db.account, "dead"))

    assert result.error == "invalid_grant"
    assert loop_tid not in db.threads["get_account"]
    assert db.threads["update_account"] == db.threads["get_account"]
    assert db.lock_held["get_account"] == [True]
    assert db.lock_held["update_account"] == [True]
    assert not CC_LINEAGE_WRITE_LOCK.locked()


def test_validate_account_reads_and_marks_checking_off_the_loop(monkeypatch):
    db = _RecordingDB({"id": 3, "provider": "claude", "access_token": "tok"})
    _patch_http(monkeypatch, lambda request: httpx.Response(500))

    verdict, loop_tid = _run(lambda: auth.validate_account(3, db))

    assert verdict == {"valid": False, "error": "Unexpected HTTP 500"}
    assert loop_tid not in db.threads["get_account"]
    # The "checking" mark and the final "unknown" mark.
    assert len(db.threads["update_account"]) == 2
    assert loop_tid not in db.threads["update_account"]


# ---------------------------------------------------------------------------
# jacked.web.oauth
# ---------------------------------------------------------------------------

def test_oauth_flow_constructor_reads_no_db_and_start_loads_the_target_off_the_loop():
    db = _RecordingDB({"id": 5, "email": "j@x.com", "organization_name": "Acme"})

    async def scenario():
        flow = OAuthFlow(db, purpose="primary", target_account_id=5, manual=True)
        assert "get_account" not in db.threads
        result = await flow.start()
        oauth._active_flows.pop(flow.flow_id, None)
        return result

    result, loop_tid = _run(scenario)

    assert result["target_email"] == "j@x.com"
    assert loop_tid not in db.threads["get_account"]


def test_reopen_browser_launches_off_the_loop(monkeypatch):
    from jacked.web import browser_launch

    launched: list[int] = []

    def fake_open(url, account, db):
        launched.append(threading.get_ident())
        return SimpleNamespace(mode="default", browser=None)

    monkeypatch.setattr(browser_launch, "open_auth_url", fake_open)
    flow = OAuthFlow(_RecordingDB(), purpose="primary")
    flow._auth_url = "https://example.invalid/authorize"

    status, loop_tid = _run(flow.reopen_browser)

    assert "reopen_error" not in status
    assert launched and loop_tid not in launched


def test_complete_auth_stores_under_the_lineage_lock_off_the_loop():
    flow = OAuthFlow(_RecordingDB(), purpose="claude_code", allow_credential_activation=False)
    seen: dict[str, tuple[int, bool]] = {}

    def fake_store(tokens, profile, usage):
        seen["store"] = (threading.get_ident(), CC_LINEAGE_WRITE_LOCK.locked())
        return {"id": 1, "email": "j@x.com"}

    def fake_should(account):
        seen["decide"] = (threading.get_ident(), CC_LINEAGE_WRITE_LOCK.locked())
        return False

    flow._exchange_code = AsyncMock(return_value={"access_token": "a"})
    flow._store_account = fake_store
    flow._should_become_active = fake_should

    response, loop_tid = _run(lambda: flow._complete_auth("code"))

    assert response == {"account_id": 1, "email": "j@x.com"}
    store_tid, store_locked = seen["store"]
    decide_tid, decide_locked = seen["decide"]
    assert store_tid != loop_tid and store_locked
    assert decide_tid == store_tid and not decide_locked
