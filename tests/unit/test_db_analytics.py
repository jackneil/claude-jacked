"""Unit tests for jacked.web.db_analytics query functions.

Covers: empty DB, single entry, mixed decisions, date range filtering,
granularity selection, heatmap, session risk, suggested rules, hot rules,
and method breakdown.
"""

from datetime import datetime, timedelta, timezone

from jacked.web.database import Database
from jacked.web.db_analytics import (
    _cutoff_iso,
    _granularity,
    _bucket_format,
    get_kpi_totals,
    get_time_series,
    get_method_breakdown,
    get_heatmap_raw,
    get_session_risk,
    get_suggested_rules,
    get_hot_rules,
)


def _make_db():
    return Database(":memory:")


def _ts_ago(days=0, hours=0):
    """UTC ISO timestamp N days/hours in the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    return dt.isoformat()


def _insert(db, decision="ALLOW", method="LOCAL", command="ls",
            session_id="s1", repo_path="/repo", timestamp=None,
            reason="ok", elapsed_ms=1.0):
    return db.record_gatekeeper_decision(
        decision,
        timestamp=timestamp or _ts_ago(0),
        command=command,
        method=method,
        reason=reason,
        elapsed_ms=elapsed_ms,
        session_id=session_id,
        repo_path=repo_path,
    )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------

class TestHelpers:
    def test_cutoff_iso_returns_string(self):
        ts = _cutoff_iso(7)
        assert isinstance(ts, str)
        assert "T" in ts

    def test_granularity_hourly(self):
        assert _granularity(1) == "hourly"

    def test_granularity_daily(self):
        assert _granularity(7) == "daily"
        assert _granularity(30) == "daily"

    def test_granularity_weekly(self):
        assert _granularity(31) == "weekly"
        assert _granularity(90) == "weekly"

    def test_bucket_format_hourly(self):
        assert _bucket_format("hourly") == "%Y-%m-%d %H:00"

    def test_bucket_format_daily(self):
        assert _bucket_format("daily") == "%Y-%m-%d"

    def test_bucket_format_weekly(self):
        assert _bucket_format("weekly") == "%Y-W%W"


# ------------------------------------------------------------------
# KPI totals
# ------------------------------------------------------------------

class TestGetKpiTotals:
    def test_empty_db(self):
        db = _make_db()
        kpi = get_kpi_totals(db, days=7)
        assert kpi["total_decisions"] == 0
        assert kpi["approval_rate"] == 0.0
        assert kpi["denials"] == 0
        assert kpi["api_evaluations"] == 0
        assert kpi["rule_coverage"] == 0.0

    def test_single_allow(self):
        db = _make_db()
        _insert(db, decision="ALLOW", method="LOCAL")
        kpi = get_kpi_totals(db, days=1)
        assert kpi["total_decisions"] == 1
        assert kpi["approval_rate"] == 100.0
        assert kpi["denials"] == 0
        assert kpi["rule_coverage"] == 100.0

    def test_mixed_decisions(self):
        db = _make_db()
        _insert(db, decision="ALLOW", method="LOCAL")
        _insert(db, decision="ALLOW", method="PERMS")
        _insert(db, decision="ASK_USER", method="API")
        _insert(db, decision="DENY", method="DENY_PATTERN")
        kpi = get_kpi_totals(db, days=1)
        assert kpi["total_decisions"] == 4
        assert kpi["approval_rate"] == 50.0
        assert kpi["denials"] == 1
        assert kpi["api_evaluations"] == 1  # only ASK_USER via API
        assert kpi["rule_coverage"] == 75.0  # 3 out of 4 from local methods

    def test_date_range_filtering(self):
        db = _make_db()
        # Recent entry
        _insert(db, decision="ALLOW", method="LOCAL", timestamp=_ts_ago(0))
        # Old entry (10 days ago)
        _insert(db, decision="DENY", method="API", timestamp=_ts_ago(10))
        kpi = get_kpi_totals(db, days=7)
        assert kpi["total_decisions"] == 1
        assert kpi["denials"] == 0

    def test_deny_pattern_counted_as_denial(self):
        db = _make_db()
        _insert(db, decision="DENY_PATTERN", method="DENY_PATTERN")
        kpi = get_kpi_totals(db, days=1)
        assert kpi["denials"] == 1


# ------------------------------------------------------------------
# Time-series
# ------------------------------------------------------------------

class TestGetTimeSeries:
    def test_empty_db(self):
        db = _make_db()
        ts = get_time_series(db, days=7)
        assert ts["granularity"] == "daily"
        assert ts["buckets"] == []

    def test_single_entry_daily(self):
        db = _make_db()
        _insert(db, decision="ALLOW")
        ts = get_time_series(db, days=7)
        assert ts["granularity"] == "daily"
        assert len(ts["buckets"]) == 1
        b = ts["buckets"][0]
        assert b["allow"] == 1
        assert b["deny"] == 0
        assert b["total"] == 1

    def test_hourly_granularity(self):
        db = _make_db()
        _insert(db, decision="ALLOW")
        ts = get_time_series(db, days=1)
        assert ts["granularity"] == "hourly"

    def test_weekly_granularity(self):
        db = _make_db()
        _insert(db, decision="ALLOW")
        ts = get_time_series(db, days=60)
        assert ts["granularity"] == "weekly"

    def test_mixed_decisions_bucketed(self):
        db = _make_db()
        _insert(db, decision="ALLOW")
        _insert(db, decision="DENY", method="API")
        _insert(db, decision="ASK_USER", method="API")
        ts = get_time_series(db, days=1)
        assert len(ts["buckets"]) >= 1
        totals = sum(b["total"] for b in ts["buckets"])
        assert totals == 3

    def test_date_range_filtering(self):
        db = _make_db()
        _insert(db, decision="ALLOW", timestamp=_ts_ago(0))
        _insert(db, decision="DENY", method="API", timestamp=_ts_ago(10))
        ts = get_time_series(db, days=7)
        totals = sum(b["total"] for b in ts["buckets"])
        assert totals == 1


# ------------------------------------------------------------------
# Method breakdown
# ------------------------------------------------------------------

class TestGetMethodBreakdown:
    def test_empty_db(self):
        db = _make_db()
        assert get_method_breakdown(db) == {}

    def test_single_method(self):
        db = _make_db()
        _insert(db, method="LOCAL")
        _insert(db, method="LOCAL")
        bd = get_method_breakdown(db, days=1)
        assert bd == {"LOCAL": 2}

    def test_multiple_methods(self):
        db = _make_db()
        _insert(db, method="LOCAL")
        _insert(db, method="API")
        _insert(db, method="PERMS")
        bd = get_method_breakdown(db, days=1)
        assert set(bd.keys()) == {"LOCAL", "API", "PERMS"}


# ------------------------------------------------------------------
# Heatmap raw
# ------------------------------------------------------------------

class TestGetHeatmapRaw:
    def test_empty_db(self):
        db = _make_db()
        assert get_heatmap_raw(db) == []

    def test_returns_timestamps(self):
        db = _make_db()
        _insert(db)
        result = get_heatmap_raw(db, days=1)
        assert len(result) == 1
        assert "timestamp" in result[0]

    def test_date_range_filtering(self):
        db = _make_db()
        _insert(db, timestamp=_ts_ago(0))
        _insert(db, timestamp=_ts_ago(10))
        result = get_heatmap_raw(db, days=7)
        assert len(result) == 1


# ------------------------------------------------------------------
# Session risk
# ------------------------------------------------------------------

class TestGetSessionRisk:
    def test_empty_db(self):
        db = _make_db()
        assert get_session_risk(db) == []

    def test_single_session_ask_user(self):
        db = _make_db()
        _insert(db, decision="ASK_USER", method="API", session_id="risky")
        result = get_session_risk(db, days=1)
        assert len(result) == 1
        assert result[0]["session_id"] == "risky"
        assert result[0]["risk_score"] == 1

    def test_deny_pattern_weighted_higher(self):
        db = _make_db()
        _insert(db, decision="DENY_PATTERN", method="DENY_PATTERN", session_id="a")
        _insert(db, decision="ASK_USER", method="API", session_id="b")
        result = get_session_risk(db, days=1)
        # Session a should have higher risk
        assert result[0]["session_id"] == "a"
        assert result[0]["risk_score"] == 3

    def test_limit_respected(self):
        db = _make_db()
        for i in range(5):
            _insert(db, decision="ASK_USER", method="API", session_id=f"s{i}")
        result = get_session_risk(db, days=1, limit=3)
        assert len(result) == 3

    def test_sessions_ordered_by_risk(self):
        db = _make_db()
        # Low risk session
        _insert(db, decision="ALLOW", method="LOCAL", session_id="safe")
        # Medium risk
        _insert(db, decision="ASK_USER", method="API", session_id="medium")
        _insert(db, decision="ASK_USER", method="API", session_id="medium")
        # High risk
        _insert(db, decision="DENY_PATTERN", method="DENY_PATTERN", session_id="danger")
        result = get_session_risk(db, days=1)
        assert result[0]["session_id"] == "danger"
        assert result[1]["session_id"] == "medium"


# ------------------------------------------------------------------
# Suggested rules
# ------------------------------------------------------------------

class TestGetSuggestedRules:
    def test_empty_db(self):
        db = _make_db()
        assert get_suggested_rules(db) == []

    def test_below_threshold(self):
        db = _make_db()
        _insert(db, decision="ASK_USER", method="API", command="npm test")
        _insert(db, decision="ASK_USER", method="API", command="npm test")
        # Only 2 hits, threshold is 3
        assert get_suggested_rules(db, days=1, min_hits=3) == []

    def test_meets_threshold(self):
        db = _make_db()
        for _ in range(4):
            _insert(db, decision="ASK_USER", method="API", command="npm test")
        rules = get_suggested_rules(db, days=1, min_hits=3)
        assert len(rules) == 1
        assert rules[0]["command"] == "npm test"
        assert rules[0]["count"] == 4

    def test_excludes_local_methods(self):
        """Commands resolved by local rules should not be suggested."""
        db = _make_db()
        for _ in range(5):
            _insert(db, decision="ALLOW", method="LOCAL", command="ls")
        assert get_suggested_rules(db, days=1, min_hits=3) == []

    def test_groups_by_command_and_decision(self):
        db = _make_db()
        for _ in range(3):
            _insert(db, decision="ASK_USER", method="API", command="npm test")
        for _ in range(3):
            _insert(db, decision="ALLOW", method="API", command="npm test")
        rules = get_suggested_rules(db, days=1, min_hits=3)
        # Two groups: ASK_USER + ALLOW for same command
        assert len(rules) == 2


# ------------------------------------------------------------------
# Hot rules
# ------------------------------------------------------------------

class TestGetHotRules:
    def test_empty_db(self):
        db = _make_db()
        assert get_hot_rules(db) == []

    def test_single_method(self):
        db = _make_db()
        _insert(db, method="LOCAL")
        _insert(db, method="LOCAL")
        hot = get_hot_rules(db, days=1)
        assert hot[0]["method"] == "LOCAL"
        assert hot[0]["count"] == 2

    def test_ordered_by_count(self):
        db = _make_db()
        _insert(db, method="LOCAL")
        _insert(db, method="LOCAL")
        _insert(db, method="LOCAL")
        _insert(db, method="API")
        hot = get_hot_rules(db, days=1)
        assert hot[0]["method"] == "LOCAL"
        assert hot[0]["count"] == 3

    def test_limit_respected(self):
        db = _make_db()
        for m in ["A", "B", "C", "D", "E"]:
            _insert(db, method=m)
        hot = get_hot_rules(db, days=1, limit=3)
        assert len(hot) == 3
