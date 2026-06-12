"""Unit tests for jacked.web.analytics_anomalies — anomaly detectors.

Covers: cache_drop, cost_outlier, subagent_explosion detectors,
detect_anomalies orchestrator, and auto_resolve_flags.
"""

from datetime import datetime, timedelta, timezone


from jacked.web.analytics_db import AnalyticsDB
from jacked.web.analytics_anomalies import (
    detect_anomalies,
    auto_resolve_flags,
    _detect_cache_drop,
    _detect_cost_outlier,
    _detect_subagent_explosion,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db():
    return AnalyticsDB(db_path=":memory:")


def _ts(hours_ago=0, days_ago=0):
    dt = datetime.now(timezone.utc) - timedelta(days=days_ago, hours=hours_ago)
    return dt.isoformat()


def _insert_session(db, session_id, project_hash, messages):
    """Helper: insert a batch of messages for a session.

    Timestamps are relative to now (1 minute apart, starting 2 hours ago)
    so they stay inside the detectors' 7-day rolling window and sessions
    with 6+ messages clear the 5-minute duration gate.
    """
    base = datetime.now(timezone.utc) - timedelta(hours=2)
    db.insert_messages([{
        "id": f"{session_id}_{i}",
        "session_id": session_id,
        "project_hash": project_hash,
        "timestamp": (base + timedelta(minutes=i)).isoformat(),
        "model": m.get("model", "claude-opus-4-6"),
        "input_tokens": m.get("input", 100),
        "output_tokens": m.get("output", 500),
        "cache_read_tokens": m.get("cache_read", 50000),
        "cache_create_tokens": m.get("cache_create", 1000),
        "estimated_cost_usd": m.get("cost", 0.05),
        "is_subagent": m.get("is_subagent", False),
    } for i, m in enumerate(messages)])


def _seed_normal_sessions(db, count=5):
    """Insert several 'normal' sessions with high cache rates and moderate cost."""
    for s in range(count):
        msgs = []
        for i in range(15):
            msgs.append({
                "cache_read": 50000,
                "cache_create": 1000,
                "input": 100,
                "cost": 0.05,
            })
        _insert_session(db, f"normal_{s}", "proj1", msgs)


# ---------------------------------------------------------------------------
# cache_drop detector
# ---------------------------------------------------------------------------

class TestCacheDropDetector:
    def test_detects_low_cache_session(self):
        """Session with ~17% cache rate should trigger warning when avg is ~98%."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        # Bad session: low cache hit ratio (~17%)
        bad_msgs = []
        for i in range(12):
            bad_msgs.append({
                "cache_read": 1000,       # very low
                "cache_create": 1000,
                "input": 4000,            # high input relative to cache
                "cost": 0.10,
            })
        _insert_session(db, "bad_cache", "proj1", bad_msgs)

        flags = _detect_cache_drop(db, ["bad_cache"])
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "cache_drop"
        assert flags[0]["session_id"] == "bad_cache"
        assert flags[0]["severity"] in ("warning", "critical")
        db.close()

    def test_critical_severity_for_very_low_cache(self):
        """Session with < 30% cache should be critical."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        bad_msgs = []
        for i in range(12):
            bad_msgs.append({
                "cache_read": 500,
                "cache_create": 1000,
                "input": 8000,
                "cost": 0.10,
            })
        _insert_session(db, "terrible_cache", "proj1", bad_msgs)

        flags = _detect_cache_drop(db, ["terrible_cache"])
        assert len(flags) == 1
        assert flags[0]["severity"] == "critical"
        db.close()

    def test_excludes_first_5_messages(self):
        """First 5 messages (warmup) should be excluded from cache analysis."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        # Session where first 5 msgs have terrible cache, rest are fine
        msgs = []
        for i in range(12):
            if i < 5:
                msgs.append({
                    "cache_read": 0,
                    "cache_create": 5000,
                    "input": 5000,
                    "cost": 0.10,
                })
            else:
                msgs.append({
                    "cache_read": 50000,
                    "cache_create": 1000,
                    "input": 100,
                    "cost": 0.05,
                })
        _insert_session(db, "warmup_session", "proj1", msgs)

        flags = _detect_cache_drop(db, ["warmup_session"])
        # After excluding first 5, cache is great -- no flag
        assert len(flags) == 0
        db.close()

    def test_skips_sessions_under_10_messages(self):
        """Sessions with fewer than 10 messages should not trigger cache_drop."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        # Only 8 messages -- below threshold
        bad_msgs = []
        for i in range(8):
            bad_msgs.append({
                "cache_read": 100,
                "cache_create": 1000,
                "input": 5000,
                "cost": 0.10,
            })
        _insert_session(db, "short_session", "proj1", bad_msgs)

        flags = _detect_cache_drop(db, ["short_session"])
        assert len(flags) == 0
        db.close()

    def test_no_flag_for_good_cache(self):
        """Session with 95%+ cache should not trigger."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        good_msgs = []
        for i in range(12):
            good_msgs.append({
                "cache_read": 50000,
                "cache_create": 500,
                "input": 100,
                "cost": 0.05,
            })
        _insert_session(db, "good_cache", "proj1", good_msgs)

        flags = _detect_cache_drop(db, ["good_cache"])
        assert len(flags) == 0
        db.close()


# ---------------------------------------------------------------------------
# cost_outlier detector
# ---------------------------------------------------------------------------

class TestCostOutlierDetector:
    def test_detects_expensive_session(self):
        """Session ~4x the average cost should trigger warning."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        # Normal sessions = 15 msgs x $0.05 = $0.75 avg
        # This session = 10 x $0.15 = $1.50 -> but need 5+ min duration
        # Actually need > 3x: 10 msgs x $0.30 = $3.00 -> $3.00/$0.75 = 4x -> warning
        expensive_msgs = []
        for i in range(10):
            expensive_msgs.append({"cost": 0.30})
        _insert_session(db, "expensive", "proj1", expensive_msgs)

        flags = _detect_cost_outlier(db, ["expensive"])
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "cost_outlier"
        assert flags[0]["session_id"] == "expensive"
        assert flags[0]["severity"] == "warning"
        db.close()

    def test_critical_severity_for_very_expensive(self):
        """Session 8x+ average should be critical."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        expensive_msgs = []
        for i in range(10):
            expensive_msgs.append({"cost": 5.00})  # 100x per-message cost
        _insert_session(db, "very_expensive", "proj1", expensive_msgs)

        flags = _detect_cost_outlier(db, ["very_expensive"])
        assert len(flags) == 1
        assert flags[0]["severity"] == "critical"
        db.close()

    def test_skips_sessions_under_5_messages(self):
        """Sessions with fewer than 5 messages should not trigger."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        short_msgs = []
        for i in range(4):
            short_msgs.append({"cost": 5.00})
        _insert_session(db, "short_expensive", "proj1", short_msgs)

        flags = _detect_cost_outlier(db, ["short_expensive"])
        assert len(flags) == 0
        db.close()

    def test_skips_sessions_under_5_minutes(self):
        """Sessions shorter than 5 minutes should not trigger."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        # 6 messages but all within 2 minutes
        now = datetime.now(timezone.utc)
        db.insert_messages([{
            "id": f"quick_{i}",
            "session_id": "quick_session",
            "project_hash": "proj1",
            "timestamp": (now - timedelta(seconds=30 * i)).isoformat(),
            "model": "claude-opus-4-6",
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_tokens": 50000,
            "cache_create_tokens": 1000,
            "estimated_cost_usd": 5.00,
            "is_subagent": 0,
        } for i in range(6)])

        flags = _detect_cost_outlier(db, ["quick_session"])
        assert len(flags) == 0
        db.close()

    def test_no_flag_for_normal_cost(self):
        """Session at normal cost should not trigger."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        normal_msgs = []
        for i in range(10):
            normal_msgs.append({"cost": 0.05})
        _insert_session(db, "normal_cost", "proj1", normal_msgs)

        flags = _detect_cost_outlier(db, ["normal_cost"])
        assert len(flags) == 0
        db.close()


# ---------------------------------------------------------------------------
# subagent_explosion detector
# ---------------------------------------------------------------------------

class TestSubagentExplosionDetector:
    def test_detects_many_subagent_messages(self):
        """25 subagent messages should trigger warning."""
        db = _make_db()
        msgs = []
        for i in range(30):
            msgs.append({
                "is_subagent": True if i < 25 else False,
                "cost": 0.05,
            })
        _insert_session(db, "subagent_heavy", "proj1", msgs)

        flags = _detect_subagent_explosion(db, ["subagent_heavy"])
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "subagent_explosion"
        assert flags[0]["severity"] == "warning"
        db.close()

    def test_critical_severity_for_extreme_subagent(self):
        """55 subagent messages should be critical."""
        db = _make_db()
        msgs = []
        for i in range(60):
            msgs.append({
                "is_subagent": True if i < 55 else False,
                "cost": 0.05,
            })
        _insert_session(db, "subagent_extreme", "proj1", msgs)

        flags = _detect_subagent_explosion(db, ["subagent_extreme"])
        assert len(flags) == 1
        assert flags[0]["severity"] == "critical"
        db.close()

    def test_no_flag_for_few_subagents(self):
        """10 subagent messages should not trigger."""
        db = _make_db()
        msgs = []
        for i in range(15):
            msgs.append({
                "is_subagent": True if i < 10 else False,
                "cost": 0.05,
            })
        _insert_session(db, "modest_subagent", "proj1", msgs)

        flags = _detect_subagent_explosion(db, ["modest_subagent"])
        assert len(flags) == 0
        db.close()


# ---------------------------------------------------------------------------
# detect_anomalies orchestrator
# ---------------------------------------------------------------------------

class TestDetectAnomalies:
    def test_orchestrator_inserts_flags(self):
        """detect_anomalies should insert flags into the DB and return them."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        # Add a session that trips subagent_explosion
        msgs = [{"is_subagent": True, "cost": 0.05} for _ in range(25)]
        _insert_session(db, "boom", "proj1", msgs)

        new_flags = detect_anomalies(db, session_ids=["boom"])
        assert len(new_flags) >= 1

        # Verify flags actually in DB
        active = db.get_active_flags()
        assert any(f["session_id"] == "boom" for f in active)
        db.close()

    def test_orchestrator_skips_existing_flags(self):
        """Flags should not be duplicated for the same session+type."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        msgs = [{"is_subagent": True, "cost": 0.05} for _ in range(25)]
        _insert_session(db, "boom", "proj1", msgs)

        # First run
        detect_anomalies(db, session_ids=["boom"])
        # Second run -- should skip existing
        second = detect_anomalies(db, session_ids=["boom"])
        assert len(second) == 0

        # Only one set of flags in DB
        active = db.get_active_flags()
        subagent_flags = [f for f in active if f["session_id"] == "boom"
                          and f["flag_type"] == "subagent_explosion"]
        assert len(subagent_flags) == 1
        db.close()

    def test_orchestrator_respects_snooze(self):
        """Snoozed flag types should not fire."""
        db = _make_db()
        _seed_normal_sessions(db, count=5)

        msgs = [{"is_subagent": True, "cost": 0.05} for _ in range(25)]
        _insert_session(db, "boom", "proj1", msgs)

        # Snooze subagent_explosion until far future
        future = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        db.set_setting("snooze_subagent_explosion_until", future)

        flags = detect_anomalies(db, session_ids=["boom"])
        subagent_flags = [f for f in flags if f["flag_type"] == "subagent_explosion"]
        assert len(subagent_flags) == 0
        db.close()


# ---------------------------------------------------------------------------
# auto_resolve_flags
# ---------------------------------------------------------------------------

class TestAutoResolveFlags:
    def test_resolves_stale_session(self):
        """Flags for sessions with no recent messages should be resolved."""
        db = _make_db()

        # Insert a message from 2 hours ago
        db.insert_messages([{
            "id": "old_msg_1",
            "session_id": "stale_session",
            "project_hash": "proj1",
            "timestamp": _ts(hours_ago=2),
            "model": "claude-opus-4-6",
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_tokens": 50000,
            "cache_create_tokens": 1000,
            "estimated_cost_usd": 0.05,
            "is_subagent": 0,
        }])
        db.insert_flag("subagent_explosion", "warning", "stale_session", "proj1",
                        "Too many subagents")

        auto_resolve_flags(db)

        active = db.get_active_flags()
        assert len(active) == 0
        db.close()

    def test_keeps_active_session_flags(self):
        """Flags for sessions with recent messages should NOT be resolved."""
        db = _make_db()

        # Insert a message from 10 minutes ago
        db.insert_messages([{
            "id": "recent_msg_1",
            "session_id": "active_session",
            "project_hash": "proj1",
            "timestamp": _ts(hours_ago=0),
            "model": "claude-opus-4-6",
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_tokens": 50000,
            "cache_create_tokens": 1000,
            "estimated_cost_usd": 0.05,
            "is_subagent": 0,
        }])
        db.insert_flag("subagent_explosion", "warning", "active_session", "proj1",
                        "Too many subagents")

        auto_resolve_flags(db)

        active = db.get_active_flags()
        assert len(active) == 1
        assert active[0]["session_id"] == "active_session"
        db.close()
