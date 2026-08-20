"""Incremental JSONL scanner for Claude Code conversation files.

Scans project directories for JSONL conversation logs, extracts assistant
messages with token usage, and inserts them into the analytics database.

Handles:
- Incremental scanning via byte offset (only reads new bytes)
- File rewrite detection (Claude Code rewrites JSONL on session resume)
- Subagent directory scanning ({session_uuid}/subagents/*.jsonl)
- Within-batch deduplication by message.id
- Malformed JSON lines and oversized lines (>1MB)
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from jacked.usage_normalizer import normalize_usage
from jacked.web.analytics_db import AnalyticsDB, estimate_cost

logger = logging.getLogger(__name__)

# Safety limit: skip lines larger than 1MB
_MAX_LINE_BYTES = 1024 * 1024


# ---------------------------------------------------------------------------
# Low-level message parser
# ---------------------------------------------------------------------------

def _parse_assistant_message(
    record: dict,
    session_id: str,
    project_hash: str,
    is_subagent: bool,
) -> dict | None:
    """Extract a message dict from a JSONL record.

    Returns None if the record lacks a message.id or message.usage.
    """
    message = record.get("message")
    if not message or not isinstance(message, dict):
        return None

    msg_id = message.get("id")
    if not msg_id:
        return None

    usage = message.get("usage")
    if not usage or not isinstance(usage, dict):
        return None

    model = message.get("model", "unknown")
    normalized = normalize_usage(usage)
    input_t = normalized["input_tokens"]
    output_t = normalized["output_tokens"]
    cache_read_t = normalized["cache_read_tokens"]
    cache_create_t = normalized["cache_create_tokens"]
    estimated = estimate_cost(model, input_t, output_t, cache_read_t, cache_create_t,
                              at=record.get("timestamp") or None)

    return {
        "id": msg_id,
        "session_id": session_id,
        "project_hash": project_hash,
        "timestamp": record.get("timestamp", ""),
        "model": model,
        "input_tokens": input_t,
        "output_tokens": output_t,
        "cache_read_tokens": cache_read_t,
        "cache_create_tokens": cache_create_t,
        "total_tokens": normalized["total_tokens"],
        "estimated_cost_usd": estimated,
        "cost_usd": normalized["cost_usd"],
        "cost_source": "provider" if normalized["cost_usd"] is not None else "estimate",
        "cost_is_authoritative": normalized["cost_usd"] is not None,
        "is_subagent": 1 if is_subagent else 0,
    }


# ---------------------------------------------------------------------------
# JSONL parser with byte-offset support
# ---------------------------------------------------------------------------

def parse_jsonl_from_offset(
    file_path: Path,
    offset: int,
) -> tuple[list[dict], int]:
    """Read JSONL from byte offset, return (parsed_messages, new_offset).

    - If offset > 0, the first partial line is discarded (we may be mid-line).
    - Lines > 1MB are skipped.
    - Malformed JSON lines are skipped.
    - Only ``type == "assistant"`` records with ``message.usage`` are processed.
    - Deduplicates within the batch by message.id.
    """
    try:
        file_size = file_path.stat().st_size
    except OSError:
        return [], 0

    if file_size == 0 or offset >= file_size:
        return [], offset if offset <= file_size else 0

    messages: list[dict] = []
    seen_ids: set[str] = set()

    try:
        with open(file_path, "rb") as f:
            f.seek(offset)

            for raw_line in f:
                if len(raw_line) > _MAX_LINE_BYTES:
                    continue

                line = raw_line.strip()
                if not line:
                    continue

                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue

                if not isinstance(record, dict):
                    continue

                if record.get("type") != "assistant":
                    continue

                # Extract session_id from record
                session_id = record.get("sessionId", "")

                parsed = _parse_assistant_message(
                    record, session_id, "", is_subagent=False,
                )
                if parsed is None:
                    continue

                # Deduplicate within batch
                if parsed["id"] in seen_ids:
                    continue
                seen_ids.add(parsed["id"])

                messages.append(parsed)

            new_offset = f.tell()
    except OSError as e:
        logger.warning("Failed to read %s: %s", file_path, e)
        return [], offset

    return messages, new_offset


# ---------------------------------------------------------------------------
# Project directory scanner
# ---------------------------------------------------------------------------

def _collect_jsonl_files(project_dir: Path) -> list[tuple[Path, str, bool]]:
    """Collect all JSONL files in a project directory.

    Returns list of (file_path, session_id, is_subagent) tuples.

    - Top-level: ``{uuid}.jsonl`` -> session_id = uuid stem
    - Subagents: ``{uuid}/subagents/agent-xxx.jsonl`` -> session_id = parent uuid
    """
    results: list[tuple[Path, str, bool]] = []

    try:
        for entry in project_dir.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                session_id = entry.stem
                results.append((entry, session_id, False))
            elif entry.is_dir():
                # Check for subagents/ directory inside session dirs
                subagents_dir = entry / "subagents"
                if subagents_dir.is_dir():
                    parent_session_id = entry.name
                    try:
                        for sub_entry in subagents_dir.iterdir():
                            if sub_entry.is_file() and sub_entry.suffix == ".jsonl":
                                results.append((sub_entry, parent_session_id, True))
                    except (PermissionError, OSError):
                        continue
    except (PermissionError, OSError):
        pass

    return results


def scan_project_dir(
    project_dir: Path,
    project_hash: str,
    db: AnalyticsDB,
) -> int:
    """Scan all JSONL files in a project directory.

    Returns count of NEW messages inserted.
    """
    if not project_dir.is_dir():
        return 0

    total_new = 0
    files = _collect_jsonl_files(project_dir)

    for file_path, session_id, is_subagent in files:
        try:
            stat = file_path.stat()
        except OSError:
            continue

        file_path_str = str(file_path)
        current_mtime = stat.st_mtime
        current_size = stat.st_size

        # Check scan state
        scan_state = db.get_scan_state(file_path_str)

        if scan_state is not None:
            stored_mtime = scan_state["last_mtime"]
            stored_offset = scan_state["last_byte_offset"]

            # Skip if mtime unchanged
            if stored_mtime == current_mtime:
                continue

            # CRITICAL: if stored offset > current file size, file was rewritten
            if stored_offset > current_size:
                offset = 0
            else:
                offset = stored_offset
        else:
            offset = 0

        # Parse new messages from offset
        messages, new_offset = parse_jsonl_from_offset(file_path, offset)

        # Override session_id and project_hash for each message
        for msg in messages:
            msg["session_id"] = session_id
            msg["project_hash"] = project_hash
            msg["is_subagent"] = 1 if is_subagent else 0

        # Insert messages into DB
        if messages:
            db.insert_messages(messages)
            total_new += len(messages)

        # Update scan state
        existing_count = scan_state["messages_count"] if scan_state else 0
        db.update_scan_state(
            file_path_str,
            new_offset,
            current_mtime,
            existing_count + len(messages),
        )

    return total_new


# ---------------------------------------------------------------------------
# Full project scan (async entry point)
# ---------------------------------------------------------------------------

def _count_project_dirs(data_dirs: list[Path]) -> int:
    """Count the number of project directories across all data dirs."""
    count = 0
    for data_dir in data_dirs:
        try:
            if not data_dir.is_dir():
                continue
            for entry in data_dir.iterdir():
                if entry.is_dir():
                    count += 1
        except (PermissionError, OSError):
            continue
    return count


async def scan_all_projects(
    data_dirs: list[Path],
    db: AnalyticsDB,
    progress_callback=None,
) -> dict:
    """Walk all project directories, scan each, report progress.

    Runs file I/O in a thread pool to avoid blocking the event loop.
    """
    total_projects = _count_project_dirs(data_dirs)
    scanned = 0
    total_messages = 0

    for data_dir in data_dirs:
        try:
            if not data_dir.is_dir():
                continue
            entries = sorted(data_dir.iterdir())
        except (PermissionError, OSError):
            continue

        for project_dir in entries:
            if not project_dir.is_dir():
                continue

            project_hash = project_dir.name
            count = await asyncio.to_thread(
                scan_project_dir, project_dir, project_hash, db,
            )
            total_messages += count
            scanned += 1

            if progress_callback:
                await progress_callback({
                    "projects_scanned": scanned,
                    "projects_total": total_projects,
                    "messages_parsed": total_messages,
                    "current_project": project_hash,
                })

            await asyncio.sleep(0)  # yield to event loop

    # Prune stale scan state entries
    prune_stale_scan_state(db, data_dirs)

    return {"projects": scanned, "messages": total_messages}


# ---------------------------------------------------------------------------
# Stale scan state pruning
# ---------------------------------------------------------------------------

def prune_stale_scan_state(db: AnalyticsDB, data_dirs: list[Path]) -> None:
    """Remove scan state entries for JSONL files that no longer exist."""
    valid_paths: set[str] = set()

    for data_dir in data_dirs:
        try:
            if not data_dir.is_dir():
                continue
            for project_dir in data_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                for file_path, _, _ in _collect_jsonl_files(project_dir):
                    valid_paths.add(str(file_path))
        except (PermissionError, OSError):
            continue

    db.prune_stale_scan_state(valid_paths)
