"""Tests for the usage monitor loop orchestrator.

Each test runs one tick of the loop by mocking dependencies.  Uses
asyncio.run() for async tests (no pytest-asyncio dependency).
"""

import asyncio
import logging
import time
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.api.usage_monitor import (
    _read_active_account_id,
    _setting_bool,
    _setting_float,
    _setting_str,
    usage_monitor_loop,
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
          email=None, resets_at=None, cc_rt="rt", scopes=""):
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
# Setting helpers (sync — no event loop needed)
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
    def test_no_action_when_disabled(self):
        """Both auto_swap and window_keeper disabled -> no fetch_usage calls."""
        db = _make_db(
            settings={"auto_swap_enabled": "false", "window_keeper_enabled": "false"},
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
                    await usage_monitor_loop(app)

                mock_fetch.assert_not_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Auto-swap triggers on high usage
# ---------------------------------------------------------------------------

class TestAutoSwapTriggers:
    def test_auto_swap_triggers_on_high_usage(self):
        """When 5h usage is above critical, swap to best target."""
        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            _acct(2, usage_5h=20, usage_7d=10),   # good target
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "window_keeper_enabled": "false",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            # 2 accounts = sleep(1) + sleep(1) pacing + sleep(interval) = 3 per tick
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
                    await usage_monitor_loop(app)

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
# No target logs warning
# ---------------------------------------------------------------------------

class TestNoTarget:
    def test_no_target_logs_warning(self, caplog):
        """should_swap=True but no eligible target -> logs warning."""
        import jacked.api.usage_monitor as mod

        # Reset exhaustion cooldown so warning fires
        mod._last_exhaustion_warning = 0.0

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),  # active, over critical
            # No other eligible accounts
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "window_keeper_enabled": "false",
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
                        await usage_monitor_loop(app)

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
# Cached response handled
# ---------------------------------------------------------------------------

class TestCachedResponse:
    def test_cached_response_handled(self):
        """fetch_usage returns {_cached: True} -> no crash, loop continues."""
        accounts = [_acct(1, usage_5h=30)]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "window_keeper_enabled": "false",
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
                    await usage_monitor_loop(app)

                # fetch_usage was called — the cached response didn't crash
                mock_fetch.assert_called()

        asyncio.run(_run())


# ---------------------------------------------------------------------------
# Window keeper pings during active hours
# ---------------------------------------------------------------------------

class TestWindowKeeperPings:
    def test_window_keeper_pings_during_active_hours(self):
        """Active hours + needs_ping -> ping_account called."""
        accounts = [
            _acct(1, usage_5h=30, resets_at=None, cc_rt="refresh_tok_1"),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "false",
                "window_keeper_enabled": "true",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            # 1 account = sleep(1) pacing + sleep(2) ping pacing + sleep(interval) = 3 per tick
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
                    "jacked.web.window_keeper.is_active_hours",
                    return_value=True,
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
                    await usage_monitor_loop(app)

                # ping_account should be called at least once
                assert mock_ping.call_count >= 1
                mock_ping.assert_any_call("refresh_tok_1", "")

        asyncio.run(_run())
