"""Tests for full_sweep_loop heartbeat + bounded fetch_usage.

Uses asyncio.run() wrappers (project convention)."""
import asyncio
import logging
from unittest.mock import MagicMock, patch


def test_heartbeat_fires_when_window_keeper_disabled(caplog):
    """Default config: window_keeper_enabled=False → heartbeat MUST still fire."""
    from jacked.api import usage_monitor

    caplog.set_level(logging.INFO, logger="jacked.api.usage_monitor")

    async def _run():
        app = MagicMock()
        db = MagicMock()
        app.state.db = db
        db.list_accounts.return_value = []

        with patch.object(usage_monitor, "_setting_bool", return_value=False), \
             patch.object(usage_monitor, "_setting_float", return_value=0.05):
            task = asyncio.create_task(usage_monitor.full_sweep_loop(app))
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    heartbeats = [r.getMessage() for r in caplog.records
                  if "heartbeat" in r.getMessage().lower()]
    assert len(heartbeats) >= 1, "expected ≥1 heartbeat when window keeper off"
    assert any("iter=" in m for m in heartbeats), \
        f"expected 'iter=N' in heartbeat; got {heartbeats}"


def test_heartbeat_includes_monotonic_iteration_count(caplog):
    """Heartbeat iter= must increment monotonically across sweeps."""
    from jacked.api import usage_monitor

    caplog.set_level(logging.INFO, logger="jacked.api.usage_monitor")

    async def _run():
        app = MagicMock()
        db = MagicMock()
        app.state.db = db
        db.list_accounts.return_value = []

        with patch.object(usage_monitor, "_setting_bool", return_value=False), \
             patch.object(usage_monitor, "_setting_float", return_value=0.03):
            task = asyncio.create_task(usage_monitor.full_sweep_loop(app))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    iters = []
    for r in caplog.records:
        msg = r.getMessage()
        if "heartbeat" in msg.lower() and "iter=" in msg:
            iters.append(int(msg.split("iter=")[1].split()[0]))
    assert len(iters) >= 2, f"expected ≥2 heartbeats; got {iters}"
    assert iters == sorted(iters), f"iters not monotonic: {iters}"
