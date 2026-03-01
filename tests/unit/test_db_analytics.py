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
    _resolve_model,
    _row_cost,
    get_kpi_totals,
    get_time_series,
    get_method_breakdown,
    get_heatmap_raw,
    get_session_risk,
    get_suggested_rules,
    get_hot_rules,
    get_token_cost_summary,
    MODEL_PRICING,
)


def _make_db():
    return Database(":memory:")


def _ts_ago(days=0, hours=0):
    """UTC ISO timestamp N days/hours in the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    return dt.isoformat()


def _insert(db, decision="ALLOW", method="LOCAL", command="ls",
            session_id="s1", repo_path="/repo", timestamp=None,
            reason="ok", elapsed_ms=1.0, input_tokens=0, output_tokens=0,
            cache_read_tokens=0, cache_write_tokens=0, model=None):
    return db.record_gatekeeper_decision(
        decision,
        timestamp=timestamp or _ts_ago(0),
        command=command,
        method=method,
        reason=reason,
        elapsed_ms=elapsed_ms,
        session_id=session_id,
        repo_path=repo_path,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        model=model,
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


# ------------------------------------------------------------------
# Token cost helpers
# ------------------------------------------------------------------

class TestResolveModel:
    def test_prefers_model_column(self):
        assert _resolve_model({"model": "claude-opus-4-6", "method": "API:opus"}) == "claude-opus-4-6"

    def test_falls_back_to_method(self):
        assert _resolve_model({"model": None, "method": "API:haiku"}) == "haiku"

    def test_unknown_when_no_info(self):
        assert _resolve_model({"model": None, "method": None}) == "unknown"

    def test_method_without_colon(self):
        assert _resolve_model({"model": None, "method": "LOCAL"}) == "unknown"


class TestRowCost:
    def test_haiku_cost(self):
        row = {
            "model": "claude-haiku-4-5-20251001", "method": "API:haiku",
            "input_tokens": 1_000_000, "output_tokens": 1_000_000,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        # 1M input * $1/MTok + 1M output * $5/MTok = $6.00
        assert _row_cost(row) == 6.00

    def test_opus_cost(self):
        row = {
            "model": "claude-opus-4-6", "method": "API:opus",
            "input_tokens": 1000, "output_tokens": 200,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        # 1000/1M * $5 + 200/1M * $25 = 0.005 + 0.005 = 0.01
        assert abs(_row_cost(row) - 0.01) < 1e-9

    def test_unknown_model_zero_cost(self):
        row = {
            "model": "unknown-model-xyz", "method": "API:unknown",
            "input_tokens": 999999, "output_tokens": 999999,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        assert _row_cost(row) == 0.0

    def test_cache_tokens_priced(self):
        row = {
            "model": "claude-haiku-4-5-20251001", "method": "API:haiku",
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 1_000_000, "cache_write_tokens": 1_000_000,
        }
        # 1M cache_read * $0.10/MTok + 1M cache_write * $1.25/MTok = $1.35
        assert abs(_row_cost(row) - 1.35) < 1e-9

    def test_zero_tokens_zero_cost(self):
        row = {
            "model": "claude-opus-4-6", "method": "API:opus",
            "input_tokens": 0, "output_tokens": 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        assert _row_cost(row) == 0.0

    def test_none_tokens_treated_as_zero(self):
        row = {
            "model": "claude-haiku-4-5-20251001", "method": "API:haiku",
            "input_tokens": None, "output_tokens": None,
            "cache_read_tokens": None, "cache_write_tokens": None,
        }
        assert _row_cost(row) == 0.0

    def test_empty_string_model(self):
        row = {
            "model": "", "method": "API:haiku",
            "input_tokens": 1000, "output_tokens": 100,
            "cache_read_tokens": 0, "cache_write_tokens": 0,
        }
        # Empty string model -> falls back to method -> haiku
        cost = _row_cost(row)
        assert cost > 0


class TestResolveModelEdgeCases:
    def test_empty_string_model_falls_back(self):
        assert _resolve_model({"model": "", "method": "API:haiku"}) == "haiku"

    def test_missing_keys(self):
        assert _resolve_model({}) == "unknown"


# ------------------------------------------------------------------
# Token cost summary
# ------------------------------------------------------------------

class TestGetTokenCostSummary:
    def test_empty_db(self):
        db = _make_db()
        summary = get_token_cost_summary(db, days=7)
        assert summary["total_cost_usd"] == 0.0
        assert summary["total_input_tokens"] == 0
        assert summary["total_output_tokens"] == 0
        assert summary["by_model"] == {}
        assert summary["daily_costs"] == []

    def test_single_api_call(self):
        db = _make_db()
        _insert(db, method="API:haiku", model="claude-haiku-4-5-20251001",
                input_tokens=500, output_tokens=50)
        summary = get_token_cost_summary(db, days=1)
        assert summary["total_input_tokens"] == 500
        assert summary["total_output_tokens"] == 50
        assert summary["total_cost_usd"] > 0
        assert "claude-haiku-4-5-20251001" in summary["by_model"]
        assert summary["by_model"]["claude-haiku-4-5-20251001"]["calls"] == 1

    def test_ignores_cli_calls(self):
        """CLI calls have 0 input_tokens — should not appear."""
        db = _make_db()
        _insert(db, method="CLI:haiku", input_tokens=0, output_tokens=0)
        summary = get_token_cost_summary(db, days=1)
        assert summary["total_cost_usd"] == 0.0
        assert summary["by_model"] == {}

    def test_ignores_local_rules(self):
        """LOCAL decisions have 0 tokens — should not appear."""
        db = _make_db()
        _insert(db, method="LOCAL", input_tokens=0, output_tokens=0)
        summary = get_token_cost_summary(db, days=1)
        assert summary["total_cost_usd"] == 0.0

    def test_multiple_models(self):
        db = _make_db()
        _insert(db, method="API:haiku", model="claude-haiku-4-5-20251001",
                input_tokens=1000, output_tokens=100)
        _insert(db, method="API:opus", model="claude-opus-4-6",
                input_tokens=1000, output_tokens=100)
        summary = get_token_cost_summary(db, days=1)
        assert len(summary["by_model"]) == 2
        # Opus costs more than haiku for same token counts
        opus_cost = summary["by_model"]["claude-opus-4-6"]["cost_usd"]
        haiku_cost = summary["by_model"]["claude-haiku-4-5-20251001"]["cost_usd"]
        assert opus_cost > haiku_cost

    def test_daily_costs_sorted(self):
        db = _make_db()
        _insert(db, method="API:haiku", model="claude-haiku-4-5-20251001",
                input_tokens=500, output_tokens=50, timestamp=_ts_ago(1))
        _insert(db, method="API:haiku", model="claude-haiku-4-5-20251001",
                input_tokens=500, output_tokens=50, timestamp=_ts_ago(0))
        summary = get_token_cost_summary(db, days=7)
        dates = [d["date"] for d in summary["daily_costs"]]
        assert dates == sorted(dates)

    def test_date_range_filtering(self):
        db = _make_db()
        _insert(db, method="API:haiku", model="claude-haiku-4-5-20251001",
                input_tokens=500, output_tokens=50, timestamp=_ts_ago(0))
        _insert(db, method="API:haiku", model="claude-haiku-4-5-20251001",
                input_tokens=500, output_tokens=50, timestamp=_ts_ago(10))
        summary = get_token_cost_summary(db, days=7)
        assert summary["by_model"]["claude-haiku-4-5-20251001"]["calls"] == 1

    def test_legacy_rows_without_model(self):
        """Old rows have model=None, should fall back to method parsing."""
        db = _make_db()
        _insert(db, method="API:haiku", model=None,
                input_tokens=1000, output_tokens=100)
        summary = get_token_cost_summary(db, days=1)
        assert "haiku" in summary["by_model"]
        assert summary["total_cost_usd"] > 0
