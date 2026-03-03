"""Specification tests for formatTimeRemaining logic.

The JS function lives in utils.js; this Python reimplementation with identical
thresholds serves as a specification test — if JS and Python diverge, this
test documents expected behavior.
"""

import math

import pytest

SECS_PER_DAY = 86400
SECS_PER_WEEK = 604800


def format_time_remaining(seconds):
    """Python reimplementation of JS formatTimeRemaining (utils.js)."""
    if seconds is None or (isinstance(seconds, float) and math.isnan(seconds)) or seconds <= 0:
        return None
    if seconds < 60:
        return "< 1m"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < SECS_PER_DAY:
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        return f"{h}h {m}m" if m > 0 else f"{h}h"
    if seconds < SECS_PER_WEEK:
        return f"{int(seconds // SECS_PER_DAY)}d"
    return "2w+"


class TestFormatTimeRemaining:
    """Edge cases matching JS formatTimeRemaining spec."""

    @pytest.mark.parametrize(
        "inp,expected",
        [
            (None, None),
            (0, None),
            (-1, None),
            (float("nan"), None),
        ],
    )
    def test_null_zero_negative_nan(self, inp, expected):
        assert format_time_remaining(inp) is expected

    def test_under_one_minute(self):
        assert format_time_remaining(1) == "< 1m"
        assert format_time_remaining(30) == "< 1m"
        assert format_time_remaining(59) == "< 1m"

    def test_minutes(self):
        assert format_time_remaining(60) == "1m"
        assert format_time_remaining(2820) == "47m"
        assert format_time_remaining(3599) == "59m"

    def test_hours_and_minutes(self):
        assert format_time_remaining(3600) == "1h"
        assert format_time_remaining(8040) == "2h 14m"
        assert format_time_remaining(86399) == "23h 59m"

    def test_days(self):
        assert format_time_remaining(86400) == "1d"
        assert format_time_remaining(259200) == "3d"
        assert format_time_remaining(604799) == "6d"

    def test_weeks_plus(self):
        assert format_time_remaining(604800) == "2w+"
        assert format_time_remaining(1000000) == "2w+"

    def test_exact_hour_no_trailing_minutes(self):
        assert format_time_remaining(7200) == "2h"

    def test_boundary_3600(self):
        """3600 is the TOKEN_EXPIRY_WARN_SECS threshold — should show 1h."""
        assert format_time_remaining(3600) == "1h"
