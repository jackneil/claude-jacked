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


def test_an_entry_older_than_the_live_service_start_is_not_shown(tmp_path):
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    os.utime(manifest, (1000, 1000))
    record("Traceback\nImportError: old\n", now=500)
    assert boot_failure.describe_latest(after_file=manifest) is None
    record("Traceback\nImportError: new\n", now=2000)
    assert "ImportError: new" in boot_failure.describe_latest(after_file=manifest)
    assert "ImportError: new" in boot_failure.describe_latest(after_file=tmp_path / "none")
