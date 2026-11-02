"""Tests for fetch_usage rate-limit hardening (2026-06 usage-fetch storm).

Covers:
- Manual floor: manual=True within _USAGE_MANUAL_FLOOR_SECONDS of the last
  fetch returns {'_cached': True} without an HTTP call
- 429/401 retries never un-pace the account (no last_fetched_at=0 reset);
  the bounded retry rides _retry_depth>0 through the ceiling instead
- 429 token rotation needs _USAGE_429_ROTATION_MIN_CONSECUTIVE consecutive
  429s on the same token and is throttled to once per guard interval
  (several poll periods, read from settings at call time)
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jacked.web.auth as mod
from jacked.web.auth import TokenExchangeResult, fetch_usage


@pytest.fixture(autouse=True)
def _no_profile_piggyback():
    """Keep the throttled profile refresh out of the GET counts.

    A successful usage fetch piggybacks a profile GET when the module-level
    ``_profile_refreshed_at`` has no recent stamp for the account. These
    tests count usage GETs only, so they must not depend on whether an
    earlier test in the same process left a stamp behind.
    """
    saved = dict(mod._profile_refreshed_at)
    mod._profile_refreshed_at.clear()
    mod._profile_refreshed_at[1] = time.time()
    yield
    mod._profile_refreshed_at.clear()
    mod._profile_refreshed_at.update(saved)


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


def _mock_client(responses):
    """Mock httpx.AsyncClient whose successive GETs return the given
    (status_code, json_data, headers) tuples."""
    resps = []
    for status_code, json_data, headers in responses:
        resp = MagicMock()
        resp.status_code = status_code
        resp.json.return_value = json_data or {}
        resp.headers = headers or {}
        resps.append(resp)

    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.get = AsyncMock(side_effect=resps)
    return client


def _arm_rotation(state, token="test_access_token", count=None):
    """Put the 429 counter one 429 short of the rotation threshold."""
    state["last_429_token_fp"] = mod._token_fingerprint(token)
    state["consecutive_429s"] = (
        mod._USAGE_429_ROTATION_MIN_CONSECUTIVE - 1 if count is None else count
    )


_USAGE_200 = {
    "five_hour": {"utilization": 10.0},
    "seven_day": {"utilization": 20.0},
}


# ---------------------------------------------------------------------------
# Manual floor
# ---------------------------------------------------------------------------


class TestManualFloor:
    def setup_method(self):
        mod._account_usage_state.clear()

    def test_floor_constant(self):
        assert mod._USAGE_MANUAL_FLOOR_SECONDS == 20

    def test_manual_within_floor_returns_cached_without_http(self):
        """manual=True within 20s of last fetch -> _cached, zero HTTP calls."""
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 5
        db = _mock_db()
        client = _mock_client([(200, _USAGE_200, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, manual=True))

        assert result == {"_cached": True}
        client.get.assert_not_called()

    def test_manual_past_floor_within_ceiling_proceeds(self):
        """manual=True past the 20s floor (but under the 65s ceiling) fetches."""
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30
        db = _mock_db()
        client = _mock_client([(200, _USAGE_200, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, manual=True))

        assert result == _USAGE_200
        assert client.get.call_count == 1

    def test_non_manual_still_honors_full_ceiling(self):
        """manual=False between floor and ceiling stays cached (65s ceiling)."""
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30
        db = _mock_db()
        client = _mock_client([(200, _USAGE_200, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))

        assert result == {"_cached": True}
        client.get.assert_not_called()


# ---------------------------------------------------------------------------
# 429/401 retries must never un-pace the account
# ---------------------------------------------------------------------------


class TestRetryPacing:
    def setup_method(self):
        mod._account_usage_state.clear()

    def test_429_retry_failure_does_not_unpace(self):
        """429 -> rotation -> retry fails with 500: last_fetched_at must NOT
        be left at 0 (the old reset let any client loop hammer the API)."""
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200
        _arm_rotation(state)
        db = _mock_db()
        client = _mock_client([(429, {}, {"retry-after": "65"}), (500, {}, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429",
                   AsyncMock(return_value="fresh-tok")):
            result = asyncio.run(fetch_usage(1, db))

        assert result is None
        assert state["last_fetched_at"] > 0
        assert time.time() - state["last_fetched_at"] < 5

    def test_429_retry_success_rides_depth_bypass(self):
        """Within the 65s ceiling, only the _retry_depth>0 bypass lets the
        rotated-token retry through — pacing state stays untouched."""
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30
        _arm_rotation(state)
        db = _mock_db()
        client = _mock_client([(429, {}, {}), (200, _USAGE_200, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429",
                   AsyncMock(return_value="fresh-tok")):
            result = asyncio.run(fetch_usage(1, db, manual=True))

        assert result == _USAGE_200
        assert client.get.call_count == 2
        assert state["last_fetched_at"] > 0

    def test_401_retry_failure_does_not_unpace(self):
        """401 -> primary refresh -> retry fails with 500: last_fetched_at
        must NOT be left at 0."""
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200
        db = _mock_db()
        client = _mock_client([(401, {}, {}), (500, {}, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_primary_token",
                   AsyncMock(return_value="fresh-tok")):
            result = asyncio.run(fetch_usage(1, db))

        assert result is None
        assert state["last_fetched_at"] > 0
        assert time.time() - state["last_fetched_at"] < 5


# ---------------------------------------------------------------------------
# Failure exits must pace the account (attempt-time stamping)
# ---------------------------------------------------------------------------


class TestFailurePacing:
    """Failure exits (401-final, generic HTTP error, transport exception)
    must stamp last_fetched_at: a permanently failing account that never
    stamps is never paced, so EVERY client request makes a fresh upstream
    GET — the unbounded half of the 2026-06 fetch storm."""

    def setup_method(self):
        mod._account_usage_state.clear()

    def test_perma_401_second_manual_call_is_cached(self):
        """Two back-to-back manual calls against a permanently-401 account
        (dead refresh token, live import yields the same token) must
        perform exactly one upstream GET."""
        db = _mock_db()
        client = _mock_client([(401, {}, {}), (401, {}, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_primary_token",
                   AsyncMock(return_value=None)), \
             patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store",
                   MagicMock()):
            first = asyncio.run(fetch_usage(1, db, manual=True))
            second = asyncio.run(fetch_usage(1, db, manual=True))

        assert first is None
        assert second == {"_cached": True}
        assert client.get.call_count == 1
        state = mod._get_usage_state(1)
        assert state["last_fetched_at"] > 0
        assert time.time() - state["last_fetched_at"] < 5

    def test_http_500_second_manual_call_is_cached(self):
        """Generic non-200/401/429 failure (upstream outage) must pace too."""
        db = _mock_db()
        client = _mock_client([(500, {}, {}), (500, {}, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            first = asyncio.run(fetch_usage(1, db, manual=True))
            second = asyncio.run(fetch_usage(1, db, manual=True))

        assert first is None
        assert second == {"_cached": True}
        assert client.get.call_count == 1

    def test_transport_exception_second_manual_call_is_cached(self):
        """The exception exit (timeout, connection reset) must pace too."""
        db = _mock_db()
        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=Exception("connection reset"))

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            first = asyncio.run(fetch_usage(1, db, manual=True))
            second = asyncio.run(fetch_usage(1, db, manual=True))

        assert first is None
        assert second == {"_cached": True}
        assert client.get.call_count == 1

    def test_background_failure_honors_full_ceiling(self):
        """manual=False after a failed fetch stays cached for the ceiling."""
        db = _mock_db()
        client = _mock_client([(500, {}, {})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            assert asyncio.run(fetch_usage(1, db)) is None
            assert asyncio.run(fetch_usage(1, db)) == {"_cached": True}

        assert client.get.call_count == 1


# ---------------------------------------------------------------------------
# 429 token-rotation throttle
# ---------------------------------------------------------------------------


class TestRotationThrottle:
    def setup_method(self):
        mod._account_usage_state.clear()

    def test_rotation_interval_spans_several_poll_periods(self):
        """The guard is 3 poll periods, floored at the 600s all-accounts poll,
        so it can never equal the poll period again (the old 600s guard
        equaled it and throttled nothing)."""
        assert mod._USAGE_429_ROTATION_INTERVAL == 1800
        db = MagicMock()
        db.get_setting.return_value = "300"
        assert mod._usage_429_rotation_interval(db) == 1800
        db.get_setting.return_value = "1200"
        assert mod._usage_429_rotation_interval(db) == 3600

    def test_first_and_second_429_never_rotate(self):
        """A 429 backs off; it does not rotate. Fewer than 3 consecutive
        429s on the same token never reach the token endpoint."""
        db = _mock_db()
        flow_mock = AsyncMock(
            return_value=TokenExchangeResult(success=False, error="http_429"))
        state = mod._get_usage_state(1)

        for _ in range(2):
            state["last_fetched_at"] = time.time() - 200
            state["backoff_until"] = 0
            client = _mock_client([(429, {}, {"retry-after": "0"})])
            with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
                 patch("jacked.web.auth._refresh_token_flow", flow_mock):
                assert asyncio.run(fetch_usage(1, db)) is None

        assert flow_mock.call_count == 0
        assert state["consecutive_429s"] == 2
        assert state["backoff_until"] > time.time()

    def test_a_new_token_restarts_the_429_count(self):
        """Consecutive 429s are counted per token."""
        db = _mock_db()
        state = mod._get_usage_state(1)
        _arm_rotation(state, token="some_other_token")
        state["last_fetched_at"] = time.time() - 200
        flow_mock = AsyncMock(
            return_value=TokenExchangeResult(success=False, error="http_429"))
        client = _mock_client([(429, {}, {"retry-after": "0"})])
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._refresh_token_flow", flow_mock):
            asyncio.run(fetch_usage(1, db))
        assert flow_mock.call_count == 0
        assert state["consecutive_429s"] == 1

    def test_second_429_within_interval_skips_rotation(self):
        """Once a rotation happened, another one waits a full guard
        interval — the incident rotated CC tokens 243 times in 4.5h."""
        db = _mock_db()
        flow_mock = AsyncMock(
            return_value=TokenExchangeResult(success=False, error="http_429"))
        state = mod._get_usage_state(1)

        def run_429():
            state["last_fetched_at"] = time.time() - 200
            state["backoff_until"] = 0
            client = _mock_client([(429, {}, {"retry-after": "0"})])
            with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
                 patch("jacked.web.auth._refresh_token_flow", flow_mock):
                return asyncio.run(fetch_usage(1, db))

        _arm_rotation(state)
        assert run_429() is None
        assert flow_mock.call_count == 1

        assert run_429() is None
        assert flow_mock.call_count == 1  # throttled — no second rotation

    def test_rotation_allowed_again_after_interval(self):
        """Once the rotation window has elapsed, a 429 may rotate again."""
        db = _mock_db()
        flow_mock = AsyncMock(
            return_value=TokenExchangeResult(success=False, error="http_429"))
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200
        state["last_429_rotation_at"] = (
            time.time() - mod._USAGE_429_ROTATION_INTERVAL - 100
        )
        _arm_rotation(state)
        client = _mock_client([(429, {}, {"retry-after": "0"})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._refresh_token_flow", flow_mock):
            asyncio.run(fetch_usage(1, db))

        assert flow_mock.call_count == 1

    def test_throttled_429_still_sets_backoff(self):
        """When rotation is skipped, the escalating backoff still engages."""
        db = _mock_db()
        flow_mock = AsyncMock(
            return_value=TokenExchangeResult(success=False, error="http_429"))
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 200
        state["last_429_rotation_at"] = time.time()  # rotation just happened
        _arm_rotation(state)
        client = _mock_client([(429, {}, {"retry-after": "0"})])

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._refresh_token_flow", flow_mock):
            result = asyncio.run(fetch_usage(1, db))

        assert result is None
        assert flow_mock.call_count == 0
        assert state["backoff_until"] > time.time()


# ---------------------------------------------------------------------------
# Real cadence: the all-accounts poll (600s + jitter) against a token that
# always 429s
# ---------------------------------------------------------------------------


class TestRotationAtRealCadence:
    def setup_method(self):
        mod._account_usage_state.clear()

    def test_always_429_rotates_at_most_once_per_guard_interval(self):
        """Run fetch_usage every 620s for 12h with a token that always 429s.

        Before the fix every poll rotated (the 600s guard equaled the
        poll period): ~140 primary renewals a day on account 1.
        """
        db = _mock_db()
        db.get_setting.return_value = "300"
        clock = {"now": 1_800_000_000.0}
        rotations: list[float] = []

        async def fake_rotation(account_id, db_, state, stale_token=None):
            rotations.append(clock["now"])
            return None  # the token endpoint did not help

        def always_429(*_a, **_k):
            resp = MagicMock()
            resp.status_code = 429
            resp.headers = {}
            resp.json.return_value = {}
            return resp

        client = AsyncMock()
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        client.get = AsyncMock(side_effect=always_429)

        polls = 0
        with patch.object(mod.time, "time", lambda: clock["now"]), \
             patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.web.auth._try_refresh_on_429", fake_rotation):
            end = clock["now"] + 12 * 3600
            while clock["now"] < end:
                asyncio.run(fetch_usage(1, db))
                polls += 1
                clock["now"] += 600 + 20

        guard = mod._usage_429_rotation_interval(db)
        assert polls >= 60
        assert client.get.call_count >= 30  # backoff skips some polls
        assert 1 <= len(rotations) <= 12 * 3600 / guard + 1
        for earlier, later in zip(rotations, rotations[1:]):
            assert later - earlier >= guard


# ---------------------------------------------------------------------------
# #149i item 4: a 429 on the Claude Code token backs off and never rotates
# ---------------------------------------------------------------------------


class TestCcToken429NeverRotates:
    """A cc rotation on a 429 spends a single-use refresh token that a
    running session may hold. A 429 on the cc token backs off only: no
    chain is exchanged, whether or not the cc chain could rotate."""

    LINE = "429 on the Claude Code token; backing off, no rotation"

    def setup_method(self):
        mod._account_usage_state.clear()
        mod.reset_locks()

    def teardown_method(self):
        mod._account_usage_state.clear()
        mod.reset_locks()

    @pytest.mark.parametrize("block", [None, "active", "live_session_holds"])
    def test_five_consecutive_429s_never_call_the_token_endpoint(
        self, tmp_path, caplog, block,
    ):
        import logging

        from jacked.web.database import Database

        db = Database(str(tmp_path / "cc429.db"))
        acct = db.create_account("cc429@test.com", "primary_at", int(time.time()) + 3600,
                                 refresh_token="primary_rt")
        aid = acct["id"]
        db.update_account(aid, cc_access_token="cc_at", cc_refresh_token="cc_rt",
                          cc_expires_at=int(time.time()) + 3600)
        state = mod._get_usage_state(aid)
        client = _mock_client([(429, {}, {"retry-after": "0"})] * 5)
        client.post = AsyncMock()  # the token endpoint
        backoffs = []
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client), \
             patch("jacked.api.credential_helpers.cc_rotation_block", return_value=block), \
             caplog.at_level(logging.INFO, logger="jacked.auth"):
            for _ in range(5):
                state["last_fetched_at"] = time.time() - 200
                state["backoff_until"] = 0
                # Every 429 from the third on is due a rotation.
                state["last_429_rotation_at"] = 0.0
                start = time.time()
                assert asyncio.run(fetch_usage(aid, db, access_token="cc_at")) is None
                backoffs.append(state["backoff_until"] - start)

        client.post.assert_not_called()
        assert client.get.call_count == 5
        # The backoff advances exactly as before: escalating, capped at 900 s.
        assert state["consecutive_429s"] == 5
        expected = [min(mod._USAGE_RATE_LIMIT_CEILING * 2 ** (n - 1), 900)
                    for n in range(1, 6)]
        for got, want in zip(backoffs, expected):
            assert want <= got <= want + 5
        lines = [r.getMessage() for r in caplog.records if self.LINE in r.getMessage()]
        assert lines == [f"Account {aid}: {self.LINE}"] * 3
        row = db.get_account(aid)
        assert (row["cc_refresh_token"], row["refresh_token"]) == ("cc_rt", "primary_rt")
        assert row["cc_refresh_failure_type"] is None

    def test_try_refresh_on_429_returns_none_for_the_cc_token(self, tmp_path):
        from jacked.web.database import Database

        db = Database(str(tmp_path / "direct.db"))
        acct = db.create_account("d@test.com", "primary_at", int(time.time()) + 3600,
                                 refresh_token="primary_rt")
        db.update_account(acct["id"], cc_access_token="cc_at", cc_refresh_token="cc_rt")
        flow = AsyncMock()
        allowed = AsyncMock(return_value=True)
        with patch("jacked.web.auth._refresh_token_flow", flow), \
             patch("jacked.web.auth._cc_rotation_allowed", allowed):
            fresh = asyncio.run(mod._try_refresh_on_429(
                acct["id"], db, mod._get_usage_state(acct["id"]), stale_token="cc_at"))
        assert fresh is None
        flow.assert_not_awaited()
        allowed.assert_not_awaited()

    def test_the_primary_token_branch_is_unchanged(self, tmp_path):
        from jacked.web.database import Database

        db = Database(str(tmp_path / "primary.db"))
        acct = db.create_account("p@test.com", "primary_at", int(time.time()) + 3600,
                                 refresh_token="primary_rt")
        db.update_account(acct["id"], cc_access_token="cc_at", cc_refresh_token="cc_rt")
        flow = AsyncMock(return_value=TokenExchangeResult(
            success=True, access_token="primary_at2", chain="primary"))
        with patch("jacked.web.auth._refresh_token_flow", flow):
            fresh = asyncio.run(mod._try_refresh_on_429(
                acct["id"], db, mod._get_usage_state(acct["id"]),
                stale_token="primary_at"))
        assert fresh == "primary_at2"
        assert flow.await_args.args[2] is mod.RefreshMode.PRIMARY_CIRCUIT_BREAKER
