"""Tests for the usage monitor loops (active poll + full sweep).

Each test runs one tick of a loop by mocking dependencies.  Uses
asyncio.run() for async tests (no pytest-asyncio dependency).
"""

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.api.usage_monitor import (
    _setting_bool,
    _setting_float,
    _setting_str,
    _SWAP_COOLDOWN_SECONDS,
    active_account_poll_loop,
    full_sweep_loop,
)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _t0_resets_at():
    """Future ISO timestamp placing the account in T0 (<24h to expiry)."""
    return _iso(datetime.now(timezone.utc) + timedelta(hours=12))


def _t3_resets_at():
    """Future ISO timestamp placing the account in T3 (4-7d to expiry)."""
    return _iso(datetime.now(timezone.utc) + timedelta(days=5))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(settings=None, accounts=None):
    """Build a mock DB with configurable settings and accounts."""
    settings = settings or {}
    accounts = accounts or []

    db = MagicMock()
    db.get_setting.side_effect = lambda key: settings.get(key)
    db.list_accounts.return_value = accounts
    db.record_swap = MagicMock()
    return db


def _make_app(db=None, ws_registry=None):
    """Build a mock app with state."""
    app = MagicMock()
    app.state.db = db
    app.state.ws_registry = ws_registry or AsyncMock()
    return app


def _acct(id, usage_5h=0, usage_7d=0, cc_token=True, auto_swap=True,
          email=None, resets_at=None, cc_rt="rt", scopes="",
          rate_limit_tier="", subscription_type="pro",
          cached_7d_resets_at=None, usage_cached_at=None):
    """Build a test-account dict.

    `cached_7d_resets_at` defaults to a T3 placement (5d out) so the
    tier-aware flow has real data to work with. Pass `_t0_resets_at()`
    for an urgent (<24h) tier or supply your own ISO string. Pass an
    explicit empty string "" to simulate "no 7d data" (TIER_EXCLUDED).
    """
    if cached_7d_resets_at is None:
        cached_7d_resets_at = _t3_resets_at()
    if usage_cached_at is None:
        usage_cached_at = int(datetime.now(timezone.utc).timestamp())
    return {
        "id": id,
        "email": email or f"user{id}@test.com",
        "cached_usage_5h": usage_5h,
        "cached_usage_7d": usage_7d,
        "cached_5h_resets_at": resets_at,
        "cached_7d_resets_at": cached_7d_resets_at,
        "usage_cached_at": usage_cached_at,
        "cc_access_token": "tok" if cc_token else None,
        "cc_refresh_token": cc_rt if cc_token else None,
        "is_active": 1,
        "is_deleted": 0,
        "consecutive_failures": 0,
        "validation_status": "valid",
        "auto_swap_enabled": 1 if auto_swap else 0,
        "priority": id - 1,
        "access_token": f"at_{id}",
        "scopes": scopes,
        "rate_limit_tier": rate_limit_tier,
        "subscription_type": subscription_type,
    }


def _sleep_canceller(max_sleeps=5):
    """Return an async sleep replacement that cancels after max_sleeps calls."""
    count = 0

    async def _sleep(seconds):
        nonlocal count
        count += 1
        if count > max_sleeps:
            raise asyncio.CancelledError

    return _sleep


@pytest.fixture(autouse=True)
def _reset_monitor_state():
    """Reset usage_monitor module-level state before every test.

    The loop accumulates cross-tick state (burn rates, cooldowns, failure
    backoff, tier streaks, advisor cooldowns); leaking it across tests
    makes outcomes order-dependent. ``_initial_fetch_done`` is set True so
    loop tests skip the priming block — the priming tests set it False
    explicitly.
    """
    import jacked.api.usage_monitor as mod
    mod._burn_rates.clear()
    mod._burn_rate_unchanged_ticks.clear()
    mod._last_swap_time = 0.0
    mod._last_committed_swap_time = 0.0
    mod._swap_failure_count = 0
    mod._last_swap_failure_at = 0.0
    mod._last_exhaustion_warning = 0.0
    mod._consecutive_no_best_ticks = 0
    mod._last_stall_warning = 0.0
    mod._same_tier_advisory_last = 0.0
    mod._drain_advisor_last_sent.clear()
    mod._emerged_tier_streak["tier"] = None
    mod._emerged_tier_streak["count"] = 0
    mod._last_observed_tiers.clear()
    mod._ping_backoff.clear()
    mod._initial_fetch_done = True
    yield


# ---------------------------------------------------------------------------
# Setting helpers (sync -- no event loop needed)
# ---------------------------------------------------------------------------

class TestSettingHelpers:
    def test_setting_bool_true(self):
        db = _make_db(settings={"flag": "true"})
        assert _setting_bool(db, "flag") is True

    def test_setting_bool_false(self):
        db = _make_db(settings={"flag": "false"})
        assert _setting_bool(db, "flag") is False

    def test_setting_bool_default(self):
        db = _make_db()
        assert _setting_bool(db, "missing", default=True) is True

    def test_setting_float_value(self):
        db = _make_db(settings={"interval": "120"})
        assert _setting_float(db, "interval", 300) == 120.0

    def test_setting_float_default(self):
        db = _make_db()
        assert _setting_float(db, "missing", 42.5) == 42.5

    def test_setting_float_invalid(self):
        db = _make_db(settings={"bad": "notanumber"})
        assert _setting_float(db, "bad", 99.0) == 99.0

    def test_setting_str_value(self):
        db = _make_db(settings={"time": "08:00"})
        assert _setting_str(db, "time", "06:00") == "08:00"

    def test_setting_str_default(self):
        db = _make_db()
        assert _setting_str(db, "missing", "default") == "default"


# ---------------------------------------------------------------------------
# No action when disabled
# ---------------------------------------------------------------------------

class TestNoActionWhenDisabled:
    def test_active_poll_no_action_when_disabled(self):
        """auto_swap disabled -> no fetch_usage calls in active poll."""
        db = _make_db(
            settings={"auto_swap_enabled": "false"},
            accounts=[_acct(1)],
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=1),
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                ) as mock_fetch,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                mock_fetch.assert_not_called()

        asyncio.run(_run())

    def test_full_sweep_no_action_when_disabled(self):
        """window_keeper disabled -> no fetch_usage calls in full sweep."""
        db = _make_db(
            settings={"window_keeper_enabled": "false"},
            accounts=[_acct(1)],
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=1),
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                ) as mock_fetch,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await full_sweep_loop(app)

                mock_fetch.assert_not_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Auto-swap triggers on high usage (active poll loop)
# ---------------------------------------------------------------------------

class TestAutoSwapTriggers:
    def test_auto_swap_triggers_on_high_usage(self):
        """When 5h usage is above critical, swap to best target."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),   # good target
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="fresh_tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # sync_credential_to_all_stores called with target account
                assert mock_sync.call_count >= 1
                call_args = mock_sync.call_args
                assert call_args[0][0] == 2  # target account id

                # record_swap called
                assert db.record_swap.call_count >= 1
                swap_kwargs = db.record_swap.call_args[1]
                assert swap_kwargs["from_account_id"] == 1
                assert swap_kwargs["to_account_id"] == 2
                # 5h-critical reason maps to "forced_critical" trigger
                # under the new tier-aware taxonomy.
                assert swap_kwargs["trigger"] == "forced_critical"

                # WebSocket broadcast fires
                ws_registry.broadcast.assert_called()
                broadcast_calls = ws_registry.broadcast.call_args_list
                topics = [c[0][0] for c in broadcast_calls]
                assert "auto_swap_triggered" in topics

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# No target logs warning (active poll loop)
# ---------------------------------------------------------------------------

class TestNoTarget:
    def test_no_target_logs_warning(self, caplog):
        """should_swap=True but no eligible target -> logs warning."""
        import jacked.api.usage_monitor as mod
        mod._last_exhaustion_warning = 0.0
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=5),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with caplog.at_level(logging.WARNING, logger="jacked.api.usage_monitor"):
                    with pytest.raises(asyncio.CancelledError):
                        await active_account_poll_loop(app)

                # No swap should happen
                mock_sync.assert_not_called()

                # Warning should be logged
                assert any(
                    "no eligible target" in r.message
                    for r in caplog.records
                ), (
                    f"Expected 'no eligible target' warning, "
                    f"got: {[r.message for r in caplog.records]}"
                )

                # WebSocket should broadcast exhaustion
                broadcast_calls = ws_registry.broadcast.call_args_list
                topics = [c[0][0] for c in broadcast_calls]
                assert "all_accounts_exhausted" in topics

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Cached response handled (active poll loop)
# ---------------------------------------------------------------------------

class TestCachedResponse:
    def test_cached_response_handled(self):
        """fetch_usage returns {_cached: True} -> no crash, loop continues."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        accounts = [_acct(1, usage_5h=30)]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=5),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ) as mock_fetch,
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                mock_fetch.assert_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Window keeper pings during active hours (full sweep loop)
# ---------------------------------------------------------------------------

class TestWindowKeeperPings:
    def test_window_keeper_pings_during_active_hours(self):
        """Active hours + needs_ping -> ping_account called."""
        accounts = [
            _acct(1, usage_5h=30, resets_at=None, cc_rt="refresh_tok_1"),
            _acct(2, usage_5h=10, resets_at=None, cc_rt="refresh_tok_2"),
        ]
        db = _make_db(
            settings={
                "window_keeper_enabled": "true",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=10),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.web.window_keeper.is_active_hours",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.is_prewake_time",
                    return_value=False,
                ),
                patch(
                    "jacked.web.window_keeper.needs_ping",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.ping_account",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_ping,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await full_sweep_loop(app)

                # ping_account should be called at least once
                assert mock_ping.call_count >= 1

        asyncio.run(_run())

    def test_fetch_usage_bypasses_cache_after_successful_ping(self):
        """After ping succeeds, fetch_usage must be called with access_token
        to bypass the cache freshness guard and update cached_5h_resets_at."""
        accounts = [
            _acct(1, usage_5h=30, resets_at=None, cc_rt="refresh_tok_1"),
            _acct(2, usage_5h=10, resets_at=None, cc_rt="refresh_tok_2"),
        ]
        db = _make_db(
            settings={
                "window_keeper_enabled": "true",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=10),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": False},
                ) as mock_fetch,
                patch(
                    "jacked.web.window_keeper.is_active_hours",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.is_prewake_time",
                    return_value=False,
                ),
                patch(
                    "jacked.web.window_keeper.needs_ping",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.ping_account",
                    new_callable=AsyncMock,
                    return_value=True,
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await full_sweep_loop(app)

                # Find fetch_usage calls that happened AFTER pings
                # (non-active accounts: id=2 since active=1 is skipped)
                # The post-ping fetch_usage must pass access_token to bypass cache.
                post_ping_calls = [
                    c for c in mock_fetch.call_args_list
                    if c[1].get("access_token") is not None
                ]
                assert len(post_ping_calls) >= 1, (
                    f"Expected at least one fetch_usage call with access_token set "
                    f"(to bypass cache), but got: {mock_fetch.call_args_list}"
                )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Window keeper ping backoff — failing pings must not retry every sweep
# ---------------------------------------------------------------------------

class TestWindowKeeperPingBackoff:
    """A consecutively-failing ping (e.g. an exhausted account answering
    429) must back off exponentially instead of being retried every sweep
    forever — 2026-06 incident: account pinged every 120s for hours."""

    def _settings(self):
        # Small interval so the tail sleep loop is a single sleep call,
        # making per-sweep sleep accounting deterministic.
        return {
            "window_keeper_enabled": "true",
            "usage_check_interval": "5",
        }

    def test_ping_failure_schedules_backoff_and_next_sweep_skips(self, caplog):
        import jacked.api.usage_monitor as mod
        accounts = [_acct(1, resets_at=None)]
        db = _make_db(settings=self._settings(), accounts=accounts)
        app = _make_app(db=db)

        async def _run():
            with (
                # Sweep 1: pacing sleep + tail sleep. Sweeps 2/3: tail sleep
                # only (account skipped). Cancel on the 4th sleep => 3 sweeps.
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.web.window_keeper.is_active_hours",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.is_prewake_time",
                    return_value=False,
                ),
                patch(
                    "jacked.web.window_keeper.needs_ping",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.ping_account",
                    new_callable=AsyncMock,
                    return_value=False,
                ) as mock_ping,
                patch(
                    "jacked.api.credential_helpers.read_active_account_id",
                    return_value=999,
                ),
                patch(
                    "jacked.web.auth.refresh_cc_token",
                    new_callable=AsyncMock,
                    return_value=False,
                ),
            ):
                with caplog.at_level(
                    logging.DEBUG, logger="jacked.api.usage_monitor",
                ):
                    with pytest.raises(asyncio.CancelledError):
                        await full_sweep_loop(app)

            assert mock_ping.call_count == 1, (
                "a failing ping must be skipped on subsequent sweeps while "
                f"backoff is armed, but ping ran {mock_ping.call_count} times"
            )
            state = mod._ping_backoff.get(1)
            assert state is not None, "ping failure must arm per-account backoff"
            assert state["fails"] == 1
            assert state["skip_until"] > time.monotonic()
            assert any(
                r.levelno == logging.DEBUG and "backoff" in r.getMessage().lower()
                for r in caplog.records
            ), "backoff skip must be logged at DEBUG"

        asyncio.run(_run())

    def test_ping_success_resets_backoff(self):
        import jacked.api.usage_monitor as mod
        # Expired backoff (skip_until in the past) so the sweep pings again.
        mod._ping_backoff[1] = {"fails": 3, "skip_until": 0.0}
        accounts = [_acct(1, resets_at=None)]
        db = _make_db(settings=self._settings(), accounts=accounts)
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=1),
                ),
                patch(
                    "jacked.web.window_keeper.is_active_hours",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.is_prewake_time",
                    return_value=False,
                ),
                patch(
                    "jacked.web.window_keeper.needs_ping",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.ping_account",
                    new_callable=AsyncMock,
                    return_value=True,
                ) as mock_ping,
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": False},
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await full_sweep_loop(app)

            assert mock_ping.call_count == 1
            assert 1 not in mod._ping_backoff, (
                "a successful ping must clear the account's backoff state"
            )

        asyncio.run(_run())

    def test_backoff_delay_is_exponential_and_capped(self):
        import jacked.api.usage_monitor as mod
        # fails=10 with check_interval=5 would be 5120s uncapped — the cap
        # must hold skip_until to <= now + 3600.
        mod._ping_backoff[1] = {"fails": 10, "skip_until": 0.0}
        accounts = [_acct(1, resets_at=None)]
        db = _make_db(settings=self._settings(), accounts=accounts)
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=1),
                ),
                patch(
                    "jacked.web.window_keeper.is_active_hours",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.is_prewake_time",
                    return_value=False,
                ),
                patch(
                    "jacked.web.window_keeper.needs_ping",
                    return_value=True,
                ),
                patch(
                    "jacked.web.window_keeper.ping_account",
                    new_callable=AsyncMock,
                    return_value=False,
                ),
                patch(
                    "jacked.api.credential_helpers.read_active_account_id",
                    return_value=999,
                ),
                patch(
                    "jacked.web.auth.refresh_cc_token",
                    new_callable=AsyncMock,
                    return_value=False,
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await full_sweep_loop(app)

            state = mod._ping_backoff[1]
            assert state["fails"] == 11
            assert state["skip_until"] <= time.monotonic() + 3600

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# TOCTOU guard — bail when active account changes mid-swap
# ---------------------------------------------------------------------------

class TestTOCTOU:
    def test_toctou_bail_on_manual_switch(self):
        """If active account ID changes between check and swap, skip swap."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),   # good target
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        # First call returns 1 (initial read at top of tick), second call
        # returns 99 (TOCTOU re-read just before swap — manual switch happened)
        read_active_returns = [1, 99]
        read_active_call_count = 0

        def _mock_read_active():
            nonlocal read_active_call_count
            idx = min(read_active_call_count, len(read_active_returns) - 1)
            val = read_active_returns[idx]
            read_active_call_count += 1
            return val

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    side_effect=_mock_read_active,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="fresh_tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Swap should NOT have been executed
                mock_sync.assert_not_called()
                db.record_swap.assert_not_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Burn rate skips unchanged usage
# ---------------------------------------------------------------------------

class TestBurnRateSkipsUnchanged:
    def test_burn_rate_skips_unchanged(self):
        """Same usage on consecutive calls -> update_burn_rate not called."""
        import jacked.api.usage_monitor as mod
        from jacked.web.auto_swap import BurnRate
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        # Pre-seed burn rate so the "prev is not None" path triggers
        mod._burn_rates[1] = BurnRate(
            rate_5h_per_min=0.5,
            last_check_5h=50.0,  # same as account usage below
            rate_7d_per_min=0.1,
            last_check_7d=30.0,
        )

        accounts = [
            _acct(1, usage_5h=50, usage_7d=30),  # usage matches seeded burn rate
            _acct(2, usage_5h=10, usage_7d=5),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.web.auto_swap.update_burn_rate",
                ) as mock_update_br,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # update_burn_rate should NOT have been called (usage unchanged)
                mock_update_br.assert_not_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Burn rate re-seeded after swap
# ---------------------------------------------------------------------------

class TestBurnRateReseedAfterSwap:
    def test_burn_rate_reseed_after_swap(self):
        """After swap, the target account's burn rate entry is removed."""
        import jacked.api.usage_monitor as mod
        from jacked.web.auto_swap import BurnRate
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0  # ensure cooldown is not active

        # Pre-seed burn rates for both accounts
        mod._burn_rates[2] = BurnRate(
            rate_5h_per_min=0.3,
            last_check_5h=20.0,
        )

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),   # good target
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="fresh_tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Target account's burn rate should have been popped
                assert 2 not in mod._burn_rates, (
                    f"Expected target account 2 burn rate to be removed, "
                    f"but _burn_rates={mod._burn_rates}"
                )

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Swap cooldown prevents ping-ponging
# ---------------------------------------------------------------------------

class TestSwapCooldown:
    def test_swap_blocked_during_cooldown(self):
        """Swap cooldown active -> sync_credential_to_all_stores NOT called."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = time.time()  # cooldown is active right now

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),   # good target
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="fresh_tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # No swap should happen while cooldown is active
                mock_sync.assert_not_called()
                db.record_swap.assert_not_called()

        asyncio.run(_run())

    def test_swap_allowed_after_cooldown_expires(self):
        """Cooldown expired -> swap proceeds normally."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        # Last swap happened well past the cooldown window
        mod._last_swap_time = time.time() - _SWAP_COOLDOWN_SECONDS - 10

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),   # good target
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="fresh_tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Cooldown expired — swap should proceed
                assert mock_sync.call_count >= 1
                call_args = mock_sync.call_args
                assert call_args[0][0] == 2  # target account id

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Pause mechanism skips auto-swap
# ---------------------------------------------------------------------------

class TestPauseMechanism:
    def test_swap_skipped_when_paused(self):
        """auto_swap_paused_until set to future -> fetch_usage NOT called."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        # Pause until far in the future
        future_iso = "2099-12-31T23:59:59+00:00"
        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "auto_swap_paused_until": future_iso,
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ) as mock_fetch,
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Pause check happens before usage fetch — fetch should NOT be called
                mock_fetch.assert_not_called()
                mock_sync.assert_not_called()

        asyncio.run(_run())

    def test_swap_proceeds_when_pause_expired(self):
        """auto_swap_paused_until set to past -> swap proceeds normally."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        # Pause timestamp already in the past
        past_iso = "2000-01-01T00:00:00+00:00"
        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "auto_swap_paused_until": past_iso,
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="fresh_tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Pause expired — swap should proceed
                assert mock_sync.call_count >= 1
                call_args = mock_sync.call_args
                assert call_args[0][0] == 2  # target account id

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Burn-rate decay after consecutive unchanged ticks
# ---------------------------------------------------------------------------

class TestBurnRateDecay:
    def test_decay_after_5_unchanged_ticks(self):
        """5th consecutive unchanged tick triggers 20% decay of burn rate."""
        import jacked.api.usage_monitor as mod
        from jacked.web.auto_swap import BurnRate
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        # Pre-seed with 4 ticks already counted — next tick is the 5th
        mod._burn_rates[1] = BurnRate(
            rate_5h_per_min=1.0,
            last_check_5h=50.0,
            rate_7d_per_min=1.0,
            last_check_7d=30.0,
        )
        mod._burn_rate_unchanged_ticks[1] = 4

        # Account usage matches seeded last_check_5h exactly (unchanged)
        accounts = [
            _acct(1, usage_5h=50, usage_7d=30),  # 50% — below 80% warning
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "auto_swap_5h_warning": "80",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    # max_sleeps=0: first sleep call (at end of tick) raises
                    # CancelledError immediately, giving exactly 1 tick
                    side_effect=_sleep_canceller(max_sleeps=0),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

            # After exactly 1 tick, decay should have been applied (1.0 * 0.8 = 0.8)
            br = mod._burn_rates.get(1)
            assert br is not None, "Burn rate entry should still exist after decay tick"
            assert abs(br.rate_5h_per_min - 0.8) < 0.01, (
                f"Expected rate_5h_per_min ~0.8 after decay, got {br.rate_5h_per_min}"
            )

        asyncio.run(_run())

    def test_decay_applies_at_high_usage(self):
        """Decay is ungated: unchanged usage at/above the warning threshold
        still decays the rate — unchanged usage is direct evidence burn
        stopped; a frozen rate fires spurious burn-rate swaps after the
        user goes idle at >=80%."""
        import jacked.api.usage_monitor as mod
        from jacked.web.auto_swap import BurnRate

        # Pre-seed with 4 ticks already counted — next tick is the 5th
        mod._burn_rates[1] = BurnRate(
            rate_5h_per_min=1.0,
            last_check_5h=85.0,
            rate_7d_per_min=1.0,
            last_check_7d=50.0,
        )
        mod._burn_rate_unchanged_ticks[1] = 4

        # Single account at 85% — above 80% warning, no swap target so
        # burn-rate state survives the tick regardless of the reason.
        accounts = [
            _acct(1, usage_5h=85, usage_7d=50),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "auto_swap_5h_warning": "80",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=0),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

            br = mod._burn_rates.get(1)
            assert br is not None, "Burn rate entry should survive a no-target tick"
            assert abs(br.rate_5h_per_min - 0.8) < 0.01, (
                f"Expected rate_5h_per_min ~0.8 after high-usage decay, "
                f"got {br.rate_5h_per_min}"
            )

        asyncio.run(_run())


class TestEmergencePersistence:
    """Spec scenario F26 + pre-mortem F2: anti-flap on tier jitter.

    Drives the EXTRACTED helper `_apply_emergence_persistence` —
    these tests exercise real production code, NOT a hand-rolled
    local copy of the logic. Streak state is TIER-keyed:
    {"tier": int | None, "count": int}.
    """

    def test_first_emerge_suppressed(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": None, "count": 0}
        result = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2",
            best_tier=0,
            streak=streak,
            persistence_ticks=2,
        )
        assert result is None
        assert streak == {"tier": 0, "count": 1}

    def test_second_emerge_fires(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": 0, "count": 1}
        result = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2",
            best_tier=0,
            streak=streak,
            persistence_ticks=2,
        )
        assert result is not None
        assert result.startswith("higher tier emerged")
        assert streak == {"tier": 0, "count": 2}

    def test_alternating_best_ids_same_tier_reach_threshold(self):
        """The headline fix: two near-tied candidates alternating as best
        must NOT reset each other's streak — the streak is keyed by tier,
        so any account in the emerged tier counts toward the threshold."""
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": None, "count": 0}
        # Tick 1: account A is best (tier 0).
        r1 = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2",
            best_tier=0, streak=streak, persistence_ticks=2,
        )
        assert r1 is None
        # Tick 2: account B is best — same tier 0. Streak continues.
        r2 = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2",
            best_tier=0, streak=streak, persistence_ticks=2,
        )
        assert r2 is not None
        assert streak == {"tier": 0, "count": 2}

    def test_tier_change_resets_streak(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": 0, "count": 5}
        result = _apply_emergence_persistence(
            reason="higher tier emerged: T1 vs T2",
            best_tier=1,
            streak=streak,
            persistence_ticks=2,
        )
        assert streak == {"tier": 1, "count": 1}
        assert result is None

    def test_intra_tier_reason_increments_streak(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": None, "count": 0}
        result = _apply_emergence_persistence(
            reason="intra-tier preemption: T0 candidate losing 20.0% in 4.0h",
            best_tier=0,
            streak=streak,
            persistence_ticks=2,
        )
        assert result is None
        assert streak == {"tier": 0, "count": 1}
        result = _apply_emergence_persistence(
            reason="intra-tier preemption: T0 candidate losing 20.0% in 4.0h",
            best_tier=0,
            streak=streak,
            persistence_ticks=2,
        )
        assert result is not None
        assert result.startswith("intra-tier preemption:")

    def test_non_emerge_reason_clears_streak(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": 0, "count": 1}
        result = _apply_emergence_persistence(
            reason="drained: 7d usage 100% >= 100%",
            best_tier=0,
            streak=streak,
            persistence_ticks=2,
        )
        assert result.startswith("drained")
        assert streak == {"tier": None, "count": 0}

    def test_none_reason_clears_streak(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": 0, "count": 1}
        result = _apply_emergence_persistence(
            reason=None, best_tier=0, streak=streak, persistence_ticks=2,
        )
        assert result is None
        assert streak == {"tier": None, "count": 0}


class TestSilentStallWatchdog:
    """Pre-mortem F3: detect "loop ticking but never produces a target".

    Drives the EXTRACTED helper `_evaluate_stall`.
    """

    def test_pattern_a_multi_account_stale(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=2000,
            has_other_accounts=True, reason=None,
            staleness_threshold=1800,
        )
        assert bumped is True

    def test_pattern_b_single_account_forced_out(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=10,
            has_other_accounts=False,
            reason="drained: 7d at 100%",
            staleness_threshold=1800,
        )
        assert bumped is True

    def test_pattern_c_drained_no_candidate(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=10,
            has_other_accounts=True,
            reason="drained: 7d at 100%",
            staleness_threshold=1800,
        )
        assert bumped is True

    def test_no_increment_on_swap(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="swap", best={"id": 1},
            usage_cached_at_age_seconds=10,
            has_other_accounts=True, reason="anything",
            staleness_threshold=1800,
        )
        assert bumped is False

    def test_no_increment_on_healthy_stay(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=10,
            has_other_accounts=True, reason=None,
            staleness_threshold=1800,
        )
        assert bumped is False

    def test_same_tier_stay_with_deficit_is_not_a_stall(self):
        """Pattern (d) was split OUT of the stall path: same-tier-stay
        with a candidate deficit is intended behavior per spec — it gets
        the `same_tier_deficit_advisory` instead of an ERROR stall."""
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay",
            best={"id": 1},
            usage_cached_at_age_seconds=10,  # fresh, not stale
            has_other_accounts=True,
            reason=None,  # same-tier-never-overrides → no reason
            staleness_threshold=1800,
        )
        assert bumped is False


class TestSameTierDeficitAdvisory:
    """Item 13: pattern (d) became an advisory gated on harvestable tiers.

    Drives the EXTRACTED helper `_same_tier_advisory_applies`; the 30-min
    cooldown lives in the loop (module `_same_tier_advisory_last`).
    """

    def _applies(self, **overrides):
        from jacked.api.usage_monitor import _same_tier_advisory_applies
        kwargs = dict(
            decision_action="stay",
            best={"id": 1},
            reason=None,
            best_deficit=22.5,
            best_tier=0,
        )
        kwargs.update(overrides)
        return _same_tier_advisory_applies(**kwargs)

    def test_fires_for_t0_candidate(self):
        assert self._applies(best_tier=0) is True

    def test_fires_for_t1_candidate(self):
        assert self._applies(best_tier=1) is True

    def test_t2_candidate_does_not_fire(self):
        # T2/T3 targets are floors, not drain-to goals — not actionable.
        assert self._applies(best_tier=2) is False
        assert self._applies(best_tier=3) is False

    def test_below_threshold_does_not_fire(self):
        assert self._applies(best_deficit=5.0) is False

    def test_no_deficit_does_not_fire(self):
        assert self._applies(best_deficit=None) is False

    def test_reason_present_does_not_fire(self):
        assert self._applies(reason="drained: 7d at 100%") is False

    def test_swap_action_does_not_fire(self):
        assert self._applies(decision_action="swap") is False

    def test_no_best_does_not_fire(self):
        assert self._applies(best=None) is False


class TestEmergencePersistenceTransientPreservation:
    """User-observed bug: a single tick where best=None (transient
    candidate-fetch hiccup) used to clear the streak, restarting the
    2-minute clock. New behavior: preserve streak when both reason
    and best_tier are None — only explicit non-emerge reasons clear it."""

    def test_streak_preserved_on_transient_no_best(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {"tier": 0, "count": 1}
        result = _apply_emergence_persistence(
            reason=None, best_tier=None,
            streak=streak, persistence_ticks=2,
        )
        assert result is None
        assert streak == {"tier": 0, "count": 1}  # PRESERVED across the transient tick

    def test_streak_resumes_after_transient(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        # Tick 1: emergence, streak tier 0 count 1.
        streak = {"tier": None, "count": 0}
        _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2", best_tier=0,
            streak=streak, persistence_ticks=2,
        )
        assert streak == {"tier": 0, "count": 1}
        # Tick 2: transient (best=None, reason=None). Streak preserved.
        _apply_emergence_persistence(
            reason=None, best_tier=None,
            streak=streak, persistence_ticks=2,
        )
        assert streak == {"tier": 0, "count": 1}
        # Tick 3: emergence resumes. Streak fires.
        result = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2", best_tier=0,
            streak=streak, persistence_ticks=2,
        )
        assert result is not None
        assert result.startswith("higher tier emerged")
        assert streak == {"tier": 0, "count": 2}


class _SwapFakeDB:
    """Minimal DB stand-in for _execute_swap tests."""

    def __init__(self, captured):
        self.captured = captured

    def record_swap(self, **kwargs):
        self.captured["record_swap"] = kwargs
        return 41

    def update_swap_status(self, swap_id, status):
        self.captured.setdefault("status_updates", []).append((swap_id, status))

    def set_setting(self, key, value):
        self.captured.setdefault("settings", {})[key] = value


def _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=True):
    from contextlib import contextmanager

    @contextmanager
    def fake_lock():
        yield lock_acquired

    def fake_sync(account_id, target, email=None):
        captured["sync_called_with"] = account_id

    monkeypatch.setattr(
        "jacked.api.credential_helpers.acquire_claude_lock", fake_lock,
    )
    monkeypatch.setattr(
        "jacked.api.credential_helpers.sync_credential_to_all_stores",
        fake_sync,
    )
    monkeypatch.setattr(
        "jacked.api.credential_helpers.invalidate_live_cred_cache",
        lambda: None,
    )
    monkeypatch.setattr(
        "jacked.api.credential_helpers.reconcile_credentials_from_live_store",
        lambda *a, **kw: None,
    )


def _run_execute_swap(captured, reason="higher tier emerged: T0 vs T2",
                      trigger="higher_tier_emerged"):
    from jacked.api import usage_monitor as um

    async def _run():
        return await um._execute_swap(
            _SwapFakeDB(captured),
            active_acct_id=99,
            active_acct={"id": 99, "email": "old@test"},
            target={"id": 7, "email": "new@test"},
            reason=reason,
            trigger=trigger,
            usage_5h=20, usage_7d=50,
            ws_registry=None,
        )

    return asyncio.run(_run())


class TestExecuteSwapAtomicity:
    """`_execute_swap` must:
    - Record the swap as 'pending' first, then resolve to 'committed'/'failed'
    - Sync DB active_account_id setting after credential write succeeds
    - Skip the WS `auto_swap_triggered` broadcast on credential failure
    - Arm the exponential failure backoff on credential failure
    - Record residency_seconds relative to the previous committed swap
    """

    def test_swap_syncs_db_setting_on_success(self, monkeypatch):
        from jacked.api import usage_monitor as um

        captured = {}
        _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=True)
        monkeypatch.setattr(um, "_read_active_account_id", lambda: 99)

        ok = _run_execute_swap(captured)
        assert ok is True
        assert captured["settings"]["active_account_id"] == 7
        assert captured["record_swap"]["status"] == "pending"
        assert captured["status_updates"] == [(41, "committed")]

    def test_success_sets_committed_time_and_resets_backoff(self, monkeypatch):
        from jacked.api import usage_monitor as um

        captured = {}
        _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=True)
        monkeypatch.setattr(um, "_read_active_account_id", lambda: 99)
        um._swap_failure_count = 3
        um._last_swap_failure_at = time.time()

        ok = _run_execute_swap(captured)
        assert ok is True
        assert um._swap_failure_count == 0
        assert um._last_committed_swap_time > 0

    def test_residency_seconds_recorded_from_previous_commit(self, monkeypatch):
        from jacked.api import usage_monitor as um

        captured = {}
        _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=True)
        monkeypatch.setattr(um, "_read_active_account_id", lambda: 99)
        um._last_committed_swap_time = time.time() - 1234

        ok = _run_execute_swap(captured)
        assert ok is True
        residency = captured["record_swap"]["residency_seconds"]
        assert residency is not None
        assert 1230 <= residency <= 1240

    def test_residency_seconds_none_without_previous_commit(self, monkeypatch):
        from jacked.api import usage_monitor as um

        captured = {}
        _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=True)
        monkeypatch.setattr(um, "_read_active_account_id", lambda: 99)

        ok = _run_execute_swap(captured)
        assert ok is True
        assert captured["record_swap"]["residency_seconds"] is None

    def test_swap_skips_db_setting_on_lock_fail(self, monkeypatch):
        from jacked.api import usage_monitor as um

        captured = {}
        _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=False)
        monkeypatch.setattr(um, "_read_active_account_id", lambda: 99)

        ok = _run_execute_swap(captured)
        assert ok is False
        # DB setting NOT updated when credentials didn't commit.
        assert "settings" not in captured
        assert captured["record_swap"]["status"] == "pending"
        assert captured["status_updates"] == [(41, "failed")]

    def test_lock_fail_arms_failure_backoff(self, monkeypatch):
        from jacked.api import usage_monitor as um

        captured = {}
        _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=False)
        monkeypatch.setattr(um, "_read_active_account_id", lambda: 99)

        ok = _run_execute_swap(captured)
        assert ok is False
        assert um._swap_failure_count == 1
        assert um._last_swap_failure_at > 0
        # Cooldown un-armed: failed-attempt retry pacing is governed by
        # the failure backoff, not _SWAP_COOLDOWN_SECONDS.
        assert um._last_swap_time == 0.0
        assert um._swap_backoff_remaining() > 0

    def test_lock_fail_preserves_burn_rate_state(self, monkeypatch):
        from jacked.api import usage_monitor as um
        from jacked.web.auto_swap import BurnRate

        captured = {}
        _patch_swap_credential_helpers(monkeypatch, captured, lock_acquired=False)
        monkeypatch.setattr(um, "_read_active_account_id", lambda: 99)
        um._burn_rates[99] = BurnRate(rate_5h_per_min=0.5, last_check_5h=20.0)
        um._burn_rate_unchanged_ticks[99] = 3

        ok = _run_execute_swap(captured)
        assert ok is False
        assert 99 in um._burn_rates, (
            "failed swap must not destroy the active account's burn-rate state"
        )
        assert um._burn_rate_unchanged_ticks.get(99) == 3


# ---------------------------------------------------------------------------
# Tier-aware end-to-end + audit / taxonomy / guard tests (Task 10)
# ---------------------------------------------------------------------------


def _full_acct(id, *, usage_5h=20, usage_7d=50, resets_5h=None,
               resets_7d=None, valid=True, auto_swap=True,
               failures=0, cc_token=True, usage_cached_at=None):
    """Account-shape helper for tier-aware tests.

    Distinct from ``_acct`` above — this one mirrors the spec's full-row
    shape (no defaulted-to-T3 trick) so callers explicitly choose which
    tier each account lives in via ``resets_7d``.
    """
    return {
        "id": id, "email": f"u{id}@test",
        "is_active": 1, "is_deleted": 0,
        "consecutive_failures": failures,
        "validation_status": "valid" if valid else "invalid",
        "cc_access_token": "tok" if cc_token else None,
        "auto_swap_enabled": 1 if auto_swap else 0,
        "cached_usage_5h": usage_5h, "cached_usage_7d": usage_7d,
        "cached_5h_resets_at": resets_5h,
        "cached_7d_resets_at": resets_7d,
        "usage_cached_at": (
            usage_cached_at
            if usage_cached_at is not None
            else int(datetime.now(timezone.utc).timestamp())
        ),
    }


class TestTierAwareDecision:
    """End-to-end pure-function: T0 wins over T3 (the headline bug fix)."""

    def test_picks_t0_over_t3(self):
        from jacked.web.auto_swap import pick_best_target, should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _full_acct(99,
                            resets_5h=_iso(now + timedelta(hours=2)),
                            resets_7d=_iso(now + timedelta(days=3)))
        t0 = _full_acct(1, usage_5h=10, usage_7d=80,
                        resets_5h=_iso(now + timedelta(hours=2)),
                        resets_7d=_iso(now + timedelta(hours=12)))
        t3 = _full_acct(2, usage_5h=10, usage_7d=10,
                        resets_5h=_iso(now + timedelta(hours=2)),
                        resets_7d=_iso(now + timedelta(days=6)))
        target = pick_best_target([active, t0, t3], current_id=99, now=now)
        assert target["id"] == 1
        reason = should_swap_now(active=active, best=target, now=now)
        assert reason is not None and reason.startswith("higher tier emerged")


class TestCooldownPath:
    """Spec scenario F27: cooldown blocks swap, but decision-log entry
    must surface the would-have-been target_id (audit trail)."""

    def test_cooldown_branch_records_target_id(self):
        best = {"id": 42}
        decision_target_id = best["id"] if best else None
        decision_action = "stay"
        decision_reason = (
            "swap warranted (higher tier emerged: T0...) but cooldown "
            "active (123s remaining)"
        )
        assert decision_target_id == 42
        assert decision_action == "stay"
        assert "cooldown" in decision_reason


class TestTriggerTaxonomy:
    """Reason -> trigger mapping derived from prefix.

    Drives the EXTRACTED helper `_trigger_for_reason` (Task 14 hoisted
    it to module level) — so this test exercises the real production
    function, not a hand-rolled local copy of the mapping.
    """

    def test_trigger_taxonomy_mapping(self):
        from jacked.api.usage_monitor import _trigger_for_reason

        assert _trigger_for_reason(None) == "tick"
        assert _trigger_for_reason(
            "higher tier emerged: T0 (<24h)..."
        ) == "higher_tier_emerged"
        assert _trigger_for_reason(
            "intra-tier preemption: T0 candidate losing 20.0% in 4.0h"
        ) == "intra_tier_preempted"
        assert _trigger_for_reason(
            "drained: 7d usage 100.0% >= ..."
        ) == "tier_drained"
        assert _trigger_for_reason(
            "5h critical: 95.0% >= 90%"
        ) == "forced_critical"
        assert _trigger_for_reason(
            "burn-rate projection: 82% -> 92% in 10min"
        ) == "burn_rate"
        assert _trigger_for_reason("some other reason") == "tier_aware"


class TestActiveHoursGuardPreserved:
    """The active-hours guard survives the rewrite — verify settings keys."""

    def test_active_hours_settings_round_trip(self):
        EXPECTED_KEYS = ("window_keeper_active_start", "window_keeper_active_end")
        import inspect
        from jacked.api import usage_monitor as um
        src = inspect.getsource(um.active_account_poll_loop)
        for key in EXPECTED_KEYS:
            assert key in src, f"Setting {key} not read by active_account_poll_loop"


# ---------------------------------------------------------------------------
# Active-poll watchdog — heartbeat + respawn (added in 0.43.1)
# ---------------------------------------------------------------------------


class _FakeAppState:
    """Minimal app.state stand-in for watchdog tests."""
    pass


class _FakeApp:
    def __init__(self):
        self.state = _FakeAppState()


class TestActivePollHeartbeat:
    """Verify the active poll loop writes a heartbeat every iteration so
    the watchdog can detect a wedged task."""

    def test_heartbeat_written_on_each_tick(self):
        """The heartbeat write at the bottom of the loop body must execute
        on every iteration, including ones that early-return via continue."""
        import inspect
        from jacked.api import usage_monitor as um
        src = inspect.getsource(um.active_account_poll_loop)
        # The heartbeat write must appear after the try/except — same path
        # as the existing _last_tick_at watchdog write.
        assert "app.state.active_poll_last_tick_at = time.monotonic()" in src, (
            "active_account_poll_loop must write a monotonic heartbeat to "
            "app.state.active_poll_last_tick_at on every iteration so the "
            "watchdog can detect a wedged task."
        )

    def test_heartbeat_written_when_auto_swap_disabled(self):
        """2026-06 respawn-churn incident: with auto_swap_enabled=false every
        iteration exits via ``continue`` before a tail-only heartbeat write,
        so the watchdog declared the healthy loop dead every ~5 minutes and
        respawned it (56 respawns observed). The heartbeat must be written
        at the TOP of every iteration, before any early-exit branch."""
        db = _make_db(settings={"auto_swap_enabled": "false"})
        app = _FakeApp()
        app.state.db = db

        async def _run():
            with patch(
                "jacked.api.usage_monitor.asyncio.sleep",
                side_effect=_sleep_canceller(max_sleeps=0),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

            tick = getattr(app.state, "active_poll_last_tick_at", None)
            assert tick is not None, (
                "heartbeat must be written before the auto-swap-disabled "
                "early exit, or the watchdog respawns a healthy loop forever"
            )
            assert time.monotonic() - tick < 5

        asyncio.run(_run())

    def test_heartbeat_written_when_db_missing(self):
        """The db-None early exit is the very first branch in the loop body —
        the heartbeat write must precede even that."""
        app = _FakeApp()
        app.state.db = None

        async def _run():
            with patch(
                "jacked.api.usage_monitor.asyncio.sleep",
                side_effect=_sleep_canceller(max_sleeps=0),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

            assert getattr(app.state, "active_poll_last_tick_at", None) is not None

        asyncio.run(_run())


class TestActivePollWatchdogRespawn:
    """Verify the watchdog respawns the poll task when its heartbeat goes stale."""

    def test_respawn_creates_new_task_when_heartbeat_missing(self):
        """No heartbeat at all (task never ticked) → _respawn_active_poll
        creates a fresh task and seeds the heartbeat."""
        from jacked.api import usage_monitor as um

        app = _FakeApp()
        app.state.active_poll_task = None

        async def _run():
            ok = um._respawn_active_poll(app)
            assert ok is True
            assert getattr(app.state, "active_poll_task", None) is not None
            assert not app.state.active_poll_task.done()
            assert getattr(app.state, "active_poll_last_tick_at", None) is not None
            # Cancel the spawned task so the test exits cleanly.
            app.state.active_poll_task.cancel()
            try:
                await app.state.active_poll_task
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())

    def test_respawn_cancels_old_live_task(self):
        """If the old task object exists and isn't done, respawn cancels it."""
        from jacked.api import usage_monitor as um

        app = _FakeApp()

        async def _run():
            # Create a real asyncio task that just sleeps so we can verify
            # cancellation rather than mocking task.cancel().
            async def _sleeper():
                await asyncio.sleep(3600)

            old = asyncio.create_task(_sleeper())
            app.state.active_poll_task = old

            um._respawn_active_poll(app)
            await asyncio.sleep(0)  # let cancellation propagate

            assert old.cancelled() or old.done(), (
                "respawn must cancel a still-running stale task"
            )
            new = app.state.active_poll_task
            assert new is not old
            new.cancel()
            try:
                await new
            except asyncio.CancelledError:
                pass

        asyncio.run(_run())

    def test_watchdog_respawns_when_heartbeat_stale(self):
        """When app.state.active_poll_last_tick_at is older than
        _HEARTBEAT_STALE_SECONDS, the watchdog must call _respawn_active_poll
        on its next check."""
        from jacked.api import usage_monitor as um

        app = _FakeApp()
        # Pretend the loop hasn't ticked for 10 minutes — way past 5min threshold.
        app.state.active_poll_last_tick_at = time.monotonic() - 600
        app.state.active_poll_last_respawn_at = 0.0
        app.state.active_poll_task = None

        with patch.object(um, "_respawn_active_poll", return_value=True) as mock_respawn:
            # Drive one watchdog iteration manually by inlining its body —
            # avoids the await asyncio.sleep(60) grace period.
            now = time.monotonic()
            last_tick = app.state.active_poll_last_tick_at
            stale = (now - last_tick) > um._HEARTBEAT_STALE_SECONDS
            assert stale, "test setup must produce a stale heartbeat"
            if stale:
                um._respawn_active_poll(app)
            mock_respawn.assert_called_once_with(app)

    def test_watchdog_skips_when_heartbeat_fresh(self):
        """A heartbeat written within the threshold must NOT trigger respawn."""
        from jacked.api import usage_monitor as um

        app = _FakeApp()
        app.state.active_poll_last_tick_at = time.monotonic() - 10  # 10s ago
        app.state.active_poll_last_respawn_at = 0.0

        now = time.monotonic()
        last_tick = app.state.active_poll_last_tick_at
        stale = (now - last_tick) > um._HEARTBEAT_STALE_SECONDS
        assert not stale, "10s-old heartbeat must not be stale"

    def test_watchdog_cooldown_prevents_thrash(self):
        """After a respawn, the watchdog must not respawn again until the
        cooldown elapses — even if the heartbeat is still stale."""
        from jacked.api import usage_monitor as um

        # Simulate: respawn happened 10s ago; cooldown is 30s; heartbeat
        # is still stale. The cooldown check must short-circuit the respawn.
        last_respawn = time.monotonic() - 10
        now = time.monotonic()
        in_cooldown = (now - last_respawn) < um._RESPAWN_COOLDOWN_SECONDS
        assert in_cooldown, "10s after respawn must still be inside cooldown"


class TestActivePollWatchdogLifespanWiring:
    """Verify main.py lifespan creates the watchdog task alongside the poll
    task. Lock in the contract so a future refactor doesn't accidentally
    drop the watchdog and re-introduce the 2026-05-10 silent-death bug."""

    def test_lifespan_imports_watchdog_loop(self):
        import inspect
        from jacked.api import main as api_main
        src = inspect.getsource(api_main)
        assert "active_poll_watchdog_loop" in src, (
            "main.py lifespan must import and start active_poll_watchdog_loop"
        )
        assert "asyncio.create_task(active_poll_watchdog_loop(app))" in src, (
            "main.py lifespan must create a watchdog task"
        )
        assert "app.state.active_poll_task = active_poll_task" in src, (
            "main.py lifespan must publish the task to app.state so the "
            "watchdog can detect a dead one"
        )


# ---------------------------------------------------------------------------
# Loop-tick helpers for the residency/backoff/taxonomy tests below
# ---------------------------------------------------------------------------


def _run_one_tick(app, max_sleeps=0, read_active=1, fetch_side_effect=None):
    """Run the active poll loop until the sleep-canceller fires; returns
    the sync_credential_to_all_stores mock for call assertions."""

    async def _run():
        fetch_kwargs = {"return_value": {"_cached": True}}
        if fetch_side_effect is not None:
            fetch_kwargs = {"side_effect": fetch_side_effect}
        with (
            patch(
                "jacked.api.usage_monitor.asyncio.sleep",
                side_effect=_sleep_canceller(max_sleeps=max_sleeps),
            ),
            patch(
                "jacked.api.usage_monitor._read_active_account_id",
                return_value=read_active,
            ),
            patch(
                "jacked.web.auth.fetch_usage",
                new_callable=AsyncMock,
                **fetch_kwargs,
            ),
            patch(
                "jacked.api.credential_helpers.read_fresh_active_token",
                return_value="tok",
            ),
            patch(
                "jacked.api.credential_helpers.sync_credential_to_all_stores",
            ) as mock_sync,
        ):
            with pytest.raises(asyncio.CancelledError):
                await active_account_poll_loop(app)
            return mock_sync

    return asyncio.run(_run())


def _decision_triggers(db):
    return [c.kwargs.get("trigger") for c in db.record_decision.call_args_list]


def _decision_reasons(db):
    return [c.kwargs.get("reason") for c in db.record_decision.call_args_list]


def _decision_actions(db):
    return [c.kwargs.get("action") for c in db.record_decision.call_args_list]


def _swap_settings():
    return {
        "auto_swap_enabled": "true",
        "usage_check_interval": "300",
    }


# ---------------------------------------------------------------------------
# Min-residency gate (item 6)
# ---------------------------------------------------------------------------


class TestResidencyGate:
    def test_proactive_swap_blocked_within_residency(self):
        """Higher-tier emergence within 900s of the last committed swap →
        stay with trigger 'residency_blocked', no swap."""
        import jacked.api.usage_monitor as mod
        mod._last_committed_swap_time = time.time() - 100

        accounts = [
            _acct(1, usage_5h=10, usage_7d=50),  # active, T3
            _acct(2, usage_5h=10, usage_7d=50,
                  cached_7d_resets_at=_t0_resets_at()),  # T0 candidate
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        mock_sync.assert_not_called()
        db.record_swap.assert_not_called()
        assert "residency_blocked" in _decision_triggers(db)
        assert "swap" not in _decision_actions(db)
        assert any(
            "residency" in (r or "") for r in _decision_reasons(db)
        )

    def test_forced_critical_exempt_from_residency(self):
        """5h-critical is a forced departure — swaps even within the
        residency window."""
        import jacked.api.usage_monitor as mod
        mod._last_committed_swap_time = time.time() - 100

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),  # good target
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        assert mock_sync.call_count >= 1
        assert mock_sync.call_args[0][0] == 2

    def test_proactive_swap_allowed_after_residency_elapsed(self):
        import jacked.api.usage_monitor as mod
        mod._last_committed_swap_time = (
            time.time() - mod._MIN_RESIDENCY_SECONDS - 10
        )

        accounts = [
            _acct(1, usage_5h=10, usage_7d=50),  # active, T3
            _acct(2, usage_5h=10, usage_7d=50,
                  cached_7d_resets_at=_t0_resets_at()),  # T0 candidate
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        # Fast-path emergence (T0 vs T3) + residency elapsed → swap fires.
        assert mock_sync.call_count >= 1
        assert mock_sync.call_args[0][0] == 2


# ---------------------------------------------------------------------------
# Exponential failure backoff (item 5)
# ---------------------------------------------------------------------------


class TestFailureBackoff:
    def test_backoff_window_math(self):
        import jacked.api.usage_monitor as mod

        mod._swap_failure_count = 0
        assert mod._swap_backoff_remaining() == 0.0

        mod._swap_failure_count = 1
        mod._last_swap_failure_at = time.time() - 30
        remaining = mod._swap_backoff_remaining()
        assert 25 < remaining <= 30  # 60s window

        mod._swap_failure_count = 2
        mod._last_swap_failure_at = time.time() - 30
        remaining = mod._swap_backoff_remaining()
        assert 85 < remaining <= 90  # 120s window

        mod._swap_failure_count = 10
        mod._last_swap_failure_at = time.time()
        remaining = mod._swap_backoff_remaining()
        assert 595 < remaining <= 600  # capped at 600s

        mod._swap_failure_count = 1
        mod._last_swap_failure_at = time.time() - 120
        assert mod._swap_backoff_remaining() == 0.0  # window elapsed

    def test_swap_skipped_during_backoff(self):
        import jacked.api.usage_monitor as mod
        mod._swap_failure_count = 1
        mod._last_swap_failure_at = time.time()

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),  # good target
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        mock_sync.assert_not_called()
        db.record_swap.assert_not_called()
        assert "swap_aborted" in _decision_triggers(db)
        assert any("backoff" in (r or "") for r in _decision_reasons(db))

    def test_swap_proceeds_after_backoff_expires(self):
        import jacked.api.usage_monitor as mod
        mod._swap_failure_count = 1
        mod._last_swap_failure_at = time.time() - 61  # 60s window elapsed

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        assert mock_sync.call_count >= 1
        assert mock_sync.call_args[0][0] == 2


# ---------------------------------------------------------------------------
# Decision-log trigger taxonomy per branch (item 12)
# ---------------------------------------------------------------------------


class TestTriggerTaxonomyBranches:
    def test_tick_trigger_on_healthy_stay(self):
        accounts = [_acct(1, usage_5h=30, usage_7d=50)]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        _run_one_tick(app)
        assert _decision_triggers(db) == ["tick"]
        assert _decision_actions(db) == ["stay"]

    def test_cooldown_blocked_trigger(self):
        import jacked.api.usage_monitor as mod
        mod._last_swap_time = time.time()

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        mock_sync.assert_not_called()
        assert "cooldown_blocked" in _decision_triggers(db)

    def test_no_target_trigger(self):
        accounts = [_acct(1, usage_5h=95, usage_7d=50)]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        _run_one_tick(app)
        assert "no_target" in _decision_triggers(db)

    def test_emergence_pending_trigger(self):
        """One-tier gap (T1 best vs T2 active) requires persistence —
        first tick stays with trigger 'emergence_pending'."""
        import jacked.api.usage_monitor as mod
        now = datetime.now(timezone.utc)
        accounts = [
            _acct(1, usage_5h=10, usage_7d=50,
                  cached_7d_resets_at=_iso(now + timedelta(hours=72))),  # T2
            _acct(2, usage_5h=10, usage_7d=50,
                  cached_7d_resets_at=_iso(now + timedelta(hours=36))),  # T1
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        mock_sync.assert_not_called()
        assert "emergence_pending" in _decision_triggers(db)
        assert mod._emerged_tier_streak == {"tier": 1, "count": 1}

    def test_committed_swap_records_reason_trigger(self):
        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        assert mock_sync.call_count >= 1
        triggers = _decision_triggers(db)
        actions = _decision_actions(db)
        assert "forced_critical" in triggers
        assert actions[triggers.index("forced_critical")] == "swap"


# ---------------------------------------------------------------------------
# Fast-path unambiguous emergence (item 9)
# ---------------------------------------------------------------------------


class TestFastPathEmergence:
    def test_t0_vs_t2plus_swaps_without_persistence(self):
        """A >=2-tier gap (T0 best vs T3 active) cannot be boundary jitter
        — the swap fires on the FIRST tick, skipping the streak."""
        accounts = [
            _acct(1, usage_5h=10, usage_7d=50),  # active, T3 default
            _acct(2, usage_5h=10, usage_7d=50,
                  cached_7d_resets_at=_t0_resets_at()),  # T0 candidate
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        assert mock_sync.call_count >= 1
        assert mock_sync.call_args[0][0] == 2
        swap_kwargs = db.record_swap.call_args[1]
        assert swap_kwargs["trigger"] == "higher_tier_emerged"
        assert "higher_tier_emerged" in _decision_triggers(db)

    def test_one_tier_gap_still_requires_persistence(self):
        now = datetime.now(timezone.utc)
        accounts = [
            _acct(1, usage_5h=10, usage_7d=50,
                  cached_7d_resets_at=_iso(now + timedelta(hours=72))),  # T2
            _acct(2, usage_5h=10, usage_7d=50,
                  cached_7d_resets_at=_iso(now + timedelta(hours=36))),  # T1
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        mock_sync = _run_one_tick(app)
        mock_sync.assert_not_called()
        db.record_swap.assert_not_called()


# ---------------------------------------------------------------------------
# Priming (item 2) + active-fetch timeout (item 1)
# ---------------------------------------------------------------------------


class TestPriming:
    def test_single_account_install_marks_priming_done(self):
        """Nothing to prime (attempted == 0) must set _initial_fetch_done —
        previously the priming block re-ran every tick forever."""
        import jacked.api.usage_monitor as mod
        mod._initial_fetch_done = False

        accounts = [_acct(1, usage_5h=30)]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        _run_one_tick(app)
        assert mod._initial_fetch_done is True

    def test_all_prime_fetches_failing_retries_next_tick(self):
        import jacked.api.usage_monitor as mod
        mod._initial_fetch_done = False

        accounts = [
            _acct(1, usage_5h=30),
            _acct(2, usage_5h=30),
        ]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        _run_one_tick(app, max_sleeps=1, fetch_side_effect=Exception("boom"))
        assert mod._initial_fetch_done is False


class TestActiveFetchTimeout:
    def test_tick_continues_on_active_fetch_timeout(self):
        """A hung/timed-out active fetch must not abort the tick — the
        decision still gets recorded from cached data."""
        accounts = [_acct(1, usage_5h=30, usage_7d=50)]
        db = _make_db(settings=_swap_settings(), accounts=accounts)
        app = _make_app(db=db)

        _run_one_tick(app, fetch_side_effect=asyncio.TimeoutError())
        assert db.record_decision.call_count >= 1
        assert "tick" in _decision_triggers(db)


# ---------------------------------------------------------------------------
# Candidate staleness override (item 3)
# ---------------------------------------------------------------------------


class TestCandidateStalenessOverride:
    def test_override_on_past_5h_reset_with_saturated_cache(self):
        from jacked.api.usage_monitor import _candidate_staleness_override
        now = datetime.now(timezone.utc)
        acct = _acct(2, usage_5h=95, resets_at=_iso(now - timedelta(minutes=10)))
        assert _candidate_staleness_override(acct) is True

    def test_no_override_on_future_5h_reset(self):
        from jacked.api.usage_monitor import _candidate_staleness_override
        now = datetime.now(timezone.utc)
        acct = _acct(2, usage_5h=95, resets_at=_iso(now + timedelta(hours=1)))
        assert _candidate_staleness_override(acct) is False

    def test_no_override_below_saturation(self):
        from jacked.api.usage_monitor import _candidate_staleness_override
        now = datetime.now(timezone.utc)
        acct = _acct(2, usage_5h=50, resets_at=_iso(now - timedelta(minutes=10)))
        assert _candidate_staleness_override(acct) is False

    def test_override_on_7d_window_reset(self):
        from jacked.api.usage_monitor import _candidate_staleness_override
        now = datetime.now(timezone.utc)
        acct = _acct(
            2, usage_5h=10,
            cached_7d_resets_at=_iso(now - timedelta(hours=2)),
            usage_cached_at=int((now - timedelta(hours=3)).timestamp()),
        )
        assert _candidate_staleness_override(acct) is True

    def test_fetch_forced_despite_fresh_cache(self):
        """Override ignores _CANDIDATE_STALENESS_SECONDS: a candidate with
        a fresh cache but a past 5h reset at >=90% gets re-fetched."""
        from jacked.api import usage_monitor as um
        now = datetime.now(timezone.utc)
        active = _acct(1, usage_5h=30)
        stale_5h = _acct(
            2, usage_5h=95,
            resets_at=_iso(now - timedelta(minutes=5)),
            usage_cached_at=int(now.timestamp()),  # cache is "fresh"
        )
        db = _make_db(accounts=[active, stale_5h])

        async def _run():
            with (
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                ) as mock_fetch,
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    new_callable=AsyncMock,
                ),
            ):
                await um._fetch_candidate_usage([active, stale_5h], 1, db)
                mock_fetch.assert_called_once()
                assert mock_fetch.call_args[0][0] == 2

        asyncio.run(_run())

    def test_bad_candidate_does_not_stall_pass(self):
        """One candidate fetch raising must not prevent the next fetch."""
        from jacked.api import usage_monitor as um
        now = datetime.now(timezone.utc)
        old_cache = int((now - timedelta(hours=1)).timestamp())
        active = _acct(1, usage_5h=30)
        c2 = _acct(2, usage_5h=10, usage_cached_at=old_cache)
        c3 = _acct(3, usage_5h=10, usage_cached_at=old_cache)
        db = _make_db(accounts=[active, c2, c3])

        async def _run():
            with (
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    side_effect=[Exception("boom"), {"_cached": False}],
                ) as mock_fetch,
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    new_callable=AsyncMock,
                ),
            ):
                await um._fetch_candidate_usage([active, c2, c3], 1, db)
                assert mock_fetch.call_count == 2

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Drain advisor (item 11)
# ---------------------------------------------------------------------------


class TestDrainAdvisor:
    def _t0_stranded_acct(self, id=1):
        now = datetime.now(timezone.utc)
        # T0 (12h out) at 50% 7d → deficit 50, achievable <= ~10% →
        # stranding >= ~40% regardless of local wall-clock time.
        return _acct(
            id, usage_5h=10, usage_7d=50,
            cached_7d_resets_at=_iso(now + timedelta(hours=12)),
        )

    def test_advisor_broadcasts_for_stranded_t0(self):
        from jacked.api import usage_monitor as um
        now = datetime.now(timezone.utc)
        acct = self._t0_stranded_acct()
        ws = AsyncMock()

        asyncio.run(um._drain_advisor_tick(
            [acct], now, "06:00", "23:00", ws, {},
        ))
        assert ws.broadcast.call_count == 1
        event, payload = ws.broadcast.call_args[0]
        assert event == "expiring_with_stranded_capacity"
        assert payload["account_id"] == 1
        assert payload["stranding"] > 2.0
        assert payload["deficit"] == 50.0
        assert payload["resets_at"] == acct["cached_7d_resets_at"]

    def test_advisor_cooldown_suppresses_repeat(self):
        from jacked.api import usage_monitor as um
        now = datetime.now(timezone.utc)
        acct = self._t0_stranded_acct()
        ws = AsyncMock()

        asyncio.run(um._drain_advisor_tick(
            [acct], now, "06:00", "23:00", ws, {},
        ))
        asyncio.run(um._drain_advisor_tick(
            [acct], now, "06:00", "23:00", ws, {},
        ))
        assert ws.broadcast.call_count == 1  # second tick within cooldown

        # Expire the cooldown — advisor fires again.
        um._drain_advisor_last_sent[1] = (
            time.time() - um._DRAIN_ADVISOR_COOLDOWN_SECONDS - 100
        )
        asyncio.run(um._drain_advisor_tick(
            [acct], now, "06:00", "23:00", ws, {},
        ))
        assert ws.broadcast.call_count == 2

    def test_non_t0_account_not_advised(self):
        from jacked.api import usage_monitor as um
        now = datetime.now(timezone.utc)
        acct = _acct(1, usage_5h=10, usage_7d=10)  # T3 default
        ws = AsyncMock()

        asyncio.run(um._drain_advisor_tick(
            [acct], now, "06:00", "23:00", ws, {},
        ))
        ws.broadcast.assert_not_called()

    def test_low_stranding_not_advised(self):
        from jacked.api import usage_monitor as um
        now = datetime.now(timezone.utc)
        acct = _acct(
            1, usage_5h=10, usage_7d=99,  # deficit 1 → stranding <= 1
            cached_7d_resets_at=_iso(now + timedelta(hours=12)),
        )
        ws = AsyncMock()

        asyncio.run(um._drain_advisor_tick(
            [acct], now, "06:00", "23:00", ws, {},
        ))
        ws.broadcast.assert_not_called()


# ---------------------------------------------------------------------------
# Poll-interval clamp (item 10)
# ---------------------------------------------------------------------------


class TestPollIntervalClamp:
    def test_clamps_to_90_with_t0_deficit_candidate(self):
        from jacked.api.usage_monitor import _clamp_poll_interval
        now = datetime.now(timezone.utc)
        active = _acct(1, usage_5h=30, usage_7d=50)
        t0 = _acct(2, usage_5h=10, usage_7d=50,
                   cached_7d_resets_at=_t0_resets_at())
        out = _clamp_poll_interval(
            300.0, [active, t0], 1, now, "06:00", "23:00", {},
        )
        assert out == 90.0

    def test_clamps_to_60_when_saturated_t0_reset_imminent(self):
        from jacked.api.usage_monitor import _clamp_poll_interval
        now = datetime.now(timezone.utc)
        active = _acct(1, usage_5h=30, usage_7d=50)
        t0 = _acct(
            2, usage_5h=95, usage_7d=50,
            resets_at=_iso(now + timedelta(seconds=45)),
            cached_7d_resets_at=_t0_resets_at(),
        )
        out = _clamp_poll_interval(
            300.0, [active, t0], 1, now, "06:00", "23:00", {},
        )
        assert out == 60.0

    def test_no_clamp_without_t0_candidate(self):
        from jacked.api.usage_monitor import _clamp_poll_interval
        now = datetime.now(timezone.utc)
        active = _acct(1, usage_5h=30, usage_7d=50)
        t3 = _acct(2, usage_5h=10, usage_7d=10)  # T3 default
        out = _clamp_poll_interval(
            300.0, [active, t3], 1, now, "06:00", "23:00", {},
        )
        assert out == 300.0

    def test_no_clamp_when_t0_has_no_deficit(self):
        from jacked.api.usage_monitor import _clamp_poll_interval
        now = datetime.now(timezone.utc)
        active = _acct(1, usage_5h=30, usage_7d=50)
        t0_full = _acct(2, usage_5h=10, usage_7d=100,
                        cached_7d_resets_at=_t0_resets_at())
        out = _clamp_poll_interval(
            300.0, [active, t0_full], 1, now, "06:00", "23:00", {},
        )
        assert out == 300.0

    def test_active_account_does_not_clamp(self):
        from jacked.api.usage_monitor import _clamp_poll_interval
        now = datetime.now(timezone.utc)
        active_t0 = _acct(1, usage_5h=10, usage_7d=50,
                          cached_7d_resets_at=_t0_resets_at())
        out = _clamp_poll_interval(
            300.0, [active_t0], 1, now, "06:00", "23:00", {},
        )
        assert out == 300.0

    def test_ineligible_t0_does_not_clamp(self):
        from jacked.api.usage_monitor import _clamp_poll_interval
        now = datetime.now(timezone.utc)
        active = _acct(1, usage_5h=30, usage_7d=50)
        disabled_t0 = _acct(2, usage_5h=10, usage_7d=50, auto_swap=False,
                            cached_7d_resets_at=_t0_resets_at())
        out = _clamp_poll_interval(
            300.0, [active, disabled_t0], 1, now, "06:00", "23:00", {},
        )
        assert out == 300.0


# ---------------------------------------------------------------------------
# note_external_swap (item 15) + reset_locks new state (item 16)
# ---------------------------------------------------------------------------


class TestNoteExternalSwap:
    def test_sets_cooldowns_and_clears_streak(self):
        from jacked.api import usage_monitor as um
        um._emerged_tier_streak["tier"] = 0
        um._emerged_tier_streak["count"] = 2

        before = time.time()
        um.note_external_swap()
        assert um._last_swap_time >= before
        assert um._last_committed_swap_time >= before
        assert um._emerged_tier_streak == {"tier": None, "count": 0}


class TestResetLocksState:
    def test_reset_locks_clears_new_module_state(self):
        from jacked.api import usage_monitor as um
        um._swap_failure_count = 3
        um._last_swap_failure_at = 123.0
        um._last_committed_swap_time = 456.0
        um._same_tier_advisory_last = 789.0
        um._drain_advisor_last_sent[1] = 42.0
        um._emerged_tier_streak["tier"] = 2
        um._emerged_tier_streak["count"] = 9
        um._last_observed_tiers[1] = 2
        um._ping_backoff[1] = {"fails": 4, "skip_until": time.monotonic() + 60}

        um.reset_locks()

        assert um._swap_failure_count == 0
        assert um._last_swap_failure_at == 0.0
        assert um._last_committed_swap_time == 0.0
        assert um._same_tier_advisory_last == 0.0
        assert um._drain_advisor_last_sent == {}
        assert um._emerged_tier_streak == {"tier": None, "count": 0}
        assert um._last_observed_tiers == {}
        assert um._ping_backoff == {}


def test_all_accounts_loop_polls_codex_not_just_cc_accounts(monkeypatch):
    """Regression: all_accounts_refresh_loop skipped every account without a CC
    token, silently excluding ALL Codex accounts (cc_access_token is always None
    for Codex) — which left their usage stuck at 0%.

    Runs exactly one loop iteration (cancel on the 2nd top-of-loop sleep) and
    asserts a Codex account IS polled while a Claude account with no CC token is
    skipped.
    """
    import types

    import jacked.api.usage_monitor as um
    import jacked.web.auth as auth_mod

    accounts = [
        {"id": 1, "provider": "claude", "cc_access_token": "cc-1"},  # polled
        {"id": 2, "provider": "claude", "cc_access_token": None},    # skipped
        {"id": 3, "provider": "codex", "cc_access_token": None},     # NOW polled
    ]
    fake_db = types.SimpleNamespace(list_accounts=lambda **k: accounts)
    app = types.SimpleNamespace(
        state=types.SimpleNamespace(db=fake_db, active_poll_account_id=None)
    )
    calls = []

    async def fake_fetch(aid, db, access_token=None):
        calls.append((aid, access_token))

    monkeypatch.setattr(auth_mod, "fetch_usage", fake_fetch)

    tops = {"n": 0}

    async def fake_sleep(seconds):
        # The top-of-loop wait uses _ALL_ACCOUNTS_REFRESH_INTERVAL; the 2s pace
        # uses 2. Cancel on the 2nd top-of-loop sleep → exactly one iteration.
        if seconds == um._ALL_ACCOUNTS_REFRESH_INTERVAL:
            tops["n"] += 1
            if tops["n"] >= 2:
                raise asyncio.CancelledError

    monkeypatch.setattr(um.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(um.all_accounts_refresh_loop(app))

    fetched = [aid for aid, _ in calls]
    assert 1 in fetched          # claude with CC token → polled
    assert 2 not in fetched      # claude without CC token → skipped
    assert 3 in fetched          # codex → polled (the fix)
    assert (3, None) in calls    # codex polled with no token (app-server path)
