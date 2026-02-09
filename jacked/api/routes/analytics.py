"""Analytics routes — gatekeeper, agents, hooks, lessons."""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

router = APIRouter()


# --- Pydantic v2 response models ---

class GatekeeperStats(BaseModel):
    total_decisions: int = 0
    approval_rate: float = 0.0
    method_breakdown: dict[str, int] = {}
    decision_breakdown: dict[str, int] = {}
    recent_denials: list[dict] = []


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


def _db_unavailable():
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
    )


# --- Routes ---

@router.get("/gatekeeper", response_model=GatekeeperStats)
async def gatekeeper_stats(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Security gatekeeper decision stats -- approval rate, method breakdown."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    cutoff = _get_cutoff_iso(days)
    all_rows = db.list_gatekeeper_decisions(limit=10000)
    rows = _filter_by_date(all_rows, cutoff)

    total = len(rows)
    if total == 0:
        return GatekeeperStats()

    decision_counts: dict[str, int] = {}
    method_counts: dict[str, int] = {}
    denials = []

    for r in rows:
        decision = r.get("decision", "UNKNOWN")
        method = r.get("method", "UNKNOWN")
        decision_counts[decision] = decision_counts.get(decision, 0) + 1
        method_counts[method] = method_counts.get(method, 0) + 1
        if decision == "DENY":
            denials.append({
                "timestamp": r.get("timestamp"),
                "command": (r.get("command") or "")[:100],
                "reason": r.get("reason"),
            })

    allowed = decision_counts.get("ALLOW", 0)
    rate = (allowed / total * 100) if total > 0 else 0.0

    return GatekeeperStats(
        total_decisions=total,
        approval_rate=round(rate, 1),
        method_breakdown=method_counts,
        decision_breakdown=decision_counts,
        recent_denials=denials[-10:],
    )


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
    all_rows = db.list_hook_executions(limit=10000)
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
