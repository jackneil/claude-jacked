"""Anomaly detectors for analytics data.

V1 detectors:
- cache_drop: session cache hit ratio much lower than 7-day average
- cost_outlier: session cost much higher than 7-day per-session average
- subagent_explosion: too many subagent messages in a session
"""

import logging
from datetime import datetime, timedelta, timezone

from jacked.web.analytics_db import AnalyticsDB

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

CACHE_DROP_WARNING_RATIO = 0.70      # < 70% when avg is 90%+
CACHE_DROP_CRITICAL_RATIO = 0.30     # < 30% always critical
CACHE_DROP_AVG_MIN = 0.90            # 7-day avg must be 90%+ to fire warning
CACHE_DROP_MIN_MESSAGES = 10         # skip sessions with < 10 messages
CACHE_DROP_WARMUP_SKIP = 5           # exclude first N messages (cache warmup)

COST_OUTLIER_WARNING_MULT = 3.0      # > 3x average
COST_OUTLIER_CRITICAL_MULT = 6.0     # > 6x average
COST_OUTLIER_MIN_MESSAGES = 5        # skip sessions with < 5 messages
COST_OUTLIER_MIN_DURATION_MIN = 5    # skip sessions < 5 minutes

SUBAGENT_WARNING_THRESHOLD = 20      # > 20 subagent messages
SUBAGENT_CRITICAL_THRESHOLD = 50     # > 50 subagent messages


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _is_snoozed(db: AnalyticsDB, flag_type: str) -> bool:
    """Check if a flag type is currently snoozed."""
    snooze_until = db.get_setting(f"snooze_{flag_type}_until")
    if snooze_until is None:
        return False
    try:
        snooze_dt = datetime.fromisoformat(snooze_until)
        if snooze_dt.tzinfo is None:
            snooze_dt = snooze_dt.replace(tzinfo=timezone.utc)
        return datetime.now(timezone.utc) < snooze_dt
    except (ValueError, TypeError):
        return False


def _has_active_flag(db: AnalyticsDB, session_id: str, flag_type: str) -> bool:
    """Check if an active (unresolved) flag already exists for this session+type."""
    for f in db.get_active_flags():
        if f["session_id"] == session_id and f["flag_type"] == flag_type:
            return True
    return False


def _7day_cutoff_iso() -> str:
    """Return UTC ISO timestamp for 7 days ago."""
    return (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()


# ---------------------------------------------------------------------------
# cache_drop detector
# ---------------------------------------------------------------------------

def _detect_cache_drop(db: AnalyticsDB, session_ids: list[str] | None = None) -> list[dict]:
    """Detect sessions with abnormally low cache hit rates.

    Cache hit ratio = cache_read / (cache_read + input + cache_create)
    Compares session ratio (excluding first 5 warmup messages) against
    the 7-day rolling average. Only fires for sessions with 10+ messages.
    """
    if _is_snoozed(db, "cache_drop"):
        return []

    cutoff = _7day_cutoff_iso()
    flags = []

    # Compute 7-day rolling average cache hit ratio across all messages
    with db._reader() as conn:
        row = conn.execute(
            "SELECT "
            "  COALESCE(SUM(cache_read_tokens), 0) AS total_cache_read, "
            "  COALESCE(SUM(input_tokens), 0) AS total_input, "
            "  COALESCE(SUM(cache_create_tokens), 0) AS total_cache_create "
            "FROM messages WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()

    total_denom = row["total_cache_read"] + row["total_input"] + row["total_cache_create"]
    if total_denom == 0:
        return []
    avg_ratio = row["total_cache_read"] / total_denom

    # Get candidate sessions
    where_clause = ""
    params: list = []
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        where_clause = f"WHERE session_id IN ({placeholders})"
        params = list(session_ids)

    with db._reader() as conn:
        sessions = conn.execute(
            f"SELECT session_id, project_hash, COUNT(*) AS msg_count "
            f"FROM messages {where_clause} "
            f"GROUP BY session_id HAVING msg_count >= ?",
            params + [CACHE_DROP_MIN_MESSAGES],
        ).fetchall()

    for s in sessions:
        sid = s["session_id"]
        if _has_active_flag(db, sid, "cache_drop"):
            continue

        # Get messages for this session, ordered by timestamp, skip first 5
        with db._reader() as conn:
            rows = conn.execute(
                "SELECT cache_read_tokens, input_tokens, cache_create_tokens "
                "FROM messages WHERE session_id = ? ORDER BY timestamp "
                f"LIMIT -1 OFFSET {CACHE_DROP_WARMUP_SKIP}",
                (sid,),
            ).fetchall()

        if not rows:
            continue

        total_read = sum(r["cache_read_tokens"] for r in rows)
        total_input = sum(r["input_tokens"] for r in rows)
        total_create = sum(r["cache_create_tokens"] for r in rows)
        denom = total_read + total_input + total_create
        if denom == 0:
            continue

        session_ratio = total_read / denom

        # Determine severity
        severity = None
        if session_ratio < CACHE_DROP_CRITICAL_RATIO:
            severity = "critical"
        elif session_ratio < CACHE_DROP_WARNING_RATIO and avg_ratio >= CACHE_DROP_AVG_MIN:
            severity = "warning"

        if severity:
            flags.append({
                "flag_type": "cache_drop",
                "severity": severity,
                "session_id": sid,
                "project_hash": s["project_hash"],
                "message": (
                    f"Cache hit ratio {session_ratio:.0%} "
                    f"(7-day avg {avg_ratio:.0%})"
                ),
            })

    return flags


# ---------------------------------------------------------------------------
# cost_outlier detector
# ---------------------------------------------------------------------------

def _detect_cost_outlier(db: AnalyticsDB, session_ids: list[str] | None = None) -> list[dict]:
    """Detect sessions with abnormally high cost.

    Compares session total cost against the 7-day per-session average.
    Only fires for sessions with 5+ messages and 5+ minute duration.
    """
    if _is_snoozed(db, "cost_outlier"):
        return []

    cutoff = _7day_cutoff_iso()
    flags = []

    # Get candidate sessions first
    where_clause = ""
    params: list = []
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        where_clause = f"WHERE session_id IN ({placeholders})"
        params = list(session_ids)

    with db._reader() as conn:
        sessions = conn.execute(
            f"SELECT session_id, project_hash, "
            f"  COUNT(*) AS msg_count, "
            f"  SUM(estimated_cost_usd) AS total_cost, "
            f"  MIN(timestamp) AS first_ts, "
            f"  MAX(timestamp) AS last_ts "
            f"FROM messages {where_clause} "
            f"GROUP BY session_id HAVING msg_count >= ?",
            params + [COST_OUTLIER_MIN_MESSAGES],
        ).fetchall()

    # Compute 7-day per-session average cost excluding candidates
    candidate_ids = {s["session_id"] for s in sessions} if session_ids else set()
    with db._reader() as conn:
        if candidate_ids:
            excl_ph = ",".join("?" for _ in candidate_ids)
            avg_row = conn.execute(
                "SELECT AVG(session_cost) AS avg_cost FROM ("
                "  SELECT session_id, SUM(estimated_cost_usd) AS session_cost "
                "  FROM messages WHERE timestamp >= ? "
                f"  AND session_id NOT IN ({excl_ph}) "
                "  GROUP BY session_id HAVING COUNT(*) >= ?"
                ")",
                (cutoff, *candidate_ids, COST_OUTLIER_MIN_MESSAGES),
            ).fetchone()
        else:
            avg_row = conn.execute(
                "SELECT AVG(session_cost) AS avg_cost FROM ("
                "  SELECT session_id, SUM(estimated_cost_usd) AS session_cost "
                "  FROM messages WHERE timestamp >= ? "
                "  GROUP BY session_id HAVING COUNT(*) >= ?"
                ")",
                (cutoff, COST_OUTLIER_MIN_MESSAGES),
            ).fetchone()

    avg_cost = avg_row["avg_cost"] if avg_row and avg_row["avg_cost"] else None
    if avg_cost is None or avg_cost == 0:
        return []

    for s in sessions:
        sid = s["session_id"]
        if _has_active_flag(db, sid, "cost_outlier"):
            continue

        # Check minimum duration (5 minutes)
        try:
            first_dt = datetime.fromisoformat(s["first_ts"])
            last_dt = datetime.fromisoformat(s["last_ts"])
            duration_min = (last_dt - first_dt).total_seconds() / 60.0
        except (ValueError, TypeError):
            continue

        if duration_min < COST_OUTLIER_MIN_DURATION_MIN:
            continue

        total_cost = s["total_cost"]
        multiplier = total_cost / avg_cost

        severity = None
        if multiplier >= COST_OUTLIER_CRITICAL_MULT:
            severity = "critical"
        elif multiplier >= COST_OUTLIER_WARNING_MULT:
            severity = "warning"

        if severity:
            flags.append({
                "flag_type": "cost_outlier",
                "severity": severity,
                "session_id": sid,
                "project_hash": s["project_hash"],
                "message": (
                    f"Session cost ${total_cost:.2f} is "
                    f"{multiplier:.1f}x the 7-day avg ${avg_cost:.2f}"
                ),
            })

    return flags


# ---------------------------------------------------------------------------
# subagent_explosion detector
# ---------------------------------------------------------------------------

def _detect_subagent_explosion(db: AnalyticsDB, session_ids: list[str] | None = None) -> list[dict]:
    """Detect sessions with excessive subagent usage.

    Warning: > 20 subagent messages. Critical: > 50.
    """
    if _is_snoozed(db, "subagent_explosion"):
        return []

    flags = []

    # Get candidate sessions with subagent counts
    where_clause = ""
    params: list = []
    if session_ids:
        placeholders = ",".join("?" for _ in session_ids)
        where_clause = f"WHERE session_id IN ({placeholders})"
        params = list(session_ids)

    with db._reader() as conn:
        sessions = conn.execute(
            f"SELECT session_id, project_hash, "
            f"  SUM(CASE WHEN is_subagent = 1 THEN 1 ELSE 0 END) AS subagent_count "
            f"FROM messages {where_clause} "
            f"GROUP BY session_id",
            params,
        ).fetchall()

    for s in sessions:
        sid = s["session_id"]
        count = s["subagent_count"]

        if count <= SUBAGENT_WARNING_THRESHOLD:
            continue

        if _has_active_flag(db, sid, "subagent_explosion"):
            continue

        severity = "critical" if count > SUBAGENT_CRITICAL_THRESHOLD else "warning"

        flags.append({
            "flag_type": "subagent_explosion",
            "severity": severity,
            "session_id": sid,
            "project_hash": s["project_hash"],
            "message": f"{count} subagent messages in session",
        })

    return flags


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def detect_anomalies(db: AnalyticsDB, session_ids: list[str] | None = None) -> list[dict]:
    """Run all v1 detectors. Returns list of newly inserted flag dicts."""
    detectors = [
        _detect_cache_drop,
        _detect_cost_outlier,
        _detect_subagent_explosion,
    ]

    new_flags = []
    for detector in detectors:
        try:
            candidates = detector(db, session_ids)
        except Exception:
            logger.exception("Anomaly detector %s failed", detector.__name__)
            continue

        for flag in candidates:
            try:
                db.insert_flag(
                    flag_type=flag["flag_type"],
                    severity=flag["severity"],
                    session_id=flag["session_id"],
                    project_hash=flag["project_hash"],
                    message=flag["message"],
                )
                new_flags.append(flag)
            except Exception:
                logger.exception("Failed to insert flag %s", flag)

    return new_flags


# ---------------------------------------------------------------------------
# Auto-resolve
# ---------------------------------------------------------------------------

def auto_resolve_flags(db: AnalyticsDB) -> None:
    """Auto-resolve flags for completed sessions (no new messages in 1 hour)."""
    one_hour_ago = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

    active = db.get_active_flags()
    for flag in active:
        sid = flag.get("session_id")
        if not sid:
            continue

        # Check if the session has any message newer than 1 hour ago
        with db._reader() as conn:
            row = conn.execute(
                "SELECT MAX(timestamp) AS last_ts FROM messages WHERE session_id = ?",
                (sid,),
            ).fetchone()

        if row is None or row["last_ts"] is None:
            # No messages at all -- resolve
            db.resolve_flag(flag["id"])
            continue

        if row["last_ts"] < one_hour_ago:
            db.resolve_flag(flag["id"])
