"""The launch stops its sync thread before the exit-time sync (#158i).

The sync thread polls the per-account file every 30 seconds. When Claude
Code exits, the launch sets a stop event, waits for the thread, and only
then runs the exit-time sync, so one fresh profile request is sent at exit
and no two syncs write the row at the same time (seen on Windows CI, where
the thread started after the exit sync had begun).
"""

from __future__ import annotations

import threading
import time
from unittest import mock

from jacked import launch


def test_the_loop_ends_within_a_second_of_the_stop_event(tmp_path):
    config_dir = tmp_path / "accounts" / "1"
    config_dir.mkdir(parents=True)
    proc = mock.Mock()
    proc.poll.return_value = None  # Claude Code keeps running
    stop = threading.Event()
    synced = []
    with mock.patch.object(launch, "_sync_tokens_from_file", lambda *_a: synced.append(1) or "unchanged"):
        thread = threading.Thread(
            target=launch._token_sync_loop, args=(proc, config_dir, "unused.db", stop), daemon=True
        )
        thread.start()
        time.sleep(0.2)
        assert thread.is_alive()
        stop.set()
        thread.join(timeout=3)
    assert not thread.is_alive()
    assert synced == []  # no file: nothing synced, and no sync at stop


def test_a_tick_sleeps_a_second_at_a_time_and_stops_early():
    stop = threading.Event()
    naps = []

    def nap(seconds):
        naps.append(seconds)
        if len(naps) == 2:
            stop.set()

    with mock.patch.object(launch, "_time", mock.Mock(sleep=nap, time=time.time)):
        assert launch._sleep_tick(stop, seconds=30) is True
    assert naps == [1, 1]


def test_a_full_tick_without_a_stop_reports_false():
    stop = threading.Event()
    with mock.patch.object(launch, "_time", mock.Mock(sleep=lambda _s: None, time=time.time)):
        assert launch._sleep_tick(stop, seconds=3) is False
