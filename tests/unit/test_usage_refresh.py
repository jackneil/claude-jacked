"""Tests for usage refresh: cache freshness guard, 429 handling.

Covers:
- Cache freshness guard in fetch_usage (integer timestamps, bypass with access_token)
- 429 response handling with increment_failures=False
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
# Cache freshness guard
# ---------------------------------------------------------------------------


class TestCacheFreshnessGuard:
    """fetch_usage should skip the API call when cache is fresh."""

    def test_fresh_cache_returns_cached_sentinel(self):
        """Cache < USAGE_CACHE_FRESHNESS_SECONDS old -> return _cached."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 5})
        result = asyncio.run(fetch_usage(1, db))
        assert result == {"_cached": True}

    def test_stale_cache_proceeds_with_fetch(self):
        """Cache > USAGE_CACHE_FRESHNESS_SECONDS old -> make API call."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 60})
        client = _mock_client(200, {
            "five_hour": {"utilization": 25.0},
            "seven_day": {"utilization": 50.0},
        })

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is not None
        assert result.get("five_hour", {}).get("utilization") == 25.0
        db.update_account_usage_cache.assert_called_once()

    def test_access_token_with_min_age_zero_bypasses_cache(self):
        """Explicit access_token + min_age=0 should bypass cache."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 2})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, access_token="fresh_token", min_age=0))
        assert result is not None
        assert result != {"_cached": True}
        client.get.assert_called_once()

    def test_none_cached_at_proceeds_with_fetch(self):
        """usage_cached_at=None (never fetched) -> make API call."""
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
        """Malformed usage_cached_at -> skip guard, proceed with fetch."""
        db = _mock_db({"usage_cached_at": "not_a_number"})
        client = _mock_client(200, {
            "five_hour": {"utilization": 5.0},
            "seven_day": {"utilization": 10.0},
        })

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is not None
        client.get.assert_called_once()

    def test_boundary_at_exactly_threshold(self):
        """Cache at exactly USAGE_CACHE_FRESHNESS_SECONDS -> not fresh."""
        db = _mock_db({
            "usage_cached_at": int(time.time()) - USAGE_CACHE_FRESHNESS_SECONDS,
        })
        client = _mock_client(200, {
            "five_hour": {"utilization": 0.0},
            "seven_day": {"utilization": 0.0},
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

    def test_429_does_not_increment_failures(self):
        """429 -> record_account_error with increment_failures=False."""
        import jacked.web.auth as mod
        mod._usage_backoff.clear()

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
        mod._usage_backoff.clear()

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={"retry-after": "120"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            asyncio.run(fetch_usage(1, db))

        error_msg = db.record_account_error.call_args[0][1]
        assert "120" in error_msg

    def test_429_returns_none(self):
        """429 -> returns None (signals failure to caller)."""
        import jacked.web.auth as mod
        mod._usage_backoff.clear()

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is None

    def test_429_without_retry_after_uses_default_backoff(self):
        """429 with no retry-after header -> defaults to 60s backoff."""
        import jacked.web.auth as mod
        mod._usage_backoff.clear()

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            asyncio.run(fetch_usage(1, db))

        error_msg = db.record_account_error.call_args[0][1]
        assert "60" in error_msg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


class TestMinAgeParameter:
    """fetch_usage min_age controls cache freshness threshold."""

    def setup_method(self):
        import jacked.web.auth as mod
        mod._usage_backoff.clear()

    def test_min_age_skips_when_fresh_enough(self):
        """Cache 10s old, min_age=55 -> skip."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 10})
        result = asyncio.run(fetch_usage(1, db, min_age=55))
        assert result == {"_cached": True}

    def test_min_age_fetches_when_stale(self):
        """Cache 60s old, min_age=55 -> fetch."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 60})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=55))
        assert result is not None
        assert result != {"_cached": True}

    def test_min_age_zero_always_fetches(self):
        """min_age=0 -> always fetch even with fresh cache."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 2})
        client = _mock_client(200, {
            "five_hour": {"utilization": 5.0},
            "seven_day": {"utilization": 10.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result is not None
        assert result != {"_cached": True}
        client.get.assert_called_once()

    def test_access_token_no_longer_bypasses_cache(self):
        """access_token alone should NOT bypass cache."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 2})
        result = asyncio.run(fetch_usage(1, db, access_token="tok"))
        assert result == {"_cached": True}


class TestUsageBackoff:
    """fetch_usage should respect 429 backoff."""

    def test_429_sets_backoff(self):
        """After a 429, subsequent calls should be skipped."""
        import jacked.web.auth as mod
        mod._usage_backoff.clear()

        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(429, {}, headers={"retry-after": "60"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result is None

        result2 = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result2 == {"_backed_off": True}

    def test_backoff_expires(self):
        """After backoff expires, fetches proceed normally."""
        import jacked.web.auth as mod
        mod._usage_backoff.clear()
        mod._usage_backoff[1] = time.time() - 10

        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(200, {
            "five_hour": {"utilization": 0.0},
            "seven_day": {"utilization": 0.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result is not None
        assert result != {"_backed_off": True}


class TestConstants:
    """Verify constants are properly defined."""

    def test_cache_freshness_constant_exists(self):
        assert USAGE_CACHE_FRESHNESS_SECONDS == 30

    def test_cache_freshness_is_positive(self):
        assert USAGE_CACHE_FRESHNESS_SECONDS > 0
