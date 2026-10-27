"""session_accounts_watch_loop forces its expiry broadcast on WALL-CLOCK time.

A cycle counter stretched with system sleep (each slept slice counted once),
so the periodic broadcast drifted far past its ~60 s target on a closed lid.
"""

import asyncio
import sqlite3
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.api import watchers


def _app(tmp_path):
    path = tmp_path / "w.db"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE session_accounts (detected_at TEXT, ended_at TEXT)")
    conn.commit()
    conn.close()
    app = MagicMock()
    app.state.db.db_path = str(path)
    registry = MagicMock()
    registry.client_count = 1
    registry.broadcast = AsyncMock()
    app.state.ws_registry = registry
    return app, registry


def _run(app, step_seconds, cycles):
    clock = {"now": 1_800_000_000.0, "sleeps": 0}

    async def _sleep(_seconds):
        clock["sleeps"] += 1
        if clock["sleeps"] > cycles:
            raise asyncio.CancelledError
        clock["now"] += step_seconds

    with (
        patch.object(watchers.time, "time", lambda: clock["now"]),
        patch("jacked.api.watchers.asyncio.sleep", _sleep),
    ):
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(watchers.session_accounts_watch_loop(app))


def test_forced_broadcast_fires_on_first_cycle_after_a_long_sleep(tmp_path):
    app, registry = _app(tmp_path)
    _run(app, step_seconds=900, cycles=1)
    registry.broadcast.assert_awaited_once_with(
        "sessions_changed", source="session_watcher"
    )


def test_no_forced_broadcast_before_sixty_wall_seconds(tmp_path):
    app, registry = _app(tmp_path)
    _run(app, step_seconds=3, cycles=19)  # 57 s of wall time, data unchanged
    registry.broadcast.assert_not_awaited()
