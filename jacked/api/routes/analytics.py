"""Analytics routes — agents, hooks, lessons, usage dashboards."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel


router = APIRouter()


# --- Pydantic v2 response models ---

class AgentStats(BaseModel):
    total_spawns: int = 0
    unique_agents: int = 0
    agent_breakdown: list[dict] = []
    avg_duration_ms: Optional[float] = None


class HookStats(BaseModel):
    total_executions: int = 0
    success_rate: float = 0.0
    hook_breakdown: list[dict] = []
    avg_duration_ms: Optional[float] = None


class LessonStats(BaseModel):
    total: int = 0
    active: int = 0
    graduated: int = 0
    archived: int = 0
    top_tags: list[dict] = []


# --- Helpers ---

def _get_cutoff_iso(days: int) -> str:
    """Return ISO timestamp for N days ago."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat()


def _filter_by_date(rows: list[dict], cutoff: str, ts_field: str = "timestamp") -> list[dict]:
    """Filter rows to only those with timestamp >= cutoff."""
    return [r for r in rows if (r.get(ts_field) or "") >= cutoff]


def _get_db(request: Request):
    """Get database from app state, or None."""
    return getattr(request.app.state, "db", None)


def _get_analytics_db(request: Request):
    """Get analytics DB from app state, or None if not ready."""
    return getattr(request.app.state, "analytics_db", None)


def _db_unavailable():
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
    )


# --- Routes ---

@router.get("/agents", response_model=AgentStats)
async def agent_stats(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Agent invocation stats -- spawn frequency, duration."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    cutoff = _get_cutoff_iso(days)
    all_rows = db.list_agent_invocations(limit=10000)
    rows = _filter_by_date(all_rows, cutoff)

    total = len(rows)
    if total == 0:
        return AgentStats()

    agent_data: dict[str, dict] = {}
    durations: list[float] = []

    for r in rows:
        name = r.get("agent_name", "unknown")
        if name not in agent_data:
            agent_data[name] = {"agent": name, "count": 0, "durations": []}
        agent_data[name]["count"] += 1
        dur = r.get("duration_ms")
        if dur is not None:
            agent_data[name]["durations"].append(dur)
            durations.append(dur)

    breakdown = []
    for data in sorted(agent_data.values(), key=lambda x: x["count"], reverse=True):
        entry: dict = {"agent": data["agent"], "count": data["count"]}
        if data["durations"]:
            entry["avg_duration_ms"] = round(sum(data["durations"]) / len(data["durations"]), 1)
        breakdown.append(entry)

    avg_dur = round(sum(durations) / len(durations), 1) if durations else None

    return AgentStats(
        total_spawns=total,
        unique_agents=len(agent_data),
        agent_breakdown=breakdown,
        avg_duration_ms=avg_dur,
    )


@router.get("/hooks", response_model=HookStats)
async def hook_stats(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Hook execution stats -- success rate, avg duration."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    cutoff = _get_cutoff_iso(days)
    all_rows = db.list_hook_executions(limit=10000)["rows"]
    rows = _filter_by_date(all_rows, cutoff)

    total = len(rows)
    if total == 0:
        return HookStats()

    hook_data: dict[str, dict] = {}
    successes = 0
    durations: list[float] = []

    for r in rows:
        name = r.get("hook_name") or r.get("hook_type", "unknown")
        if name not in hook_data:
            hook_data[name] = {"hook": name, "count": 0, "success": 0, "durations": []}
        hook_data[name]["count"] += 1
        if r.get("success"):
            hook_data[name]["success"] += 1
            successes += 1
        dur = r.get("duration_ms")
        if dur is not None:
            hook_data[name]["durations"].append(dur)
            durations.append(dur)

    breakdown = []
    for data in sorted(hook_data.values(), key=lambda x: x["count"], reverse=True):
        entry: dict = {
            "hook": data["hook"],
            "count": data["count"],
            "success_rate": round(data["success"] / data["count"] * 100, 1) if data["count"] else 0,
        }
        if data["durations"]:
            entry["avg_duration_ms"] = round(sum(data["durations"]) / len(data["durations"]), 1)
        breakdown.append(entry)

    rate = (successes / total * 100) if total > 0 else 0.0
    avg_dur = round(sum(durations) / len(durations), 1) if durations else None

    return HookStats(
        total_executions=total,
        success_rate=round(rate, 1),
        hook_breakdown=breakdown,
        avg_duration_ms=avg_dur,
    )


@router.get("/lessons", response_model=LessonStats)
async def lesson_stats(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Lesson tracking stats -- active/graduated counts, top tags."""
    import json

    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    rows = db.list_lessons(limit=10000)

    total = len(rows)
    if total == 0:
        return LessonStats()

    active = 0
    graduated = 0
    archived = 0
    tag_counts: dict[str, int] = {}

    for r in rows:
        st = r.get("status", "learning")
        if st == "learning":
            active += 1
        elif st == "graduated":
            graduated += 1
        elif st == "archived":
            archived += 1
        tags_raw = r.get("tags")
        if isinstance(tags_raw, str):
            try:
                tags = json.loads(tags_raw)
                for t in tags:
                    tag_counts[t] = tag_counts.get(t, 0) + 1
            except (json.JSONDecodeError, TypeError):
                pass

    top_tags = sorted(
        [{"tag": k, "count": v} for k, v in tag_counts.items()],
        key=lambda x: x["count"],
        reverse=True,
    )[:10]

    return LessonStats(
        total=total,
        active=active,
        graduated=graduated,
        archived=archived,
        top_tags=top_tags,
    )


# --- Dashboard endpoints (new) ---


@router.get("/usage-overview")
async def get_usage_overview(request: Request, days: int = Query(default=1, ge=1, le=365)):
    """Overview: today's totals, cache health, project breakdown, active flags."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready — scan in progress"}, status_code=503)

    overview = db.get_overview(days=days)
    flags = db.get_active_flags()
    return {"overview": overview, "flags": flags}


@router.get("/usage-sessions")
async def get_usage_sessions(
    request: Request,
    days: int = Query(default=1, ge=1, le=365),
    project: str = Query(default=None),
    flagged_only: bool = Query(default=False),
):
    """Session list ranked by cost."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready"}, status_code=503)

    sessions = db.get_session_list(days=days, project_hash=project, flagged_only=flagged_only)
    return {"sessions": sessions}


@router.get("/usage-session-detail/{session_id}")
async def get_usage_session_detail(request: Request, session_id: str):
    """Message-level detail for a single session."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready"}, status_code=503)

    messages = db.get_messages_for_session(session_id)
    return {"messages": messages}


@router.get("/usage-trends")
async def get_usage_trends(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Daily summaries for trends chart."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready"}, status_code=503)

    summaries = db.get_daily_summaries(days=days)
    return {"summaries": summaries}


@router.get("/usage-flags")
async def get_usage_flags(request: Request):
    """Active anomaly flags."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready"}, status_code=503)

    return {"flags": db.get_active_flags()}


@router.post("/usage-flag-dismiss/{flag_id}")
async def dismiss_usage_flag(request: Request, flag_id: int):
    """Dismiss an anomaly flag."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready"}, status_code=503)

    db.resolve_flag(flag_id)
    return {"dismissed": True}


@router.post("/usage-flag-snooze/{flag_type}")
async def snooze_usage_flag_type(request: Request, flag_type: str, hours: int = Query(default=24)):
    """Snooze a flag type for N hours."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready"}, status_code=503)

    snooze_until = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
    db.set_setting(f"snooze_{flag_type}_until", snooze_until)
    return {"snoozed_until": snooze_until}


@router.get("/usage-scan-status")
async def get_usage_scan_status(request: Request):
    """Current scan status and DB info."""
    db = _get_analytics_db(request)
    if db is None:
        return {"status": "scanning", "ready": False}

    # Get basic DB stats
    import os
    db_path = db.db_path if hasattr(db, 'db_path') else None
    db_size = os.path.getsize(db_path) if db_path and os.path.exists(db_path) else 0
    purge_days = db.get_setting("purge_days")

    return {
        "status": "ready",
        "ready": True,
        "db_size_bytes": db_size,
        "purge_days": int(purge_days) if purge_days else None,
    }


@router.post("/usage-settings")
async def update_usage_settings(request: Request):
    """Update analytics settings (purge_days)."""
    db = _get_analytics_db(request)
    if db is None:
        return JSONResponse({"error": "Analytics not ready"}, status_code=503)

    body = await request.json()
    purge_days = body.get("purge_days")
    if purge_days is not None:
        if purge_days == 0 or purge_days is False:
            db.set_setting("purge_days", None)
        else:
            db.set_setting("purge_days", str(int(purge_days)))

    return {"updated": True}
