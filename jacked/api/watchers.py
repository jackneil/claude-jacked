"""Background watcher loops for the jacked dashboard.

These async loops poll SQLite tables for changes and broadcast
notifications through the WebSocket registry.  Extracted from
main.py to stay under the 500-line guardrail.
"""

import asyncio
import logging
import sqlite3
import subprocess
from pathlib import Path

from jacked.api.websocket import WebSocketRegistry

logger = logging.getLogger(__name__)


async def session_accounts_watch_loop(app, interval: int = 3):
    """Watch session_accounts table for changes, broadcast via WebSocket.

    Uses PRAGMA data_version (connection-scoped) to cheaply detect when any
    external process writes to the DB.  Only queries session_accounts when
    the version changes, and only broadcasts when the actual MAX timestamps
    differ from cached values.

    Also forces a periodic broadcast every ~60s (20 cycles) to handle
    time-based session expiry.  Heartbeat writes change data_version but
    the secondary MAX(detected_at)/MAX(ended_at) check filters them out
    (heartbeats only touch last_activity_at).  The periodic broadcast
    ensures the dashboard re-evaluates the 60-minute read-side filter.

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
    cycle_counter = 0
    FORCE_BROADCAST_EVERY = 20  # ~60s at 3s interval

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
            cycle_counter += 1
            try:
                should_broadcast = False

                # Periodic forced broadcast for time-based session expiry
                if cycle_counter >= FORCE_BROADCAST_EVERY:
                    cycle_counter = 0
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
    """Watch gatekeeper_decisions, hook_executions, version_checks for changes.

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
        "gatekeeper_decisions": None,
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
    """Resurrect stale sessions if Claude processes are still alive.

    Runs every ``interval`` seconds (default 60).  Uses a coarse strategy:
    if ANY Claude process is running, bump ALL stale sessions.  This avoids
    the problem where fresh sessions (started without ``--resume``) have no
    session ID in their process arguments.

    If no Claude processes are alive and sessions are stale for more than
    DEAD_SESSION_HOURS (4h), auto-close them.

    >>> # Verified via integration test
    """
    db_obj = getattr(app.state, "db", None)
    if db_obj is None:
        return

    try:
        while True:
            await asyncio.sleep(interval)
            try:
                # 1. Check for stale sessions
                stale = await asyncio.to_thread(db_obj.get_stale_open_sessions)
                if not stale:
                    continue

                # 2. Are any Claude processes alive?
                alive = await asyncio.to_thread(_any_claude_process_alive)
                changed = False

                if alive:
                    # Bump all stale sessions — at least one process is running
                    count = await asyncio.to_thread(db_obj.bump_all_stale_sessions)
                    if count > 0:
                        changed = True
                        logger.info(
                            "Process sweeper: bumped %d stale session(s)", count
                        )
                else:
                    # No Claude processes — close sessions stale > DEAD_SESSION_HOURS
                    count = await asyncio.to_thread(db_obj.close_dead_sessions)
                    if count > 0:
                        changed = True
                        logger.info(
                            "Process sweeper: closed %d dead session(s)", count
                        )

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
