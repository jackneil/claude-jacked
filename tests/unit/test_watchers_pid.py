"""The process sweeper's per-session verdict is safe on every platform.

The sweeper once called ``os.kill(pid, 0)`` directly, which on Windows could
surface CPython SystemError ("returned a result with an exception set").
It now asks ``session_processes_running`` (the verdict the credential holder
set uses too). An error in the probe gives an unknown answer, and the sweeper
never closes a session on an unknown answer.
"""

import os
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from jacked.session_ancestry import session_processes_running

def _row(pid, written=None):
    written = written or datetime.now(timezone.utc)
    return {"session_id": f"s-{pid}", "pid": pid, "detected_at": written.isoformat()}


def test_no_usable_pid_is_unknown():
    assert session_processes_running([_row(None), _row(0), _row(-1), _row(1)]) == [
        None, None, None, None,
    ]


def test_the_current_process_runs_a_row_written_after_it_started():
    assert session_processes_running([_row(os.getpid())]) == [True]


def test_a_row_written_before_the_process_started_is_a_reused_pid():
    """This process started after a row dated a year ago: the id was reused."""
    old = datetime.now(timezone.utc) - timedelta(days=365)
    assert session_processes_running([_row(os.getpid(), old)]) == [False]


def test_a_dead_pid_does_not_run_the_session():
    assert session_processes_running([_row(999_999_999)]) == [False]


def test_a_probe_error_is_unknown_never_a_close():
    """A liveness probe that raises (the Windows SystemError quirk) gives an
    unknown answer, so the sweeper leaves the session open."""
    with patch(
        "jacked.service.process.process_liveness",
        side_effect=SystemError("returned a result with an exception set"),
    ):
        assert session_processes_running([_row(12345)]) == [None]
