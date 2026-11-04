"""A Claude Code session that is idle but alive still holds its refresh token.

``live_holder_account_ids`` once counted a tracked session only while its
last activity was inside ``HOLDER_WINDOW_MINUTES`` (8 hours). A laptop that
slept all night left its sessions idle for more than 8 hours with their
processes still alive, so jacked could rotate their single-use Claude Code
refresh token, and each session asked for /login when it woke.

The holder set is now: activity inside the window, OR a recorded process
that still runs the session (alive, and started before the tracker row was
written, so a reused process id does not count). An unknown answer counts
(fail closed). A row with no process id keeps the time rule. Every consumer
(``cc_rotation_block`` for launch and the window keeper, the refresh loop,
and the dashboard sweeper) reads the same verdict.

No test here reads the real process table except where it says so.
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jacked.process_identity as pi
import jacked.session_ancestry as anc
from jacked.api import credential_helpers as ch
from jacked.web.database import Database

_NO_TOKENS = {"claudeAiOauth": {}}
EARLY = 1.0  # a process that started long before any tracker row


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "holders.db"))
    yield database
    database.close()


def _session(db, session_id, account_id, *, pid=None, idle_hours=9.0, token=None):
    db.record_session_account(
        session_id, account_id=account_id, detection_method="session_start", pid=pid,
        pid_token=token,
    )
    seen = (datetime.now(timezone.utc) - timedelta(hours=idle_hours)).isoformat()
    with db._writer() as conn:
        conn.execute(
            "UPDATE session_accounts SET detected_at = ?, last_activity_at = ? "
            "WHERE session_id = ?",
            (seen, seen, session_id),
        )


class _Processes:
    """A fake process table: liveness and start time per pid, with call logs."""

    def __init__(self, monkeypatch, alive=None, starts=None, tokens=None):
        self.alive = dict(alive or {})
        self.starts = dict(starts or {})
        self.tokens = dict(tokens or {})
        self.probed: list[int] = []
        self.start_reads: list[list[int]] = []
        monkeypatch.setattr("jacked.service.process.process_liveness", self._liveness)
        monkeypatch.setattr(anc, "process_starts", self._process_starts)
        monkeypatch.setattr(anc, "process_tokens", self._process_tokens)

    def _liveness(self, pid):
        self.probed.append(pid)
        return self.alive.get(pid, False)

    def _process_starts(self, pids):
        self.start_reads.append(sorted(pids))
        return {p: self.starts[p] for p in pids if p in self.starts}

    def _process_tokens(self, pids):
        return {p: self.tokens[p] for p in pids if p in self.tokens}


# ---------------------------------------------------------------------------
# The holder set
# ---------------------------------------------------------------------------


def test_an_idle_but_alive_session_is_a_holder(db, monkeypatch):
    _session(db, "slept", 4, pid=4100, idle_hours=9)
    _Processes(monkeypatch, alive={4100: True}, starts={4100: EARLY})
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == {4}


def test_a_dead_pid_with_old_activity_is_not_a_holder(db, monkeypatch):
    _session(db, "gone", 4, pid=4100, idle_hours=9)
    _Processes(monkeypatch, alive={4100: False})
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == set()


def test_a_pid_reused_by_a_newer_process_is_not_a_holder(db, monkeypatch):
    """The process now at 4100 has a different token than the row's process."""
    _session(db, "crashed", 4, pid=4100, idle_hours=9, token="tok-old")
    _Processes(monkeypatch, alive={4100: True}, tokens={4100: "tok-new"})
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == set()


def test_a_tokenless_row_with_a_later_start_fails_closed(db, monkeypatch):
    """With no token, a later start can be a clock step: still a holder."""
    _session(db, "legacy", 4, pid=4100, idle_hours=9)
    _Processes(monkeypatch, alive={4100: True}, starts={4100: time.time() - 60})
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == {4}


def test_a_row_with_no_pid_keeps_the_time_rule(db, monkeypatch):
    _session(db, "old", 4, idle_hours=9)
    _session(db, "recent", 5, idle_hours=1)
    procs = _Processes(monkeypatch)
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == {5}
    assert procs.probed == [] and procs.start_reads == []


def test_recent_activity_holds_even_with_a_dead_pid(db, monkeypatch):
    """The window rule is unchanged: activity inside it is enough."""
    _session(db, "recent", 5, pid=4200, idle_hours=1)
    _Processes(monkeypatch, alive={4200: False})
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == {5}


def test_an_unknown_start_time_fails_closed(db, monkeypatch):
    _session(db, "slept", 4, pid=4100, idle_hours=9)
    _Processes(monkeypatch, alive={4100: True})  # start unreadable
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == {4}


def test_an_indeterminate_liveness_probe_fails_closed(db, monkeypatch):
    _session(db, "slept", 4, pid=4100, idle_hours=9)
    procs = _Processes(monkeypatch, alive={4100: None})
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == {4}
    assert procs.start_reads == []  # only a live pid has its start read


def test_an_idle_running_session_with_no_account_makes_holders_unknown(db, monkeypatch):
    _session(db, "slept", None, pid=4100, idle_hours=9)
    _Processes(monkeypatch, alive={4100: True}, starts={4100: EARLY})
    assert ch.live_holder_account_ids(db, _NO_TOKENS) is None


def test_the_check_is_batched(db, monkeypatch):
    """One probe per pid and ONE start-time read for all live pids."""
    _session(db, "a", 4, pid=4100)
    _session(db, "b", 5, pid=4200)
    _session(db, "c", 6, pid=4300)
    procs = _Processes(
        monkeypatch, alive={4100: True, 4200: True, 4300: False},
        starts={4100: EARLY, 4200: EARLY},
    )
    assert ch.live_holder_account_ids(db, _NO_TOKENS) == {4, 5}
    assert sorted(procs.probed) == [4100, 4200, 4300]
    assert procs.start_reads == [[4100, 4200]]


# ---------------------------------------------------------------------------
# Every consumer sees the same holder set
# ---------------------------------------------------------------------------


def test_cc_rotation_block_refuses_an_idle_alive_session(db, monkeypatch):
    """cc_rotation_block is the gate of launch and of the window keeper."""
    _session(db, "slept", 4, pid=4100)
    _session(db, "gone", 6, pid=4300)
    _Processes(monkeypatch, alive={4100: True}, starts={4100: EARLY})
    monkeypatch.setattr(ch, "read_active_account_id", lambda: 1)
    monkeypatch.setattr(ch, "read_live_credentials", lambda: _NO_TOKENS)
    assert ch.cc_rotation_block(4, db) == "live_session_holds"
    assert ch.cc_rotation_block(6, db) is None


def test_launch_and_keeper_use_the_shared_gate():
    """Both consumers decide through cc_rotation_block, so they read the same
    holder set as the refresh loop."""
    import inspect

    import jacked.api.keeper.auth_ladder as ladder
    import jacked.launch as launch

    assert "cc_rotation_block(account_id, db)" in inspect.getsource(launch)
    assert "cc_rotation_block, account_id, db" in inspect.getsource(ladder)


def test_the_refresh_loop_skips_an_idle_alive_session(monkeypatch):
    from jacked.web import auth

    now = int(time.time())
    accounts = [
        {"id": i, "refresh_token": None, "expires_at": now + 3600,
         "cc_access_token": f"cc_at_{i}", "cc_refresh_token": f"cc_rt_{i}",
         "cc_expires_at": now + 120}
        for i in (1, 2, 3)
    ]
    stale = (datetime.now(timezone.utc) - timedelta(hours=9)).isoformat()
    mock_db = MagicMock()
    mock_db.list_accounts.return_value = accounts
    mock_db.get_active_sessions.return_value = []
    mock_db.get_stale_open_sessions.return_value = [
        {"session_id": "slept", "account_id": 2, "pid": 4100,
         "detected_at": stale, "last_activity_at": stale},
    ]
    _Processes(monkeypatch, alive={4100: True}, starts={4100: EARLY})
    refresh_cc = AsyncMock(return_value=True)
    live = {"claudeAiOauth": {"accessToken": "x", "refreshToken": "y"}}
    with (
        patch("jacked.api.credential_helpers.read_active_account_id", return_value=1),
        patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store"),
        patch("jacked.api.credential_helpers.read_live_credentials", return_value=live),
        patch("jacked.web.auth.refresh_cc_token", refresh_cc),
    ):
        asyncio.run(auth.refresh_all_expiring_tokens(db=mock_db))

    assert [c.args[0] for c in refresh_cc.call_args_list] == [3]
    mock_db.get_stale_open_sessions.assert_called_with(
        staleness_minutes=ch.HOLDER_WINDOW_MINUTES
    )


def test_the_sweeper_uses_the_same_verdict(db, monkeypatch):
    """Alive and same process: heartbeat. Reused pid: closed. Unknown: left alone."""
    from jacked.api import watchers

    _session(db, "alive", 4, pid=4100, idle_hours=2)
    _session(db, "reused", 5, pid=4200, idle_hours=2, token="tok-old")
    _session(db, "unknown", 6, pid=4300, idle_hours=2)
    _Processes(
        monkeypatch, alive={4100: True, 4200: True, 4300: None},
        starts={4100: EARLY}, tokens={4200: "tok-new"},
    )
    sleeps = []

    async def _sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(watchers.asyncio, "sleep", _sleep)
    app = SimpleNamespace(state=SimpleNamespace(db=db, ws_registry=None))
    asyncio.run(watchers.process_alive_sweeper_loop(app, interval=0))

    with db._reader() as conn:
        rows = {
            sid: (ended, seen)
            for sid, ended, seen in conn.execute(
                "SELECT session_id, ended_at, last_activity_at FROM session_accounts"
            )
        }
    recent = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    assert rows["alive"][0] is None and rows["alive"][1] > recent
    assert rows["reused"][0] is not None
    assert rows["unknown"][0] is None and rows["unknown"][1] < recent


def test_the_sweeper_sends_an_unusable_pid_to_the_no_pid_heuristic(db, monkeypatch):
    """A row whose pid is 1 (an orphaned hook) has no usable process id: the
    "any claude running?" heuristic decides it, it is never left stale."""
    from jacked.api import watchers

    _session(db, "orphan", 4, pid=1, idle_hours=2)
    procs = _Processes(monkeypatch)
    monkeypatch.setattr(watchers, "_any_claude_process_alive", lambda: True)
    sleeps = []

    async def _sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(watchers.asyncio, "sleep", _sleep)
    app = SimpleNamespace(state=SimpleNamespace(db=db, ws_registry=None))
    asyncio.run(watchers.process_alive_sweeper_loop(app, interval=0))

    assert procs.probed == []
    assert any(s["session_id"] == "orphan" for s in db.get_active_sessions())


# ---------------------------------------------------------------------------
# Start times: cheap, batched, cross-platform
# ---------------------------------------------------------------------------


def test_ps_reads_every_start_in_one_run(monkeypatch):
    """macOS has no /proc: one ps run answers every pid."""
    monkeypatch.setattr(pi, "_on_windows", lambda: False)
    monkeypatch.setattr(pi, "_proc_stat", lambda pid: None)
    calls = []

    def _run(cmd, **_k):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout="  5000    01:00:00\n 5001 2-00:00:00\n", stderr=""
        )

    monkeypatch.setattr(subprocess, "run", _run)
    starts = anc.process_starts([5001, 5000, 5002])
    assert calls == [["ps", "-o", "pid=,etime=", "-p", "5000,5001,5002"]]
    assert starts[5000] == pytest.approx(time.time() - 3600, abs=2)
    assert starts[5001] == pytest.approx(time.time() - 172800, abs=2)
    assert 5002 not in starts


def test_proc_answers_linux_with_no_subprocess(monkeypatch):
    fields = ["S", "4100"] + ["0"] * 17 + ["250"]  # field 22 (starttime) = 250 ticks
    monkeypatch.setattr(pi, "_on_windows", lambda: False)
    monkeypatch.setattr(pi, "_proc_stat", lambda pid: fields)
    monkeypatch.setattr(pi, "_linux_boot_time", lambda: 1_000.0)
    monkeypatch.setattr(pi.os, "sysconf", lambda name: 100, raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no ps on Linux"))
    assert anc.process_starts([5000, 5001]) == {5000: 1_002.5, 5001: 1_002.5}


def test_windows_asks_the_kernel_for_each_pid(monkeypatch):
    monkeypatch.setattr(pi, "_on_windows", lambda: True)
    monkeypatch.setattr(pi, "_windows_process_start", {5000: 42.0}.get)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no ps on Windows"))
    assert anc.process_starts([5000, 5001]) == {5000: 42.0}


def test_a_windows_probe_error_is_an_unknown_start(monkeypatch):
    def _boom(pid):
        raise OSError("access denied")

    monkeypatch.setattr(pi, "_on_windows", lambda: True)
    monkeypatch.setattr(pi, "_windows_process_start", _boom)
    assert anc.process_starts([5000]) == {}


class _Fn:
    """A ctypes function stand-in that accepts argtypes/restype settings."""

    def __init__(self, impl):
        self.impl = impl

    def __call__(self, *args):
        return self.impl(*args)


def _fake_kernel32(monkeypatch, *, handle=77, created=None, times_ok=True):
    closed = []

    def _times(_handle, creation, *_rest):
        if created is not None:
            creation._obj.dwLowDateTime = created & 0xFFFFFFFF
            creation._obj.dwHighDateTime = created >> 32
        return 1 if times_ok else 0

    kernel32 = SimpleNamespace(
        OpenProcess=_Fn(lambda _access, _inherit, _pid: handle),
        GetProcessTimes=_Fn(_times),
        CloseHandle=_Fn(lambda h: closed.append(h) or 1),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False)
    return closed


def test_windows_creation_time_is_converted_to_epoch_seconds(monkeypatch):
    epoch = 1_700_000_000
    closed = _fake_kernel32(
        monkeypatch, created=(epoch + pi._FILETIME_EPOCH_OFFSET) * 10_000_000
    )
    assert pi._windows_process_start(5000) == pytest.approx(epoch)
    assert closed == [77]


def test_windows_unopenable_or_unreadable_process_has_no_start(monkeypatch):
    _fake_kernel32(monkeypatch, handle=0)
    assert pi._windows_process_start(5000) is None
    closed = _fake_kernel32(monkeypatch, times_ok=False)
    assert pi._windows_process_start(5000) is None
    assert closed == [77]
