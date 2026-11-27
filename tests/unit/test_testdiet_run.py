"""Tests for jacked.testdiet.run and jacked.testdiet.record: run, record, seen and the repeat notice.

Each gate is ``sys.executable -c`` with a small script. The script copies a
real sample report to the path that it gets, and exits with a chosen code.
Each repository is a throwaway one under ``tmp_path`` with a hermetic git
config. The evidence files of a gate stay outside the repository, so the
tree does not change for a wrong reason.
"""

from __future__ import annotations

import dataclasses
import gzip
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from jacked.testdiet import globs, history, junit, machine, process, record, reports, run, tree
from jacked.testdiet.model import TreeId
from tests._testdiet import fixture_project, fixture_reports, make_repo
from tests._testdiet import git as _git
from tests._testdiet import hermetic_git as _hermetic_git

POSIX = pytest.mark.skipif(os.name == "nt", reason="needs POSIX signals, FIFOs or file names")

GREEN = fixture_reports("pytest_rerun")[0]          # 2 passed, no failure
RED = fixture_reports("pytest")[0]                  # 1 failed, 1 skipped
RED_OTHER = fixture_reports("pytest_import_error")[0]  # 1 error, a different failing set

# The gate script. Arguments: the exit code, then operations.
#   copy SRC DST   copy a file
#   half SRC DST   write the first half of SRC
#   write PATH     write one byte to PATH
#   mtime PATH NS  set the modification time of PATH
#   argv PATH TEXT write TEXT (an argument that the gate got) to PATH
#   sleep SECONDS  sleep
#   kill           stop the gate with SIGTERM
_SCRIPT = r"""
import os, shutil, sys, time
code = int(sys.argv[1]); ops = sys.argv[2:]; i = 0
while i < len(ops):
    op = ops[i]
    if op == "copy":
        shutil.copyfile(ops[i + 1], ops[i + 2]); i += 3
    elif op == "half":
        data = open(ops[i + 1], "rb").read()
        open(ops[i + 2], "wb").write(data[: len(data) // 2]); i += 3
    elif op == "write":
        open(ops[i + 1], "wb").write(b"x"); i += 2
    elif op == "mtime":
        ns = int(ops[i + 2]); os.utime(ops[i + 1], ns=(ns, ns)); i += 3
    elif op == "argv":
        open(ops[i + 1], "wb").write(ops[i + 2].encode("utf-8")); i += 3
    elif op == "sleep":
        time.sleep(float(ops[i + 1])); i += 2
    elif op == "kill":
        import signal
        os.kill(os.getpid(), signal.SIGTERM); time.sleep(30); i += 1
    else:
        sys.exit("unknown gate operation: " + op)
sys.exit(code)
"""


def gate(code: int, *ops: str) -> list[str]:
    return [sys.executable, "-c", _SCRIPT, str(code), *ops]


def junit_gate(code: int, report: Path) -> list[str]:
    return gate(code, "copy", str(report), "{junit}")


@pytest.fixture
def hermetic_git(tmp_path, monkeypatch):
    return _hermetic_git(tmp_path, monkeypatch)


@pytest.fixture
def repo(hermetic_git):
    if shutil.which("git") is None:
        pytest.skip("this test drives the real git binary")
    path = make_repo(hermetic_git / "repo")
    # The test files of the sample projects, so the pytest IDs have their files.
    for name in ("pytest", "pytest_rerun", "pytest_import_error"):
        shutil.copytree(fixture_project(name) / "tests", path / "tests", dirs_exist_ok=True)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "sample tests")
    return path


@pytest.fixture
def side(tmp_path) -> Path:
    """A folder for evidence files, outside the repository."""
    path = tmp_path / "side"
    path.mkdir()
    return path


def _runs(repo: Path) -> list:
    return list(history.iter_runs(tree.store_dir(repo)))


def _last(repo: Path):
    runs = _runs(repo)
    assert runs, "no record"
    return runs[0]


def _seen(repo: Path, argv, *, inputs=(), any_machine=False, machine_id="unset", folder="."):
    mid = machine.machine_id() if machine_id == "unset" else machine_id
    return record.seen(tree.store_dir(repo), tree.tree_id(repo), tree.command_sha(argv, folder=folder),
                       folder=folder, inputs_sha=globs.inputs_fingerprint(repo, inputs), machine_id=mid,
                       any_machine=any_machine)


def _set_default_branch(repo: Path) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


# --------------------------------------------------------------------------- #
# The exit code
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code", [0, 1, 2, 5])
def test_run_returns_the_exit_code_of_the_gate(repo, code):
    assert run.run_gate(junit_gate(code, GREEN), cwd=repo) == code
    assert _last(repo).exit_code == code


def test_run_records_counts_failing_ids_the_tree_and_the_command_sha(repo):
    argv = junit_gate(1, RED)
    run.run_gate(argv, cwd=repo, kind="full", label="ci-gate", runner="pytest")
    meta = _last(repo)
    assert meta.source == "run" and meta.verdict == "failed"
    assert meta.counts == {"passed": 6, "failed": 1, "error": 0, "skipped": 1, "flaky": 0}
    assert meta.failing == ("tests/test_sample.py::test_fails",)
    assert meta.failing_sig is not None
    assert meta.tree == tree.tree_id(repo) and meta.tree.clean is True
    assert meta.tree_changed is False
    assert meta.command_sha == tree.command_sha(argv, folder=".")
    assert (meta.folder, meta.schema) == (".", 1)
    assert (meta.kind, meta.label, meta.runner, meta.machine) == ("full", "ci-gate", "pytest", "local")
    assert meta.machine_id == machine.machine_id()
    assert meta.wall_seconds is not None and meta.wall_seconds > 0
    data = history.load_tests(tree.store_dir(repo), meta.id)
    assert data is not None and len(data.tests) == 8


def test_the_store_holds_the_sha_of_the_command_and_not_its_text(repo):
    marker = "command-text-marker-4471"
    argv = [*junit_gate(0, GREEN), "argv", os.devnull, marker]
    run.run_gate(argv, cwd=repo)
    store = tree.store_dir(repo)
    for path in (store / history.RUNS).iterdir():
        raw = path.read_bytes()
        if path.name.endswith(".gz"):
            raw = gzip.decompress(raw)
        assert marker.encode() not in raw
        assert b"import os, shutil" not in raw
    assert _last(repo).command_sha == tree.command_sha(argv, folder=".")


def test_run_puts_a_report_path_in_place_of_the_placeholder(repo, side):
    got = side / "got.txt"
    argv = gate(0, "argv", str(got), "--junitxml={junit}", "copy", str(GREEN), "{junit}")
    run.run_gate(argv, cwd=repo)
    text = got.read_text(encoding="utf-8")
    assert text.startswith("--junitxml=") and "{junit}" not in text
    assert text.endswith("junit.xml") and os.path.isabs(text[len("--junitxml="):])
    assert _last(repo).verdict == "passed"


def test_two_runs_with_different_junit_folders_have_one_command_sha(repo, side):
    got = side / "got.txt"
    argv = gate(0, "copy", str(GREEN), "{junit}", "argv", str(got), "{junit}")
    paths = []
    for _ in range(2):
        run.run_gate(argv, cwd=repo)
        paths.append(got.read_text(encoding="utf-8"))
    assert paths[0] != paths[1]
    runs = _runs(repo)
    assert len(runs) == 2 and runs[0].command_sha == runs[1].command_sha


def test_seen_is_true_for_the_same_command_with_a_new_junit_folder(repo):
    argv = junit_gate(0, GREEN)
    assert _seen(repo, argv) is None
    run.run_gate(argv, cwd=repo)
    found = _seen(repo, argv)
    assert found is not None and found.id == _last(repo).id


def test_the_placeholder_folder_is_outside_the_tree_and_is_removed(repo, side):
    got = side / "got.txt"
    run.run_gate(gate(0, "argv", str(got), "{junit}", "copy", str(GREEN), "{junit}"), cwd=repo)
    path = Path(got.read_text(encoding="utf-8"))
    assert not path.exists() and not path.parent.exists()
    assert not path.resolve().is_relative_to(repo.resolve())
    assert _last(repo).tree_changed is False


# --------------------------------------------------------------------------- #
# Reading the report (focus 1)
# --------------------------------------------------------------------------- #

def test_a_gate_that_writes_no_report_at_the_placeholder_is_no_data(repo):
    assert run.run_gate(gate(0, "argv", os.devnull, "{junit}"), cwd=repo) == 0
    meta = _last(repo)
    assert meta.verdict == "no-data" and meta.counts is None


def test_run_without_junit_is_no_data_and_keeps_the_exit_code(repo, capsys):
    assert run.run_gate(gate(0), cwd=repo) == 0
    meta = _last(repo)
    assert meta.verdict == "no-data"
    err = capsys.readouterr().err.splitlines()
    warning = [line for line in err if "has no report" in line]
    assert len(warning) == 1 and "cannot prove a tree" in warning[0]
    assert "{junit}" in warning[0] and "--junit GLOB" in warning[0]
    [closing] = [line for line in err if line.startswith("jacked tests: recorded ")]
    assert closing.startswith(f"jacked tests: recorded {meta.id}, verdict no-data, recorder ")
    assert closing.endswith(" s.")
    assert run.run_gate(gate(4), cwd=repo) == 4
    assert _last(repo).verdict == "failed"


def test_the_no_report_line_comes_before_the_gate_and_only_with_no_report_source(repo, side, capfd):
    marker = side / "ran"
    run.run_gate(gate(0, "argv", str(marker), "x"), cwd=repo)
    err = capfd.readouterr().err
    assert err.index("has no report") < err.index("recorded ")
    for argv, fixed in ((junit_gate(0, GREEN), ()), (gate(0), (str(side / "*.xml"),))):
        run.run_gate(argv, cwd=repo, junit=fixed)
        assert "has no report" not in capfd.readouterr().err


def test_each_recorded_run_prints_one_closing_line_with_its_id_and_verdict(repo, capsys):
    for code, report, verdict in ((0, GREEN, "passed"), (1, RED, "failed"), (0, RED, "conflict")):
        run.run_gate(junit_gate(code, report), cwd=repo)
        meta = _last(repo)
        lines = [line for line in capsys.readouterr().err.splitlines()
                 if line.startswith("jacked tests: recorded ")]
        assert len(lines) == 1 and re.fullmatch(
            rf"jacked tests: recorded {meta.id}, verdict {verdict}, recorder \d+\.\d s\.", lines[0]), lines


def test_run_reads_a_fixed_report_whose_content_changed(repo, side):
    fixed = side / "report.xml"
    size = GREEN.stat().st_size
    fixed.write_bytes(b" " * size)  # an old file of the same size
    old_ns = 1_600_000_000_000_000_000
    os.utime(fixed, ns=(old_ns, old_ns))
    argv = gate(0, "copy", str(GREEN), str(fixed), "mtime", str(fixed), str(old_ns))
    assert run.run_gate(argv, cwd=repo, junit=[str(fixed)]) == 0
    assert fixed.stat().st_size == size and fixed.stat().st_mtime_ns == old_ns
    meta = _last(repo)
    assert meta.verdict == "passed" and meta.counts["passed"] == 2


def test_run_does_not_read_a_fixed_report_that_did_not_change_and_says_so(repo, side, capsys):
    fixed = side / "report.xml"
    shutil.copyfile(GREEN, fixed)
    assert run.run_gate(gate(0), cwd=repo, junit=[str(fixed)]) == 0
    err = capsys.readouterr().err
    assert "jacked tests: " in err and "did not change" in err and "report.xml" in err
    assert _last(repo).verdict == "no-data"


def test_run_with_a_truncated_junit_keeps_the_exit_code_and_is_no_data(repo, capsys):
    assert run.run_gate(gate(0, "half", str(GREEN), "{junit}"), cwd=repo) == 0
    assert _last(repo).verdict == "no-data"
    assert "jacked tests: " in capsys.readouterr().err


@POSIX
def test_a_junit_path_that_is_a_fifo_does_not_block_the_run(repo, side, capsys):
    fifo = side / "report.xml"
    os.mkfifo(fifo)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("code", run.run_gate(gate(3), cwd=repo, junit=[str(fifo)])),
        daemon=True)
    worker.start()
    worker.join(60)
    assert not worker.is_alive(), "the run blocked on a FIFO"
    assert result["code"] == 3
    assert "report.xml" in capsys.readouterr().err


# --------------------------------------------------------------------------- #
# Conflict and strict (focus 2)
# --------------------------------------------------------------------------- #

def test_exit_0_with_failures_in_the_report_is_a_conflict_and_is_printed(repo, capsys):
    assert run.run_gate(junit_gate(0, RED), cwd=repo) == 0
    assert _last(repo).verdict == "conflict"
    lines = [line for line in capsys.readouterr().err.splitlines() if "returned 0" in line]
    assert len(lines) == 1 and "1 failed or error test)" in lines[0] and "error tests" not in lines[0]
    assert "seen does not accept it" in lines[0] and "Give --strict" in lines[0]
    run.run_gate(junit_gate(0, RED), cwd=repo, strict=True)
    lines = [line for line in capsys.readouterr().err.splitlines() if "returned 0" in line]
    assert len(lines) == 1 and "--strict makes the exit code 1" in lines[0]


def test_strict_returns_1_on_a_conflict_and_the_gate_code_in_each_other_case(repo):
    assert run.run_gate(junit_gate(0, RED), cwd=repo, strict=True) == 1
    assert run.run_gate(junit_gate(0, GREEN), cwd=repo, strict=True) == 0
    assert run.run_gate(junit_gate(2, RED), cwd=repo, strict=True) == 2
    assert run.run_gate(gate(0), cwd=repo, strict=True) == 0  # no-data


def _boom(*args, **kwargs):
    raise RuntimeError("recorder broke")


@pytest.mark.parametrize("owner, name", [
    (history, "write_run"), (tree, "tree_state"), (junit, "read_junit"), (tree, "store_dir"),
    (globs, "input_fingerprint"),
], ids=["write_run", "tree_state", "read_junit", "store_dir", "input_fingerprint"])
@pytest.mark.parametrize("code", [0, 7])
def test_a_recorder_error_in_one_step_does_not_change_the_exit_code(repo, monkeypatch, capsys, owner,
                                                                    name, code):
    store = tree.store_dir(repo)
    monkeypatch.setattr(owner, name, _boom)
    assert run.run_gate(junit_gate(code, RED), cwd=repo, inputs=["*.txt"]) == code
    err = capsys.readouterr().err
    assert "failed (RuntimeError)" in err and "Traceback" not in err
    runs = list(history.iter_runs(store))
    if name in ("write_run", "store_dir"):
        assert runs == []
        return
    [meta] = runs
    assert meta.exit_code == code
    if name == "read_junit":
        assert meta.counts is None and meta.verdict == ("failed" if code else "no-data")
    elif name == "tree_state":
        assert meta.tree is None and meta.tree_changed is None
    else:
        assert meta.inputs_sha == record.INPUTS_UNAVAILABLE


def test_strict_keeps_the_gate_code_when_the_store_cannot_be_found(repo, monkeypatch):
    monkeypatch.setattr(tree, "store_dir", _boom)
    assert run.run_gate(junit_gate(0, RED), cwd=repo, strict=True) == 0


def test_a_command_that_cannot_start_returns_127(repo, tmp_path, capsys):
    missing = str(tmp_path / "no-such-program")
    assert run.run_gate([missing, "x"], cwd=repo) == 127
    assert _last(repo).exit_code == 127
    lines = [line for line in capsys.readouterr().err.splitlines() if "cannot start" in line]
    assert len(lines) == 1 and "no-such-program" in lines[0]


def test_a_bare_program_name_that_is_not_found_returns_127_and_one_line(repo, capsys):
    assert run.run_gate(["jacked-no-such-program-4471", "x"], cwd=repo) == 127
    lines = [line for line in capsys.readouterr().err.splitlines() if "cannot start" in line]
    assert len(lines) == 1 and "jacked-no-such-program-4471" in lines[0]


@pytest.mark.skipif(os.name != "nt", reason="PATHEXT and .cmd shims exist only on Windows")
def test_a_cmd_shim_by_its_bare_name_starts_on_windows(repo, tmp_path, monkeypatch):
    shims = tmp_path / "shims"
    shims.mkdir()
    (shims / "jackedshim.cmd").write_bytes(b"@exit /b 7\r\n")
    monkeypatch.setenv("PATH", str(shims) + os.pathsep + os.environ.get("PATH", ""))
    argv = ["jackedshim", "--flag"]
    assert run.run_gate(argv, cwd=repo) == 7
    assert _last(repo).command_sha == tree.command_sha(argv, folder=".")


@POSIX
def test_a_sigterm_to_the_run_records_and_cleans_up(repo, side, tmp_path, monkeypatch):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("a signal handler needs the main thread")
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    ready = side / "ready"
    leaked = []
    # A handler of the test: a SIGTERM outside the run's own window is
    # caught here and fails the test, and never stops the session.
    def caught(signum, frame):
        leaked.append(signum)

    previous = signal.signal(signal.SIGTERM, caught)

    def send_when_ready():
        deadline = time.monotonic() + 60
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        os.kill(os.getpid(), signal.SIGTERM)

    sender = threading.Thread(target=send_when_ready, daemon=True)
    sender.start()
    try:
        argv = gate(0, "copy", str(GREEN), "{junit}", "write", str(ready), "sleep", "20")
        code = run.run_gate(argv, cwd=repo)
        restored = signal.getsignal(signal.SIGTERM)
    finally:
        sender.join(60)
        signal.signal(signal.SIGTERM, previous)
    assert leaked == []
    assert code == 128 + signal.SIGTERM
    runs = _runs(repo)
    assert len(runs) == 1 and runs[0].exit_code == 128 + signal.SIGTERM
    assert sorted(p.name for p in temp.iterdir()) == []
    assert restored is caught  # the run put the handler of the test back


def test_an_interrupt_after_the_gate_keeps_its_exit_code_and_writes_a_record_that_proves_nothing(
        repo, monkeypatch, capsys):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(junit, "read_junit", interrupt)
    assert run.run_gate(junit_gate(0, GREEN), cwd=repo) == 0
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "after the gate stopped" in line]
    assert len(lines) == 1 and "cannot prove a tree" in lines[0]
    [meta] = _runs(repo)
    assert meta.exit_code == 0 and meta.tree_changed is None and meta.counts is None
    assert _seen(repo, junit_gate(0, GREEN)) is None


def test_an_interrupt_before_the_gate_gives_130_and_runs_no_gate(repo, side, monkeypatch, capsys):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    marker = side / "ran"
    monkeypatch.setattr(tree, "tree_state", interrupt)
    assert run.run_gate(gate(0, "write", str(marker)), cwd=repo) == 130
    lines = [line for line in capsys.readouterr().err.splitlines() if "interrupt" in line]
    assert len(lines) == 1 and "before the gate started" in lines[0]
    assert not marker.exists() and _runs(repo) == []


@POSIX
def test_a_child_that_a_signal_stops_returns_128_plus_the_signal(repo):
    assert run.run_gate(gate(0, "kill"), cwd=repo) == 128 + signal.SIGTERM
    assert _last(repo).exit_code == 128 + signal.SIGTERM


def test_an_interrupt_of_the_parent_waits_for_the_child(repo, monkeypatch, capsys):
    # The first wait for the gate raises KeyboardInterrupt, as a Ctrl-C does.
    # The git calls use the same Popen class, so only the gate is changed.
    base = subprocess.Popen
    interrupted = []

    class InterruptOnce(base):
        def wait(self, *args, **kwargs):
            if self.args and self.args[0] == sys.executable and not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return super().wait(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", InterruptOnce)
    assert run.run_gate(gate(3, "sleep", "0.2"), cwd=repo) == 3
    assert interrupted == [True]
    meta = _last(repo)
    assert meta.exit_code == 3 and meta.interrupted is True
    lines = [line for line in capsys.readouterr().err.splitlines() if "interrupt" in line]
    assert len(lines) == 1 and "the user stopped the run" in lines[0]
    run.run_gate(gate(0), cwd=repo)
    assert _last(repo).interrupted is False


def test_run_outside_a_git_repository_records_nothing_and_keeps_the_exit_code(hermetic_git, capsys):
    plain = hermetic_git / "plain"
    plain.mkdir()
    assert run.run_gate(junit_gate(5, GREEN), cwd=plain) == 5
    err = capsys.readouterr().err
    assert len([line for line in err.splitlines() if line.startswith("jacked tests: ")]) == 1
    assert sorted(p.name for p in plain.iterdir()) == []


# --------------------------------------------------------------------------- #
# Tree changes and seen (focus 3)
# --------------------------------------------------------------------------- #

def test_a_gate_that_writes_a_file_into_the_tree_is_named_and_never_seen(repo, capsys):
    argv = [*junit_gate(0, GREEN), "write", str(repo / "made-by-gate.txt")]
    assert run.run_gate(argv, cwd=repo) == 0
    err = capsys.readouterr().err
    assert "made-by-gate.txt" in err and "changed 1 path in the work tree" in err
    lines = err.splitlines()
    path_line = next(i for i, line in enumerate(lines) if "made-by-gate.txt" in line)
    assert ".gitignore" in lines[path_line + 1]
    meta = _last(repo)
    assert meta.tree_changed is True and meta.verdict == "passed"
    found = record.seen(tree.store_dir(repo), meta.tree, meta.command_sha, folder=".", inputs_sha=None,
                        machine_id=machine.machine_id())
    assert found is None
    (repo / "made-by-gate.txt").unlink()
    assert _seen(repo, argv) is None


_OTHER_TREE = TreeId(commit="c" * 40, tree="d" * 40, clean=True)


@pytest.mark.parametrize("field, value", [
    ("source", "record"),
    ("verdict", "conflict"),
    ("verdict", "no-data"),
    ("tree_changed", None),
    ("tree_changed", True),
    ("tree", None),
    ("tree", _OTHER_TREE),
    ("command_sha", "0" * 64),
    ("inputs_sha", "1" * 64),
    ("inputs_sha", "unavailable"),
    ("machine_id", "f" * 32),
    ("machine_id", None),
    ("folder", "sub"),
    ("folder", None),
    ("schema", None),
    ("schema", 2),
])
def test_seen_rejects_a_run_that_differs_in_one_field(repo, tmp_path, field, value):
    argv = junit_gate(0, GREEN)
    run.run_gate(argv, cwd=repo)
    real = _last(repo)
    control, twin = tmp_path / "control", tmp_path / "twin"
    history.write_run(control, real, None)
    history.write_run(twin, dataclasses.replace(real, **{field: value}), None)
    args = dict(folder=".", inputs_sha=real.inputs_sha, machine_id=machine.machine_id())
    current = tree.tree_id(repo)
    assert record.seen(control, current, real.command_sha, **args) is not None
    assert record.seen(twin, current, real.command_sha, **args) is None
    if field == "machine_id":
        assert record.seen(twin, current, real.command_sha, any_machine=True, **args) is not None


def test_seen_is_false_for_a_dirty_tree_whose_head_passed(repo):
    argv = junit_gate(0, GREEN)
    run.run_gate(argv, cwd=repo)
    assert _seen(repo, argv) is not None
    (repo / "README.md").write_bytes(b"changed\n")
    assert _seen(repo, argv) is None


def test_seen_is_false_when_the_gate_command_differs(repo):
    run.run_gate(junit_gate(0, GREEN), cwd=repo)
    assert _seen(repo, [*junit_gate(0, GREEN), "-x"]) is None


def test_seen_is_false_for_a_conflict_and_for_no_data(repo):
    conflict = junit_gate(0, RED)
    run.run_gate(conflict, cwd=repo)
    no_data = gate(0)
    run.run_gate(no_data, cwd=repo)
    assert [m.verdict for m in _runs(repo)] == ["no-data", "conflict"]
    assert _seen(repo, conflict) is None and _seen(repo, no_data) is None


def test_seen_is_false_for_a_green_record_that_was_not_wrapped(repo):
    meta = record.record_files([GREEN], cwd=repo, kind="full", label=None, runner=None,
                               commit="HEAD", exit_code=0, wall_seconds=3.0, machine=None,
                               report_root=None)
    assert meta.source == "record" and meta.verdict == "passed"
    assert meta.tree is not None and meta.tree.tree == tree.tree_id(repo).tree
    stored = _last(repo)
    found = record.seen(tree.store_dir(repo), tree.tree_id(repo), "0" * 64, folder=".", inputs_sha=None,
                        machine_id=machine.machine_id(), any_machine=True)
    assert stored.id == meta.id and found is None


def test_record_without_an_exit_code_is_unknown(repo):
    meta = record.record_files([GREEN], cwd=repo, kind=None, label=None, runner=None, commit=None,
                               exit_code=None, wall_seconds=None, machine=None, report_root=None)
    assert meta.verdict == "unknown" and meta.exit_code is None and meta.wall_seconds is None
    assert _last(repo).verdict == "unknown"


def test_seen_is_false_when_a_declared_input_changed(repo):
    (repo / ".gitignore").write_bytes(b"cache.bin\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore")
    cache = repo / "cache.bin"
    cache.write_bytes(b"one")
    argv = junit_gate(0, GREEN)
    run.run_gate(argv, cwd=repo, inputs=["cache.bin"])
    assert _seen(repo, argv, inputs=["cache.bin"]) is not None
    before_tree = tree.tree_id(repo)
    cache.write_bytes(b"two")
    os.utime(cache, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    assert tree.tree_id(repo) == before_tree  # the ignored file is not in the tree
    assert _seen(repo, argv, inputs=["cache.bin"]) is None


def test_seen_is_false_for_a_record_of_a_different_machine_without_the_flag(repo, monkeypatch):
    monkeypatch.setattr(machine, "machine_id", lambda home=None: "a" * 32)
    argv = junit_gate(0, GREEN)
    run.run_gate(argv, cwd=repo)
    assert _seen(repo, argv, machine_id="a" * 32) is not None
    assert _seen(repo, argv, machine_id="b" * 32) is None
    assert _seen(repo, argv, machine_id="b" * 32, any_machine=True) is not None


def test_seen_is_false_when_this_machine_has_no_identity(repo, monkeypatch):
    argv = junit_gate(0, GREEN)
    monkeypatch.setattr(machine, "machine_id", lambda home=None: None)
    run.run_gate(argv, cwd=repo)
    assert _last(repo).machine_id is None
    assert _seen(repo, argv, machine_id=None) is None
    assert _seen(repo, argv, machine_id="a" * 32) is None


def test_seen_is_true_for_a_second_commit_with_the_same_content(repo):
    argv = junit_gate(0, GREEN)
    run.run_gate(argv, cwd=repo)
    first = tree.tree_id(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "same content")
    second = tree.tree_id(repo)
    assert second.commit != first.commit and second.tree == first.tree
    assert _seen(repo, argv) is not None


def test_record_without_a_commit_has_no_tree(repo):
    meta = record.record_files([GREEN], cwd=repo, kind=None, label=None, runner=None, commit=None,
                               exit_code=0, wall_seconds=None, machine=None, report_root=None)
    assert meta.tree is None and meta.tree_changed is None and meta.on_default_branch is None


def test_record_maps_a_report_of_a_different_root(repo, side):
    # No sample report holds an absolute path, so this one is made here in the
    # vitest form (as tests/unit/test_testdiet_junit.py does).
    report = side / "ci.xml"
    report.write_bytes(b'<testsuites name="vitest tests"><testsuite name="s">'
                       b'<testcase classname="/runner/build/src/a.test.js" name="t" time="0.1"/>'
                       b"</testsuite></testsuites>")
    args = dict(cwd=repo, kind=None, label=None, runner="vitest", commit=None, exit_code=0,
                wall_seconds=None, machine="ci", report_root=None)
    plain = record.record_files([report], **args)
    mapped = record.record_files([report], **{**args, "report_root": "/runner/build"})
    store = tree.store_dir(repo)
    assert [c.file for c in history.load_tests(store, plain.id).tests] == [None]
    assert [c.id for c in history.load_tests(store, mapped.id).tests] == ["src/a.test.js > t"]
    assert [c.file for c in history.load_tests(store, mapped.id).tests] == ["src/a.test.js"]
    assert mapped.machine == "ci" and mapped.machine_id is None


# --------------------------------------------------------------------------- #
# The repeat-failure notice
# --------------------------------------------------------------------------- #

def _notice_lines(err: str) -> list[str]:
    return [line for line in err.splitlines() if "earlier record" in line]


def test_a_red_run_with_a_failing_set_in_two_earlier_records_prints_them(repo, capsys):
    argv = junit_gate(1, RED)
    run.run_gate(argv, cwd=repo)
    run.run_gate(argv, cwd=repo)
    earlier = [m.id for m in _runs(repo)]
    capsys.readouterr()
    run.run_gate(argv, cwd=repo)
    err = capsys.readouterr().err
    lines = _notice_lines(err)
    assert lines and "2 earlier records" in lines[0]
    assert "trees: 1," in lines[0]
    for run_id in earlier:
        assert run_id in err
    assert "tests/test_sample.py::test_fails" in err


def test_a_red_run_with_a_new_failing_set_prints_no_notice(repo, capsys):
    run.run_gate(junit_gate(1, RED), cwd=repo)
    run.run_gate(junit_gate(1, RED), cwd=repo)
    capsys.readouterr()
    run.run_gate(junit_gate(1, RED_OTHER), cwd=repo)
    assert _notice_lines(capsys.readouterr().err) == []
    assert record.repeat_notice(tree.store_dir(repo), _last(repo)) is None


def test_the_notice_counts_default_branch_records_and_gives_no_cause(repo, capsys):
    argv = junit_gate(1, RED)
    run.run_gate(argv, cwd=repo)  # no default branch yet
    _set_default_branch(repo)
    run.run_gate(argv, cwd=repo)
    assert _last(repo).on_default_branch is True
    capsys.readouterr()
    run.run_gate(argv, cwd=repo)
    text = record.repeat_notice(tree.store_dir(repo), _last(repo))
    assert text is not None
    assert "2 earlier records" in text and "on the default branch: 1 (1 not recorded)" in text
    for word in ("cause", "caused", "defect", "fault", "blame", "flaky", "your", "regression"):
        assert not re.search(rf"\b{word}\b", text, flags=re.IGNORECASE), word


# --------------------------------------------------------------------------- #
# What the record holds
# --------------------------------------------------------------------------- #

def test_the_load_and_the_processor_time_are_recorded_or_empty_on_windows(repo):
    run.run_gate(junit_gate(0, GREEN), cwd=repo)
    meta = _last(repo)
    if os.name == "nt":
        assert meta.load_start is None and meta.load_end is None and meta.child_cpu_seconds is None
    else:
        assert meta.load_start is not None and meta.load_start >= 0
        assert meta.load_end is not None and meta.load_end >= 0
        assert meta.child_cpu_seconds is not None and meta.child_cpu_seconds > 0


def test_record_writes_to_no_tracked_file(repo):
    run.run_gate(junit_gate(1, RED), cwd=repo)
    record.record_files([GREEN], cwd=repo, kind=None, label=None, runner=None, commit="HEAD",
                        exit_code=0, wall_seconds=None, machine=None, report_root=None)
    assert _git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all") == ""
    assert tree.store_dir(repo).is_relative_to(tree.common_dir(repo))


def test_the_store_holds_no_failure_text(repo):
    run.run_gate(junit_gate(1, RED), cwd=repo)
    raw = b""
    for path in (tree.store_dir(repo) / history.RUNS).iterdir():
        data = path.read_bytes()
        raw += gzip.decompress(data) if path.name.endswith(".gz") else data
    assert b"tests/test_sample.py::test_fails" in raw
    for text in (b"sample-failure-message", b"sample-skip-reason", b"AssertionError: ",
                 b"a different value"):
        assert text not in raw


def test_a_printed_id_has_no_escape_sequence(repo, side, capsys):
    # A test ID with a C1 control (CSI, U+009B). XML 1.0 cannot carry ESC,
    # so this is the control that a JUnit file can bring to a terminal.
    text = RED.read_bytes().decode("utf-8").replace('name="test_fails"', 'name="test_fails\u009b31m"')
    report = side / "csi.xml"
    report.write_bytes(text.encode("utf-8"))
    argv = junit_gate(1, report)
    run.run_gate(argv, cwd=repo)
    run.run_gate(argv, cwd=repo)
    err = capsys.readouterr().err
    assert "tests/test_sample.py::test_fails31m" in err
    assert "\u009b" not in err and "\x1b" not in err


@POSIX
def test_a_printed_path_has_no_escape_sequence(repo, capsys):
    name = "evil\x1b[31m.txt"
    run.run_gate([*junit_gate(0, GREEN), "write", str(repo / name)], cwd=repo)
    err = capsys.readouterr().err
    assert "evil[31m.txt" in err and "\x1b" not in err


def test_a_label_with_a_newline_at_its_end_is_refused(repo, side):
    got = side / "ran.txt"
    with pytest.raises(ValueError):
        run.run_gate(gate(0, "write", str(got)), cwd=repo, label="gate\n")
    assert not got.exists()
    with pytest.raises(ValueError):
        record.record_files([GREEN], cwd=repo, kind=None, label=None, runner=None, commit=None,
                            exit_code=0, wall_seconds=None, machine="ci\n", report_root=None)
    assert _runs(repo) == []


def test_a_failed_hash_of_the_fixed_reports_reads_no_old_report(repo, side, monkeypatch, capsys):
    fixed = side / "report.xml"
    shutil.copyfile(GREEN, fixed)  # an old green report that this run does not write

    def boom(top, globs):
        raise RuntimeError("glob broke")

    real_matches = reports.matches
    calls = []

    def first_call_fails(top, globs):
        calls.append(1)
        return boom(top, globs) if len(calls) == 1 else real_matches(top, globs)

    monkeypatch.setattr(reports, "matches", first_call_fails)
    assert run.run_gate(gate(0), cwd=repo, junit=[str(fixed)]) == 0
    assert _last(repo).verdict == "no-data"
    assert "not hashed before the run" in capsys.readouterr().err


def test_a_standard_error_that_cannot_be_written_does_not_stop_the_gate(hermetic_git, monkeypatch):
    class Broken:
        def write(self, text):
            raise BrokenPipeError

        def flush(self):
            raise BrokenPipeError

    plain = hermetic_git / "plain"
    plain.mkdir()
    monkeypatch.setattr(sys, "stderr", Broken())
    assert run.run_gate(gate(4), cwd=plain) == 4


def test_a_failed_input_hash_is_unavailable_and_never_seen(repo, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("hash broke")

    argv = junit_gate(0, GREEN)
    real = globs.input_fingerprint
    monkeypatch.setattr(globs, "input_fingerprint", boom)
    run.run_gate(argv, cwd=repo, inputs=["cache.bin"])
    meta = _last(repo)
    assert meta.verdict == "passed" and meta.inputs_sha == record.INPUTS_UNAVAILABLE
    store, current = tree.store_dir(repo), tree.tree_id(repo)
    args = dict(folder=".", machine_id=machine.machine_id())
    # Neither the sentinel nor "no --input" matches it.
    assert record.seen(store, current, meta.command_sha, inputs_sha=record.INPUTS_UNAVAILABLE, **args) is None
    assert record.seen(store, current, meta.command_sha, inputs_sha=None, **args) is None
    monkeypatch.setattr(globs, "input_fingerprint", real)
    run.run_gate(argv, cwd=repo)  # no --input: inputs_sha None
    assert _last(repo).inputs_sha is None


def test_a_store_that_cannot_be_found_says_so(repo, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("store broke")

    monkeypatch.setattr(tree, "store_dir", boom)
    assert run.run_gate(gate(3), cwd=repo) == 3
    err = capsys.readouterr().err
    assert "the history store cannot be found" in err and "not in a git repository" not in err


def test_the_notice_groups_records_by_machine_and_has_no_date_range(repo, capsys):
    argv = junit_gate(1, RED)
    run.run_gate(argv, cwd=repo)
    ci = record.record_files([RED], cwd=repo, kind=None, label=None, runner=None, commit=None,
                             exit_code=1, wall_seconds=None, machine="ci", report_root=None,
                             started="2026-11-01T00:00:00+00:00")
    run.run_gate(argv, cwd=repo)
    text = record.repeat_notice(tree.store_dir(repo), _last(repo))
    lines = text.splitlines()
    assert "2 earlier records" in lines[0] and " to " not in lines[0] and "from" not in lines[0]
    rows = [line.strip() for line in lines[lines.index("the earlier records, by machine:") + 1:-1]]
    assert len(rows) == 2 and lines[-1].startswith('see "A red gate"')
    assert rows[0].startswith(f"ci {ci.id} 2026-11-01T00:00:00+00:00 record")
    assert rows[1].startswith("local ")


class _CaughtTerm:
    """A SIGTERM handler of the test: a signal outside the run's window lands here, never on pytest."""

    def __init__(self):
        self.caught: list[int] = []

    def __call__(self, signum, frame):
        self.caught.append(signum)


@POSIX
def test_a_sigterm_during_the_post_run_steps_keeps_the_code_and_cleans_up(repo, tmp_path, monkeypatch,
                                                                          capsys):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("a signal handler needs the main thread")
    temp = tmp_path / "tmp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    blocked = []

    def slow_read(*args, **kwargs):
        # The report read of a big repository: a SIGTERM comes while it runs.
        blocked.append(True)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(10)
        raise AssertionError("the signal did not stop the recorder")

    monkeypatch.setattr(junit, "read_junit", slow_read)
    caught = _CaughtTerm()
    previous = signal.signal(signal.SIGTERM, caught)
    try:
        started = time.monotonic()
        code = run.run_gate(junit_gate(5, GREEN), cwd=repo)
        elapsed = time.monotonic() - started
        restored = signal.getsignal(signal.SIGTERM)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert blocked == [True] and caught.caught == [] and restored is caught
    assert code == 5 and elapsed < 9
    runs = _runs(repo)
    assert len(runs) == 1 and runs[0].exit_code == 5
    assert sorted(p.name for p in temp.iterdir()) == []
    assert "skips its other steps" in capsys.readouterr().err


@POSIX
def test_a_sigterm_before_the_gate_starts_leaves_no_orphan(repo, side, monkeypatch):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("a signal handler needs the main thread")
    base = subprocess.Popen
    pid_file = side / "pid"

    class TermFirst(base):
        def __init__(self, args, *a, **kw):
            if args and args[0] == sys.executable:
                # The signal comes after the handler is set and before the gate exists.
                os.kill(os.getpid(), signal.SIGTERM)
            super().__init__(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", TermFirst)
    caught = _CaughtTerm()
    previous = signal.signal(signal.SIGTERM, caught)
    try:
        started = time.monotonic()
        code = run.run_gate(gate(0, "argv", str(pid_file), "x", "sleep", "30"), cwd=repo)
        elapsed = time.monotonic() - started
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert caught.caught == []
    assert code == 128 + signal.SIGTERM and elapsed < 25  # the gate did not run its 30 s
    assert _last(repo).exit_code == 128 + signal.SIGTERM


def test_a_signal_goes_to_the_gate_once_for_each_signal_number():
    sent = []

    class Proc:
        def poll(self):
            return None

        def send_signal(self, signum):
            sent.append(signum)

    signals = process.Signals()
    signals._handle(15, None)  # before the gate exists: kept
    assert sent == []
    signals.started(Proc())
    signals._handle(15, None)  # the same signal again: not sent a second time
    signals._handle(1, None)
    assert sent == [15, 1]
    signals.child_done = True
    signals._handle(15, None)  # after the gate stopped, outside the post-run steps: only noted
    assert signals.stopped == 15 and sent == [15, 1]


def test_a_bare_name_only_in_an_install_folder_off_path_does_not_start(repo, tmp_path, monkeypatch, capsys):
    from jacked import findbin

    folder = tmp_path / "tool-bin"
    folder.mkdir()
    name = "jacked-offpath-tool-4471"
    target = folder / (name + (".exe" if os.name == "nt" else ""))
    target.write_bytes(b"#!/bin/sh\nexit 0\n")
    target.chmod(0o755)
    monkeypatch.setenv("UV_TOOL_BIN_DIR", str(folder))
    assert findbin.find_on_path(name) is None
    assert run.run_gate([name], cwd=repo) == 127
    assert name in capsys.readouterr().err


def test_the_repeat_notice_shows_a_record_with_no_label_after_each_real_label(tmp_path):
    from tests._testdiet import make_case, store_run

    store = tmp_path / "store"
    red = [make_case("tests/test_x.py::test_fails", "tests/test_x.py", 0.1, "failed")]
    no_label = store_run(store, "20261120T080000", exit_code=1, tests=red, machine=None)
    ci = store_run(store, "20261121T080000", exit_code=1, tests=red, machine="ci", machine_id=None,
                   source="record", command_sha=None)
    now = store_run(store, "20261122T080000", exit_code=1, tests=red)
    meta = next(m for m in history.iter_runs(store) if m.id == now)
    assert {m.id: m.machine for m in history.iter_runs(store)}[no_label] is None
    notice = record.repeat_notice(store, meta)
    assert notice is not None
    lines = notice.splitlines()
    assert lines[0].startswith("the same failing set is in 2 earlier records")
    records = lines[lines.index("the earlier records, by machine:") + 1:-1]
    assert [line.split()[:2] for line in records] == [["ci", ci], ["(no", "label)"]]
    assert lines[-1].startswith('see "A red gate"')
    assert no_label in records[1] and "None" not in notice


def test_a_test_id_with_a_line_break_prints_on_one_line_in_the_notice_and_in_seen(tmp_path):
    from tests._testdiet import make_case, store_run

    store = tmp_path / "store"
    fake = "tests/test_x.py::test_fails\nthe earlier records, by machine:"
    red = [make_case(fake, "tests/test_x.py", 0.1, "failed")]
    store_run(store, "20261120T080000", exit_code=1, tests=red)
    now = store_run(store, "20261121T080000", exit_code=1, tests=red)
    meta = next(m for m in history.iter_runs(store) if m.id == now)
    lines = record.repeat_notice(store, meta).splitlines()
    assert lines.count("the earlier records, by machine:") == 1
    assert "  tests/test_x.py::test_fails\\nthe earlier records, by machine:" in lines
    shown = dataclasses.replace(meta, started="2026-11-21\nfake line", counts={"passed\nx": 1})
    line = record.seen_line(shown)
    assert "\n" not in line
    assert line == f"passed before: {now} started 2026-11-21\\nfake line, folder ., wall 100.0 s, passed\\nx 1"


def test_the_repeat_notice_of_run_prints_one_line_for_a_test_id_with_u2028(repo, side, capsys):
    text = RED.read_bytes().decode("utf-8").replace('name="test_fails"',
                                                    'name="test_fails&#x2028;jacked tests: forged"')
    report = side / "u2028.xml"
    report.write_bytes(text.encode("utf-8"))
    argv = junit_gate(1, report)
    run.run_gate(argv, cwd=repo)
    capsys.readouterr()
    run.run_gate(argv, cwd=repo)
    err = capsys.readouterr().err
    assert "\u2028" not in err
    forged = [line for line in err.splitlines() if "forged" in line]
    assert len(forged) == 1 and "test_fails\\u2028jacked tests: forged" in forged[0]
    assert all(line.startswith("jacked tests: ") for line in err.splitlines())


@POSIX
def test_a_junit_match_with_a_line_break_in_its_name_prints_on_one_line(repo, side, capsys):
    (side / "r\nx.xml").mkdir()
    assert run.run_gate(gate(0), cwd=repo, junit=[str(side / "*.xml")]) == 0
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "x.xml" in line]
    assert len(lines) == 1 and lines[0].startswith("jacked tests: ") and "r\\nx.xml" in lines[0]


@POSIX
def test_a_changed_path_with_a_line_break_prints_on_one_line(repo, capsys):
    run.run_gate([*junit_gate(0, GREEN), "write", str(repo / "made\nforged.txt")], cwd=repo)
    err = capsys.readouterr().err
    assert all(line.startswith("jacked tests: ") for line in err.splitlines())
    assert "made\\nforged.txt" in err


ROOT_OR_WINDOWS = pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                                     reason="needs POSIX modes and a user that is not root")


@ROOT_OR_WINDOWS
def test_a_store_that_cannot_be_written_gives_one_line_and_keeps_the_code(repo, capsys, caplog):
    store = tree.store_dir(repo)
    runs = store / history.RUNS
    runs.mkdir(parents=True)
    runs.chmod(0o500)
    try:
        with caplog.at_level(logging.DEBUG, logger="jacked.testdiet"):
            assert run.run_gate(junit_gate(6, RED), cwd=repo) == 6
    finally:
        runs.chmod(0o700)
    err = capsys.readouterr().err
    lines = [line for line in err.splitlines() if "not recorded" in line]
    assert len(lines) == 1 and str(store) in lines[0] and "read-only" in lines[0]
    assert "Traceback" not in err and "recorded 2" not in err
    assert not any(r.exc_info for r in caplog.records if r.levelno >= logging.WARNING)
    assert list(runs.iterdir()) == []


def test_a_recorder_step_error_logs_no_traceback_at_warning(repo, monkeypatch, caplog):
    def boom(*args, **kwargs):
        raise RuntimeError("recorder broke")

    monkeypatch.setattr(history, "write_run", boom)
    with caplog.at_level(logging.DEBUG, logger="jacked.testdiet"):
        assert run.run_gate(junit_gate(6, RED), cwd=repo) == 6
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings and not any(r.exc_info for r in warnings)
    assert any(r.exc_info for r in caplog.records if r.levelno == logging.DEBUG)


def test_the_notice_and_history_give_one_summary_of_a_failing_set(tmp_path):
    from jacked.testdiet import views
    from tests._testdiet import make_case, store_run

    store = tmp_path / "store"
    red = [make_case("tests/test_x.py::test_fails", "tests/test_x.py", 0.1, "failed")]
    store_run(store, "20261120T080000", exit_code=1, tests=red)
    store_run(store, "20261121T080000", exit_code=1, tests=red, tree=None)
    now = store_run(store, "20261122T080000", exit_code=1, tests=red)
    meta = next(m for m in history.iter_runs(store) if m.id == now)
    notice = record.repeat_notice(store, meta).splitlines()[0]
    assert "on the default branch: not recorded (2 runs)" in notice and " 0 " not in notice
    assert "trees: 1 (1 not recorded)" in notice
    text = views.render_history(views.history(store))
    assert "trees: 1 (1 not recorded), on the default branch: not recorded (3 runs)" in text
    assert views.recorded_count(None, 1, 1) == "not recorded (1 run)"


@ROOT_OR_WINDOWS
def test_a_work_tree_that_git_cannot_read_gives_one_line_with_the_git_detail(repo, capsys):
    locked = repo / "locked.txt"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        assert run.run_gate(junit_gate(0, GREEN), cwd=repo) == 0
    finally:
        locked.chmod(0o600)
    lines = [line for line in capsys.readouterr().err.splitlines() if "cannot prove a tree" in line]
    assert len(lines) == 1, lines
    assert "git add exited with" in lines[0] and "locked.txt" in lines[0]
    assert _last(repo).tree is None


def test_a_repository_with_no_commit_says_so_in_run(hermetic_git, capsys):
    bare_start = hermetic_git / "fresh"
    bare_start.mkdir()
    subprocess.run(["git", "init", "-q", str(bare_start)], check=True, capture_output=True)
    assert run.run_gate(junit_gate(0, GREEN), cwd=bare_start) == 0
    lines = [line for line in capsys.readouterr().err.splitlines() if "cannot prove a tree" in line]
    assert len(lines) == 1 and "no commit" in lines[0]


def test_the_tree_state_bounds_the_git_detail(repo, monkeypatch):
    real = tree.git

    def failing_add(cwd, *args, env=None, stderr=None, stdin=None):
        if "add" in args:
            stderr.write(b"error: " + b"x" * 5000 + b"\n")
            return 128, b""
        return real(cwd, *args, env=env, stderr=stderr, stdin=stdin)

    monkeypatch.setattr(tree, "git", failing_add)
    found, problem = tree.tree_state(repo)
    assert found is None and "git add exited with 128" in problem and len(problem) < 400


@POSIX
def test_an_ignored_sighup_stays_ignored_for_the_gate_and_is_not_forwarded(repo, side):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("a signal handler needs the main thread")
    got = side / "sighup.txt"
    probe = ("import signal, sys; open(sys.argv[1], 'w').write(str(int(signal.getsignal(signal.SIGHUP))))")
    previous = signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        assert run.run_gate([sys.executable, "-c", probe, str(got)], cwd=repo) == 0
        restored = signal.getsignal(signal.SIGHUP)
        signals = process.Signals()
        signals.install()
        try:
            assert signal.SIGHUP not in signals._old
            assert signal.getsignal(signal.SIGHUP) is signal.SIG_IGN
        finally:
            signals.restore()
    finally:
        signal.signal(signal.SIGHUP, previous)
    assert got.read_text(encoding="utf-8") == str(int(signal.SIG_IGN))
    assert restored is signal.SIG_IGN


# --------------------------------------------------------------------------- the newest run of one identity decides


def _identity_store(tmp_path, *verdict_codes, machine_id=None):
    from tests._testdiet import LOCAL_ID, make_case, store_run

    store = tmp_path / "identity-store"
    green = [make_case("t::a", "t.py", 1.0)]
    ids = []
    for n, (code, mid) in enumerate(verdict_codes):
        ids.append(store_run(store, f"2026112{n}T080000", exit_code=code, tests=green,
                             machine_id=mid or machine_id or LOCAL_ID))
    return store, ids


def test_seen_says_no_when_a_newer_run_of_the_same_identity_did_not_pass(tmp_path):
    from tests._testdiet import COMMAND, LOCAL_ID, TREE_1

    store, (r1, r2) = _identity_store(tmp_path, (0, None), (1, None))
    current = TreeId(commit="c" * 40, tree=TREE_1, clean=True)
    answer = record.seen_answer(store, current, COMMAND, folder=".", inputs_sha=None, machine_id=LOCAL_ID)
    assert answer.hit is None and answer.decider.id == r2
    assert r2 in answer.why and "failed" in answer.why
    assert record.seen(store, current, COMMAND, folder=".", inputs_sha=None, machine_id=LOCAL_ID) is None


def test_seen_says_yes_when_the_newest_run_of_the_identity_passed(tmp_path):
    from tests._testdiet import COMMAND, LOCAL_ID, TREE_1

    store, (r1, r2) = _identity_store(tmp_path, (1, None), (0, None))
    current = TreeId(commit="c" * 40, tree=TREE_1, clean=True)
    found = record.seen(store, current, COMMAND, folder=".", inputs_sha=None, machine_id=LOCAL_ID)
    assert found is not None and found.id == r2


def test_any_machine_lets_the_newest_run_of_each_machine_decide_for_that_machine(tmp_path):
    from tests._testdiet import COMMAND, LOCAL_ID, LOCAL_TWIN_ID, TREE_1

    # The twin machine passed and then failed; this machine passed.
    store, (twin_pass, local_pass, twin_fail) = _identity_store(
        tmp_path, (0, LOCAL_TWIN_ID), (0, LOCAL_ID), (1, LOCAL_TWIN_ID))
    current = TreeId(commit="c" * 40, tree=TREE_1, clean=True)
    args = dict(folder=".", inputs_sha=None, machine_id="9" * 32, any_machine=True)
    assert record.seen(store, current, COMMAND, **args).id == local_pass
    store2, _ = _identity_store(tmp_path / "two", (0, LOCAL_TWIN_ID), (1, LOCAL_TWIN_ID))
    assert record.seen(store2, current, COMMAND, **args) is None


_FLAG_GATE = ("import os, shutil, sys; red = os.path.exists(sys.argv[1]); "
              "shutil.copyfile(sys.argv[3] if red else sys.argv[2], sys.argv[4]); sys.exit(1 if red else 0)")


def test_cli_seen_names_the_newer_run_that_failed(repo, side, monkeypatch):
    from click.testing import CliRunner

    from jacked.cli import main

    flag = side / "red"
    argv = [sys.executable, "-c", _FLAG_GATE, str(flag), str(GREEN), str(RED), "{junit}"]
    assert run.run_gate(argv, cwd=repo) == 0
    flag.write_bytes(b"x")
    assert run.run_gate(argv, cwd=repo) == 1
    newer = _last(repo).id
    monkeypatch.chdir(repo)
    result = CliRunner().invoke(main, ["tests", "seen", "--", *argv], catch_exceptions=False)
    assert result.exit_code == 1
    assert newer in result.stderr and "failed" in result.stderr


def test_the_repeat_notice_ends_with_the_next_action(tmp_path):
    from tests._testdiet import make_case, store_run

    store = tmp_path / "store"
    red = [make_case("tests/test_x.py::test_fails", "tests/test_x.py", 0.1, "failed")]
    store_run(store, "20261120T080000", exit_code=1, tests=red)
    now = store_run(store, "20261121T080000", exit_code=1, tests=red)
    meta = next(m for m in history.iter_runs(store) if m.id == now)
    last = record.repeat_notice(store, meta).splitlines()[-1]
    assert "A red gate" in last and "never clears the branch" in last


def test_the_seen_miss_names_the_nearest_record(repo, side, monkeypatch):
    argv = junit_gate(0, GREEN)
    current = tree.tree_id(repo)
    sha = tree.command_sha(argv, folder=".")
    args = dict(folder=".", inputs_sha=None, machine_id=machine.machine_id())
    store = tree.store_dir(repo)
    assert "no run of this tree" in record.seen_answer(store, current, sha, **args).why
    (repo / "sub").mkdir()
    run.run_gate(argv, cwd=repo / "sub")
    assert "folder sub" in record.seen_answer(store, current, sha, **args).why
    run.run_gate([*argv, "-x"], cwd=repo)
    monkeypatch.setattr(machine, "machine_id", lambda home=None: "a" * 32)
    run.run_gate(argv, cwd=repo)
    why = record.seen_answer(store, current, sha, **args).why
    assert "another machine" in why and "--any-machine" in why
    record.record_files([GREEN], cwd=repo, kind=None, label=None, runner=None, commit="HEAD",
                        exit_code=0, wall_seconds=None, machine="ci", report_root=None)
    other = tree.command_sha(["other"], folder=".")
    why = record.seen_answer(store, current, other, **args).why
    assert "other commands" in why


def test_the_repeat_notice_shows_at_most_default_top_rows_and_counts_the_rest(tmp_path):
    from jacked.testdiet.model import DEFAULT_TOP
    from tests._testdiet import make_case, store_run

    store = tmp_path / "store"
    red = [make_case(f"tests/test_x.py::test_{n:02d}", "tests/test_x.py", 0.1, "failed")
           for n in range(DEFAULT_TOP + 3)]
    for day in range(DEFAULT_TOP + 5):
        store_run(store, f"202611{day + 1:02d}T080000", exit_code=1, tests=red)
    now = store_run(store, "20261130T080000", exit_code=1, tests=red)
    meta = next(m for m in history.iter_runs(store) if m.id == now)
    lines = record.repeat_notice(store, meta).splitlines()
    assert f"in {DEFAULT_TOP + 5} earlier records" in lines[0]
    ids = [line for line in lines if line.startswith("  tests/test_x.py::")]
    rows = [line for line in lines if line.startswith("  local ")]
    assert len(ids) == DEFAULT_TOP and len(rows) == DEFAULT_TOP
    assert ("  3 more failing tests are not shown. jacked tests history --json gives each ID of the set."
            in lines)
    assert ("  5 more earlier records are not shown. jacked tests history --machine LABEL shows the "
            "failing sets of each machine." in lines)
    assert lines[-1].startswith('see "A red gate"')


def test_a_gate_that_changes_an_input_file_cannot_prove_a_tree(repo, capsys):
    (repo / ".gitignore").write_bytes(b"data/\n")
    _git(repo, "add", ".gitignore")
    _git(repo, "commit", "-q", "-m", "ignore data")
    (repo / "data").mkdir()
    (repo / "data" / "in.txt").write_bytes(b"before")
    argv = [*junit_gate(0, GREEN), "write", str(repo / "data" / "in.txt")]
    assert run.run_gate(argv, cwd=repo, inputs=["data/*.txt"]) == 0
    lines = [line for line in capsys.readouterr().err.splitlines() if "--input" in line]
    assert len(lines) == 1 and "cannot prove a tree" in lines[0]
    meta = _last(repo)
    assert meta.inputs_changed is True and meta.verdict == "passed"
    assert record.seen_answer(tree.store_dir(repo), tree.tree_id(repo), meta.command_sha, folder=".",
                              inputs_sha=meta.inputs_sha, machine_id=meta.machine_id).hit is None
    run.run_gate(junit_gate(0, GREEN), cwd=repo, inputs=["data/*.txt"])
    assert _last(repo).inputs_changed is False


@pytest.mark.parametrize("owner, name, consequence", [
    (history, "write_run", "The run is not recorded."),
    (junit, "read_junit", "The run has no test data."),
], ids=["write_run", "read_junit"])
def test_a_failed_recorder_step_says_what_it_costs_and_prints_no_traceback(repo, monkeypatch, capsys,
                                                                            owner, name, consequence):
    monkeypatch.setattr(owner, name, _boom)
    assert run.run_gate(junit_gate(0, GREEN), cwd=repo) == 0
    err = capsys.readouterr().err
    [line] = [line for line in err.splitlines() if "failed (RuntimeError)" in line]
    assert line.endswith(consequence) and "Traceback" not in err


def test_the_closing_line_gives_the_time_of_the_recorder(repo, capsys):
    import re as _re

    run.run_gate(junit_gate(0, GREEN), cwd=repo)
    [line] = [line for line in capsys.readouterr().err.splitlines() if line.startswith("jacked tests: recorded ")]
    assert _re.fullmatch(r"jacked tests: recorded \S+, verdict passed, recorder \d+\.\d s\.", line), line


def test_the_fixed_reports_are_listed_one_time_after_the_gate(repo, monkeypatch):
    from jacked.testdiet import globs as _globs

    calls = []
    real = _globs.matches
    monkeypatch.setattr(_globs, "matches", lambda top, pattern: calls.append(pattern) or real(top, pattern))
    reports.read_reports(repo, None, ["a/*.xml", "b/*.xml"], {}, None)
    assert calls == ["a/*.xml", "b/*.xml"]
