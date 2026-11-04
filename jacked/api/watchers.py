"""Background watcher loops for the jacked dashboard.

These async loops poll SQLite tables for changes and broadcast
notifications through the WebSocket registry.
"""

import asyncio
import logging
import sqlite3
import subprocess
import time
from pathlib import Path

from jacked.api.websocket import WebSocketRegistry
from jacked.session_ancestry import session_processes_running, tracked_pid

logger = logging.getLogger(__name__)


async def session_accounts_watch_loop(app, interval: int = 3):
    """Watch session_accounts table for changes, broadcast via WebSocket.

    Uses PRAGMA data_version (connection-scoped) to cheaply detect when any
    external process writes to the DB.  Only queries session_accounts when
    the version changes, and only broadcasts when the actual MAX timestamps
    differ from cached values.

    Also forces a periodic broadcast every ~60s of WALL-CLOCK time to handle
    time-based session expiry. (A cycle count would stretch with system
    sleep: each sleep slice counts once however long the process slept.)

    >>> # Verified via integration test
    """
    db_obj = getattr(app.state, "db", None)
    if db_obj is None:
        return
    db_path = getattr(db_obj, "db_path", None)
    if not db_path or not Path(db_path).exists():
        return

    # Single raw connection — PRAGMA data_version is connection-scoped
    conn = sqlite3.connect(db_path, timeout=2.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    last_data_version: int | None = None
    last_max_detected: str | None = None
    last_max_ended: str | None = None
    force_broadcast_seconds = 60.0
    last_forced_at = time.time()

    # Seed initial values
    try:
        last_data_version = conn.execute("PRAGMA data_version").fetchone()[0]
        row = conn.execute(
            "SELECT MAX(detected_at), MAX(ended_at) FROM session_accounts"
        ).fetchone()
        if row:
            last_max_detected, last_max_ended = row[0], row[1]
    except sqlite3.Error:
        pass

    try:
        while True:
            await asyncio.sleep(interval)
            try:
                should_broadcast = False

                # Periodic forced broadcast for time-based session expiry
                if time.time() - last_forced_at >= force_broadcast_seconds:
                    last_forced_at = time.time()
                    should_broadcast = True

                dv = await asyncio.to_thread(
                    lambda: conn.execute("PRAGMA data_version").fetchone()[0]
                )
                if dv != last_data_version:
                    last_data_version = dv
                    row = await asyncio.to_thread(
                        lambda: conn.execute(
                            "SELECT MAX(detected_at), MAX(ended_at) FROM session_accounts"
                        ).fetchone()
                    )
                    cur_detected = row[0] if row else None
                    cur_ended = row[1] if row else None
                    if cur_detected != last_max_detected or cur_ended != last_max_ended:
                        last_max_detected = cur_detected
                        last_max_ended = cur_ended
                        should_broadcast = True

                if not should_broadcast:
                    continue

                registry: WebSocketRegistry = getattr(app.state, "ws_registry", None)
                if registry and registry.client_count > 0:
                    await registry.broadcast(
                        "sessions_changed", source="session_watcher"
                    )
                    logger.debug(
                        "Session accounts changed — notified %d client(s)",
                        registry.client_count,
                    )
            except sqlite3.Error as e:
                logger.debug("Session watcher DB error: %s", e)
                continue
            except Exception as e:
                logger.warning("Session watcher error: %s", e)
                continue
    finally:
        try:
            conn.close()
        except Exception:
            pass


async def logs_watch_loop(app, interval: int = 3):
    """Watch hook_executions and version_checks for changes.

    Same PRAGMA data_version pattern as session_accounts_watch_loop.
    Broadcasts 'logs_changed' with payload.tables listing which table(s) changed.

    >>> # Verified via integration test
    """
    db_obj = getattr(app.state, "db", None)
    if db_obj is None:
        return
    db_path = getattr(db_obj, "db_path", None)
    if not db_path or not Path(db_path).exists():
        return

    conn = sqlite3.connect(db_path, timeout=2.0, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout = 5000")

    last_data_version: int | None = None
    last_max_ids: dict[str, int | None] = {
        "hook_executions": None,
        "version_checks": None,
    }

    def _get_max_id(table: str) -> int | None:
        row = conn.execute(f"SELECT MAX(id) FROM {table}").fetchone()
        return row[0] if row else None

    # Seed initial values
    try:
        last_data_version = conn.execute("PRAGMA data_version").fetchone()[0]
        for table in last_max_ids:
            last_max_ids[table] = _get_max_id(table)
    except sqlite3.Error:
        pass

    try:
        while True:
            await asyncio.sleep(interval)
            try:
                dv = await asyncio.to_thread(
                    lambda: conn.execute("PRAGMA data_version").fetchone()[0]
                )
                if dv == last_data_version:
                    continue
                last_data_version = dv

                changed_tables = []
                for table in last_max_ids:
                    cur_max = await asyncio.to_thread(lambda t=table: _get_max_id(t))
                    if cur_max != last_max_ids[table]:
                        changed_tables.append(table)
                        last_max_ids[table] = cur_max

                if not changed_tables:
                    continue

                registry: WebSocketRegistry = getattr(app.state, "ws_registry", None)
                if registry and registry.client_count > 0:
                    await registry.broadcast(
                        "logs_changed",
                        payload={"tables": changed_tables},
                        source="logs_watcher",
                    )
                    logger.debug(
                        "Log tables changed (%s) — notified %d client(s)",
                        ", ".join(changed_tables),
                        registry.client_count,
                    )
            except sqlite3.Error as e:
                logger.debug("Logs watcher DB error: %s", e)
                continue
            except Exception as e:
                logger.warning("Logs watcher error: %s", e)
                continue
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _any_claude_process_alive() -> bool:
    """Check if any Claude Code process is running.

    Uses ``pgrep -x claude`` for exact process name matching.
    Returns False on any error (fail-safe — never incorrectly closes sessions).

    >>> # Can't reliably doctest process detection
    """
    # macOS/Linux only — returns False (fail-safe) on Windows
    try:
        result = subprocess.run(
            ["pgrep", "-x", "claude"],
            capture_output=True, timeout=5,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


async def process_alive_sweeper_loop(app, interval: int = 60):
    """Close sessions whose Claude process has exited.

    For sessions with a PID: one batched check (``session_processes_running``,
    the verdict the credential holder set uses too). A session whose process
    is alive and started before its row was written gets a heartbeat. A
    session whose process ended, or whose PID a newer process reuses, is
    closed. An unknown answer leaves the row alone (never a false close).
    For sessions without a usable PID (none, 0 or 1): fall back to the old
    "any claude running?" heuristic.

    >>> # Verified via integration test
    """
    db_obj = getattr(app.state, "db", None)
    if db_obj is None:
        return

    try:
        while True:
            await asyncio.sleep(interval)
            try:
                stale = await asyncio.to_thread(db_obj.get_stale_open_sessions)
                if not stale:
                    continue

                changed = False

                # Split into PID-tracked and non-PID sessions
                pid_sessions = [s for s in stale if tracked_pid(s) is not None]
                no_pid_sessions = [s for s in stale if tracked_pid(s) is None]

                # PID-tracked: one batched liveness + identity check.
                running = await asyncio.to_thread(
                    session_processes_running, pid_sessions
                )
                for sess, state in zip(pid_sessions, running):
                    if state is None:
                        continue
                    if state:
                        await asyncio.to_thread(
                            db_obj.heartbeat_session, sess["session_id"]
                        )
                        changed = True
                    else:
                        await asyncio.to_thread(
                            db_obj.end_session_account, sess["session_id"]
                        )
                        changed = True
                        logger.info(
                            "Sweeper: closed session %s (PID %s ended or reused)",
                            sess["session_id"][-8:],
                            sess["pid"],
                        )

                # Non-PID: old heuristic fallback
                if no_pid_sessions:
                    alive = await asyncio.to_thread(_any_claude_process_alive)
                    if alive:
                        count = await asyncio.to_thread(
                            db_obj.bump_all_stale_sessions
                        )
                        if count > 0:
                            changed = True
                    else:
                        count = await asyncio.to_thread(db_obj.close_dead_sessions)
                        if count > 0:
                            changed = True

                if changed:
                    registry = getattr(app.state, "ws_registry", None)
                    if registry and registry.client_count > 0:
                        await registry.broadcast(
                            "sessions_changed", source="process_sweeper"
                        )

            except Exception as e:
                logger.warning("Process sweeper error: %s", e)
                continue
    except asyncio.CancelledError:
        pass
