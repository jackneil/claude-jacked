"""The account routes run their jacked.db work off the event loop (#162).

The guard (test_db_off_loop_guard.py) proves this from the source. These
tests prove it at run time for the two routes that keep coroutine work
between their DB calls: the bulk usage refresh, whose per-account helper
reads and writes the DB between awaits, and the manual account switch.
Each fake DB records ``threading.get_ident()`` per call, and the test
compares it with the loop thread.

Uses asyncio.run() wrappers (project convention, no pytest-asyncio).
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path
from unittest import mock
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jacked.api.routes import auth as routes_auth
from jacked.credentials.models import (
    IdentityAxis,
    ProviderVerificationState,
    SessionActivationState,
    SwitchOutcome,
    SwitchResult,
)
from jacked.web.database import Database


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Fresh bulk-refresh lock per test, and no real credential stores."""
    routes_auth.reset_locks()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield
    routes_auth.reset_locks()


@pytest.fixture(autouse=True)
def _restore_usage_monitor_swap_state():
    """A committed /use arms usage_monitor's module-level swap clocks."""
    from jacked.api import usage_monitor as um

    saved = (
        um._last_swap_time,
        um._last_committed_swap_time,
        dict(um._emerged_tier_streak),
    )
    yield
    um._last_swap_time, um._last_committed_swap_time = saved[0], saved[1]
    um._emerged_tier_streak.clear()
    um._emerged_tier_streak.update(saved[2])


class _RecordingDB:
    """Wraps a DB and records (method, thread id) for every method call."""

    def __init__(self, inner):
        self._inner = inner
        self.calls: list[tuple[str, int]] = []

    def __getattr__(self, name):
        attr = getattr(self._inner, name)
        if not callable(attr):
            return attr

        def _call(*args, **kwargs):
            self.calls.append((name, threading.get_ident()))
            return attr(*args, **kwargs)

        return _call


# ---------------------------------------------------------------------------
# refresh_all_usage: the per-account helper
# ---------------------------------------------------------------------------


def test_bulk_refresh_runs_every_db_call_off_the_loop(monkeypatch):
    """Account 1 fetches, account 2 times out. The timeout write, the error
    re-read and the progress payload reads all run in worker threads, and
    the progress numbers stay one per completion."""
    inner = MagicMock()
    inner.list_accounts.return_value = [
        {"id": 1, "email": "a@x"},
        {"id": 2, "email": "b@x"},
    ]
    inner.get_account.side_effect = lambda account_id: {
        "id": account_id,
        "email": f"{account_id}@x",
        "expires_at": int(time.time()) + 3600,
        "last_error": "timed out" if account_id == 2 else None,
    }
    db = _RecordingDB(inner)
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)
    monkeypatch.setattr(routes_auth, "_get_active_account_id_cached", lambda: None)
    monkeypatch.setattr(routes_auth, "_BULK_PER_ACCOUNT_TIMEOUT", 0.2)

    broadcasts: list[tuple[str, dict]] = []
    loop_thread: dict = {}

    class _Registry:
        async def broadcast(self, event, payload):
            loop_thread["id"] = threading.get_ident()
            broadcasts.append((event, payload))

    request = MagicMock()
    request.app.state.ws_registry = _Registry()

    async def fake_fetch(account_id, *args, **kwargs):
        if account_id == 2:
            await asyncio.sleep(3600)
        return {"five_hour": {"utilization": 1.0}, "seven_day": {"utilization": 2.0}}

    async def _run():
        loop_thread["run"] = threading.get_ident()
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            return await routes_auth.refresh_all_usage(request)

    resp = asyncio.run(_run())

    assert resp.refreshed == 1 and resp.failed == 1
    assert loop_thread["id"] == loop_thread["run"]
    names = [name for name, _ in db.calls]
    assert "list_accounts" in names
    assert "update_account" in names  # the timeout write
    assert names.count("get_account") == 3  # 2 payloads + 1 error re-read
    assert all(ident != loop_thread["run"] for _, ident in db.calls), db.calls

    done = [p for e, p in broadcasts if e == "usage_refresh_progress" and "progress" in p]
    assert sorted(p["progress"] for p in done) == [1, 2]
    by_id = {p["account_id"]: p for p in done}
    assert by_id[1]["status"] == "done"
    assert by_id[2]["status"] == "failed"
    assert by_id[2]["account_data"]["id"] == 2
    failed_row = next(r for r in resp.results if r["account_id"] == 2)
    assert failed_row["error"] == "timed out"


def test_bulk_refresh_throttled_pass_reads_off_the_loop(monkeypatch):
    """The throttled pass serves DB values; that read also runs in a thread."""
    inner = MagicMock()
    inner.list_accounts.return_value = [{"id": 1, "email": "a@x"}]
    db = _RecordingDB(inner)
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)
    monkeypatch.setattr(routes_auth, "_last_bulk_completed_at", time.time())

    request = MagicMock()
    request.app.state.ws_registry = None
    loop_thread: dict = {}

    async def _run():
        loop_thread["id"] = threading.get_ident()
        return await routes_auth.refresh_all_usage(request)

    resp = asyncio.run(_run())

    assert resp.throttled == 1
    assert [name for name, _ in db.calls] == ["list_accounts"]
    assert db.calls[0][1] != loop_thread["id"]


# ---------------------------------------------------------------------------
# use_account: the manual switch
# ---------------------------------------------------------------------------


def _committed_result():
    return SwitchResult(
        operation_id="test-operation",
        outcome=SwitchOutcome.COMMITTED,
        desired_default=IdentityAxis(1, "desired"),
        storage=IdentityAxis(1, "committed"),
        committed_authority=IdentityAxis(1, "committed"),
        existing_session_activation=SessionActivationState.PENDING_NEXT_ACTIVITY,
        provider_verification=ProviderVerificationState.UNVERIFIED,
    )


def test_use_account_runs_every_db_call_off_the_loop(tmp_path):
    real = Database(str(tmp_path / "test.db"))
    with real._writer() as conn:
        for account_id, email in ((1, "alice@test.com"), (3, "carol@test.com")):
            conn.execute(
                """INSERT INTO accounts
                   (id, email, access_token, refresh_token, expires_at,
                    is_active, is_deleted, validation_status,
                    cc_access_token, cc_refresh_token, cc_expires_at,
                    consecutive_failures)
                   VALUES (?, ?, 'at', 'rt', 1900000000, 1, 0, 'valid',
                           'cc_at', 'cc_rt', 1900000000, 0)""",
                (account_id, email),
            )
    # A longer explicit pause than 15 minutes: the switch must keep it.
    real.set_setting("auto_swap_paused_until", "2999-01-01T00:00:00+00:00")
    db = _RecordingDB(real)

    loop_thread: dict = {}
    broadcasts: list[tuple[str, dict]] = []

    class _Registry:
        async def broadcast(self, event, payload):
            loop_thread["id"] = threading.get_ident()
            broadcasts.append((event, payload))

    app = FastAPI()
    app.include_router(routes_auth.router, prefix="/api/auth")
    app.state.db = db
    app.state.ws_registry = _Registry()
    app.state.credential_switcher = mock.Mock(return_value=_committed_result())
    client = TestClient(app, headers={"X-Jacked-Page-Session": "page-session-123456"})

    try:
        with (
            mock.patch(
                "jacked.api.credential_helpers.reconcile_credentials_from_live_store",
                return_value=True,
            ),
            mock.patch(
                "jacked.api.usage_monitor._read_active_account_id", return_value=3
            ),
        ):
            resp = client.post("/api/auth/accounts/1/use")

        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "committed"
        assert "id" in loop_thread
        names = {name for name, _ in db.calls}
        assert {
            "get_account",
            "claim_auth_action",
            "finish_auth_action",
            "mark_global_sessions_pending",
            "set_setting",
            "update_setting",
        } <= names
        assert all(ident != loop_thread["id"] for _, ident in db.calls), db.calls

        assert real.get_setting("active_account_id") == "1"
        assert real.get_setting("auto_swap_paused_until") == "2999-01-01T00:00:00+00:00"
        swap = next(p for e, p in broadcasts if e == "auto_swap_triggered")
        assert swap["from_email"] == "carol@test.com"
        assert swap["to_account_id"] == 1
    finally:
        real.close()
