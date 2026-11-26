"""Tests for jacked.testdiet.record: run, record, seen and the repeat-failure notice.

Each gate is ``sys.executable -c`` with a small script. The script copies a
real sample report to the path that it gets, and exits with a chosen code.
Each repository is a throwaway one under ``tmp_path`` with a hermetic git
config. The evidence files of a gate stay outside the repository, so the
tree does not change for a wrong reason.
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from jacked.testdiet import history, record, tree
from tests._testdiet import fixture_project, fixture_reports, make_repo

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
    global_config = tmp_path / "gitconfig-global"
    global_config.write_bytes(b"")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR"):
        monkeypatch.delenv(key, raising=False)
    for key, val in {
        "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "test@example.com",
    }.items():
        monkeypatch.setenv(key, val)
    return tmp_path


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


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, f"git {args[0]} failed: {proc.stderr}"
    return proc.stdout.strip()


def _runs(repo: Path) -> list:
    return list(history.iter_runs(tree.store_dir(repo)))


def _last(repo: Path):
    runs = _runs(repo)
    assert runs, "no record"
    return runs[0]


def _seen(repo: Path, argv, *, inputs=(), any_machine=False, machine_id="unset"):
    mid = tree.machine_id() if machine_id == "unset" else machine_id
    return record.seen(tree.store_dir(repo), tree.tree_id(repo), tree.command_sha(argv),
                       inputs_sha=tree.inputs_fingerprint(repo, inputs), machine_id=mid,
                       any_machine=any_machine)


def _set_default_branch(repo: Path) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


# --------------------------------------------------------------------------- #
# The exit code
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("code", [0, 1, 2, 5])
def test_run_returns_the_exit_code_of_the_gate(repo, code):
    assert record.run_gate(junit_gate(code, GREEN), cwd=repo) == code
    assert _last(repo).exit_code == code


def test_run_records_counts_failing_ids_the_tree_and_the_command_sha(repo):
    argv = junit_gate(1, RED)
    record.run_gate(argv, cwd=repo, kind="full", label="ci-gate", runner="pytest")
    meta = _last(repo)
    assert meta.source == "run" and meta.verdict == "failed"
    assert meta.counts == {"passed": 6, "failed": 1, "error": 0, "skipped": 1, "flaky": 0}
    assert meta.failing == ("tests/test_sample.py::test_fails",)
    assert meta.failing_sig is not None
    assert meta.tree == tree.tree_id(repo) and meta.tree.clean is True
    assert meta.tree_changed is False
    assert meta.command_sha == tree.command_sha(argv)
    assert (meta.kind, meta.label, meta.runner, meta.machine) == ("full", "ci-gate", "pytest", "local")
    assert meta.machine_id == tree.machine_id()
    assert meta.wall_seconds is not None and meta.wall_seconds > 0
    data = history.load_tests(tree.store_dir(repo), meta.id)
    assert data is not None and len(data.tests) == 8


def test_the_store_holds_the_sha_of_the_command_and_not_its_text(repo):
    marker = "command-text-marker-4471"
    argv = [*junit_gate(0, GREEN), "argv", os.devnull, marker]
    record.run_gate(argv, cwd=repo)
    store = tree.store_dir(repo)
    for path in (store / history.RUNS).iterdir():
        raw = path.read_bytes()
        if path.name.endswith(".gz"):
            raw = gzip.decompress(raw)
        assert marker.encode() not in raw
        assert b"import os, shutil" not in raw
    assert _last(repo).command_sha == tree.command_sha(argv)


def test_run_puts_a_report_path_in_place_of_the_placeholder(repo, side):
    got = side / "got.txt"
    argv = gate(0, "argv", str(got), "--junitxml={junit}", "copy", str(GREEN), "{junit}")
    record.run_gate(argv, cwd=repo)
    text = got.read_text(encoding="utf-8")
    assert text.startswith("--junitxml=") and "{junit}" not in text
    assert text.endswith("junit.xml") and os.path.isabs(text[len("--junitxml="):])
    assert _last(repo).verdict == "passed"


def test_two_runs_with_different_junit_folders_have_one_command_sha(repo, side):
    got = side / "got.txt"
    argv = gate(0, "copy", str(GREEN), "{junit}", "argv", str(got), "{junit}")
    paths = []
    for _ in range(2):
        record.run_gate(argv, cwd=repo)
        paths.append(got.read_text(encoding="utf-8"))
    assert paths[0] != paths[1]
    runs = _runs(repo)
    assert len(runs) == 2 and runs[0].command_sha == runs[1].command_sha


def test_seen_is_true_for_the_same_command_with_a_new_junit_folder(repo):
    argv = junit_gate(0, GREEN)
    assert _seen(repo, argv) is None
    record.run_gate(argv, cwd=repo)
    found = _seen(repo, argv)
    assert found is not None and found.id == _last(repo).id


def test_the_placeholder_folder_is_outside_the_tree_and_is_removed(repo, side):
    got = side / "got.txt"
    record.run_gate(gate(0, "argv", str(got), "{junit}", "copy", str(GREEN), "{junit}"), cwd=repo)
    path = Path(got.read_text(encoding="utf-8"))
    assert not path.exists() and not path.parent.exists()
    assert not path.resolve().is_relative_to(repo.resolve())
    assert _last(repo).tree_changed is False


# --------------------------------------------------------------------------- #
# Reading the report (focus 1)
# --------------------------------------------------------------------------- #

def test_a_gate_that_writes_no_report_at_the_placeholder_is_no_data(repo):
    assert record.run_gate(gate(0, "argv", os.devnull, "{junit}"), cwd=repo) == 0
    meta = _last(repo)
    assert meta.verdict == "no-data" and meta.counts is None


def test_run_without_junit_is_no_data_and_keeps_the_exit_code(repo):
    assert record.run_gate(gate(0), cwd=repo) == 0
    assert _last(repo).verdict == "no-data"
    assert record.run_gate(gate(4), cwd=repo) == 4
    assert _last(repo).verdict == "failed"


def test_run_reads_a_fixed_report_whose_content_changed(repo, side):
    fixed = side / "report.xml"
    size = GREEN.stat().st_size
    fixed.write_bytes(b" " * size)  # an old file of the same size
    old_ns = 1_600_000_000_000_000_000
    os.utime(fixed, ns=(old_ns, old_ns))
    argv = gate(0, "copy", str(GREEN), str(fixed), "mtime", str(fixed), str(old_ns))
    assert record.run_gate(argv, cwd=repo, junit=[str(fixed)]) == 0
    assert fixed.stat().st_size == size and fixed.stat().st_mtime_ns == old_ns
    meta = _last(repo)
    assert meta.verdict == "passed" and meta.counts["passed"] == 2


def test_run_does_not_read_a_fixed_report_that_did_not_change_and_says_so(repo, side, capsys):
    fixed = side / "report.xml"
    shutil.copyfile(GREEN, fixed)
    assert record.run_gate(gate(0), cwd=repo, junit=[str(fixed)]) == 0
    err = capsys.readouterr().err
    assert "jacked tests: " in err and "did not change" in err and "report.xml" in err
    assert _last(repo).verdict == "no-data"


def test_run_with_a_truncated_junit_keeps_the_exit_code_and_is_no_data(repo, capsys):
    assert record.run_gate(gate(0, "half", str(GREEN), "{junit}"), cwd=repo) == 0
    assert _last(repo).verdict == "no-data"
    assert "jacked tests: " in capsys.readouterr().err


@POSIX
def test_a_junit_path_that_is_a_fifo_does_not_block_the_run(repo, side, capsys):
    fifo = side / "report.xml"
    os.mkfifo(fifo)
    result: dict = {}
    worker = threading.Thread(
        target=lambda: result.setdefault("code", record.run_gate(gate(3), cwd=repo, junit=[str(fifo)])),
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
    assert record.run_gate(junit_gate(0, RED), cwd=repo) == 0
    assert _last(repo).verdict == "conflict"
    lines = [line for line in capsys.readouterr().err.splitlines() if "returned 0" in line]
    assert len(lines) == 1 and "failures" in lines[0]


def test_strict_returns_1_on_a_conflict_and_the_gate_code_in_each_other_case(repo):
    assert record.run_gate(junit_gate(0, RED), cwd=repo, strict=True) == 1
    assert record.run_gate(junit_gate(0, GREEN), cwd=repo, strict=True) == 0
    assert record.run_gate(junit_gate(2, RED), cwd=repo, strict=True) == 2
    assert record.run_gate(gate(0), cwd=repo, strict=True) == 0  # no-data


def test_a_recorder_error_does_not_change_the_exit_code(repo, monkeypatch, capsys):
    def boom(*args, **kwargs):
        raise RuntimeError("recorder broke")

    store = tree.store_dir(repo)
    monkeypatch.setattr(history, "write_run", boom)
    assert record.run_gate(junit_gate(6, RED), cwd=repo) == 6
    monkeypatch.setattr(tree, "tree_id", boom)
    assert record.run_gate(junit_gate(6, RED), cwd=repo) == 6
    monkeypatch.setattr(record.junit, "read_junit", boom)
    monkeypatch.setattr(tree, "store_dir", boom)
    monkeypatch.setattr(tree, "inputs_fingerprint", boom)
    assert record.run_gate(junit_gate(7, RED), cwd=repo, inputs=["*.txt"]) == 7
    assert record.run_gate(junit_gate(0, RED), cwd=repo, strict=True) == 0
    assert "jacked tests: " in capsys.readouterr().err
    assert list(history.iter_runs(store)) == []


def test_a_command_that_cannot_start_returns_127(repo, tmp_path):
    missing = str(tmp_path / "no-such-program")
    assert record.run_gate([missing, "x"], cwd=repo) == 127
    assert _last(repo).exit_code == 127


@POSIX
def test_a_child_that_a_signal_stops_returns_128_plus_the_signal(repo):
    assert record.run_gate(gate(0, "kill"), cwd=repo) == 128 + signal.SIGTERM
    assert _last(repo).exit_code == 128 + signal.SIGTERM


@POSIX
def test_an_interrupt_of_the_parent_waits_for_the_child(repo):
    if threading.current_thread() is not threading.main_thread():
        pytest.skip("SIGINT goes to the main thread")
    timer = threading.Timer(0.5, os.kill, (os.getpid(), signal.SIGINT))
    timer.start()
    try:
        assert record.run_gate(gate(3, "sleep", "2"), cwd=repo) == 3
    finally:
        timer.cancel()
    assert _last(repo).exit_code == 3


def test_run_outside_a_git_repository_records_nothing_and_keeps_the_exit_code(hermetic_git, capsys):
    plain = hermetic_git / "plain"
    plain.mkdir()
    assert record.run_gate(junit_gate(5, GREEN), cwd=plain) == 5
    err = capsys.readouterr().err
    assert len([line for line in err.splitlines() if line.startswith("jacked tests: ")]) == 1
    assert sorted(p.name for p in plain.iterdir()) == []


# --------------------------------------------------------------------------- #
# Tree changes and seen (focus 3)
# --------------------------------------------------------------------------- #

def test_a_gate_that_writes_a_file_into_the_tree_is_named_and_never_seen(repo, capsys):
    argv = [*junit_gate(0, GREEN), "write", str(repo / "made-by-gate.txt")]
    assert record.run_gate(argv, cwd=repo) == 0
    assert "made-by-gate.txt" in capsys.readouterr().err
    meta = _last(repo)
    assert meta.tree_changed is True and meta.verdict == "passed"
    found = record.seen(tree.store_dir(repo), meta.tree, meta.command_sha, inputs_sha=None,
                        machine_id=tree.machine_id())
    assert found is None
    (repo / "made-by-gate.txt").unlink()
    assert _seen(repo, argv) is None


def test_seen_is_false_for_a_dirty_tree_whose_head_passed(repo):
    argv = junit_gate(0, GREEN)
    record.run_gate(argv, cwd=repo)
    assert _seen(repo, argv) is not None
    (repo / "README.md").write_bytes(b"changed\n")
    assert _seen(repo, argv) is None


def test_seen_is_false_when_the_gate_command_differs(repo):
    record.run_gate(junit_gate(0, GREEN), cwd=repo)
    assert _seen(repo, [*junit_gate(0, GREEN), "-x"]) is None


def test_seen_is_false_for_a_conflict_and_for_no_data(repo):
    conflict = junit_gate(0, RED)
    record.run_gate(conflict, cwd=repo)
    no_data = gate(0)
    record.run_gate(no_data, cwd=repo)
    assert [m.verdict for m in _runs(repo)] == ["no-data", "conflict"]
    assert _seen(repo, conflict) is None and _seen(repo, no_data) is None


def test_seen_is_false_for_a_green_record_that_was_not_wrapped(repo):
    meta = record.record_files([GREEN], cwd=repo, kind="full", label=None, runner=None,
                               commit="HEAD", exit_code=0, wall_seconds=3.0, machine=None,
                               report_root=None)
    assert meta.source == "record" and meta.verdict == "passed"
    assert meta.tree is not None and meta.tree.tree == tree.tree_id(repo).tree
    stored = _last(repo)
    found = record.seen(tree.store_dir(repo), tree.tree_id(repo), "0" * 64, inputs_sha=None,
                        machine_id=tree.machine_id(), any_machine=True)
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
    record.run_gate(argv, cwd=repo, inputs=["cache.bin"])
    assert _seen(repo, argv, inputs=["cache.bin"]) is not None
    before_tree = tree.tree_id(repo)
    cache.write_bytes(b"two")
    os.utime(cache, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    assert tree.tree_id(repo) == before_tree  # the ignored file is not in the tree
    assert _seen(repo, argv, inputs=["cache.bin"]) is None


def test_seen_is_false_for_a_record_of_a_different_machine_without_the_flag(repo, monkeypatch):
    monkeypatch.setattr(tree, "machine_id", lambda home=None: "a" * 32)
    argv = junit_gate(0, GREEN)
    record.run_gate(argv, cwd=repo)
    assert _seen(repo, argv, machine_id="a" * 32) is not None
    assert _seen(repo, argv, machine_id="b" * 32) is None
    assert _seen(repo, argv, machine_id="b" * 32, any_machine=True) is not None


def test_seen_is_false_when_this_machine_has_no_identity(repo, monkeypatch):
    argv = junit_gate(0, GREEN)
    monkeypatch.setattr(tree, "machine_id", lambda home=None: None)
    record.run_gate(argv, cwd=repo)
    assert _last(repo).machine_id is None
    assert _seen(repo, argv, machine_id=None) is None
    assert _seen(repo, argv, machine_id="a" * 32) is None


def test_seen_is_true_for_a_second_commit_with_the_same_content(repo):
    argv = junit_gate(0, GREEN)
    record.run_gate(argv, cwd=repo)
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
    record.run_gate(argv, cwd=repo)
    record.run_gate(argv, cwd=repo)
    earlier = [m.id for m in _runs(repo)]
    capsys.readouterr()
    record.run_gate(argv, cwd=repo)
    err = capsys.readouterr().err
    lines = _notice_lines(err)
    assert lines and "2 earlier records" in lines[0]
    assert "1 tree" in lines[0]
    for run_id in earlier:
        assert run_id in err
    assert "tests/test_sample.py::test_fails" in err


def test_a_red_run_with_a_new_failing_set_prints_no_notice(repo, capsys):
    record.run_gate(junit_gate(1, RED), cwd=repo)
    record.run_gate(junit_gate(1, RED), cwd=repo)
    capsys.readouterr()
    record.run_gate(junit_gate(1, RED_OTHER), cwd=repo)
    assert _notice_lines(capsys.readouterr().err) == []
    assert record.repeat_notice(tree.store_dir(repo), _last(repo)) is None


def test_the_notice_counts_default_branch_records_and_gives_no_cause(repo, capsys):
    argv = junit_gate(1, RED)
    record.run_gate(argv, cwd=repo)  # no default branch yet
    _set_default_branch(repo)
    record.run_gate(argv, cwd=repo)
    assert _last(repo).on_default_branch is True
    capsys.readouterr()
    record.run_gate(argv, cwd=repo)
    text = record.repeat_notice(tree.store_dir(repo), _last(repo))
    assert text is not None
    assert "2 earlier records" in text and "1 of them on the default branch" in text
    for word in ("cause", "caused", "defect", "fault", "blame", "flaky", "your", "regression"):
        assert not re.search(rf"\b{word}\b", text, flags=re.IGNORECASE), word


# --------------------------------------------------------------------------- #
# What the record holds
# --------------------------------------------------------------------------- #

def test_the_load_and_the_processor_time_are_recorded_or_empty_on_windows(repo):
    record.run_gate(junit_gate(0, GREEN), cwd=repo)
    meta = _last(repo)
    if os.name == "nt":
        assert meta.load_start is None and meta.load_end is None and meta.child_cpu_seconds is None
    else:
        assert meta.load_start is not None and meta.load_start >= 0
        assert meta.load_end is not None and meta.load_end >= 0
        assert meta.child_cpu_seconds is not None and meta.child_cpu_seconds > 0


def test_record_writes_to_no_tracked_file(repo):
    record.run_gate(junit_gate(1, RED), cwd=repo)
    record.record_files([GREEN], cwd=repo, kind=None, label=None, runner=None, commit="HEAD",
                        exit_code=0, wall_seconds=None, machine=None, report_root=None)
    assert _git(repo, "status", "--porcelain", "--ignored", "--untracked-files=all") == ""
    assert tree.store_dir(repo).is_relative_to(tree.common_dir(repo))


def test_the_store_holds_no_failure_text(repo):
    record.run_gate(junit_gate(1, RED), cwd=repo)
    raw = b""
    for path in (tree.store_dir(repo) / history.RUNS).iterdir():
        data = path.read_bytes()
        raw += gzip.decompress(data) if path.name.endswith(".gz") else data
    assert b"tests/test_sample.py::test_fails" in raw
    for text in (b"sample-failure-message", b"sample-skip-reason", b"AssertionError: ",
                 b"a different value"):
        assert text not in raw


@POSIX
def test_a_printed_id_has_no_escape_sequence(repo, side, capsys):
    # A test ID with a C1 control (CSI, U+009B). XML 1.0 cannot carry ESC,
    # so this is the control that a JUnit file can bring to a terminal.
    text = RED.read_bytes().decode("utf-8").replace('name="test_fails"', 'name="test_fails\u009b31m"')
    report = side / "csi.xml"
    report.write_bytes(text.encode("utf-8"))
    argv = [*junit_gate(1, report), "write", str(repo / "evil\x1b[31m.txt")]
    record.run_gate(argv, cwd=repo)
    (repo / "evil\x1b[31m.txt").unlink()
    record.run_gate(argv, cwd=repo)
    err = capsys.readouterr().err
    assert "test_fails31m" in err and "evil[31m.txt" in err
    assert "\x1b" not in err and "\u009b" not in err


def test_a_label_with_a_newline_at_its_end_is_refused(repo, side):
    got = side / "ran.txt"
    with pytest.raises(ValueError):
        record.run_gate(gate(0, "write", str(got)), cwd=repo, label="gate\n")
    assert not got.exists()
    with pytest.raises(ValueError):
        record.record_files([GREEN], cwd=repo, kind=None, label=None, runner=None, commit=None,
                            exit_code=0, wall_seconds=None, machine="ci\n", report_root=None)
    assert _runs(repo) == []
