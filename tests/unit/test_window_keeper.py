"""Tests for window keeper schedule evaluation and ping_account."""

import asyncio
import logging
from datetime import datetime
from unittest.mock import patch

from jacked.web.window_keeper import is_active_hours, needs_ping, prewake_due


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
# prewake_due — first sweep at or after the pre-wake time, once per date
# ---------------------------------------------------------------------------

class TestPrewakeDue:
    def test_before_prewake_is_not_due(self):
        assert prewake_due(datetime(2026, 10, 27, 3, 58)) is False

    def test_first_sweep_after_prewake_is_due(self):
        assert prewake_due(datetime(2026, 10, 27, 4, 6)) is True

    def test_late_sweep_still_due_when_slice_was_missed(self):
        # The old 5-minute slice check returned False here and the day's
        # pre-wake never happened.
        assert prewake_due(datetime(2026, 10, 27, 4, 40)) is True

    def test_not_due_twice_on_the_same_date(self):
        now = datetime(2026, 10, 27, 4, 6)
        assert prewake_due(now, done_date="2026-10-27") is False
        assert prewake_due(now, done_date="2026-10-26") is True

    def test_not_due_inside_active_hours(self):
        assert prewake_due(datetime(2026, 10, 27, 9, 0)) is False

    def test_due_before_a_late_active_start(self):
        assert prewake_due(datetime(2026, 10, 27, 9, 0), active_start="10:00") is True

    def test_not_due_after_active_hours_end(self):
        assert prewake_due(datetime(2026, 10, 27, 23, 30)) is False


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

    def test_needs_ping_malformed_value_does_not_raise(self, caplog):
        with caplog.at_level("WARNING", logger="jacked.web.window_keeper"):
            assert needs_ping("not-a-timestamp") is True
            assert needs_ping(1774933200) is True
        assert any("unparseable" in r.getMessage() for r in caplog.records)


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

    def test_garbage_string_needs_ping(self):
        """Unparseable timestamp: ping (the read-back rewrites the cache)
        instead of raising out of the sweep."""
        from jacked.web.window_keeper import needs_7d_ping
        assert needs_7d_ping("not-a-date", 12345) is True
        assert needs_7d_ping(12345, 12345) is True


# ---------------------------------------------------------------------------
# ping_account
# ---------------------------------------------------------------------------

SECRET = "sk-ant-oat01-SECRET-VALUE-0123456789"


def _ping_with(handler, token=SECRET):
    """Run ping_account against an httpx.MockTransport handler."""
    import httpx

    from jacked.web.window_keeper import ping_account

    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(*args, **kwargs)

    async def _run():
        with patch("httpx.AsyncClient", side_effect=_client):
            return await ping_account(token, account_id=7)

    return asyncio.run(_run())


class TestPingAccount:
    def test_ping_success_carries_request_id_and_ratelimit_headers(self):
        import httpx

        seen = {}

        def handler(request):
            seen["auth"] = request.headers["authorization"]
            return httpx.Response(
                200,
                json={"content": []},
                headers={
                    "request-id": "req_011abc",
                    "Anthropic-Ratelimit-Unified-5h-Reset": "1774933200",
                    "anthropic-ratelimit-unified-5h-utilization": "0.07",
                    "x-other": "ignored",
                },
            )

        result = _ping_with(handler)
        assert result.ok is True
        assert result.status == 200
        assert result.request_id == "req_011abc"
        assert result.rate_limit == {
            "anthropic-ratelimit-unified-5h-reset": "1774933200",
            "anthropic-ratelimit-unified-5h-utilization": "0.07",
        }
        assert result.reset_5h_iso() == "2026-03-31T05:00:00+00:00"
        assert seen["auth"] == f"Bearer {SECRET}"

    def test_ping_401_parses_anthropic_error_body(self):
        import httpx

        def handler(request):
            return httpx.Response(
                401,
                json={
                    "type": "error",
                    "error": {"type": "authentication_error", "message": "invalid x-api-key"},
                    "request_id": "req_401",
                },
                headers={"request-id": "req_401"},
            )

        result = _ping_with(handler)
        assert result.ok is False
        assert result.status == 401
        assert result.is_auth_failure is True
        assert result.error_type == "authentication_error"
        assert result.request_id == "req_401"
        assert "invalid x-api-key" in result.message

    def test_ping_429_reads_retry_after(self):
        import httpx

        def handler(request):
            return httpx.Response(
                429,
                json={"type": "error", "error": {"type": "rate_limit_error", "message": "slow down"}},
                headers={"retry-after": "42", "anthropic-ratelimit-unified-status": "rejected"},
            )

        result = _ping_with(handler)
        assert result.is_rate_limited is True
        assert result.retry_after == 42.0
        assert result.error_type == "rate_limit_error"
        assert result.rate_limit["anthropic-ratelimit-unified-status"] == "rejected"

    def test_ping_non_json_body_is_truncated(self):
        import httpx

        def handler(request):
            return httpx.Response(502, text="x" * 1000)

        result = _ping_with(handler)
        assert result.status == 502
        assert result.is_transient is True
        assert result.error_type is None
        assert len(result.message) == 200

    def test_ping_transport_error_is_a_result_not_an_exception(self):
        import httpx

        def handler(request):
            raise httpx.ConnectError("connection refused")

        result = _ping_with(handler)
        assert result.ok is False
        assert result.transport_error is True
        assert result.is_transient is True
        assert result.status is None
        assert result.message == "ConnectError"

    def test_token_never_appears_in_logs_or_result(self, caplog):
        """The Authorization value must not leak into a log record or any
        PingResult field, even when the server echoes it back."""
        import dataclasses

        import httpx

        def echo(request):
            return httpx.Response(
                401,
                text=f"bad token {request.headers['authorization']}",
                headers={"request-id": "req_echo"},
            )

        caplog.set_level(logging.DEBUG)
        results = [_ping_with(echo)]

        def boom(request):
            raise httpx.ReadTimeout(f"timeout for {SECRET}")

        results.append(_ping_with(boom))
        for record in caplog.records:
            assert SECRET not in record.getMessage()
            assert SECRET not in str(record.args)
            assert SECRET not in (record.exc_text or "")
        for result in results:
            assert SECRET not in repr(dataclasses.asdict(result))
        assert "[redacted]" in results[0].message
        assert any(
            "Window keeper ping failed" in r.getMessage() and "req_echo" in r.getMessage()
            for r in caplog.records
        )
