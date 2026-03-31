"""Tests for window keeper schedule evaluation (pure functions only).

ping_account is NOT tested here — it spawns real subprocesses.
"""

from datetime import datetime

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
