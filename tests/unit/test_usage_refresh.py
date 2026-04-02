"""Tests for usage refresh: coordinator ceiling, 429 handling.

Covers:
- Hard rate-limit ceiling via per-account coordinator state
- 429 response handling with increment_failures=False
- force=True parameter behavior
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from jacked.web.auth import USAGE_CACHE_FRESHNESS_SECONDS, fetch_usage


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_db(account_overrides=None):
    """Return a mock Database with a single active account."""
    base = {
        "id": 1,
        "email": "user@test.com",
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "is_active": True,
        "usage_cached_at": None,
        "cached_usage_5h": None,
        "cached_usage_7d": None,
    }
    if account_overrides:
        base.update(account_overrides)

    db = MagicMock()
    db.get_account.return_value = base
    db.update_account_usage_cache = MagicMock()
    db.record_account_error = MagicMock()
    return db


def _mock_client(status_code, json_data=None, headers=None):
    """Create a mock httpx.AsyncClient context manager."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# Usage ceiling (hard rate limit)
# ---------------------------------------------------------------------------


class TestUsageCeiling:
    """fetch_usage enforces a hard per-account rate-limit ceiling."""

    def test_fetch_within_ceiling_returns_cached(self):
        """Fetch within _USAGE_RATE_LIMIT_CEILING seconds -> return _cached."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30
        db = _mock_db({"usage_cached_at": int(time.time()) - 30})
        result = asyncio.run(fetch_usage(1, db))
        assert result == {"_cached": True}

    def test_fetch_past_ceiling_proceeds(self):
        """Fetch past _USAGE_RATE_LIMIT_CEILING seconds -> make API call."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 70
        db = _mock_db({"usage_cached_at": int(time.time()) - 70})
        client = _mock_client(200, {"five_hour": {"utilization": 10.0}, "seven_day": {"utilization": 20.0}})
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))
        assert result is not None
        assert result != {"_cached": True}

    def test_force_still_respects_ceiling(self):
        """force=True still respects the hard ceiling."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30
        db = _mock_db({"usage_cached_at": int(time.time()) - 30})
        result = asyncio.run(fetch_usage(1, db, force=True))
        assert result == {"_cached": True}

    def test_429_sets_backoff_in_state(self):
        """429 response sets backoff_until in coordinator state."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120
        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(429, {}, headers={"retry-after": "120"})
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))
        assert result is None
        assert mod._get_usage_state(1)["backoff_until"] > time.time()


# ---------------------------------------------------------------------------
# Cache freshness guard (now backed by coordinator state)
# ---------------------------------------------------------------------------


class TestCacheFreshnessGuard:
    """fetch_usage should skip the API call when within the rate limit ceiling."""

    def setup_method(self):
        import jacked.web.auth as mod
        mod._account_usage_state.clear()

    def test_fresh_cache_returns_cached_sentinel(self):
        """last_fetched_at recent -> return _cached."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 5
        db = _mock_db({"usage_cached_at": int(time.time()) - 5})
        result = asyncio.run(fetch_usage(1, db))
        assert result == {"_cached": True}

    def test_stale_cache_proceeds_with_fetch(self):
        """last_fetched_at old -> make API call."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120
        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(200, {
            "five_hour": {"utilization": 25.0},
            "seven_day": {"utilization": 50.0},
        })

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is not None
        assert result.get("five_hour", {}).get("utilization") == 25.0
        db.update_account_usage_cache.assert_called_once()

    def test_access_token_with_force_bypasses_old_cache(self):
        """Explicit access_token + force=True bypasses old cache (but not ceiling)."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120  # past ceiling
        db = _mock_db({"usage_cached_at": int(time.time()) - 2})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, access_token="fresh_token", force=True))
        assert result is not None
        assert result != {"_cached": True}
        client.get.assert_called_once()

    def test_none_cached_at_proceeds_with_fetch(self):
        """usage_cached_at=None (never fetched) + state default -> make API call."""
        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(200, {
            "five_hour": {"utilization": 0.0},
            "seven_day": {"utilization": 0.0},
        })

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is not None
        client.get.assert_called_once()

    def test_malformed_cached_at_proceeds_with_fetch(self):
        """Malformed usage_cached_at -> state default last_fetched_at=0 -> fetch."""
        db = _mock_db({"usage_cached_at": "not_a_number"})
        client = _mock_client(200, {
            "five_hour": {"utilization": 5.0},
            "seven_day": {"utilization": 10.0},
        })

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is not None
        client.get.assert_called_once()


# ---------------------------------------------------------------------------
# 429 handling
# ---------------------------------------------------------------------------


class TestFetchUsage429:
    """429 responses should NOT increment consecutive_failures."""

    def setup_method(self):
        import jacked.web.auth as mod
        mod._account_usage_state.clear()

    def test_429_does_not_increment_failures(self):
        """429 -> record_account_error with increment_failures=False."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={"retry-after": "5"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is None
        db.record_account_error.assert_called_once()
        # Check increment_failures=False was passed
        _, kwargs = db.record_account_error.call_args
        assert kwargs.get("increment_failures") is False

    def test_429_extracts_retry_after_header(self):
        """429 -> error message includes retry-after value."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={"retry-after": "120"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            asyncio.run(fetch_usage(1, db))

        error_msg = db.record_account_error.call_args[0][1]
        assert "120" in error_msg

    def test_429_returns_none(self):
        """429 -> returns None (signals failure to caller)."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is None

    def test_429_without_retry_after_uses_default_backoff(self):
        """429 with no retry-after header -> defaults to ceiling-based backoff."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            asyncio.run(fetch_usage(1, db))

        error_msg = db.record_account_error.call_args[0][1]
        assert "65" in error_msg


# ---------------------------------------------------------------------------
# Backoff via coordinator state
# ---------------------------------------------------------------------------


class TestUsageBackoff:
    """fetch_usage should respect 429 backoff via coordinator state."""

    def setup_method(self):
        import jacked.web.auth as mod
        mod._account_usage_state.clear()

    def test_429_sets_backoff(self):
        """After a 429, subsequent calls should be skipped."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 120

        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(429, {}, headers={"retry-after": "65"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))
        assert result is None

        result2 = asyncio.run(fetch_usage(1, db))
        assert result2 == {"_backed_off": True}

    def test_backoff_expires(self):
        """After backoff expires, fetches proceed normally."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["backoff_until"] = time.time() - 10
        state["last_fetched_at"] = time.time() - 120

        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(200, {
            "five_hour": {"utilization": 0.0},
            "seven_day": {"utilization": 0.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))
        assert result is not None
        assert result != {"_backed_off": True}


class TestUrgencyTier:
    """Tests for adaptive polling urgency tier computation."""

    def test_idle_low_usage_no_burn(self):
        """Usage < 50%, burn rate ~0 -> IDLE (300s)."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=20.0, usage_7d=30.0,
            burn_rate_5h=0.0, critical_5h=90.0,
        )
        assert tier == "idle"
        assert interval == 300

    def test_normal_moderate_usage(self):
        """Usage 50-70%, low burn -> NORMAL (150s)."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=0.2, critical_5h=90.0,
        )
        assert tier == "normal"
        assert interval == 150

    def test_warning_high_usage(self):
        """Usage 70-85% -> WARNING (90s)."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=75.0, usage_7d=30.0,
            burn_rate_5h=0.5, critical_5h=90.0,
        )
        assert tier == "warning"
        assert interval == 90

    def test_critical_very_high_usage(self):
        """Usage > 85% -> CRITICAL (65s)."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=88.0, usage_7d=30.0,
            burn_rate_5h=1.0, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 65

    def test_burn_rate_projects_critical_soon(self):
        """Usage 60% but burn rate projects critical in 5 min -> CRITICAL."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=6.0, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 65

    def test_burn_rate_projects_critical_in_15_min(self):
        """Usage 60% but burn rate projects critical in 15 min -> WARNING."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=2.0, critical_5h=90.0,
        )
        assert tier == "warning"
        assert interval == 90

    def test_7d_escalation(self):
        """7d > 80% bumps up one tier regardless of 5h."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=20.0, usage_7d=85.0,
            burn_rate_5h=0.0, critical_5h=90.0,
        )
        assert tier == "normal"
        assert interval == 150

    def test_7d_escalation_stacks_with_warning(self):
        """7d > 80% bumps WARNING to CRITICAL."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=75.0, usage_7d=85.0,
            burn_rate_5h=0.5, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 65

    def test_none_usage_defaults_to_idle(self):
        """None usage (never fetched) -> IDLE."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=None, usage_7d=None,
            burn_rate_5h=0.0, critical_5h=90.0,
        )
        assert tier == "idle"
        assert interval == 300


class TestConstants:
    """Verify constants are properly defined."""

    def test_cache_freshness_constant_exists(self):
        assert USAGE_CACHE_FRESHNESS_SECONDS == 30

    def test_cache_freshness_is_positive(self):
        assert USAGE_CACHE_FRESHNESS_SECONDS > 0
