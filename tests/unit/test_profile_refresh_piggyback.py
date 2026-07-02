"""fetch_usage piggybacks a THROTTLED profile refresh.

Subscription metadata (subscription_type, rate_limit_tier) otherwise only
updates at account-add and after a token refresh — a parked account whose
token never rotates would show a stale plan forever after a Pro <-> Max or
5x <-> 20x change. A successful usage fetch now also refreshes the profile
at most once per _PROFILE_REFRESH_INTERVAL per account.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import jacked.web.auth as mod
from jacked.web.auth import fetch_usage


def _mock_db():
    db = MagicMock()
    db.get_account.return_value = {
        "id": 1,
        "email": "user@test.com",
        "access_token": "test_access_token",
        "refresh_token": "test_refresh_token",
        "is_active": True,
        "usage_cached_at": None,
        "cached_usage_5h": None,
        "cached_usage_7d": None,
    }
    return db


def _mock_client(n=3):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "five_hour": {"utilization": 10.0},
        "seven_day": {"utilization": 20.0},
    }
    resp.headers = {}
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=[resp] * n)
    return client


def _reset_state():
    mod._account_usage_state.clear()
    mod._profile_refreshed_at.clear()


class TestProfilePiggyback:
    def test_successful_fetch_triggers_profile_refresh(self):
        _reset_state()
        db = _mock_db()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=_mock_client()), \
             patch("jacked.web.auth.fetch_profile", new=AsyncMock()) as prof:
            result = asyncio.run(fetch_usage(1, db, manual=True))
        assert result and result.get("five_hour")
        prof.assert_awaited_once()
        assert prof.await_args.args[0] == 1

    def test_refresh_throttled_within_interval(self):
        _reset_state()
        db = _mock_db()
        with patch("jacked.web.auth.httpx.AsyncClient", side_effect=lambda **k: _mock_client()), \
             patch("jacked.web.auth.fetch_profile", new=AsyncMock()) as prof, \
             patch("jacked.web.auth._USAGE_MANUAL_FLOOR_SECONDS", 0), \
             patch("jacked.web.auth._USAGE_FETCH_MIN_INTERVAL", 0, create=True):
            asyncio.run(fetch_usage(1, db, manual=True))
            # Force pacing to allow a second real fetch
            mod._account_usage_state[1]["last_fetched_at"] = 0
            asyncio.run(fetch_usage(1, db, manual=True))
        # Two usage fetches, ONE profile refresh (second was throttled)
        prof.assert_awaited_once()

    def test_profile_failure_is_nonfatal_and_does_not_retry_every_poll(self):
        _reset_state()
        db = _mock_db()
        boom = AsyncMock(side_effect=RuntimeError("profile API down"))
        with patch("jacked.web.auth.httpx.AsyncClient", side_effect=lambda **k: _mock_client()), \
             patch("jacked.web.auth.fetch_profile", new=boom):
            result = asyncio.run(fetch_usage(1, db, manual=True))
            assert result and result.get("five_hour")  # usage still succeeds
            mod._account_usage_state[1]["last_fetched_at"] = 0
            asyncio.run(fetch_usage(1, db, manual=True))
        boom.assert_awaited_once()  # stamped before the call — no per-poll retry storm
