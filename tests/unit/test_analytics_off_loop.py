"""Analytics SQLite work must never run on the service event loop.

Regression for the 2026-10-10 Windows outage: ``live_monitor_loop`` called
``detect_anomalies`` on the event loop, a slow 7-day average query blocked
the loop, and every HTTP route timed out. These tests prove the monitor
loops and the analytics routes do their DB work in worker threads, and that
the loop keeps serving other coroutines while that work blocks.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked.api.routes.analytics import router
from jacked.web import analytics_anomalies, analytics_monitor, analytics_paths, analytics_scanner
from jacked.web.analytics_db import AnalyticsDB
from jacked.web.database import Database


class _FakeDB:
    """Analytics DB stand-in for the live monitor (records nothing real)."""

    def __init__(self):
        self.inserted: list[dict] = []

    def insert_messages(self, messages):
        self.inserted.extend(messages)


def _patch_live_inputs(monkeypatch, tmp_path: Path) -> None:
    """Feed the live monitor one active file with one new message."""
    jsonl = tmp_path / "projects" / "proj-hash" / "sess-1.jsonl"
    monkeypatch.setattr(analytics_paths, "get_claude_data_dirs", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(
        analytics_monitor, "_find_active_with_sizes", lambda dirs, age: [(jsonl, 100)],
    )
    monkeypatch.setattr(
        analytics_scanner,
        "parse_jsonl_from_offset",
        lambda path, offset: ([{"id": "m1", "session_id": "sess-1"}], 100),
    )
    monkeypatch.setattr(analytics_anomalies, "auto_resolve_flags", lambda db: None)


# ---------------------------------------------------------------------------
# live_monitor_loop
# ---------------------------------------------------------------------------

def test_live_monitor_runs_anomaly_detection_off_the_loop_thread(monkeypatch, tmp_path):
    _patch_live_inputs(monkeypatch, tmp_path)
    seen: dict = {}

    async def scenario():
        loop = asyncio.get_running_loop()
        called = asyncio.Event()

        def fake_detect(db, session_ids=None):
            seen["tid"] = threading.get_ident()
            seen["session_ids"] = session_ids
            loop.call_soon_threadsafe(called.set)
            return []

        monkeypatch.setattr(analytics_anomalies, "detect_anomalies", fake_detect)
        app = SimpleNamespace(state=SimpleNamespace(analytics_db=_FakeDB()))
        task = asyncio.create_task(analytics_monitor.live_monitor_loop(app))
        try:
            await asyncio.wait_for(called.wait(), timeout=5)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        return threading.get_ident()

    loop_tid = asyncio.run(scenario())
    assert seen["tid"] != loop_tid
    assert seen["session_ids"] == ["sess-1"]


def test_live_monitor_blocking_detection_does_not_starve_the_loop(monkeypatch, tmp_path):
    """While detect_anomalies blocks, another coroutine keeps running."""
    _patch_live_inputs(monkeypatch, tmp_path)
    release = threading.Event()
    entered = threading.Event()
    outcome: dict = {}

    def blocking_detect(db, session_ids=None):
        entered.set()
        # Only a coroutine on the event loop sets ``release``. If detection
        # ran on the loop (the regression), nothing can set it, and the
        # bounded wait times out instead of hanging the suite.
        outcome["released_by_loop"] = release.wait(timeout=2)
        return []

    monkeypatch.setattr(analytics_anomalies, "detect_anomalies", blocking_detect)

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(analytics_db=_FakeDB()))
        task = asyncio.create_task(analytics_monitor.live_monitor_loop(app))
        try:
            while not entered.is_set():
                await asyncio.sleep(0.01)
            ticks = 0
            started = time.monotonic()
            while time.monotonic() - started < 0.3:
                await asyncio.sleep(0.01)
                ticks += 1
            still_blocked = not release.is_set()
            release.set()
            # Let the worker thread return before the task is cancelled.
            while "released_by_loop" not in outcome:
                await asyncio.sleep(0.01)
            return ticks, still_blocked
        finally:
            release.set()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    ticks, still_blocked = asyncio.run(scenario())
    assert outcome["released_by_loop"] is True
    assert still_blocked
    assert ticks >= 5, f"event loop starved while detection blocked ({ticks} ticks)"


def test_live_monitor_detection_failure_warns_once_then_rate_limits(monkeypatch, tmp_path, caplog):
    """A persistent failure gives ONE warning with a traceback, not one per cycle."""
    jsonl = tmp_path / "projects" / "proj-hash" / "sess-1.jsonl"
    cycles = {"n": 0}

    def growing_file(dirs, age):
        cycles["n"] += 1
        return [(jsonl, cycles["n"] * 100)]

    monkeypatch.setattr(analytics_paths, "get_claude_data_dirs", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(analytics_monitor, "_find_active_with_sizes", growing_file)
    monkeypatch.setattr(
        analytics_scanner,
        "parse_jsonl_from_offset",
        lambda path, offset: ([{"id": f"m{offset}", "session_id": "sess-1"}], offset + 100),
    )

    def boom(db, session_ids=None):
        raise RuntimeError("database disk image is malformed")

    monkeypatch.setattr(analytics_anomalies, "detect_anomalies", boom)
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds, *args, **kwargs):
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    caplog.set_level("DEBUG", logger="jacked.web.analytics_monitor")

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace(analytics_db=_FakeDB()))
        task = asyncio.create_task(analytics_monitor.live_monitor_loop(app))
        while cycles["n"] < 6:
            await real_sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    failures = [r for r in caplog.records if "Live anomaly detection failed" in r.getMessage()]
    warnings = [r for r in failures if r.levelname == "WARNING"]
    assert len(failures) >= 5
    assert len(warnings) == 1
    assert warnings[0].exc_info is not None
    assert all(r.levelname == "DEBUG" for r in failures if r is not warnings[0])


# ---------------------------------------------------------------------------
# initial_scan_loop
# ---------------------------------------------------------------------------

def test_initial_scan_runs_db_work_off_the_loop_thread(monkeypatch, tmp_path):
    db = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
    db.set_setting("purge_days", "30")
    threads: dict[str, int] = {}

    def record(name, fn):
        def wrapper(*args, **kwargs):
            threads[name] = threading.get_ident()
            return fn(*args, **kwargs)
        return wrapper

    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds, *args, **kwargs):
        await real_sleep(0)

    async def fake_scan(data_dirs, db_arg, progress_callback=None):
        return {"projects": 0, "messages": 0}

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(
        analytics_monitor, "_open_analytics_db", record("open", lambda: db),
    )
    monkeypatch.setattr(analytics_paths, "get_claude_data_dirs", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(analytics_scanner, "scan_all_projects", fake_scan)
    monkeypatch.setattr(
        analytics_anomalies, "detect_anomalies", record("detect", lambda db, session_ids=None: []),
    )
    monkeypatch.setattr(analytics_anomalies, "auto_resolve_flags", record("resolve", lambda db: None))
    monkeypatch.setattr(db, "get_setting", record("get_setting", db.get_setting))
    monkeypatch.setattr(
        db, "purge_messages_older_than", record("purge", db.purge_messages_older_than),
    )

    async def scenario():
        app = SimpleNamespace(state=SimpleNamespace())
        await analytics_monitor.initial_scan_loop(app)
        assert app.state.analytics_db is db
        return threading.get_ident()

    loop_tid = asyncio.run(scenario())
    assert set(threads) == {"open", "detect", "resolve", "get_setting", "purge"}
    for name, tid in threads.items():
        assert tid != loop_tid, f"{name} ran on the event loop thread"
    # detect + auto-resolve share ONE worker-thread hop.
    assert threads["detect"] == threads["resolve"]
    db.close()


def test_initial_scan_detection_failure_logs_warning(monkeypatch, tmp_path, caplog):
    db = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
    real_sleep = asyncio.sleep

    async def fast_sleep(_seconds, *args, **kwargs):
        await real_sleep(0)

    async def fake_scan(data_dirs, db_arg, progress_callback=None):
        return {"projects": 0, "messages": 0}

    def boom(db_arg, session_ids=None):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    monkeypatch.setattr(analytics_monitor, "_open_analytics_db", lambda: db)
    monkeypatch.setattr(analytics_paths, "get_claude_data_dirs", lambda: [tmp_path / "projects"])
    monkeypatch.setattr(analytics_scanner, "scan_all_projects", fake_scan)
    monkeypatch.setattr(analytics_anomalies, "detect_anomalies", boom)

    caplog.set_level("DEBUG", logger="jacked.web.analytics_monitor")
    asyncio.run(analytics_monitor.initial_scan_loop(SimpleNamespace(state=SimpleNamespace())))
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("Initial anomaly detection failed" in r.getMessage() for r in warnings)
    assert all(r.exc_info for r in warnings if "anomaly" in r.getMessage())
    db.close()


def test_find_active_with_sizes_reads_sizes(tmp_path):
    proj = tmp_path / "projects" / "proj"
    proj.mkdir(parents=True)
    f = proj / "s.jsonl"
    f.write_text("x" * 42)
    result = analytics_monitor._find_active_with_sizes([tmp_path / "projects"], 600)
    assert result == [(f, 42)]


def test_scan_all_projects_walks_and_prunes_off_the_loop(monkeypatch, tmp_path):
    db = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
    data_dir = tmp_path / "projects"
    (data_dir / "proj").mkdir(parents=True)
    threads: dict[str, int] = {}

    real_prune = db.prune_stale_scan_state
    real_list = analytics_scanner._list_project_dirs

    def prune(valid):
        threads["prune"] = threading.get_ident()
        return real_prune(valid)

    def list_dirs(d):
        threads["list"] = threading.get_ident()
        return real_list(d)

    monkeypatch.setattr(db, "prune_stale_scan_state", prune)
    monkeypatch.setattr(analytics_scanner, "_list_project_dirs", list_dirs)

    async def scenario():
        result = await analytics_scanner.scan_all_projects([data_dir], db)
        return result, threading.get_ident()

    result, loop_tid = asyncio.run(scenario())
    assert result == {"projects": 1, "messages": 0}
    assert threads["prune"] != loop_tid
    assert threads["list"] != loop_tid
    db.close()


# ---------------------------------------------------------------------------
# Analytics routes
# ---------------------------------------------------------------------------

_MAIN_DB_METHODS = ("list_agent_invocations", "list_hook_executions", "list_lessons")
_ANALYTICS_DB_METHODS = (
    "get_overview",
    "get_active_flags",
    "get_session_list",
    "get_messages_for_session",
    "get_daily_summaries",
    "resolve_flag",
    "set_setting",
    "get_setting",
)


def _instrument(obj, names, calls: list[tuple[str, int]]) -> None:
    for name in names:
        original = getattr(obj, name)

        def wrapper(*args, _name=name, _orig=original, **kwargs):
            calls.append((_name, threading.get_ident()))
            return _orig(*args, **kwargs)

        setattr(obj, name, wrapper)


def test_analytics_routes_run_db_work_off_the_loop_thread(tmp_path):
    main_db = Database(str(tmp_path / "jacked.db"))
    adb = AnalyticsDB(db_path=str(tmp_path / "analytics.db"))
    calls: list[tuple[str, int]] = []
    _instrument(main_db, _MAIN_DB_METHODS, calls)
    _instrument(adb, _ANALYTICS_DB_METHODS, calls)

    app = FastAPI()
    app.state.db = main_db
    app.state.analytics_db = adb
    app.include_router(router, prefix="/api/analytics")

    @app.get("/loop-tid")
    async def loop_tid():
        return {"tid": threading.get_ident()}

    requests = [
        ("get", "/api/analytics/agents?days=7", None),
        ("get", "/api/analytics/hooks?days=7", None),
        ("get", "/api/analytics/lessons?days=7", None),
        ("get", "/api/analytics/usage-overview?days=1", None),
        ("get", "/api/analytics/usage-sessions?days=1", None),
        ("get", "/api/analytics/usage-session-detail/sess-1", None),
        ("get", "/api/analytics/usage-trends?days=7", None),
        ("get", "/api/analytics/usage-flags", None),
        ("post", "/api/analytics/usage-flag-dismiss/1", None),
        ("post", "/api/analytics/usage-flag-snooze/cost_outlier?hours=2", None),
        ("get", "/api/analytics/usage-scan-status", None),
        ("post", "/api/analytics/usage-settings", {"purge_days": 30}),
        ("post", "/api/analytics/usage-settings", {"purge_days": 0}),
    ]
    with TestClient(app) as client:
        tid = client.get("/loop-tid").json()["tid"]
        for method, url, body in requests:
            resp = getattr(client, method)(url, json=body) if body else getattr(client, method)(url)
            assert resp.status_code == 200, (url, resp.status_code, resp.text)
        status = client.get("/api/analytics/usage-scan-status").json()

    called = {name for name, _ in calls}
    assert called == set(_MAIN_DB_METHODS) | set(_ANALYTICS_DB_METHODS)
    on_loop = [name for name, t in calls if t == tid]
    assert on_loop == [], f"DB work ran on the event loop thread: {on_loop}"
    # The purge_days=0 write cleared the setting (response shape unchanged).
    assert status["ready"] is True and status["purge_days"] is None
    main_db.close()
    adb.close()
    for p in ("jacked.db", "analytics.db"):
        assert os.path.exists(tmp_path / p)


def test_concurrent_anomaly_passes_are_serialized(monkeypatch):
    """The initial full pass and a live pass never run detect at the same time.

    Two overlapping passes would both see "no active flag" and insert
    duplicate flag rows.
    """
    import asyncio as _asyncio
    import threading as _threading
    import time as _time

    from jacked.web import analytics_anomalies, analytics_monitor

    state = {"in_flight": 0, "peak": 0}
    guard = _threading.Lock()

    def fake_detect(db, session_ids=None):
        with guard:
            state["in_flight"] += 1
            state["peak"] = max(state["peak"], state["in_flight"])
        _time.sleep(0.05)
        with guard:
            state["in_flight"] -= 1
        return []

    monkeypatch.setattr(analytics_anomalies, "detect_anomalies", fake_detect)
    monkeypatch.setattr(analytics_anomalies, "auto_resolve_flags", lambda db: None)

    async def run_two():
        await _asyncio.gather(
            _asyncio.to_thread(analytics_monitor._run_anomaly_pass, object()),
            _asyncio.to_thread(analytics_monitor._run_anomaly_pass, object(), ["s1"]),
        )

    _asyncio.run(run_two())
    assert state["peak"] == 1
