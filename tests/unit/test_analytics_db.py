"""Unit tests for jacked.web.analytics_db — self-contained analytics SQLite DB.

Covers: schema creation, message insert/query/dedup, daily summary rollup,
purge, scan state CRUD, flags lifecycle, settings get/set, and cost estimation.
"""

import os
from datetime import datetime, timedelta, timezone

import pytest

from jacked.web.analytics_db import AnalyticsDB, estimate_cost


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(tmp_path=None):
    """Create an in-memory AnalyticsDB for testing."""
    return AnalyticsDB(db_path=":memory:")


def _ts(days_ago=0, hours_ago=0):
    """UTC ISO timestamp offset into the past."""
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return dt.isoformat()


def _msg(msg_id="m1", session_id="s1", project_hash="proj1",
         timestamp=None, model="claude-opus-4-6", input_tokens=1000,
         output_tokens=500, cache_read_tokens=0, cache_create_tokens=0,
         estimated_cost_usd=0.0, is_subagent=0):
    return {
        "id": msg_id,
        "session_id": session_id,
        "project_hash": project_hash,
        "timestamp": timestamp or _ts(),
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read_tokens,
        "cache_create_tokens": cache_create_tokens,
        "estimated_cost_usd": estimated_cost_usd,
        "is_subagent": is_subagent,
    }


# ---------------------------------------------------------------------------
# Schema creation
# ---------------------------------------------------------------------------

class TestSchemaCreation:
    def test_all_tables_exist(self):
        db = _make_db()
        expected = {"messages", "daily_summaries", "flags", "scan_state", "analytics_settings"}
        with db._reader() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            tables = {r["name"] for r in rows}
        assert expected.issubset(tables), f"Missing tables: {expected - tables}"
        db.close()

    def test_messages_indexes_exist(self):
        db = _make_db()
        with db._reader() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            indexes = {r["name"] for r in rows}
        for idx in ("idx_messages_ts", "idx_messages_session", "idx_messages_project"):
            assert idx in indexes, f"Missing index: {idx}"
        db.close()

    def test_flags_index_exists(self):
        db = _make_db()
        with db._reader() as conn:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
            indexes = {r["name"] for r in rows}
        assert "idx_flags_active" in indexes
        db.close()


# ---------------------------------------------------------------------------
# Insert and query messages
# ---------------------------------------------------------------------------

class TestMessages:
    def test_insert_and_query(self):
        db = _make_db()
        msgs = [_msg("m1", session_id="s1"), _msg("m2", session_id="s1")]
        db.insert_messages(msgs)
        result = db.get_messages_for_session("s1")
        assert len(result) == 2
        assert result[0]["id"] == "m1"
        assert result[1]["id"] == "m2"
        db.close()

    def test_query_empty_session(self):
        db = _make_db()
        result = db.get_messages_for_session("nonexistent")
        assert result == []
        db.close()

    def test_dedup_by_message_id(self):
        db = _make_db()
        db.insert_messages([_msg("m1", input_tokens=100)])
        db.insert_messages([_msg("m1", input_tokens=999)])  # duplicate
        result = db.get_messages_for_session("s1")
        assert len(result) == 1
        # Original value preserved (INSERT OR IGNORE)
        assert result[0]["input_tokens"] == 100
        db.close()

    def test_insert_empty_list(self):
        db = _make_db()
        db.insert_messages([])  # Should not raise
        db.close()

    def test_messages_ordered_by_timestamp(self):
        db = _make_db()
        db.insert_messages([
            _msg("m2", session_id="s1", timestamp=_ts(hours_ago=1)),
            _msg("m1", session_id="s1", timestamp=_ts(hours_ago=2)),
            _msg("m3", session_id="s1", timestamp=_ts(hours_ago=0)),
        ])
        result = db.get_messages_for_session("s1")
        assert [r["id"] for r in result] == ["m1", "m2", "m3"]
        db.close()


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

class TestOverview:
    def test_overview_empty_db(self):
        db = _make_db()
        ov = db.get_overview(days=1)
        assert ov["total_tokens"] == 0
        assert ov["total_cost_usd"] == 0.0
        assert ov["session_count"] == 0
        db.close()

    def test_overview_aggregation(self):
        db = _make_db()
        db.insert_messages([
            _msg("m1", session_id="s1", project_hash="p1",
                 input_tokens=1000, output_tokens=500,
                 cache_read_tokens=200, cache_create_tokens=100,
                 estimated_cost_usd=0.05),
            _msg("m2", session_id="s2", project_hash="p1",
                 input_tokens=2000, output_tokens=1000,
                 cache_read_tokens=0, cache_create_tokens=0,
                 estimated_cost_usd=0.10),
            _msg("m3", session_id="s2", project_hash="p2",
                 input_tokens=500, output_tokens=250,
                 estimated_cost_usd=0.02),
        ])
        ov = db.get_overview(days=1)
        # total_tokens = input + output + cache_read + cache_create
        assert ov["total_tokens"] == (1000 + 500 + 200 + 100) + (2000 + 1000) + (500 + 250)
        assert ov["total_cost_usd"] == pytest.approx(0.17)
        assert ov["session_count"] == 2
        assert len(ov["project_breakdown"]) == 2
        db.close()


# ---------------------------------------------------------------------------
# Session list
# ---------------------------------------------------------------------------

class TestSessionList:
    def test_session_list_empty(self):
        db = _make_db()
        result = db.get_session_list(days=1)
        assert result == []
        db.close()

    def test_session_list_ranked_by_cost(self):
        db = _make_db()
        db.insert_messages([
            _msg("m1", session_id="cheap", estimated_cost_usd=0.01),
            _msg("m2", session_id="expensive", estimated_cost_usd=1.00),
        ])
        result = db.get_session_list(days=1)
        assert len(result) == 2
        assert result[0]["session_id"] == "expensive"
        db.close()

    def test_session_list_filter_by_project(self):
        db = _make_db()
        db.insert_messages([
            _msg("m1", session_id="s1", project_hash="p1"),
            _msg("m2", session_id="s2", project_hash="p2"),
        ])
        result = db.get_session_list(days=1, project_hash="p1")
        assert len(result) == 1
        assert result[0]["session_id"] == "s1"
        db.close()

    def test_session_list_flagged_only(self):
        db = _make_db()
        db.insert_messages([
            _msg("m1", session_id="s1"),
            _msg("m2", session_id="s2"),
        ])
        db.insert_flag("cost_spike", "warning", "s1", "p1", "High cost")
        result = db.get_session_list(days=1, flagged_only=True)
        assert len(result) == 1
        assert result[0]["session_id"] == "s1"
        db.close()


# ---------------------------------------------------------------------------
# Daily summaries
# ---------------------------------------------------------------------------

class TestDailySummaries:
    def test_rollup_and_query(self):
        db = _make_db()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        ts = _ts()
        db.insert_messages([
            _msg("m1", model="claude-opus-4-6", project_hash="p1",
                 timestamp=ts, input_tokens=1000, output_tokens=500,
                 cache_read_tokens=100, cache_create_tokens=50,
                 estimated_cost_usd=0.05, session_id="s1"),
            _msg("m2", model="claude-opus-4-6", project_hash="p1",
                 timestamp=ts, input_tokens=2000, output_tokens=1000,
                 cache_read_tokens=200, cache_create_tokens=0,
                 estimated_cost_usd=0.10, session_id="s2"),
        ])
        db.rollup_daily_summaries(today)
        summaries = db.get_daily_summaries(days=1)
        assert len(summaries) == 1
        s = summaries[0]
        assert s["date"] == today
        assert s["input_tokens"] == 3000
        assert s["output_tokens"] == 1500
        assert s["total_messages"] == 2
        assert s["total_sessions"] == 2
        db.close()

    def test_rollup_replaces_existing(self):
        db = _make_db()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        db.insert_messages([_msg("m1", timestamp=_ts(), estimated_cost_usd=0.05)])
        db.rollup_daily_summaries(today)

        # Add another message and re-rollup
        db.insert_messages([_msg("m2", timestamp=_ts(), estimated_cost_usd=0.10)])
        db.rollup_daily_summaries(today)

        summaries = db.get_daily_summaries(days=1)
        assert len(summaries) == 1
        assert summaries[0]["total_messages"] == 2
        db.close()

    def test_get_daily_summaries_empty(self):
        db = _make_db()
        result = db.get_daily_summaries(days=7)
        assert result == []
        db.close()


# ---------------------------------------------------------------------------
# Purge old messages
# ---------------------------------------------------------------------------

class TestPurge:
    def test_purge_old_messages(self):
        db = _make_db()
        db.insert_messages([
            _msg("old", timestamp=_ts(days_ago=10)),
            _msg("new", timestamp=_ts(days_ago=1)),
        ])
        db.purge_messages_older_than(days=5)
        result = db.get_messages_for_session("s1")
        assert len(result) == 1
        assert result[0]["id"] == "new"
        db.close()

    def test_purge_keeps_all_when_recent(self):
        db = _make_db()
        db.insert_messages([
            _msg("m1", timestamp=_ts(days_ago=1)),
            _msg("m2", timestamp=_ts(days_ago=2)),
        ])
        db.purge_messages_older_than(days=30)
        result = db.get_messages_for_session("s1")
        assert len(result) == 2
        db.close()


# ---------------------------------------------------------------------------
# Scan state
# ---------------------------------------------------------------------------

class TestScanState:
    def test_update_and_get(self):
        db = _make_db()
        db.update_scan_state("/path/to/file.jsonl", 1024, 1700000000.0, 42)
        state = db.get_scan_state("/path/to/file.jsonl")
        assert state is not None
        assert state["last_byte_offset"] == 1024
        assert state["last_mtime"] == 1700000000.0
        assert state["messages_count"] == 42
        db.close()

    def test_get_nonexistent(self):
        db = _make_db()
        assert db.get_scan_state("/no/such/file") is None
        db.close()

    def test_update_overwrites(self):
        db = _make_db()
        db.update_scan_state("/f", 100, 1.0, 5)
        db.update_scan_state("/f", 200, 2.0, 10)
        state = db.get_scan_state("/f")
        assert state["last_byte_offset"] == 200
        assert state["messages_count"] == 10
        db.close()

    def test_prune_stale(self):
        db = _make_db()
        db.update_scan_state("/keep", 0, 0.0, 0)
        db.update_scan_state("/remove1", 0, 0.0, 0)
        db.update_scan_state("/remove2", 0, 0.0, 0)
        db.prune_stale_scan_state({"/keep"})
        assert db.get_scan_state("/keep") is not None
        assert db.get_scan_state("/remove1") is None
        assert db.get_scan_state("/remove2") is None
        db.close()

    def test_prune_empty_valid_paths(self):
        db = _make_db()
        db.update_scan_state("/file", 0, 0.0, 0)
        db.prune_stale_scan_state(set())
        assert db.get_scan_state("/file") is None
        db.close()


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

class TestFlags:
    def test_insert_and_query_active(self):
        db = _make_db()
        fid = db.insert_flag("cost_spike", "warning", "s1", "p1", "Cost exceeded threshold")
        assert isinstance(fid, int)
        assert fid > 0
        flags = db.get_active_flags()
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "cost_spike"
        assert flags[0]["severity"] == "warning"
        assert flags[0]["message"] == "Cost exceeded threshold"
        assert flags[0]["resolved_at"] is None
        db.close()

    def test_insert_with_detail(self):
        db = _make_db()
        db.insert_flag("anomaly", "critical", "s1", "p1", "Unusual pattern",
                       detail="Details here")
        flags = db.get_active_flags()
        assert flags[0]["detail"] == "Details here"
        db.close()

    def test_resolve_flag(self):
        db = _make_db()
        fid = db.insert_flag("test", "info", "s1", "p1", "msg")
        db.resolve_flag(fid)
        flags = db.get_active_flags()
        assert len(flags) == 0
        db.close()

    def test_resolve_flags_for_session(self):
        db = _make_db()
        db.insert_flag("a", "info", "s1", "p1", "msg1")
        db.insert_flag("b", "warning", "s1", "p1", "msg2")
        db.insert_flag("c", "info", "s2", "p1", "msg3")
        db.resolve_flags_for_session("s1")
        flags = db.get_active_flags()
        assert len(flags) == 1
        assert flags[0]["session_id"] == "s2"
        db.close()

    def test_resolve_nonexistent_flag(self):
        db = _make_db()
        db.resolve_flag(999)  # Should not raise
        db.close()


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class TestSettings:
    def test_get_default(self):
        db = _make_db()
        assert db.get_setting("missing") is None
        assert db.get_setting("missing", "fallback") == "fallback"
        db.close()

    def test_set_and_get(self):
        db = _make_db()
        db.set_setting("theme", "dark")
        assert db.get_setting("theme") == "dark"
        db.close()

    def test_set_overwrites(self):
        db = _make_db()
        db.set_setting("key", "v1")
        db.set_setting("key", "v2")
        assert db.get_setting("key") == "v2"
        db.close()


# ---------------------------------------------------------------------------
# Cost estimation
# ---------------------------------------------------------------------------

class TestCostEstimation:
    def test_known_opus_model(self):
        cost = estimate_cost("claude-opus-4-6", 1_000_000, 1_000_000, 0, 0)
        # Opus: $5/M in + $25/M out = $30
        assert cost == pytest.approx(30.0)

    def test_known_sonnet_model(self):
        cost = estimate_cost("claude-sonnet-4-5-20250929", 1_000_000, 0, 0, 0)
        # Sonnet: $3/M in
        assert cost == pytest.approx(3.0)

    def test_known_haiku_model(self):
        cost = estimate_cost("claude-haiku-4-5-20251001", 0, 1_000_000, 0, 0)
        # Haiku: $5/M out
        assert cost == pytest.approx(5.0)

    def test_cache_tokens(self):
        cost = estimate_cost("claude-opus-4-6", 0, 0, 1_000_000, 1_000_000)
        # Opus: $0.50/M cache_read + $6.25/M cache_write = $6.75
        assert cost == pytest.approx(6.75)

    def test_versioned_model_fallback(self):
        """A model with a date suffix should match the base model."""
        cost = estimate_cost("claude-opus-4-6-20260401", 1_000_000, 0, 0, 0)
        # Should match claude-opus-4-6 pricing: $5/M in
        assert cost == pytest.approx(5.0)

    def test_unknown_model_falls_back_to_opus(self):
        cost = estimate_cost("totally-unknown-model", 1_000_000, 0, 0, 0)
        # Falls back to Opus: $5/M in
        assert cost == pytest.approx(5.0)

    def test_zero_tokens(self):
        cost = estimate_cost("claude-opus-4-6", 0, 0, 0, 0)
        assert cost == 0.0

    # --- Sonnet 5 introductory pricing ($2/$10 through 2026-08-31) ---

    def test_sonnet5_intro_pricing_inside_window(self):
        cost = estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000, 0, 0,
                             at="2026-07-04T12:00:00Z")
        # Intro: $2/M in + $10/M out = $12
        assert cost == pytest.approx(12.0)

    def test_sonnet5_standard_pricing_after_window(self):
        cost = estimate_cost("claude-sonnet-5", 1_000_000, 1_000_000, 0, 0,
                             at="2026-09-01T00:00:00Z")
        # Standard: $3/M in + $15/M out = $18
        assert cost == pytest.approx(18.0)

    def test_sonnet5_intro_last_day(self):
        cost = estimate_cost("claude-sonnet-5", 1_000_000, 0, 0, 0,
                             at="2026-08-31T23:59:59Z")
        assert cost == pytest.approx(2.0)

    def test_sonnet5_intro_cache_rates(self):
        cost = estimate_cost("claude-sonnet-5", 0, 0, 1_000_000, 1_000_000,
                             at="2026-07-04T12:00:00Z")
        # Intro cache: $0.20/M read + $2.50/M write = $2.70
        assert cost == pytest.approx(2.70)

    def test_sonnet5_dated_variant_gets_intro_pricing(self):
        cost = estimate_cost("claude-sonnet-5-20260630", 1_000_000, 0, 0, 0,
                             at="2026-07-04T12:00:00Z")
        assert cost == pytest.approx(2.0)

    def test_sonnet_alias_stays_standard(self):
        """The bare 'sonnet' alias can't be dated to a generation - it keeps
        standard pricing even inside the intro window."""
        cost = estimate_cost("sonnet", 1_000_000, 0, 0, 0,
                             at="2026-07-04T12:00:00Z")
        assert cost == pytest.approx(3.0)

    def test_sonnet_45_unaffected_by_intro_window(self):
        cost = estimate_cost("claude-sonnet-4-5-20250929", 1_000_000, 0, 0, 0,
                             at="2026-07-04T12:00:00Z")
        assert cost == pytest.approx(3.0)

    # --- Tier inference for future model IDs (version-proofing) ---

    def test_future_fable_infers_fable_tier(self):
        """A fable model the map hasn't caught up with must NOT fall through
        to Opus pricing (the 0.71.0 bug class: 2x undercount)."""
        cost = estimate_cost("claude-fable-6", 1_000_000, 0, 0, 0)
        assert cost == pytest.approx(10.0)

    def test_mythos_preview_infers_fable_tier(self):
        cost = estimate_cost("claude-mythos-preview", 1_000_000, 0, 0, 0)
        assert cost == pytest.approx(10.0)

    def test_future_opus_infers_opus_tier(self):
        cost = estimate_cost("claude-opus-4-9", 1_000_000, 0, 0, 0)
        assert cost == pytest.approx(5.0)

    def test_future_haiku_infers_haiku_tier(self):
        cost = estimate_cost("claude-haiku-5", 1_000_000, 0, 0, 0)
        assert cost == pytest.approx(1.0)

    def test_truly_unknown_model_still_falls_back_to_opus(self):
        cost = estimate_cost("some-other-vendor-model", 1_000_000, 0, 0, 0)
        assert cost == pytest.approx(5.0)


# ---------------------------------------------------------------------------
# File-based DB (not :memory:)
# ---------------------------------------------------------------------------

class TestFileDB:
    def test_creates_db_file(self, tmp_path):
        db_path = str(tmp_path / "subdir" / "test-analytics.db")
        db = AnalyticsDB(db_path=db_path)
        assert os.path.exists(db_path)
        db.close()
