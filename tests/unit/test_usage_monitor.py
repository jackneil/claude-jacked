"""Tests for the usage monitor loops (active poll + full sweep).

Each test runs one tick of a loop by mocking dependencies.  Uses
asyncio.run() for async tests (no pytest-asyncio dependency).
"""

import asyncio
import logging
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from jacked.api.usage_monitor import (
    _read_active_account_id,
    _setting_bool,
    _setting_float,
    _setting_str,
    _SWAP_COOLDOWN_SECONDS,
    active_account_poll_loop,
    full_sweep_loop,
)


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
          rate_limit_tier="", subscription_type="pro"):
    return {
        "id": id,
        "email": email or f"user{id}@test.com",
        "cached_usage_5h": usage_5h,
        "cached_usage_7d": usage_7d,
        "cached_5h_resets_at": resets_at,
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
                assert swap_kwargs["trigger"] == "auto_swap"

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

    def test_no_decay_above_warning_threshold(self):
        """Usage at or above warning threshold -> no burn-rate decay applied."""
        import jacked.api.usage_monitor as mod
        from jacked.web.auto_swap import BurnRate
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        # Pre-seed with many unchanged ticks
        mod._burn_rates[1] = BurnRate(
            rate_5h_per_min=1.0,
            last_check_5h=85.0,
            rate_7d_per_min=1.0,
            last_check_7d=50.0,
        )
        mod._burn_rate_unchanged_ticks[1] = 10  # well above the 5-tick threshold

        # Account at 85% — above 80% warning, may trigger swap
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
                    side_effect=_sleep_canceller(max_sleeps=1),
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
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

            # At 85% usage the account may trigger a swap and pop the burn rate.
            # Only assert the rate is undecayed if the entry still exists.
            br = mod._burn_rates.get(1)
            if br is not None:
                assert br.rate_5h_per_min >= 1.0, (
                    f"Expected rate_5h_per_min undecayed (>= 1.0), "
                    f"got {br.rate_5h_per_min}"
                )

        asyncio.run(_run())
