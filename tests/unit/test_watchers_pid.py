"""Regression tests for _is_pid_alive safety on Windows.

Previously used os.kill(pid, 0) unconditionally which on Windows could
surface CPython SystemError ("returned a result with an exception set").
Now delegates to jacked.service.process.is_process_alive and fail-safe
returns True on any exception so user sessions are never incorrectly
closed.
"""

from unittest.mock import patch


def test_returns_false_for_none_or_zero():
    from jacked.api.watchers import _is_pid_alive
    assert _is_pid_alive(None) is False
    assert _is_pid_alive(0) is False
    assert _is_pid_alive(-1) is False


def test_returns_true_for_current_process():
    import os as _os
    from jacked.api.watchers import _is_pid_alive
    assert _is_pid_alive(_os.getpid()) is True


def test_returns_false_for_dead_pid():
    from jacked.api.watchers import _is_pid_alive
    assert _is_pid_alive(999_999_999) is False


def test_fail_safe_on_unexpected_exception():
    """If is_process_alive raises (Windows SystemError quirk), fall back
    to True so we don't incorrectly close live sessions."""
    from jacked.api import watchers
    with patch(
        "jacked.service.process.is_process_alive",
        side_effect=SystemError("returned a result with an exception set"),
    ):
        assert watchers._is_pid_alive(12345) is True
