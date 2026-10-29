"""The async routes and helpers that still await something run their
jacked.db work in a worker thread, not on the event loop (#162).

Each test records ``threading.get_ident()`` inside a fake DB method and
compares it with the loop thread (the thread ``asyncio.run`` drives), the
same pattern as tests/unit/test_analytics_off_loop.py. The routes that have
no ``await`` left are plain ``def`` routes, which FastAPI runs in its
threadpool; the static guard (test_db_off_loop_guard.py) covers those.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest


class _RecordingDB:
    """Records the thread of every DB call it gets."""

    def __init__(self, activity=None):
        self.threads: list[tuple[str, int]] = []
        self._activity = activity or []

    def _mark(self, name):
        self.threads.append((name, threading.get_ident()))

    def list_accounts(self, include_inactive=False):
        self._mark("list_accounts")
        return []

    def set_setting(self, key, value):
        self._mark(f"set_setting:{key}")

    def set_settings(self, values):
        self._mark("set_settings:" + ",".join(values))

    def get_project_activity_summary(self, limit=100):
        self._mark("get_project_activity_summary")
        return self._activity

    def claude_identity_coverage(self):
        self._mark("claude_identity_coverage")
        return {"total": 0, "with_uuid": 0, "without_uuid": 0,
                "with_profile": 0, "without_profile": 0}


def _request(db, host="127.0.0.1"):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(db=db, ws_registry=None)),
        client=SimpleNamespace(host=host),
    )


def _run_on_loop(coro_fn):
    """Run ``coro_fn()`` under asyncio.run; return (result, loop thread id)."""
    async def main():
        return await coro_fn(), threading.get_ident()

    return asyncio.run(main())


def _repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


# --- menubar -----------------------------------------------------------------


def test_menubar_summary_lists_accounts_off_the_loop(monkeypatch):
    from jacked.api.routes import auth as auth_routes
    from jacked.api.routes.menubar import menubar_summary

    async def _no_active(request):
        return SimpleNamespace(account_id=None)

    monkeypatch.setattr(auth_routes, "get_active_credential", _no_active)
    db = _RecordingDB()
    result, loop_thread = _run_on_loop(lambda: menubar_summary(_request(db)))

    assert result == {"active": None, "active_account_id": None,
                      "worst": None, "account_count": 0}
    assert [name for name, _ in db.threads] == ["list_accounts"]
    assert all(t != loop_thread for _, t in db.threads)


# --- settings_swap -----------------------------------------------------------


def test_update_swap_settings_writes_off_the_loop_and_wakes_on_it(monkeypatch):
    """The settings writes run in ONE worker hop, in their original order;
    the sweep's asyncio.Event is set afterwards, on the loop thread."""
    import jacked.api.usage_monitor as usage_monitor
    from jacked.api.routes.settings_swap import SwapSettings, update_swap_settings

    class _Wake:
        thread = None

        def set(self):
            _Wake.thread = threading.get_ident()

    monkeypatch.setattr(usage_monitor, "_sweep_wake", _Wake())
    db = _RecordingDB()
    body = SwapSettings(auto_swap_paused_until="2026-10-29T12:00:00+00:00")
    result, loop_thread = _run_on_loop(lambda: update_swap_settings(_request(db), body))

    assert result is body
    # One transaction for the whole bundle (#162 review: two overlapping
    # saves must never persist a mixed threshold pair).
    assert [name for name, _ in db.threads] == [
        "set_settings:auto_swap_enabled,auto_swap_5h_warning,"
        "auto_swap_5h_critical,auto_swap_7d_threshold,usage_check_interval,"
        "window_keeper_enabled,window_keeper_active_start,"
        "window_keeper_active_end,window_keeper_prewake,auto_swap_paused_until",
    ]
    worker_threads = {t for _, t in db.threads}
    assert len(worker_threads) == 1 and loop_thread not in worker_threads
    assert _Wake.thread == loop_thread


def test_update_swap_settings_write_error_propagates_and_skips_the_wake(monkeypatch):
    import jacked.api.usage_monitor as usage_monitor
    from jacked.api.routes.settings_swap import SwapSettings, update_swap_settings

    woken = []
    monkeypatch.setattr(usage_monitor, "_sweep_wake", SimpleNamespace(set=lambda: woken.append(1)))

    class _LockedDB(_RecordingDB):
        def set_settings(self, values):
            raise sqlite3.OperationalError("database is locked")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        _run_on_loop(lambda: update_swap_settings(_request(_LockedDB()), SwapSettings()))
    assert woken == []


# --- settings_remote ---------------------------------------------------------


def test_restart_reads_the_saved_setting_off_the_loop(monkeypatch):
    """The restart payload read runs in a worker. A read that raises still
    reaches the route's except block, which releases the restart lock."""
    import jacked.api.routes.settings_remote as sr

    monkeypatch.setattr(sr, "_restart_lock", threading.Lock())
    seen = []

    def _boom(db):
        seen.append(threading.get_ident())
        raise RuntimeError("database is locked")

    monkeypatch.setattr(sr, "_read_enabled_scope", _boom)

    async def main():
        loop_thread = threading.get_ident()
        with pytest.raises(RuntimeError, match="database is locked"):
            await sr.restart_remote_access(_request(_RecordingDB()))
        return loop_thread

    loop_thread = asyncio.run(main())
    assert len(seen) == 1 and seen[0] != loop_thread
    assert not sr._restart_lock.locked()


# --- permissions -------------------------------------------------------------


def test_set_project_permissions_validates_the_repo_off_the_loop(tmp_path):
    from jacked.api.routes.permissions import (
        ProjectPermissionsRequest,
        set_project_permissions,
    )

    repo = _repo(tmp_path)
    db = _RecordingDB(activity=[{"repo_path": str(repo.resolve())}])
    body = ProjectPermissionsRequest(repo_path=str(repo), allow=["Bash(ls:*)"])
    result, loop_thread = _run_on_loop(lambda: set_project_permissions(body, _request(db)))

    assert result == {"ok": True}
    assert [name for name, _ in db.threads] == ["get_project_activity_summary"]
    assert db.threads[0][1] != loop_thread


def test_add_project_rule_validates_the_repo_off_the_loop(tmp_path):
    from jacked.api.routes.permissions import AddRuleRequest, add_permission_rule

    repo = _repo(tmp_path)
    # Unknown project: the validation rejects it, as before the thread hop.
    db = _RecordingDB(activity=[])
    body = AddRuleRequest(pattern="Bash(ls:*)", scope="project", repo_path=str(repo))
    result, loop_thread = _run_on_loop(lambda: add_permission_rule(body, _request(db)))

    assert result.status_code == 422
    assert [name for name, _ in db.threads] == ["get_project_activity_summary"]
    assert db.threads[0][1] != loop_thread


# --- main: profile backfill and shutdown -------------------------------------


def test_backfill_timeout_logs_coverage_off_the_loop(monkeypatch):
    import jacked.api.main as api_main
    import jacked.web.auth as web_auth

    async def _hangs(db, counts=None):
        counts["candidates"] = 3
        await asyncio.Event().wait()

    seen = {}

    def _coverage(db, counts=None, *, timed_out=False):
        seen.update(thread=threading.get_ident(), counts=dict(counts), timed_out=timed_out)

    monkeypatch.setattr(api_main, "PROFILE_BACKFILL_DELAY", 0)
    monkeypatch.setattr(api_main, "SWEEP_PASS_TIMEOUT", 0.05)
    monkeypatch.setattr(web_auth, "backfill_missing_profiles", _hangs)
    monkeypatch.setattr(web_auth, "log_identity_coverage", _coverage)
    app = SimpleNamespace(state=SimpleNamespace(db=_RecordingDB()))
    _, loop_thread = _run_on_loop(lambda: api_main._profile_backfill_once(app))

    assert seen["timed_out"] is True
    assert seen["counts"] == {"candidates": 3}
    assert seen["thread"] != loop_thread


def test_shutdown_closes_the_loop_threads_connection_in_a_worker(tmp_path):
    """Database connections are thread-local, so Database.close() run in a
    worker would close the WORKER's connection. The shutdown helper closes
    the connection the loop thread opened, and does the close in a worker."""
    import jacked.api.main as api_main
    from jacked.web.database import Database

    class _SpyConnection:
        def __init__(self, real):
            self.real = real
            self.closed_on = None

        def close(self):
            self.closed_on = threading.get_ident()
            self.real.close()

    async def main():
        loop_thread = threading.get_ident()
        db = Database(str(tmp_path / "jacked.db"))  # schema opens the loop thread's connection
        real = db._local.connection
        assert real is not None
        spy = _SpyConnection(real)
        db._local.connection = spy
        await api_main._close_loop_db_connection(SimpleNamespace(state=SimpleNamespace(db=db)))
        return loop_thread, db, real, spy

    loop_thread, db, real, spy = asyncio.run(main())
    assert spy.closed_on is not None and spy.closed_on != loop_thread
    assert db._local.connection is None
    with pytest.raises(sqlite3.ProgrammingError):
        real.execute("SELECT 1")


def test_shutdown_close_is_a_noop_without_a_db_or_a_connection(tmp_path):
    import jacked.api.main as api_main
    from jacked.web.database import Database

    asyncio.run(api_main._close_loop_db_connection(SimpleNamespace(state=SimpleNamespace())))
    asyncio.run(api_main._close_loop_db_connection(SimpleNamespace(state=SimpleNamespace(db=None))))

    db = Database(str(tmp_path / "jacked.db"))
    db.close()  # this thread's connection is already gone
    asyncio.run(api_main._close_loop_db_connection(SimpleNamespace(state=SimpleNamespace(db=db))))
    assert db._local.connection is None


def test_detach_connection_hands_over_this_threads_connection(tmp_path):
    """detach_connection returns the calling thread's connection unclosed and
    forgets it; the next query on the thread opens a fresh connection."""
    from jacked.web.database import Database

    db = Database(str(tmp_path / "jacked.db"))
    db.list_settings()
    conn = db.detach_connection()
    assert conn is not None
    conn.execute("SELECT 1")  # still open
    assert db.detach_connection() is None
    db.list_settings()  # reopens
    assert db.detach_connection() is not None
    conn.close()


# --- atomic settings primitives (#162 review) ------------------------------


def test_update_setting_holds_the_write_lock_through_compute(tmp_path):
    """A writer on ANOTHER Database object (another thread or process) that
    arrives while compute() runs waits for the commit, so the read-modify-write
    can never overwrite it with a decision made on a stale read."""
    from jacked.web.database import Database

    path = str(tmp_path / "jacked.db")
    a, b = Database(path), Database(path)
    a.set_setting("auto_swap_paused_until", "short")
    in_compute = threading.Event()
    order = []

    def compute(current):
        in_compute.set()
        time.sleep(0.3)  # the other writer arrives now
        order.append(("compute saw", current))
        return "extended"

    def other_writer():
        in_compute.wait(5)
        b.set_setting("auto_swap_paused_until", "one-day")
        order.append(("other wrote", None))

    t = threading.Thread(target=other_writer)
    t.start()
    a.update_setting("auto_swap_paused_until", compute)
    t.join(10)
    assert order == [("compute saw", "short"), ("other wrote", None)]
    assert b.get_setting("auto_swap_paused_until") == "one-day"


def test_a_snapshot_never_sees_half_of_a_bundle(tmp_path):
    from jacked.web.database import Database

    path = str(tmp_path / "jacked.db")
    writer, reader = Database(path), Database(path)
    pairs = [("20", "30"), ("80", "90")]
    writer.set_settings({"warning": "20", "critical": "30"})
    stop = threading.Event()
    seen = set()

    def write():
        i = 0
        while not stop.is_set():
            w, c = pairs[i % 2]
            writer.set_settings({"warning": w, "critical": c})
            i += 1

    t = threading.Thread(target=write)
    t.start()
    try:
        for _ in range(300):
            snap = reader.get_settings(["warning", "critical"])
            seen.add((snap["warning"], snap["critical"]))
    finally:
        stop.set()
        t.join(10)
    assert seen <= set(pairs), seen
