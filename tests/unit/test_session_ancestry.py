"""calling_session_account: the nearest tracked session decides, a reused pid
is skipped, and nothing in the lookup can fail the command that asks.

No test here reads the real process table: ancestry, start times, /proc and
ps are all replaced.
"""

import subprocess
import time
from pathlib import Path

import pytest

import jacked.session_ancestry as anc
from jacked.web.database import Database

EARLY = 1.0  # a process that started long before any tracker row


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "jacked.db"))
    yield database
    database.close()


def _track(db, session_id, pid, account_id=None):
    db.record_session_account(
        session_id, account_id=account_id,
        email=None if account_id is None else f"{session_id}@x.com",
        detection_method="session_start", pid=pid,
    )


def _ancestry(monkeypatch, pids, starts):
    monkeypatch.setattr(anc, "ancestor_pids", lambda *a, **k: list(pids))
    monkeypatch.setattr(anc, "process_start", lambda pid: starts.get(pid))


def test_the_nearest_tracked_session_decides(db, monkeypatch):
    _track(db, "outer", 4100, account_id=1)
    _track(db, "inner", 5000, account_id=2)
    _ancestry(monkeypatch, [5100, 5000, 4100], {5000: EARLY, 4100: EARLY})
    assert anc.calling_session_account(db) == 2


def test_a_nested_session_with_no_account_claims_nothing(db, monkeypatch):
    """A session started inside a session on account 1 has no account yet:
    the answer is unknown, never the outer session's account."""
    _track(db, "outer", 4100, account_id=1)
    _track(db, "inner", 5000)
    _ancestry(monkeypatch, [5100, 5000, 4100], {5000: EARLY, 4100: EARLY})
    assert anc.calling_session_account(db) is None


def test_a_reused_pid_is_skipped(db, monkeypatch):
    """A crashed session left an open row for pid 5000. The process now at
    5000 started after that row was written, so the walk moves past it."""
    _track(db, "crashed", 5000, account_id=7)
    _track(db, "real", 4100, account_id=1)
    _ancestry(monkeypatch, [5000, 4100], {5000: time.time() + 60, 4100: EARLY})
    assert anc.calling_session_account(db) == 1


def test_an_unverifiable_nearest_session_claims_nothing(db, monkeypatch):
    _track(db, "s", 5000, account_id=7)
    _track(db, "outer", 4100, account_id=1)
    _ancestry(monkeypatch, [5000, 4100], {4100: EARLY})  # 5000's start is unknown
    assert anc.calling_session_account(db) is None


def test_an_ended_session_is_not_a_match(db, monkeypatch):
    _track(db, "old", 5000, account_id=7)
    db.end_session_account("old")
    _ancestry(monkeypatch, [5000], {5000: EARLY})
    assert anc.calling_session_account(db) is None


def test_a_lookup_error_never_fails_the_caller(db, monkeypatch):
    def _boom(*_a, **_k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(anc, "ancestor_pids", _boom)
    assert anc.calling_session_account(db) is None


def test_an_unreadable_proc_entry_never_raises(monkeypatch):
    """With hidepid, /proc/<pid>/stat of a root-owned ancestor raises
    PermissionError, which Path.exists does not swallow."""
    real = Path.read_text

    def _read(self, *a, **k):
        if str(self).startswith("/proc/"):
            raise PermissionError(13, "Permission denied")
        return real(self, *a, **k)

    monkeypatch.setattr(Path, "read_text", _read)
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 1, stdout="", stderr=""),
    )
    assert anc._parent_of(4242) is None
    assert anc.process_start(4242) is None


def test_ps_answers_without_proc(monkeypatch):
    """macOS has no /proc: the parent and the start time come from ps."""
    monkeypatch.setattr(anc, "_proc_stat", lambda pid: None)
    monkeypatch.setattr(anc, "_on_windows", lambda: False)
    answers = {"ppid=": " 4100\n", "pid=,etime=": " 5000  01:00:00\n"}
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, **_k: subprocess.CompletedProcess(cmd, 0, stdout=answers[cmd[2]], stderr=""),
    )
    assert anc._parent_of(5000) == 4100
    assert anc.process_start(5000) == pytest.approx(time.time() - 3600, abs=2)


def test_proc_stat_gives_parent_and_start_on_linux(monkeypatch):
    fields = ["S", "4100"] + ["0"] * 17 + ["250"]  # field 22 (starttime) = 250 ticks
    monkeypatch.setattr(anc, "_proc_stat", lambda pid: fields)
    monkeypatch.setattr(anc, "_on_windows", lambda: False)
    monkeypatch.setattr(anc, "_linux_boot_time", lambda: 1_000.0)
    monkeypatch.setattr(anc.os, "sysconf", lambda name: 100, raising=False)
    assert anc._parent_of(5000) == 4100
    assert anc.process_start(5000) == pytest.approx(1_002.5)
