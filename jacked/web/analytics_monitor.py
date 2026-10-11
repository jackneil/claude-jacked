"""Background loops for analytics: initial JSONL scan and live file polling.

Two async tasks meant to run as background tasks on the FastAPI app:

- ``initial_scan_loop(app)`` — runs once on startup, parses all JSONL history
  and broadcasts progress via WebSocket.
- ``live_monitor_loop(app)`` — polls recently-active JSONL files for new data
  and pushes incremental updates to connected dashboards.

All file I/O and all SQLite work run via ``asyncio.to_thread()``. The
service has ONE event loop for every HTTP route. A slow query on that loop
stops all routes (the 2026-10-10 Windows outage: the anomaly detector's
7-day average query blocked the loop on an I/O-saturated machine).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper: rollup pending days
# ---------------------------------------------------------------------------

def _rollup_pending_days(db) -> None:
    """Roll up unsummarized days into daily_summaries.

    Looks at the ``last_rollup_date`` setting, finds distinct message dates
    after it, and calls ``db.rollup_daily_summaries(date)`` for each.
    """
    from jacked.web.analytics_db import AnalyticsDB  # noqa: F401 — type hint

    last_rollup = db.get_setting("last_rollup_date")

    # Query distinct dates from messages that need rollup.
    # We exclude today (still in progress) and go up to yesterday.
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")

    try:
        with db._reader() as conn:
            if last_rollup:
                rows = conn.execute(
                    "SELECT DISTINCT substr(timestamp, 1, 10) AS d "
                    "FROM messages "
                    "WHERE substr(timestamp, 1, 10) > ? "
                    "  AND substr(timestamp, 1, 10) <= ? "
                    "ORDER BY d",
                    (last_rollup, yesterday),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT DISTINCT substr(timestamp, 1, 10) AS d "
                    "FROM messages "
                    "WHERE substr(timestamp, 1, 10) <= ? "
                    "ORDER BY d",
                    (yesterday,),
                ).fetchall()

        dates = [r["d"] for r in rows if r["d"]]
        last_success = None
        for date_str in dates:
            try:
                db.rollup_daily_summaries(date_str)
                last_success = date_str
            except Exception:
                logger.debug("Rollup failed for %s", date_str, exc_info=True)
                break  # Stop at first failure — don't skip dates

        if last_success:
            db.set_setting("last_rollup_date", last_success)
    except Exception:
        logger.debug("_rollup_pending_days failed", exc_info=True)


# ---------------------------------------------------------------------------
# Helper: anomaly pass (runs in a worker thread)
# ---------------------------------------------------------------------------

# Serializes anomaly passes. ``detect_anomalies`` reads "is a flag already
# active" and then inserts, and the flags table has no unique constraint. The
# initial scan's full pass and a live pass run in different worker threads,
# so without this lock both can insert the same flag. The lock blocks only
# the other worker thread, never the event loop.
_ANOMALY_PASS_LOCK = threading.Lock()


def _run_anomaly_pass(db, session_ids: list[str] | None = None) -> list[dict]:
    """Detect anomalies, then auto-resolve stale flags. Return the new flags.

    This function does blocking SQLite work on the analytics DB. Call it
    through ``asyncio.to_thread`` only, never on the event loop.

    Raises ImportError when the anomaly module is not available.
    """
    from jacked.web.analytics_anomalies import auto_resolve_flags, detect_anomalies

    with _ANOMALY_PASS_LOCK:
        flags = detect_anomalies(db, session_ids=session_ids)
        auto_resolve_flags(db)
    return flags


def _stat_sizes(paths: list[Path]) -> list[tuple[Path, int]]:
    """Return (path, size) for each path that can be read.

    The live monitor calls this through ``asyncio.to_thread``, so the stat
    calls do not block the event loop. A path that fails ``stat`` is skipped.

    >>> _stat_sizes([Path("/nonexistent/jacked/file.jsonl")])
    []
    """
    out: list[tuple[Path, int]] = []
    for path in paths:
        try:
            out.append((path, path.stat().st_size))
        except OSError:
            continue
    return out


def _find_active_with_sizes(data_dirs: list[Path], max_age_seconds: int) -> list[tuple[Path, int]]:
    """Find recently modified JSONL files and return them with their sizes."""
    from jacked.web.analytics_paths import find_active_jsonl_files

    return _stat_sizes(find_active_jsonl_files(data_dirs, max_age_seconds))


#: Minimum time in seconds between two "Live anomaly detection failed"
#: warnings. The live loop runs each 1 to 5 s, so a persistent failure
#: without this limit writes a traceback to the log each cycle.
LIVE_FAILURE_WARN_INTERVAL = 300.0


class _WarnLimiter:
    """Decide if a repeated failure gets a WARNING or a DEBUG record.

    The first failure gets a WARNING. Then one WARNING is permitted each
    *interval* seconds. ``suppressed`` counts the failures since the last
    WARNING, so the next WARNING can give that count.

    >>> clock = iter([0.0, 10.0, 400.0]).__next__
    >>> limiter = _WarnLimiter(300.0, clock=clock)
    >>> limiter.should_warn(), limiter.should_warn(), limiter.suppressed
    (True, False, 1)
    >>> limiter.should_warn(), limiter.take_suppressed(), limiter.suppressed
    (True, 1, 0)
    """

    def __init__(self, interval: float, clock=None) -> None:
        self.interval = interval
        self._clock = clock or time.monotonic
        self._last_warn: float | None = None
        self.suppressed = 0

    def should_warn(self) -> bool:
        """Return True when this failure must be logged as a WARNING."""
        now = self._clock()
        if self._last_warn is None or now - self._last_warn >= self.interval:
            self._last_warn = now
            return True
        self.suppressed += 1
        return False

    def take_suppressed(self) -> int:
        """Return the suppressed count and set it to zero."""
        count, self.suppressed = self.suppressed, 0
        return count


def _open_analytics_db():
    """Open the analytics DB (schema init and migrations run here)."""
    from jacked.web.analytics_db import AnalyticsDB

    db_path = str(Path.home() / ".claude" / "jacked-analytics.db")
    return AnalyticsDB(db_path)


# ---------------------------------------------------------------------------
# Path helpers for the live monitor
# ---------------------------------------------------------------------------

def _extract_ids_from_jsonl_path(jsonl_path: Path) -> tuple[str, str, bool]:
    """Derive (project_hash, session_id, is_subagent) from a JSONL path.

    Expected layouts under a data_dir::

        {data_dir}/{project_hash}/{session_uuid}.jsonl
        {data_dir}/{project_hash}/{session_uuid}/subagents/agent-xxx.jsonl

    Returns ("", "", False) if the path doesn't match the expected structure.
    """
    parts = jsonl_path.parts

    # .../project_hash/session.jsonl  (parent = project_hash dir)
    # .../project_hash/session_uuid/subagents/agent.jsonl
    if len(parts) >= 2:
        parent_name = jsonl_path.parent.name
        if parent_name == "subagents" and len(parts) >= 4:
            # subagent file: grandparent.parent = session dir, grandparent.parent.parent = project dir
            session_id = jsonl_path.parent.parent.name
            project_hash = jsonl_path.parent.parent.parent.name
            return project_hash, session_id, True
        else:
            # top-level session file
            session_id = jsonl_path.stem
            project_hash = parent_name
            return project_hash, session_id, False

    return "", "", False


# ---------------------------------------------------------------------------
# Initial scan loop
# ---------------------------------------------------------------------------

async def initial_scan_loop(app) -> None:
    """Parse all JSONL files on startup, broadcast progress via WebSocket."""
    from jacked.web.analytics_scanner import scan_all_projects
    from jacked.web.analytics_paths import get_claude_data_dirs

    # Wait for other services to initialize
    await asyncio.sleep(5)

    try:
        # Schema init runs a migration UPDATE over the messages table, so
        # the open must not run on the event loop either.
        db = await asyncio.to_thread(_open_analytics_db)
        app.state.analytics_db = db

        data_dirs = await asyncio.to_thread(get_claude_data_dirs)
        if not data_dirs:
            logger.info("No Claude Code data directories found — analytics disabled")
            return

        ws = getattr(app.state, "ws_registry", None)

        async def on_progress(info: dict) -> None:
            if ws:
                try:
                    await ws.broadcast("analytics_scan_progress", {
                        "status": "scanning",
                        **info,
                    })
                except Exception:
                    pass

        logger.info(
            "Analytics: starting initial scan of %d data directories",
            len(data_dirs),
        )
        result = await scan_all_projects(data_dirs, db, progress_callback=on_progress)
        logger.info(
            "Analytics: initial scan complete — %d projects, %d messages",
            result.get("projects", 0),
            result.get("messages", 0),
        )

        # Run initial anomaly detection (Task 4 may not be committed yet)
        try:
            flags = await asyncio.to_thread(_run_anomaly_pass, db)
            if flags:
                logger.info(
                    "Analytics: %d anomaly flags raised during initial scan",
                    len(flags),
                )
        except ImportError:
            logger.debug("analytics_anomalies not available yet — skipping")
        except Exception:
            logger.warning("Initial anomaly detection failed", exc_info=True)

        # Rollup any unsummarized days
        await asyncio.to_thread(_rollup_pending_days, db)

        # Purge if configured
        purge_days = await asyncio.to_thread(db.get_setting, "purge_days")
        if purge_days:
            try:
                await asyncio.to_thread(db.purge_messages_older_than, int(purge_days))
            except Exception:
                logger.warning("Analytics purge failed", exc_info=True)

        # Broadcast completion
        if ws:
            try:
                await ws.broadcast("analytics_scan_complete", {
                    "status": "complete",
                    "projects": result.get("projects", 0),
                    "messages": result.get("messages", 0),
                })
            except Exception:
                pass

    except Exception:
        logger.warning("Analytics initial scan failed", exc_info=True)


# ---------------------------------------------------------------------------
# Live monitor loop
# ---------------------------------------------------------------------------

async def live_monitor_loop(app) -> None:
    """Poll active JSONL files and push updates via WebSocket."""
    from jacked.web.analytics_paths import get_claude_data_dirs
    from jacked.web.analytics_scanner import parse_jsonl_from_offset

    # Wait for initial scan to complete (it sets app.state.analytics_db)
    while not hasattr(app.state, "analytics_db"):
        await asyncio.sleep(5)

    db = app.state.analytics_db
    data_dirs = await asyncio.to_thread(get_claude_data_dirs)
    active_files: dict[str, int] = {}  # path -> last_byte_offset
    detect_failures = _WarnLimiter(LIVE_FAILURE_WARN_INTERVAL)

    while True:
        try:
            ws = getattr(app.state, "ws_registry", None)
            has_viewers = ws.has_subscribers("analytics") if ws else False
            interval = 1.0 if has_viewers else 5.0

            # One worker-thread call finds the active files AND reads their
            # sizes, so no stat() runs on the event loop.
            active = await asyncio.to_thread(
                _find_active_with_sizes, data_dirs, 600,
            )
            new_messages_total: list[dict] = []
            affected_sessions: set[str] = set()

            for jsonl_path, size in active:
                path_str = str(jsonl_path)
                last_offset = active_files.get(path_str, 0)

                # Handle file rewrites (offset > size)
                if last_offset > size:
                    last_offset = 0

                if size <= last_offset:
                    continue

                # Parse new bytes in thread pool
                messages, new_offset = await asyncio.to_thread(
                    parse_jsonl_from_offset, jsonl_path, last_offset,
                )
                active_files[path_str] = new_offset

                if messages:
                    # Derive project_hash and session_id from directory structure
                    project_hash, session_id, is_subagent = _extract_ids_from_jsonl_path(
                        jsonl_path,
                    )
                    for m in messages:
                        m["project_hash"] = project_hash
                        m["session_id"] = session_id or m.get("session_id", "")
                        m["is_subagent"] = 1 if is_subagent else m.get("is_subagent", 0)

                    await asyncio.to_thread(db.insert_messages, messages)
                    new_messages_total.extend(messages)
                    for m in messages:
                        sid = m.get("session_id")
                        if sid:
                            affected_sessions.add(sid)

            # Run anomaly detection on affected sessions
            if affected_sessions:
                try:
                    new_flags = await asyncio.to_thread(
                        _run_anomaly_pass, db, list(affected_sessions),
                    )

                    # Broadcast flag events
                    if ws and new_flags:
                        for flag in new_flags:
                            try:
                                await ws.broadcast("analytics_flag_raised", flag)
                            except Exception:
                                pass
                except ImportError:
                    pass
                except Exception:
                    if detect_failures.should_warn():
                        skipped = detect_failures.take_suppressed()
                        suffix = (
                            f" ({skipped} more failures since the last warning)"
                            if skipped
                            else ""
                        )
                        logger.warning(
                            "Live anomaly detection failed%s",
                            suffix,
                            exc_info=True,
                        )
                    else:
                        logger.debug("Live anomaly detection failed", exc_info=True)

            # Broadcast live update if new data and viewers connected
            if new_messages_total and ws and has_viewers:
                try:
                    await ws.broadcast("analytics_live_update", {
                        "new_messages": len(new_messages_total),
                        "sessions_affected": list(affected_sessions),
                    })
                except Exception:
                    pass

            await asyncio.sleep(interval)

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Analytics live monitor error", exc_info=True)
            await asyncio.sleep(10)
