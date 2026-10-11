"""Tests for the event-loop stall watchdog (jacked.api.loop_watchdog)."""

from __future__ import annotations

import asyncio
import logging
import threading
import time

from jacked.api.loop_watchdog import LoopWatchdog

LOGGER = "jacked.api.loop_watchdog"


def _block_the_event_loop_for(seconds: float) -> None:
    """Simulate a blocking SQLite query on the loop thread."""
    time.sleep(seconds)


def _records(caplog, level):
    return [r for r in caplog.records if r.name == LOGGER and r.levelno == level]


def test_watchdog_warns_once_with_the_blocking_function_then_logs_recovery(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)

    async def scenario():
        wd = LoopWatchdog(
            asyncio.get_running_loop(),
            stall_seconds=0.3,
            probe_interval=0.05,
            rewarn_seconds=60.0,
        )
        wd.start()
        assert wd.is_running
        await asyncio.sleep(0.2)  # healthy period: no warning
        assert _records(caplog, logging.WARNING) == []
        _block_the_event_loop_for(1.0)
        await asyncio.sleep(0.3)  # let the watchdog see the recovery
        wd.stop()
        return wd

    wd = asyncio.run(scenario())
    assert not wd.is_running
    assert wd.stall_episodes == 1

    warnings = _records(caplog, logging.WARNING)
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    message = warnings[0].getMessage()
    assert "Event loop stalled" in message
    assert "_block_the_event_loop_for" in message
    assert "test_loop_watchdog.py:" in message

    infos = [r for r in _records(caplog, logging.INFO) if "recovered" in r.getMessage()]
    assert len(infos) == 1
    assert "stall" in infos[0].getMessage()


def test_check_rewarns_on_long_stall_and_reports_total_with_fake_clock(caplog):
    caplog.set_level(logging.INFO, logger=LOGGER)
    now = [0.0]
    loop = asyncio.new_event_loop()  # not running: probes never get a response
    try:
        wd = LoopWatchdog(
            loop,
            loop_thread_id=threading.get_ident(),
            stall_seconds=5.0,
            rewarn_seconds=60.0,
            clock=lambda: now[0],
        )
        assert wd.check() is True  # sends the first probe at t=0
        for t in (1.0, 4.9):
            now[0] = t
            wd.check()
        assert _records(caplog, logging.WARNING) == []

        now[0] = 6.0
        wd.check()
        now[0] = 30.0
        wd.check()
        assert len(_records(caplog, logging.WARNING)) == 1

        now[0] = 66.0
        wd.check()
        warnings = _records(caplog, logging.WARNING)
        assert len(warnings) == 2
        assert "still stalled" in warnings[1].getMessage()
        assert wd.stall_episodes == 1

        now[0] = 70.0
        wd._on_probe()  # the loop finally runs the probe
        now[0] = 71.0
        assert wd.check() is True
        infos = [r.getMessage() for r in _records(caplog, logging.INFO)]
        assert any("recovered after a 70.0s stall" in m for m in infos)
    finally:
        loop.close()
    # A closed loop makes the watchdog exit instead of raising.
    assert wd.check() is False


def test_watchdog_thread_exits_on_closed_loop():
    loop = asyncio.new_event_loop()
    loop.close()
    wd = LoopWatchdog(loop, loop_thread_id=threading.get_ident(), probe_interval=0.01)
    wd.start()
    deadline = time.monotonic() + 2
    while wd.is_running and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not wd.is_running
    wd.stop()


def test_stop_is_prompt_and_idempotent():
    async def scenario():
        wd = LoopWatchdog(asyncio.get_running_loop(), probe_interval=1.0)
        wd.start()
        wd.start()  # second start is a no-op
        await asyncio.sleep(0.05)
        started = time.monotonic()
        wd.stop()
        elapsed = time.monotonic() - started
        wd.stop()
        return wd, elapsed

    wd, elapsed = asyncio.run(scenario())
    assert not wd.is_running
    assert elapsed < 0.5


def test_lifespan_starts_watchdog_before_yield_and_stops_it_after():
    """The service lifespan wires the watchdog in (checked on the source, so
    the test does not run the real lifespan and its real loops)."""
    import ast
    import inspect

    import jacked.api.main as api_main

    tree = ast.parse(inspect.getsource(api_main))
    lifespan = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "lifespan"
    )
    yield_line = next(n.lineno for n in ast.walk(lifespan) if isinstance(n, ast.Yield))

    watchdog_vars = {
        t.id
        for n in ast.walk(lifespan)
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Name) and n.value.func.id == "LoopWatchdog"
        for t in n.targets if isinstance(t, ast.Name)
    }
    assert len(watchdog_vars) == 1, "lifespan must create exactly one LoopWatchdog"
    var = watchdog_vars.pop()

    def attr_lines(attr):
        return [
            n.lineno for n in ast.walk(lifespan)
            if isinstance(n, ast.Attribute) and n.attr == attr
            and isinstance(n.value, ast.Name) and n.value.id == var
        ]

    assert any(line < yield_line for line in attr_lines("start")), "start() must run before yield"
    assert any(line > yield_line for line in attr_lines("stop")), "stop() must run after yield"


class _NeverRunsLoop:
    """A loop stand-in that accepts probes and never runs them."""

    def is_closed(self):
        return False

    def call_soon_threadsafe(self, callback):
        return None


def test_clock_jump_after_sleep_is_not_reported_as_a_stall(caplog):
    """A sleep/resume jump of the monotonic clock resyncs, it does not warn."""
    caplog.set_level(logging.INFO, logger=LOGGER)
    state = {"t": 0.0, "calls": 0}

    def clock():
        state["calls"] += 1
        if state["calls"] == 3:
            state["t"] += 3600.0  # the computer slept for an hour
        state["t"] += 0.001
        return state["t"]

    wd = LoopWatchdog(
        _NeverRunsLoop(),
        loop_thread_id=threading.get_ident(),
        stall_seconds=5.0,
        probe_interval=0.01,
        clock=clock,
    )
    wd.start()
    try:
        time.sleep(0.3)
    finally:
        wd.stop()
    assert state["calls"] > 5  # the thread ran many steps past the jump
    assert _records(caplog, logging.WARNING) == []
    assert wd.stall_episodes == 0
