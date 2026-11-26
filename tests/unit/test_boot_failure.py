"""A crash at import leaves a trace in ~/.claude/jacked-boot-failure.log."""

import os
import runpy
import stat
import sys

import pytest

from jacked import boot_failure
from jacked.boot_failure import DROPPED_LINE, ENTRY_MARK, MAX_BYTES, latest, log_path, record


def test_a_forced_import_error_writes_one_entry_and_re_raises(monkeypatch):
    monkeypatch.setitem(sys.modules, "jacked.cli", None)  # import now raises
    monkeypatch.delitem(sys.modules, "jacked.__main__", raising=False)
    with pytest.raises(ImportError):
        runpy.run_module("jacked.__main__", run_name="__main__")
    text = log_path().read_text(encoding="utf-8")
    assert text.count(ENTRY_MARK) == 1
    assert "ModuleNotFoundError: import of jacked.cli halted" in text
    stamp, last = latest()
    assert stamp.endswith("Z")
    assert last.startswith("ModuleNotFoundError")
    if os.name == "posix":
        assert stat.S_IMODE(log_path().stat().st_mode) == 0o600


def test_the_log_stays_under_its_limit_and_says_what_it_dropped(tmp_path):
    log = tmp_path / "boot.log"
    chunk = "x" * (MAX_BYTES // 3)
    for index in range(6):
        record(f"Traceback\n{chunk}\nImportError: number {index}\n", path=log, now=index)
    text = log.read_text(encoding="utf-8")
    assert len(text) <= MAX_BYTES
    assert text.startswith(DROPPED_LINE)
    assert text.count(DROPPED_LINE) == 1
    assert latest(path=log) == ("1970-01-01T00:00:05Z", "ImportError: number 5")
    assert "number 0" not in text


def test_one_huge_entry_is_cut_to_the_limit(tmp_path):
    log = tmp_path / "boot.log"
    record("y" * (3 * MAX_BYTES), path=log, now=0)
    assert len(log.read_text(encoding="utf-8")) <= MAX_BYTES


def test_no_log_reads_as_no_failure(tmp_path):
    assert latest(path=tmp_path / "missing.log") is None
    assert boot_failure.describe_latest(path=tmp_path / "missing.log") is None


def test_service_status_prints_the_newest_entry(tmp_path, capsys):
    from jacked.cli import _print_start_failure_breaker
    from jacked.service.instance_models import ServicePaths

    record("Traceback\nOSError: [WinError 10106] provider\n", now=0)
    _print_start_failure_breaker(ServicePaths.in_directory(tmp_path / "state"))
    out = " ".join(capsys.readouterr().out.split())
    assert "Last boot failure: OSError: [WinError 10106] provider" in out
    assert "1970-01-01T00:00:00Z" in out


def test_a_failure_then_an_ok_start_then_a_stop_shows_nothing(tmp_path):
    """A clean stop deletes the manifest; the OK line still hides the failure."""
    record("Traceback\nImportError: old\n", now=500)
    assert "ImportError: old" in boot_failure.describe_latest()
    boot_failure.record_started(now=600)
    # The service stops cleanly: nothing else changes in the log.
    assert boot_failure.describe_latest() is None
    record("Traceback\nImportError: new\n", now=2000)
    assert "ImportError: new" in boot_failure.describe_latest()


def test_an_ok_start_adds_a_line_only_after_a_failure(tmp_path):
    log = tmp_path / "boot.log"
    boot_failure.record_started(path=log, now=1)
    assert not log.exists()
    record("Traceback\nImportError: x\n", path=log, now=2)
    boot_failure.record_started(path=log, now=3)
    boot_failure.record_started(path=log, now=4)
    assert log.read_text(encoding="utf-8").count(boot_failure.OK_MARK) == 1


def test_the_limit_counts_utf8_bytes(tmp_path):
    log = tmp_path / "boot.log"
    chunk = "Ж" * (MAX_BYTES // 6)  # two bytes each
    for index in range(4):
        record(chunk, path=log, now=index)
    assert len(log.read_bytes()) <= MAX_BYTES
    record("Ж" * MAX_BYTES, path=log, now=9)
    data = log.read_bytes()
    assert len(data) <= MAX_BYTES
    data.decode("utf-8")  # the cut never splits a character


def test_a_failed_write_says_so_on_stderr_and_raises(tmp_path, monkeypatch, capsys):
    def refuse(*args, **kwargs):
        raise PermissionError("refused")

    monkeypatch.setattr("jacked.atomic_io.atomic_write_text", refuse)
    with pytest.raises(PermissionError):
        record("Traceback\nImportError: x\n", path=tmp_path / "boot.log", now=0)
    assert "could not write the boot-failure log" in capsys.readouterr().err


def test_on_windows_the_log_gets_the_private_acl_best_effort(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(boot_failure, "_IS_WINDOWS", True)
    monkeypatch.setattr(
        "jacked.service.windows_security.secure_windows_path", lambda path: calls.append(path)
    )
    record("Traceback\nImportError: x\n", path=tmp_path / "boot.log", now=0)
    assert calls == [tmp_path / "boot.log"]

    def fail(path):
        raise OSError("no ACL")

    monkeypatch.setattr("jacked.service.windows_security.secure_windows_path", fail)
    record("Traceback\nImportError: y\n", path=tmp_path / "boot.log", now=1)
    assert latest(path=tmp_path / "boot.log")[1] == "ImportError: y"
    assert "could not set the private ACL" in capsys.readouterr().err


def test_status_reads_the_log_once(tmp_path, monkeypatch):
    """A log removed after the first read must not break `service status`."""
    log = tmp_path / "boot.log"
    record("Traceback\nImportError: x\n", path=log, now=0)
    real_read = boot_failure._read
    calls = []

    def read_then_remove(path):
        calls.append(path)
        found = real_read(path)
        path.unlink(missing_ok=True)
        return found

    monkeypatch.setattr(boot_failure, "_read", read_then_remove)
    assert boot_failure.describe_latest(path=log).startswith("ImportError: x (at 1970")
    assert len(calls) == 1
