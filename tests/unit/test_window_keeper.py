"""Tests for window keeper schedule evaluation and ping_account."""

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

from jacked.web.window_keeper import is_active_hours, is_prewake_time, needs_ping


# ---------------------------------------------------------------------------
# is_active_hours
# ---------------------------------------------------------------------------

class TestIsActiveHours:
    def test_active_hours_during_day(self):
        now = datetime(2026, 3, 29, 14, 0)  # 14:00
        assert is_active_hours(now, start="06:00", end="23:00") is True

    def test_active_hours_before_start(self):
        now = datetime(2026, 3, 29, 5, 0)  # 05:00
        assert is_active_hours(now, start="06:00", end="23:00") is False

    def test_active_hours_after_end(self):
        now = datetime(2026, 3, 29, 23, 30)  # 23:30
        assert is_active_hours(now, start="06:00", end="23:00") is False


# ---------------------------------------------------------------------------
# is_prewake_time
# ---------------------------------------------------------------------------

class TestIsPrewakeTime:
    def test_prewake_within_window(self):
        now = datetime(2026, 3, 29, 4, 2)  # 04:02
        assert is_prewake_time(now, prewake="04:00", check_interval_min=5) is True

    def test_prewake_outside_window(self):
        now = datetime(2026, 3, 29, 4, 10)  # 04:10
        assert is_prewake_time(now, prewake="04:00", check_interval_min=5) is False


# ---------------------------------------------------------------------------
# needs_ping
# ---------------------------------------------------------------------------

class TestNeedsPing:
    def test_needs_ping_expired_window(self):
        # A timestamp in the past
        assert needs_ping("2026-03-29T10:00:00") is True

    def test_needs_ping_no_window(self):
        assert needs_ping(None) is True

    def test_needs_ping_active_window(self):
        # A timestamp far in the future
        assert needs_ping("2099-12-31T23:59:59") is False


# ---------------------------------------------------------------------------
# needs_7d_ping
# ---------------------------------------------------------------------------

class TestNeeds7dPing:
    def test_none_resets_at_needs_ping(self):
        """No 7d window ever opened → needs ping."""
        from jacked.web.window_keeper import needs_7d_ping
        assert needs_7d_ping(None, None) is True

    def test_future_reset_no_ping(self):
        """7d window still active → no ping needed."""
        from datetime import datetime, timezone, timedelta
        import time as _time
        from jacked.web.window_keeper import needs_7d_ping

        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        now = int(_time.time())
        assert needs_7d_ping(future, now) is False

    def test_past_reset_with_stale_data_needs_ping(self):
        """7d window reset and cached data is older than reset → needs ping."""
        from datetime import datetime, timezone, timedelta
        from jacked.web.window_keeper import needs_7d_ping

        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        past_ts = int((datetime.now(timezone.utc) - timedelta(hours=3)).timestamp())
        assert needs_7d_ping(past, past_ts) is True

    def test_past_reset_with_fresh_data_no_ping(self):
        """7d reset already happened and data was refreshed after → no ping."""
        from datetime import datetime, timezone, timedelta
        from jacked.web.window_keeper import needs_7d_ping

        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        fresh_ts = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        assert needs_7d_ping(past, fresh_ts) is False

    def test_none_cached_at_with_past_reset_needs_ping(self):
        """Never cached + reset in past → needs ping."""
        from datetime import datetime, timezone, timedelta
        from jacked.web.window_keeper import needs_7d_ping

        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        assert needs_7d_ping(past, None) is True

    def test_garbage_string_returns_false(self):
        """Unparseable timestamp → safe default: no ping."""
        from jacked.web.window_keeper import needs_7d_ping
        assert needs_7d_ping("not-a-date", 12345) is False


# ---------------------------------------------------------------------------
# ping_account
# ---------------------------------------------------------------------------

class TestPingAccount:
    def test_ping_success(self):
        """HTTP 200 from messages API -> returns True."""
        from jacked.web.window_keeper import ping_account

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await ping_account("fake-token")
                assert result is True
                mock_client.post.assert_called_once()
                call_kwargs = mock_client.post.call_args
                assert "Bearer fake-token" in call_kwargs[1]["headers"]["Authorization"]

        asyncio.run(_run())

    def test_ping_401_returns_false(self):
        """HTTP 401 (expired token) -> returns False."""
        from jacked.web.window_keeper import ping_account

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await ping_account("expired-token")
                assert result is False

        asyncio.run(_run())

    def test_ping_exception_returns_false(self):
        """Network error -> returns False, doesn't crash."""
        from jacked.web.window_keeper import ping_account

        async def _run():
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = Exception("connection refused")
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await ping_account("any-token")
                assert result is False

        asyncio.run(_run())
