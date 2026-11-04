"""A wall-clock step never closes a live session; a real pid reuse is still found (CRED-4).

The ownership check compared a tracker row's wall-clock ``detected_at`` with
a process start time read later. A clock step between the two (a resumed
virtual machine, an NTP correction) moves the start of a LIVE process past
the row time, and the check called it a reused id: the sweeper closed the
row and the account left the holder set while its session held the token.

The verdict now uses a clock-independent process token that the hook
records with the row (Linux: boot id and start in clock ticks since boot;
Windows: the kernel's creation time; other systems: the start that ps
prints from the kernel, in UTC). Equal token: the same process. Different
token: a newer process reuses the id. A row with no token can only be
confirmed by an earlier start; a later start is unknown (fail closed).

No test here reads the real process table except where it says so.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

import jacked.process_identity as pi
import jacked.service.instance_storage  # noqa: F401 - imported before sys.platform is patched
import jacked.session_ancestry as anc
from jacked.api import credential_helpers as ch
from jacked.web.database import Database

STEP = 6 * 3600.0  # the wall clock jumped six hours forward after the row


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "clock.db"))
    yield database
    database.close()


def _session(db, session_id, account_id, pid, token=None, idle_hours=9.0):
    db.record_session_account(session_id, account_id=account_id, pid=pid,
                              pid_token=token, detection_method="session_start")
    seen = (datetime.now(timezone.utc) - timedelta(hours=idle_hours)).isoformat()
    with db._writer() as conn:
        conn.execute(
            "UPDATE session_accounts SET detected_at = ?, last_activity_at = ? "
            "WHERE session_id = ?", (seen, seen, session_id),
        )


def _table(monkeypatch, alive, starts, tokens):
    monkeypatch.setattr("jacked.service.process.process_liveness", alive.get)
    monkeypatch.setattr(anc, "process_starts", lambda pids: {
        p: starts[p] for p in pids if p in starts})
    monkeypatch.setattr(anc, "process_tokens", lambda pids: {
        p: tokens[p] for p in pids if p in tokens})
    monkeypatch.setattr(anc, "process_start", lambda pid: starts.get(pid))
    monkeypatch.setattr(anc, "process_token", lambda pid: tokens.get(pid))


def _row(pid, token=None, written=None):
    written = written or datetime.now(timezone.utc) - timedelta(hours=1)
    return {"pid": pid, "pid_token": token, "detected_at": written.isoformat()}


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------


def test_a_clock_step_does_not_end_a_live_tokened_row(monkeypatch):
    """After the step the live process reads as started after the row."""
    _table(monkeypatch, {4100: True}, {4100: time.time() + STEP}, {4100: "tok-a"})
    assert anc.session_processes_running([_row(4100, "tok-a")]) == [True]


def test_a_real_pid_reuse_is_still_found(monkeypatch):
    _table(monkeypatch, {4100: True}, {4100: time.time() - 60}, {4100: "tok-new"})
    assert anc.session_processes_running([_row(4100, "tok-a")]) == [False]


def test_a_tokenless_row_with_a_later_start_is_unknown_never_closed(monkeypatch):
    """No token: a clock step and a reuse look the same. Fail closed."""
    _table(monkeypatch, {4100: True}, {4100: time.time() + STEP}, {})
    assert anc.session_processes_running([_row(4100)]) == [None]


def test_a_tokenless_row_with_an_earlier_start_is_running(monkeypatch):
    _table(monkeypatch, {4100: True}, {4100: 1.0}, {})
    assert anc.session_processes_running([_row(4100)]) == [True]


def test_an_unreadable_token_falls_back_to_the_start(monkeypatch):
    _table(monkeypatch, {4100: True, 4200: True},
           {4100: 1.0, 4200: time.time() + STEP}, {})
    rows = [_row(4100, "tok-a"), _row(4200, "tok-b")]
    assert anc.session_processes_running(rows) == [True, None]


def test_tokens_and_starts_are_read_in_batches(monkeypatch):
    reads = {"tokens": [], "starts": []}
    monkeypatch.setattr("jacked.service.process.process_liveness", lambda pid: True)
    monkeypatch.setattr(anc, "process_tokens", lambda pids: (
        reads["tokens"].append(sorted(pids)) or {4100: "tok-a", 4200: "tok-b"}))
    monkeypatch.setattr(anc, "process_starts", lambda pids: (
        reads["starts"].append(sorted(pids)) or {4300: 1.0}))
    rows = [_row(4100, "tok-a"), _row(4200, "tok-x"), _row(4300)]
    assert anc.session_processes_running(rows) == [True, False, True]
    assert reads == {"tokens": [[4100, 4200]], "starts": [[4300]]}


# ---------------------------------------------------------------------------
# Every consumer: holder set, sweeper, calling session
# ---------------------------------------------------------------------------


def test_the_holder_set_keeps_a_session_across_a_clock_step(db, monkeypatch):
    _session(db, "stepped", 4, 4100, "tok-a")
    _session(db, "reused", 5, 4200, "tok-b")
    _table(monkeypatch, {4100: True, 4200: True},
           {4100: time.time() + STEP, 4200: time.time() - 60},
           {4100: "tok-a", 4200: "tok-new"})
    assert ch.live_holder_account_ids(db, {"claudeAiOauth": {}}) == {4}


def test_the_sweeper_keeps_a_stepped_session_and_closes_a_reused_one(db, monkeypatch):
    from jacked.api import watchers

    _session(db, "stepped", 4, 4100, "tok-a", idle_hours=2)
    _session(db, "reused", 5, 4200, "tok-b", idle_hours=2)
    _session(db, "legacy", 6, 4300, None, idle_hours=2)
    _table(monkeypatch, {4100: True, 4200: True, 4300: True},
           {4100: time.time() + STEP, 4200: time.time() - 60, 4300: time.time() + STEP},
           {4100: "tok-a", 4200: "tok-new", 4300: "tok-c"})
    sleeps = []

    async def _sleep(_seconds):
        sleeps.append(_seconds)
        if len(sleeps) > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(watchers.asyncio, "sleep", _sleep)
    app = SimpleNamespace(state=SimpleNamespace(db=db, ws_registry=None))
    asyncio.run(watchers.process_alive_sweeper_loop(app, interval=0))
    with db._reader() as conn:
        ended = dict(conn.execute("SELECT session_id, ended_at FROM session_accounts"))
    assert ended["stepped"] is None
    assert ended["reused"] is not None
    assert ended["legacy"] is None


def test_the_calling_session_survives_a_clock_step(db, monkeypatch):
    db.record_session_account("inner", account_id=2, pid=5000, pid_token="tok-i")
    db.record_session_account("outer", account_id=1, pid=4100, pid_token="tok-o")
    monkeypatch.setattr(anc, "ancestor_pids", lambda *a, **k: [5000, 4100])
    _table(monkeypatch, {}, {5000: time.time() + STEP, 4100: 1.0},
           {5000: "tok-i", 4100: "tok-o"})
    assert anc.calling_session_account(db) == 2


def test_the_calling_session_walks_past_a_reused_pid(db, monkeypatch):
    db.record_session_account("crashed", account_id=7, pid=5000, pid_token="tok-old")
    db.record_session_account("real", account_id=1, pid=4100, pid_token="tok-o")
    monkeypatch.setattr(anc, "ancestor_pids", lambda *a, **k: [5000, 4100])
    _table(monkeypatch, {}, {5000: 1.0, 4100: 1.0}, {5000: "tok-new", 4100: "tok-o"})
    assert anc.calling_session_account(db) == 1


# ---------------------------------------------------------------------------
# Reading tokens and starts
# ---------------------------------------------------------------------------


def _proc(monkeypatch, btime, ticks="250"):
    """A fake Linux /proc: one process (4100) and the btime line."""
    fields = ["S", "4100"] + ["0"] * 17 + [ticks]
    real = Path.read_text

    def _read(self, *a, **k):
        # as_posix: on Windows str() of Path("/proc/stat") uses back slashes.
        name = self.as_posix()
        if name == "/proc/stat":
            return f"cpu 1 2 3\nbtime {btime[0]}\n"
        if name == "/proc/4100/stat":
            return "4100 (claude) " + " ".join(fields)
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _read)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(pi, "_on_windows", lambda: False)
    monkeypatch.setattr(pi.os, "sysconf", lambda name: 100, raising=False)
    monkeypatch.setattr(pi.os, "readlink", lambda path: "/usr/bin/claude")
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("no ps on Linux"))


def test_linux_reads_btime_on_every_pass(monkeypatch):
    """btime moves when the wall clock steps; a cached value mixes two frames."""
    btime = [1_000]
    _proc(monkeypatch, btime)
    assert pi.process_starts([4100]) == {4100: 1_002.5}
    btime[0] = 1_000 + int(STEP)
    assert pi.process_starts([4100]) == {4100: 1_002.5 + STEP}


def test_linux_token_ignores_a_clock_step(monkeypatch):
    btime = [1_000]
    _proc(monkeypatch, btime)
    before = pi.process_tokens([4100])
    btime[0] += int(STEP)
    assert pi.process_tokens([4100]) == before == {4100: "linux-boot-ticks:250"}


def test_tokens_are_the_shared_creation_id(monkeypatch):
    """One owner: the update status file and the tracker use the same marker."""
    from jacked.service import update_status

    assert update_status._creation_id is pi.creation_id
    monkeypatch.setattr(pi, "creation_id", {5000: "darwin-start:1.000001"}.get)
    assert pi.process_tokens([5001, 5000, True]) == {5000: "darwin-start:1.000001"}


def test_an_unreadable_identity_gives_no_token(monkeypatch):
    def _boom(pid):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("jacked.service.instance_storage.process_identity", _boom)
    assert pi.process_tokens([5000]) == {}


def test_windows_token_is_the_kernel_creation_time(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "jacked.service.instance_storage.process_identity",
        lambda pid: SimpleNamespace(creation_id=f"windows-filetime:{pid}0000"),
    )
    assert pi.process_tokens([5000]) == {5000: "windows-filetime:50000000"}


@pytest.mark.skipif(os.name != "posix", reason="reads the real POSIX process table")
def test_the_real_token_of_this_process_is_stable():
    """Reads the real process table: two reads of one process agree."""
    first = pi.process_token(os.getpid())
    assert first is not None and first == pi.process_token(os.getpid())
