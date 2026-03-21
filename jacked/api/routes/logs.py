"""Server log endpoints — live log buffer access."""

from __future__ import annotations

from fastapi import APIRouter, Query

from jacked.api.log_capture import server_log_buffer

router = APIRouter()

_LEVEL_ORDER = {"DEBUG": 0, "INFO": 1, "WARNING": 2, "ERROR": 3, "CRITICAL": 4}


@router.get("/logs/server")
async def list_server_logs(
    limit: int = Query(default=200, ge=1, le=2000),
    level: str | None = Query(default=None),
) -> dict:
    """Return recent server log entries from the in-memory ring buffer.

    Query params:
      - ``limit`` — max entries to return (default 200, max 2000)
      - ``level`` — minimum severity filter (DEBUG/INFO/WARNING/ERROR/CRITICAL)
    """
    entries = server_log_buffer.get_recent(limit=limit if not level else 2000)

    if level:
        min_ord = _LEVEL_ORDER.get(level.upper(), 0)
        entries = [e for e in entries if _LEVEL_ORDER.get(e["level"], 0) >= min_ord]
        entries = entries[-limit:]

    return {"entries": entries, "buffer_size": server_log_buffer.buffer_size}
