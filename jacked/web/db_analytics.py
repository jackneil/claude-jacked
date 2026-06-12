"""Analytics query functions for the gatekeeper dashboard.

Pure-function layer over a Database instance. All queries are parameterized
(no f-strings) and return plain dicts/lists for JSON serialisation.
"""

from datetime import datetime, timedelta, timezone

# ---------------------------------------------------------------------------
# Anthropic API pricing per million tokens (USD)
# Verify at: https://platform.claude.com/docs/en/about-claude/pricing
# Last verified: 2026-02-28
# ---------------------------------------------------------------------------

# Price tiers — single source of truth, mapped by full ID and short alias below.
_TIER_HAIKU  = {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25}
_TIER_SONNET = {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75}
_TIER_OPUS   = {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_write": 6.25}

# Keep in sync with MODEL_MAP in security_gatekeeper.py when adding models.
MODEL_PRICING: dict[str, dict[str, float]] = {
    # Full model IDs (stored in DB model column)
    "claude-haiku-4-5-20251001": _TIER_HAIKU,
    "claude-sonnet-4-5-20250929": _TIER_SONNET,
    "claude-sonnet-4-6": _TIER_SONNET,
    "claude-opus-4-6": _TIER_OPUS,
    # Short aliases (fallback for legacy rows without model column)
    "haiku": _TIER_HAIKU,
    "sonnet": _TIER_SONNET,
    "opus": _TIER_OPUS,
}

_ZERO_PRICES: dict[str, float] = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cutoff_iso(days: int) -> str:
    """Return UTC ISO timestamp for *days* ago.

    >>> ts = _cutoff_iso(7)
    >>> isinstance(ts, str) and 'T' in ts
    True
    >>> from datetime import datetime, timezone
    >>> dt = datetime.fromisoformat(ts)
    >>> dt.tzinfo is not None or '+' in ts or 'Z' in ts  # timezone-aware
    True
    >>> _cutoff_iso(0)[-6:]  # ends with TZ offset
    '+00:00'
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat()


def _granularity(days: int) -> str:
    """Choose bucket granularity based on date range.

    <2 days  -> hourly
    2-30 days -> daily
    >30 days -> weekly

    >>> _granularity(1)
    'hourly'
    >>> _granularity(2)
    'daily'
    >>> _granularity(30)
    'daily'
    >>> _granularity(31)
    'weekly'
    """
    if days < 2:
        return "hourly"
    if days <= 30:
        return "daily"
    return "weekly"


def _bucket_format(gran: str) -> str:
    """Return SQLite strftime format string for the given granularity.

    >>> _bucket_format('hourly')
    '%Y-%m-%d %H:00'
    >>> _bucket_format('daily')
    '%Y-%m-%d'
    >>> _bucket_format('weekly')
    '%Y-W%W'
    """
    if gran == "hourly":
        return "%Y-%m-%d %H:00"
    if gran == "daily":
        return "%Y-%m-%d"
    return "%Y-W%W"


# ---------------------------------------------------------------------------
# KPI totals
# ---------------------------------------------------------------------------

def get_kpi_totals(db, days: int = 7) -> dict:
    """Aggregate KPI metrics over the given window.

    Returns dict with: total_decisions, approval_rate, denials,
    api_evaluations, rule_coverage.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> kpi = get_kpi_totals(db, days=7)
    >>> kpi['total_decisions']
    0
    >>> kpi['approval_rate']
    0.0
    >>> kpi['denials']
    0
    """
    cutoff = _cutoff_iso(days)
    with db._reader() as conn:
        # Total decisions
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM gatekeeper_decisions WHERE timestamp >= ?",
            (cutoff,),
        ).fetchone()
        total = row["cnt"] if row else 0

        if total == 0:
            return {
                "total_decisions": 0,
                "approval_rate": 0.0,
                "denials": 0,
                "api_evaluations": 0,
                "rule_coverage": 0.0,
            }

        # Decision breakdown
        rows = conn.execute(
            "SELECT decision, COUNT(*) AS cnt FROM gatekeeper_decisions "
            "WHERE timestamp >= ? GROUP BY decision",
            (cutoff,),
        ).fetchall()
        counts = {r["decision"]: r["cnt"] for r in rows}

        allows = counts.get("ALLOW", 0)
        denials = counts.get("DENY", 0) + counts.get("DENY_PATTERN", 0)

        # Method breakdown for rule_coverage and api_evaluations
        method_rows = conn.execute(
            "SELECT method, COUNT(*) AS cnt FROM gatekeeper_decisions "
            "WHERE timestamp >= ? GROUP BY method",
            (cutoff,),
        ).fetchall()
        method_counts = {r["method"]: r["cnt"] for r in method_rows}

        # "API evaluations" = decisions made via the API call path (not local rules)
        # Local rule methods: PERMS, LOCAL, DENY_PATTERN
        local_methods = {"PERMS", "LOCAL", "DENY_PATTERN"}
        api_evals = sum(
            cnt for m, cnt in method_counts.items()
            if m not in local_methods
        )

        # Rule coverage = fraction resolved by local rules (no API call needed)
        local_resolved = sum(
            cnt for m, cnt in method_counts.items()
            if m in local_methods
        )
        rule_coverage = round((local_resolved / total) * 100, 1) if total else 0.0

        approval_rate = round((allows / total) * 100, 1) if total else 0.0

    return {
        "total_decisions": total,
        "approval_rate": approval_rate,
        "denials": denials,
        "api_evaluations": api_evals,
        "rule_coverage": rule_coverage,
    }


# ---------------------------------------------------------------------------
# Time-series
# ---------------------------------------------------------------------------

def get_time_series(db, days: int = 7) -> dict:
    """Bucketed time-series of decisions.

    Returns ``{"granularity": "...", "buckets": [...]}``.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> ts = get_time_series(db, days=7)
    >>> ts['granularity']
    'daily'
    >>> ts['buckets']
    []
    >>> db.record_gatekeeper_decision("ALLOW", command="ls", method="LOCAL",
    ...     reason="ok", elapsed_ms=1.0, session_id="s1", repo_path="/r")
    1
    >>> ts2 = get_time_series(db, days=1)
    >>> ts2['granularity']
    'hourly'
    >>> len(ts2['buckets']) >= 1
    True
    """
    cutoff = _cutoff_iso(days)
    gran = _granularity(days)
    fmt = _bucket_format(gran)

    with db._reader() as conn:
        rows = conn.execute(
            "SELECT strftime(?, timestamp) AS bucket, "
            "  SUM(CASE WHEN decision = 'ALLOW' THEN 1 ELSE 0 END) AS allow_cnt, "
            "  SUM(CASE WHEN decision IN ('DENY', 'DENY_PATTERN') THEN 1 ELSE 0 END) AS deny_cnt, "
            "  SUM(CASE WHEN decision = 'ASK_USER' THEN 1 ELSE 0 END) AS ask_user_cnt, "
            "  COUNT(*) AS total "
            "FROM gatekeeper_decisions "
            "WHERE timestamp >= ? "
            "GROUP BY bucket ORDER BY bucket",
            (fmt, cutoff),
        ).fetchall()

    buckets = [
        {
            "bucket": r["bucket"],
            "allow": r["allow_cnt"],
            "deny": r["deny_cnt"],
            "ask_user": r["ask_user_cnt"],
            "total": r["total"],
        }
        for r in rows
    ]
    return {"granularity": gran, "buckets": buckets}


# ---------------------------------------------------------------------------
# Method breakdown
# ---------------------------------------------------------------------------

def get_method_breakdown(db, days: int = 7) -> dict:
    """Decision count per method.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_method_breakdown(db)
    {}
    >>> db.record_gatekeeper_decision("ALLOW", command="ls", method="LOCAL",
    ...     reason="ok", elapsed_ms=1.0, session_id="s1", repo_path="/r")
    1
    >>> get_method_breakdown(db, days=1)
    {'LOCAL': 1}
    """
    cutoff = _cutoff_iso(days)
    with db._reader() as conn:
        rows = conn.execute(
            "SELECT method, COUNT(*) AS cnt FROM gatekeeper_decisions "
            "WHERE timestamp >= ? GROUP BY method ORDER BY cnt DESC",
            (cutoff,),
        ).fetchall()
    return {r["method"]: r["cnt"] for r in rows}


# ---------------------------------------------------------------------------
# Heatmap raw timestamps
# ---------------------------------------------------------------------------

def get_heatmap_raw(db, days: int = 7) -> list[dict]:
    """Return raw timestamps for client-side heatmap bucketing.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_heatmap_raw(db)
    []
    >>> db.record_gatekeeper_decision("ALLOW", command="ls", method="LOCAL",
    ...     reason="ok", elapsed_ms=1.0, session_id="s1", repo_path="/r")
    1
    >>> len(get_heatmap_raw(db, days=1)) >= 1
    True
    """
    cutoff = _cutoff_iso(days)
    with db._reader() as conn:
        rows = conn.execute(
            "SELECT timestamp FROM gatekeeper_decisions WHERE timestamp >= ? "
            "ORDER BY timestamp LIMIT 50000",
            (cutoff,),
        ).fetchall()
    return [{"timestamp": r["timestamp"]} for r in rows]


# ---------------------------------------------------------------------------
# Session risk scoring
# ---------------------------------------------------------------------------

def get_session_risk(db, days: int = 7, limit: int = 20) -> list[dict]:
    """Sessions ranked by weighted risk score.

    Weights: DENY_PATTERN method = 3, DENY via API = 2, ASK_USER = 1.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_session_risk(db)
    []
    >>> db.record_gatekeeper_decision("ASK_USER", command="rm -rf /",
    ...     method="API", reason="dangerous", elapsed_ms=5.0,
    ...     session_id="risky", repo_path="/evil")
    1
    >>> result = get_session_risk(db, days=1)
    >>> result[0]['session_id']
    'risky'
    >>> result[0]['risk_score']
    1
    """
    cutoff = _cutoff_iso(days)
    with db._reader() as conn:
        rows = conn.execute(
            "SELECT session_id, "
            "  repo_path, "
            "  COUNT(*) AS total_decisions, "
            "  SUM(CASE WHEN decision IN ('DENY', 'DENY_PATTERN') THEN 1 ELSE 0 END) AS denials, "
            "  MIN(timestamp) AS first_seen, "
            "  MAX(timestamp) AS last_seen, "
            "  SUM(CASE WHEN method = 'DENY_PATTERN' THEN 3 "
            "       WHEN decision IN ('DENY') AND method != 'DENY_PATTERN' THEN 2 "
            "       WHEN decision = 'ASK_USER' THEN 1 "
            "       ELSE 0 END) AS risk_score "
            "FROM gatekeeper_decisions "
            "WHERE timestamp >= ? AND session_id IS NOT NULL AND session_id != '' "
            "GROUP BY session_id "
            "ORDER BY risk_score DESC "
            "LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Suggested rules
# ---------------------------------------------------------------------------

def get_suggested_rules(db, days: int = 7, min_hits: int = 3) -> list[dict]:
    """Commands hitting the API 3+ times with the same decision.

    Candidates for new permission rules.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_suggested_rules(db)
    []
    >>> for i in range(4):
    ...     _ = db.record_gatekeeper_decision("ASK_USER", command="npm test",
    ...         method="API", reason="no rule", elapsed_ms=2.0,
    ...         session_id="s1", repo_path="/r")
    >>> rules = get_suggested_rules(db, days=1, min_hits=3)
    >>> len(rules) >= 1
    True
    >>> rules[0]['command']
    'npm test'
    """
    cutoff = _cutoff_iso(days)
    local_methods = ("PERMS", "LOCAL", "DENY_PATTERN")
    with db._reader() as conn:
        rows = conn.execute(
            "SELECT command, decision, COUNT(*) AS cnt "
            "FROM gatekeeper_decisions "
            "WHERE timestamp >= ? "
            "  AND method NOT IN (?, ?, ?) "
            "  AND command IS NOT NULL AND command != '' "
            "GROUP BY command, decision "
            "HAVING cnt >= ? "
            "ORDER BY cnt DESC",
            (cutoff, *local_methods, min_hits),
        ).fetchall()
    return [
        {"command": r["command"], "decision": r["decision"], "count": r["cnt"]}
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Hot rules (most-triggered methods)
# ---------------------------------------------------------------------------

def get_hot_rules(db, days: int = 7, limit: int = 10) -> list[dict]:
    """Most-triggered methods by count.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_hot_rules(db)
    []
    >>> db.record_gatekeeper_decision("ALLOW", command="ls", method="LOCAL",
    ...     reason="ok", elapsed_ms=1.0, session_id="s1", repo_path="/r")
    1
    >>> db.record_gatekeeper_decision("ALLOW", command="pwd", method="LOCAL",
    ...     reason="ok", elapsed_ms=1.0, session_id="s1", repo_path="/r")
    2
    >>> hot = get_hot_rules(db, days=1)
    >>> hot[0]['method']
    'LOCAL'
    >>> hot[0]['count']
    2
    """
    cutoff = _cutoff_iso(days)
    with db._reader() as conn:
        rows = conn.execute(
            "SELECT method, COUNT(*) AS cnt "
            "FROM gatekeeper_decisions "
            "WHERE timestamp >= ? AND method IS NOT NULL "
            "GROUP BY method "
            "ORDER BY cnt DESC "
            "LIMIT ?",
            (cutoff, limit),
        ).fetchall()
    return [{"method": r["method"], "count": r["cnt"]} for r in rows]


# ---------------------------------------------------------------------------
# Token cost analytics
# ---------------------------------------------------------------------------

def _resolve_model(row: dict) -> str:
    """Extract model identifier from a gatekeeper_decisions row.

    Prefers the `model` column (full ID). Falls back to parsing the short
    name from the `method` column (e.g. 'API:haiku' -> 'haiku').

    >>> _resolve_model({"model": "claude-opus-4-6", "method": "API:opus"})
    'claude-opus-4-6'
    >>> _resolve_model({"model": None, "method": "API:haiku"})
    'haiku'
    >>> _resolve_model({"model": None, "method": None})
    'unknown'
    """
    model = row.get("model")
    if model:
        return model
    method = row.get("method") or ""
    if ":" in method:
        return method.split(":", 1)[1]
    return "unknown"


def _row_cost(row: dict) -> float:
    """Calculate USD cost for a single gatekeeper decision row.

    >>> _row_cost({"model": "claude-haiku-4-5-20251001", "method": "API:haiku",
    ...            "input_tokens": 1000, "output_tokens": 100,
    ...            "cache_read_tokens": 0, "cache_write_tokens": 0})
    1.5e-03
    """
    model_key = _resolve_model(row)
    prices = MODEL_PRICING.get(model_key, _ZERO_PRICES)
    return (
        (row.get("input_tokens", 0) or 0) / 1_000_000 * prices["input"]
        + (row.get("output_tokens", 0) or 0) / 1_000_000 * prices["output"]
        + (row.get("cache_read_tokens", 0) or 0) / 1_000_000 * prices["cache_read"]
        + (row.get("cache_write_tokens", 0) or 0) / 1_000_000 * prices["cache_write"]
    )


def get_token_cost_summary(db, days: int = 7) -> dict:
    """Aggregate token usage and estimated cost over the given window.

    Only includes rows where input_tokens > 0 (i.e. API calls, not CLI/local).

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> summary = get_token_cost_summary(db, days=7)
    >>> summary["total_cost_usd"]
    0.0
    >>> summary["total_input_tokens"]
    0
    """
    cutoff = _cutoff_iso(days)
    with db._reader() as conn:
        rows = conn.execute(
            "SELECT timestamp, method, model, input_tokens, output_tokens, "
            "       cache_read_tokens, cache_write_tokens "
            "FROM gatekeeper_decisions "
            "WHERE timestamp >= ? AND input_tokens > 0 "
            "ORDER BY timestamp DESC LIMIT 10000",
            (cutoff,),
        ).fetchall()

    if not rows:
        return {
            "total_input_tokens": 0,
            "total_output_tokens": 0,
            "total_cache_read_tokens": 0,
            "total_cache_write_tokens": 0,
            "total_cost_usd": 0.0,
            "by_model": {},
            "daily_costs": [],
        }

    total_in = total_out = total_cr = total_cw = 0
    total_cost = 0.0
    by_model: dict[str, dict] = {}
    daily: dict[str, dict] = {}

    for row in rows:
        r = dict(row)
        model_key = _resolve_model(r)
        cost = _row_cost(r)
        in_tok = r.get("input_tokens", 0) or 0
        out_tok = r.get("output_tokens", 0) or 0
        cr_tok = r.get("cache_read_tokens", 0) or 0
        cw_tok = r.get("cache_write_tokens", 0) or 0

        total_in += in_tok
        total_out += out_tok
        total_cr += cr_tok
        total_cw += cw_tok
        total_cost += cost

        # By model
        if model_key not in by_model:
            by_model[model_key] = {"input_tokens": 0, "output_tokens": 0, "cost_usd": 0.0, "calls": 0}
        by_model[model_key]["input_tokens"] += in_tok
        by_model[model_key]["output_tokens"] += out_tok
        by_model[model_key]["cost_usd"] += cost
        by_model[model_key]["calls"] += 1

        # Daily
        ts = r.get("timestamp", "")
        date_key = ts[:10] if len(ts) >= 10 else "unknown"
        if date_key not in daily:
            daily[date_key] = {"date": date_key, "cost_usd": 0.0, "calls": 0}
        daily[date_key]["cost_usd"] += cost
        daily[date_key]["calls"] += 1

    # Round costs for display
    for entry in by_model.values():
        entry["cost_usd"] = round(entry["cost_usd"], 6)
    daily_costs = sorted(daily.values(), key=lambda d: d["date"])
    for entry in daily_costs:
        entry["cost_usd"] = round(entry["cost_usd"], 6)

    return {
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cache_read_tokens": total_cr,
        "total_cache_write_tokens": total_cw,
        "total_cost_usd": round(total_cost, 6),
        "by_model": by_model,
        "daily_costs": daily_costs,
    }
