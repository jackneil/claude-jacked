"""Background loops for analytics: initial JSONL scan and live file polling.

Two async tasks meant to run as background tasks on the FastAPI app:

- ``initial_scan_loop(app)`` — runs once on startup, parses all JSONL history
  and broadcasts progress via WebSocket.
- ``live_monitor_loop(app)`` — polls recently-active JSONL files for new data
  and pushes incremental updates to connected dashboards.

All file I/O runs via ``asyncio.to_thread()`` to avoid blocking the event loop.
"""

from __future__ import annotations

import asyncio
import logging
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
        for date_str in dates:
            try:
                db.rollup_daily_summaries(date_str)
            except Exception:
                logger.debug("Rollup failed for %s", date_str, exc_info=True)
                continue

        if dates:
            db.set_setting("last_rollup_date", dates[-1])
    except Exception:
        logger.debug("_rollup_pending_days failed", exc_info=True)


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
    from jacked.web.analytics_db import AnalyticsDB
    from jacked.web.analytics_scanner import scan_all_projects
    from jacked.web.analytics_paths import get_claude_data_dirs

    # Wait for other services to initialize
    await asyncio.sleep(5)

    try:
        db_path = str(Path.home() / ".claude" / "jacked-analytics.db")
        db = AnalyticsDB(db_path)
        app.state.analytics_db = db

        data_dirs = get_claude_data_dirs()
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
            from jacked.web.analytics_anomalies import detect_anomalies, auto_resolve_flags
            flags = detect_anomalies(db)
            auto_resolve_flags(db)
            if flags:
                logger.info(
                    "Analytics: %d anomaly flags raised during initial scan",
                    len(flags),
                )
        except ImportError:
            logger.debug("analytics_anomalies not available yet — skipping")
        except Exception:
            logger.debug("Initial anomaly detection failed", exc_info=True)

        # Rollup any unsummarized days
        await asyncio.to_thread(_rollup_pending_days, db)

        # Purge if configured
        purge_days = db.get_setting("purge_days")
        if purge_days:
            try:
                await asyncio.to_thread(db.purge_messages_older_than, int(purge_days))
            except (ValueError, Exception):
                pass

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
    from jacked.web.analytics_paths import get_claude_data_dirs, find_active_jsonl_files
    from jacked.web.analytics_scanner import parse_jsonl_from_offset

    # Wait for initial scan to complete (it sets app.state.analytics_db)
    while not hasattr(app.state, "analytics_db"):
        await asyncio.sleep(5)

    db = app.state.analytics_db
    data_dirs = get_claude_data_dirs()
    active_files: dict[str, int] = {}  # path -> last_byte_offset

    while True:
        try:
            ws = getattr(app.state, "ws_registry", None)
            has_viewers = ws.has_subscribers("analytics") if ws else False
            interval = 1.0 if has_viewers else 5.0

            active = await asyncio.to_thread(
                find_active_jsonl_files, data_dirs, 600,
            )
            new_messages_total: list[dict] = []
            affected_sessions: set[str] = set()

            for jsonl_path in active:
                path_str = str(jsonl_path)
                try:
                    size = jsonl_path.stat().st_size
                except OSError:
                    continue

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
                    from jacked.web.analytics_anomalies import (
                        detect_anomalies,
                        auto_resolve_flags,
                    )
                    new_flags = detect_anomalies(
                        db, session_ids=list(affected_sessions),
                    )
                    auto_resolve_flags(db)

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
