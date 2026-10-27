"""Tests for usage refresh: coordinator ceiling, 429 handling.

Covers:
- Hard rate-limit ceiling via per-account coordinator state
- 429 response handling with increment_failures=False
- 429 backoff cap at 900s
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
        state["last_fetched_at"] = time.time() - 200
        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
        client = _mock_client(200, {"five_hour": {"utilization": 10.0}, "seven_day": {"utilization": 20.0}})
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))
        assert result is not None
        assert result != {"_cached": True}

    def test_429_sets_backoff_in_state(self):
        """429 response sets backoff_until in coordinator state."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200
        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
        client = _mock_client(429, {}, headers={"retry-after": "240"})
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", AsyncMock(return_value=None)):
            result = asyncio.run(fetch_usage(1, db))
        assert result is None
        assert mod._get_usage_state(1)["backoff_until"] > time.time()

    def test_429_backoff_honors_retry_after_up_to_the_bound(self):
        """A huge retry-after is honored, up to the 6h sanity bound."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200
        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
        client = _mock_client(429, {}, headers={"retry-after": "999999"})
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", AsyncMock(return_value=None)):
            asyncio.run(fetch_usage(1, db))
        backoff = mod._get_usage_state(1)["backoff_until"] - time.time()
        assert mod._USAGE_RETRY_AFTER_MAX_SECONDS - 5 <= backoff
        assert backoff <= mod._USAGE_RETRY_AFTER_MAX_SECONDS + 1


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
        state["last_fetched_at"] = time.time() - 200
        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
        client = _mock_client(200, {
            "five_hour": {"utilization": 25.0},
            "seven_day": {"utilization": 50.0},
        })

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result is not None
        assert result.get("five_hour", {}).get("utilization") == 25.0
        db.update_account_usage_cache.assert_called_once()

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
        state["last_fetched_at"] = time.time() - 200

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={"retry-after": "5"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", AsyncMock(return_value=None)):
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
        state["last_fetched_at"] = time.time() - 200

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={"retry-after": "240"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", AsyncMock(return_value=None)):
            asyncio.run(fetch_usage(1, db))

        error_msg = db.record_account_error.call_args[0][1]
        assert "240" in error_msg

    def test_429_returns_none(self):
        """429 -> returns None (signals failure to caller)."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", AsyncMock(return_value=None)):
            result = asyncio.run(fetch_usage(1, db))

        assert result is None

    def test_429_without_retry_after_uses_default_backoff(self):
        """429 with no retry-after header -> defaults to ceiling-based backoff."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200

        db = _mock_db({"usage_cached_at": None})
        client = _mock_client(429, headers={})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", AsyncMock(return_value=None)):
            asyncio.run(fetch_usage(1, db))

        error_msg = db.record_account_error.call_args[0][1]
        assert "180" in error_msg


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
        state["last_fetched_at"] = time.time() - 200

        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
        client = _mock_client(429, {}, headers={"retry-after": "65"})

        # Isolate pure backoff behavior — stub the 429 refresh attempt (mirrors
        # the other 429 tests) so the real refresh path doesn't leak an
        # unawaited coroutine through the mocked httpx client.
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", AsyncMock(return_value=None)):
            result = asyncio.run(fetch_usage(1, db))
        assert result is None

        result2 = asyncio.run(fetch_usage(1, db))
        assert result2 == {"_backed_off": True}

    def test_backoff_expires(self):
        """After backoff expires, fetches proceed normally."""
        import jacked.web.auth as mod
        state = mod._get_usage_state(1)
        state["backoff_until"] = time.time() - 10
        state["last_fetched_at"] = time.time() - 200

        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
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
        assert interval == 300

    def test_warning_high_usage(self):
        """Usage 70-85% -> WARNING (90s)."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=75.0, usage_7d=30.0,
            burn_rate_5h=0.5, critical_5h=90.0,
        )
        assert tier == "warning"
        assert interval == 240

    def test_critical_very_high_usage(self):
        """Usage > 85% -> CRITICAL (65s)."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=88.0, usage_7d=30.0,
            burn_rate_5h=1.0, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 180

    def test_burn_rate_projects_critical_soon(self):
        """Usage 60% but burn rate projects critical in 5 min -> CRITICAL."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=6.0, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 180

    def test_burn_rate_projects_critical_in_15_min(self):
        """Usage 60% but burn rate projects critical in 15 min -> WARNING."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=2.0, critical_5h=90.0,
        )
        assert tier == "warning"
        assert interval == 240

    def test_7d_escalation(self):
        """7d > 80% bumps up one tier regardless of 5h."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=20.0, usage_7d=85.0,
            burn_rate_5h=0.0, critical_5h=90.0,
        )
        assert tier == "normal"
        assert interval == 300

    def test_7d_escalation_stacks_with_warning(self):
        """7d > 80% bumps WARNING to CRITICAL."""
        from jacked.web.auth import compute_urgency_tier

        tier, interval = compute_urgency_tier(
            usage_5h=75.0, usage_7d=85.0,
            burn_rate_5h=0.5, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 180

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


# ---------------------------------------------------------------------------
# Escalating backoff on consecutive 429s
# ---------------------------------------------------------------------------


class TestEscalatingBackoff:
    def test_consecutive_429s_escalate(self):
        """Each consecutive 429 should increase the backoff."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()

        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
        client = _mock_client(429, {}, headers={"retry-after": "0"})

        # Mock out _try_refresh_on_429 so we test pure backoff behavior
        refresh_mock = AsyncMock(return_value=None)

        # First 429
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", refresh_mock):
            asyncio.run(fetch_usage(1, db))
        assert state["consecutive_429s"] == 1
        backoff1 = state["backoff_until"] - time.time()
        assert 170 <= backoff1 <= 190  # ~180s

        # Second 429
        state["last_fetched_at"] = time.time() - 200
        state["backoff_until"] = 0
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", refresh_mock):
            asyncio.run(fetch_usage(1, db))
        assert state["consecutive_429s"] == 2
        backoff2 = state["backoff_until"] - time.time()
        assert 350 <= backoff2 <= 370  # ~360s

    def test_success_resets_consecutive_count(self):
        """Successful fetch should reset consecutive_429s to 0."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["consecutive_429s"] = 5
        state["last_fetched_at"] = time.time() - 200

        db = _mock_db({"usage_cached_at": int(time.time()) - 200})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            asyncio.run(fetch_usage(1, db))
        assert state["consecutive_429s"] == 0


# ---------------------------------------------------------------------------
# B3: a 401 refreshes ONLY the lineage that owns the failing token
# ---------------------------------------------------------------------------


def _real_db(tmp_path, **cc):
    """A real Database with one Claude account that holds both chains."""
    from jacked.web.database import Database

    db = Database(str(tmp_path / "chains.db"))
    db.create_account(
        email="alice@test.com",
        access_token="primary_at",
        refresh_token="primary_rt",
        expires_at=int(time.time()) + 6 * 3600,
        scopes=None,
    )
    db.update_account(
        1,
        cc_access_token=cc.get("cc_access_token", "cc_at"),
        cc_refresh_token=cc.get("cc_refresh_token", "cc_rt"),
        cc_expires_at=cc.get("cc_expires_at", int(time.time()) + 6 * 3600),
        cc_refresh_failure_type=cc.get("cc_refresh_failure_type"),
    )
    return db


def _seq_client(*statuses):
    """Usage GETs answer the given statuses in order; records the tokens sent."""
    sent: list[str] = []
    resps = []
    for status in statuses:
        resp = MagicMock()
        resp.status_code = status
        resp.headers = {}
        resp.json.return_value = (
            {"five_hour": {"utilization": 5.0, "resets_at": "R5"},
             "seven_day": {"utilization": 6.0, "resets_at": "R7"}}
            if status == 200 else {}
        )
        resps.append(resp)

    async def get(url, headers=None, **_kw):
        sent.append(headers["Authorization"].removeprefix("Bearer "))
        return resps.pop(0)

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=get)
    return client, sent


class TestCcToken401Ownership:
    def setup_method(self):
        import jacked.web.auth as mod

        mod._account_usage_state.clear()
        mod._profile_refreshed_at[1] = time.time()  # no piggybacked profile GET
        mod.reset_locks()

    def test_cc_401_on_active_account_falls_back_to_primary_and_records_cc_failure(
        self, tmp_path,
    ):
        """The active account's cc chain belongs to Claude Code (I2): a 401
        on its cc token must not rotate it, must not take the primary
        circuit-breaker path, must record the cc failure, and the retry
        uses the primary token."""
        from jacked.web.auth import RefreshMode

        db = _real_db(tmp_path)
        client, sent = _seq_client(401, 200)
        flow = AsyncMock()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.cc_rotation_block", return_value="active"), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            result = asyncio.run(fetch_usage(1, db, access_token="cc_at"))

        assert result is not None and "five_hour" in result
        assert sent == ["cc_at", "primary_at"]
        assert all(c.args[2] != RefreshMode.PRIMARY_CIRCUIT_BREAKER for c in flow.await_args_list)
        flow.assert_not_awaited()
        row = db.get_account(1)
        assert row["cc_refresh_failure_type"] == "auth_rejected"
        assert row["cc_refresh_last_failed_at"] is not None
        # Primary chain untouched.
        assert row["refresh_token"] == "primary_rt"
        assert row["refresh_failure_type"] is None

    def test_after_a_cc_401_the_chooser_stops_picking_the_cc_token(self, tmp_path):
        from jacked.web.auth import choose_usage_token

        db = _real_db(tmp_path)
        client, _sent = _seq_client(401, 200)
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.cc_rotation_block", return_value="active"):
            asyncio.run(fetch_usage(1, db, access_token="cc_at"))

        choice = choose_usage_token(db.get_account(1))
        assert choice.chain == "primary"
        assert choice.token == "primary_at"

    def test_cc_401_on_inactive_account_rotates_only_the_cc_chain(self, tmp_path):
        from jacked.web.auth import RefreshMode, TokenExchangeResult

        db = _real_db(tmp_path)
        client, sent = _seq_client(401, 200)
        calls: list = []

        async def flow(account_id, db_, mode):
            calls.append(mode)
            db_.update_account(account_id, cc_access_token="cc_at_new")
            return TokenExchangeResult(
                success=True, access_token="cc_at_new", exchanged=True, chain="cc",
            )

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.cc_rotation_block", return_value=None), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            asyncio.run(fetch_usage(1, db, access_token="cc_at"))

        assert calls == [RefreshMode.CC]
        assert sent == ["cc_at", "cc_at_new"]
        assert db.get_account(1)["cc_refresh_failure_type"] is None

    def test_rotated_cc_token_refused_again_records_auth_rejected(self, tmp_path):
        """A rotation that yields a token the API still refuses must record
        auth_rejected on the cc chain: otherwise every poll rotates the cc
        chain once more, forever (CSO 2026-10-26)."""
        from jacked.web.auth import RefreshMode, TokenExchangeResult, choose_usage_token

        db = _real_db(tmp_path)
        client, sent = _seq_client(401, 401)
        calls: list = []

        async def flow(account_id, db_, mode):
            calls.append(mode)
            db_.update_account(account_id, cc_access_token="cc_at_new")
            return TokenExchangeResult(
                success=True, access_token="cc_at_new", exchanged=True, chain="cc",
            )

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.cc_rotation_block", return_value=None), \
             patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store"), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            result = asyncio.run(fetch_usage(1, db, access_token="cc_at"))

        assert result is None
        assert calls == [RefreshMode.CC]
        assert sent == ["cc_at", "cc_at_new"]
        row = db.get_account(1)
        assert row["cc_refresh_failure_type"] == "auth_rejected"
        assert choose_usage_token(row).chain == "primary"

    def test_a_reauth_during_the_retry_request_is_never_marked_rejected(self, tmp_path):
        """A newer cc token stored while the retry request was in flight was
        never refused: the auth_rejected write must not touch it."""
        from jacked.web.auth import TokenExchangeResult, choose_usage_token

        db = _real_db(tmp_path)
        client, sent = _seq_client(401, 401)
        inner_get = client.get.side_effect

        async def get_and_reauth(url, headers=None, **kw):
            resp = await inner_get(url, headers=headers, **kw)
            if len(sent) == 2:  # the retry frame: a dashboard re-auth lands meanwhile
                db.update_account(1, cc_access_token="cc_at_reauthed",
                                  cc_refresh_token="cc_rt_reauthed")
            return resp

        client.get = AsyncMock(side_effect=get_and_reauth)

        async def flow(account_id, db_, mode):
            db_.update_account(account_id, cc_access_token="cc_at_new")
            return TokenExchangeResult(
                success=True, access_token="cc_at_new", exchanged=True, chain="cc",
            )

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.cc_rotation_block", return_value=None), \
             patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store"), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            asyncio.run(fetch_usage(1, db, access_token="cc_at"))

        assert sent == ["cc_at", "cc_at_new"]
        row = db.get_account(1)
        assert row["cc_refresh_failure_type"] is None
        assert choose_usage_token(row).token == "cc_at_reauthed"

    def test_primary_401_still_uses_the_primary_circuit_breaker(self, tmp_path):
        from jacked.web.auth import RefreshMode, TokenExchangeResult

        db = _real_db(tmp_path)
        client, _sent = _seq_client(401, 200)
        calls: list = []

        async def flow(account_id, db_, mode):
            calls.append(mode)
            return TokenExchangeResult(success=True, access_token="primary_at_new")

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            asyncio.run(fetch_usage(1, db))

        assert calls == [RefreshMode.PRIMARY_CIRCUIT_BREAKER]
        assert db.get_account(1)["cc_refresh_failure_type"] is None

    def test_429_on_cc_token_never_rotates_the_primary_chain(self, tmp_path):
        """The old cc_429 path silently renewed the PRIMARY chain whenever
        the cc refresh token was gone."""
        db = _real_db(tmp_path, cc_refresh_token=None)
        client, _sent = _seq_client(429)
        flow = AsyncMock()
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            assert asyncio.run(fetch_usage(1, db, access_token="cc_at")) is None
        flow.assert_not_awaited()


# ---------------------------------------------------------------------------
# B5: null windows and null resets_at
# ---------------------------------------------------------------------------


class TestNullWindows:
    def setup_method(self):
        import jacked.web.auth as mod

        mod._account_usage_state.clear()
        mod._profile_refreshed_at[1] = time.time()

    def _fetch(self, db, body):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {}
        resp.json.return_value = body
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(return_value=resp)
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            return asyncio.run(fetch_usage(1, db))

    def test_null_top_level_window_does_not_raise_and_clears_it(self, tmp_path):
        db = _real_db(tmp_path)
        db.update_account_usage_cache(
            1, five_hour=40.0, five_hour_resets_at="2026-10-17T10:00:00Z")
        db.update_account(1, usage_cached_at=0)

        body = {"five_hour": None, "seven_day": {"utilization": 12.0, "resets_at": "R7"}}
        assert self._fetch(db, body) == body

        row = db.get_account(1)
        assert row["cached_usage_5h"] is None
        assert row["cached_5h_resets_at"] is None
        assert row["cached_usage_7d"] == 12.0
        assert row["cached_7d_resets_at"] == "R7"

    def test_null_resets_at_clears_the_stale_reset_and_keeps_utilization(self, tmp_path):
        """Account 1 kept a 10-17 reset for 9 days while the API said
        resets_at: null."""
        db = _real_db(tmp_path)
        db.update_account_usage_cache(
            1, five_hour=40.0, five_hour_resets_at="2026-10-17T10:00:00Z",
            seven_day=50.0, seven_day_resets_at="2026-10-20T10:00:00Z")
        db.update_account(1, usage_cached_at=0)

        body = {
            "five_hour": {"utilization": 0.0, "resets_at": None},
            "seven_day": {"utilization": 51.0, "resets_at": None},
        }
        self._fetch(db, body)

        row = db.get_account(1)
        assert row["cached_usage_5h"] == 0.0
        assert row["cached_5h_resets_at"] is None
        assert row["cached_usage_7d"] == 51.0
        assert row["cached_7d_resets_at"] is None

    def test_missing_window_key_keeps_the_old_cache(self, tmp_path):
        db = _real_db(tmp_path)
        db.update_account_usage_cache(
            1, seven_day=50.0, seven_day_resets_at="R7-old")
        db.update_account(1, usage_cached_at=0)

        self._fetch(db, {"five_hour": {"utilization": 3.0, "resets_at": "R5"}})

        row = db.get_account(1)
        assert row["cached_usage_7d"] == 50.0
        assert row["cached_7d_resets_at"] == "R7-old"
