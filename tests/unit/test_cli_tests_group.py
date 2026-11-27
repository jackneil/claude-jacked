"""Tests for the ``jacked tests`` command group: run, record and seen."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner
from rich.console import Console

import jacked.cli as cli
from jacked.cli import main
from jacked.testdiet import history, machine, tree
from tests._testdiet import fixture_reports, make_repo

GREEN = fixture_reports("pytest_rerun")[0]
RED = fixture_reports("pytest")[0]
_GATE = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2]); sys.exit(int(sys.argv[3]))"


def gate(report: Path, code: int) -> list[str]:
    return [sys.executable, "-c", _GATE, str(report), "{junit}", str(code)]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        pytest.skip("this test drives the real git binary")
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
    path = make_repo(tmp_path / "repo")
    (path / "sub").mkdir()
    monkeypatch.chdir(path)
    return path


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def test_cli_run_passes_the_exit_code_through(repo):
    result = _invoke("tests", "run", "--kind", "full", "--label", "gate", "--", *gate(RED, 3))
    assert result.exit_code == 3
    meta = next(history.iter_runs(tree.store_dir(repo)))
    assert (meta.exit_code, meta.kind, meta.label, meta.verdict) == (3, "full", "gate", "failed")
    assert _invoke("tests", "run", "--", *gate(GREEN, 0)).exit_code == 0


def test_cli_run_strict_returns_1_on_a_conflict(repo):
    assert _invoke("tests", "run", "--", *gate(RED, 0)).exit_code == 0
    assert _invoke("tests", "run", "--strict", "--", *gate(RED, 0)).exit_code == 1


def test_cli_run_refuses_a_bad_label_and_an_empty_command(repo):
    assert _invoke("tests", "run", "--label", "a b", "--", *gate(GREEN, 0)).exit_code == 2
    assert _invoke("tests", "run", "--").exit_code == 2
    assert list(history.iter_runs(tree.store_dir(repo))) == []


def test_cli_seen_exits_0_after_a_green_run_and_1_before_it(repo):
    argv = gate(GREEN, 0)
    before = _invoke("tests", "seen", "--", *argv)
    assert before.exit_code == 1
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    after = _invoke("tests", "seen", "--", *argv)
    assert after.exit_code == 0
    run_id = next(history.iter_runs(tree.store_dir(repo))).id
    assert run_id in after.stdout
    as_json = _invoke("tests", "seen", "--json", "--", *argv)
    assert as_json.exit_code == 0
    data = json.loads(as_json.stdout)
    assert data["id"] == run_id and data["verdict"] == "passed"
    assert "machine_id" not in data and machine.machine_id() not in as_json.output
    assert json.loads(_invoke("tests", "seen", "--json", "--", *gate(RED, 0)).stdout) is None


def test_cli_seen_matches_only_the_folder_where_the_gate_ran(repo, monkeypatch):
    argv = gate(GREEN, 0)
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    monkeypatch.chdir(repo / "sub")
    assert _invoke("tests", "seen", "--", *argv).exit_code == 1
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    hit = _invoke("tests", "seen", "--", *argv)
    assert hit.exit_code == 0 and "folder sub" in hit.stdout
    monkeypatch.chdir(repo)
    hit = _invoke("tests", "seen", "--", *argv)
    assert hit.exit_code == 0 and "folder ." in hit.stdout
    (repo / "data.bin").write_bytes(b"x")
    monkeypatch.chdir(repo / "sub")
    assert _invoke("tests", "run", "--input", "data.bin", "--", *argv).exit_code == 0
    assert _invoke("tests", "seen", "--input", "data.bin", "--", *argv).exit_code == 0
    monkeypatch.chdir(repo)
    assert _invoke("tests", "seen", "--input", "data.bin", "--", *argv).exit_code == 1


_SUB_ONLY = ("import os, shutil, sys; ok = os.path.basename(os.getcwd()) == 'sub'; "
             "shutil.copyfile(sys.argv[1] if ok else sys.argv[2], sys.argv[3]); sys.exit(0 if ok else 1)")


def test_cli_seen_says_no_at_the_top_for_a_gate_that_passed_only_in_a_sub_folder(repo, monkeypatch):
    argv = [sys.executable, "-c", _SUB_ONLY, str(GREEN), str(RED), "{junit}"]
    monkeypatch.chdir(repo / "sub")
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    assert _invoke("tests", "seen", "--", *argv).exit_code == 0
    monkeypatch.chdir(repo)
    assert _invoke("tests", "seen", "--", *argv).exit_code == 1
    assert _invoke("tests", "run", "--", *argv).exit_code == 1


def test_cli_record_writes_a_record_and_prints_its_id(repo):
    head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          cwd=repo, check=True).stdout.strip()
    result = _invoke("tests", "record", "--junit", str(GREEN), "--exit-code", "0", "--wall", "2.5",
                     "--commit", head, "--machine", "ci", "--kind", "full")
    assert result.exit_code == 0
    meta = next(history.iter_runs(tree.store_dir(repo)))
    assert meta.id in result.stdout
    assert (meta.source, meta.verdict, meta.machine, meta.machine_id) == ("record", "passed", "ci", None)
    assert meta.tree.commit == head and meta.wall_seconds == 2.5
    assert _invoke("tests", "record", "--junit", str(GREEN), "--commit", "no-such-rev").exit_code == 1
    assert _invoke("tests", "record", "--junit", str(GREEN), "--machine", "c i").exit_code == 2


def test_cli_tests_does_not_ask_to_install(repo, monkeypatch):
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(cli.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(cli, "_already_installed", lambda: False)
    buf = io.StringIO()
    monkeypatch.setattr(cli, "console", Console(file=buf, width=100))
    monkeypatch.setattr(cli.click, "confirm",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("prompted")))
    cli._maybe_prompt_first_run(SimpleNamespace(invoked_subcommand="tests"))
    assert buf.getvalue() == ""
    result = _invoke("tests", "seen", "--", *gate(GREEN, 0))
    assert result.exit_code == 1 and buf.getvalue() == ""


def test_cli_record_takes_a_start_time_and_else_uses_the_record_time(repo):
    given = _invoke("tests", "record", "--junit", str(GREEN), "--started", "2026-11-20T08:00:00-05:00")
    assert given.exit_code == 0
    store = tree.store_dir(repo)
    by_id = {m.id: m for m in history.iter_runs(store)}
    assert by_id[given.stdout.split()[0]].started == "2026-11-20T13:00:00+00:00"
    plain = _invoke("tests", "record", "--junit", str(GREEN))
    by_id = {m.id: m for m in history.iter_runs(store)}
    assert by_id[plain.stdout.split()[0]].started[:10] == plain.stdout.split()[0][:4] + "-" \
        + plain.stdout.split()[0][4:6] + "-" + plain.stdout.split()[0][6:8]
    assert _invoke("tests", "record", "--junit", str(GREEN), "--started", "2026-11-20T08:00:00").exit_code == 2
    assert _invoke("tests", "record", "--junit", str(GREEN), "--started", "yesterday").exit_code == 2
    assert "time of the record" in _invoke("tests", "record", "--help").stdout


def test_cli_record_gives_one_line_and_exit_1_for_a_bad_value(repo, monkeypatch):
    from jacked.testdiet import record as _record

    def refuse(*args, **kwargs):
        raise ValueError("the wall time must be 0 or more seconds")

    monkeypatch.setattr(_record, "record_files", refuse)
    result = _invoke("tests", "record", "--junit", str(GREEN))
    assert result.exit_code == 1 and "nothing is recorded" in result.stderr


def test_cli_seen_says_why_when_this_machine_has_no_identity(repo, tmp_path, monkeypatch):
    argv = gate(GREEN, 0)
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    blocked = tmp_path / "blocked-home"
    blocked.mkdir()
    (blocked / ".claude").write_bytes(b"a file where the .claude folder must be")
    monkeypatch.setenv("JACKED_HOME", str(blocked))
    result = _invoke("tests", "seen", "--", *argv)
    assert result.exit_code == 1
    lines = [line for line in result.stderr.splitlines() if "machine identity" in line]
    assert len(lines) == 1 and "jacked-tests-machine-id" in lines[0] and "--any-machine" in lines[0]
    damaged = tmp_path / "damaged-home"
    (damaged / ".claude").mkdir(parents=True)
    (damaged / ".claude" / "jacked-tests-machine-id").write_bytes(b"not an id")
    monkeypatch.setenv("JACKED_HOME", str(damaged))
    result = _invoke("tests", "seen", "--", *argv)
    lines = [line for line in result.stderr.splitlines() if "machine identity" in line]
    assert result.exit_code == 1 and len(lines) == 1 and "no valid identity" in lines[0]


def test_cli_seen_sees_a_change_inside_an_input_folder(repo):
    (repo / ".gitignore").write_bytes(b"data/\n")
    subprocess.run(["git", "add", ".gitignore"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "-c", "commit.gpgsign=false", "commit", "-q", "-m", "ignore"], cwd=repo,
                   check=True, capture_output=True)
    (repo / "data").mkdir()
    (repo / "data" / "fixture.db").write_bytes(b"one")
    argv = gate(GREEN, 0)
    assert _invoke("tests", "run", "--input", "data", "--", *argv).exit_code == 0
    assert _invoke("tests", "seen", "--input", "data", "--", *argv).exit_code == 0
    (repo / "data" / "fixture.db").write_bytes(b"one and a change")
    assert _invoke("tests", "seen", "--input", "data", "--", *argv).exit_code == 1


def test_cli_run_and_seen_name_an_input_glob_that_matches_no_regular_file(repo):
    argv = gate(GREEN, 0)
    for sub in ("run", "seen"):
        result = _invoke("tests", sub, "--input", "nothing/*", "--", *argv)
        lines = [line for line in result.stderr.splitlines() if "nothing/*" in line]
        assert len(lines) == 1 and "no regular file matches" in lines[0], sub


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(__import__("os"), "geteuid")
                                                and __import__("os").geteuid() == 0),
                    reason="needs POSIX modes and a user that is not root")
def test_cli_seen_says_why_the_work_tree_cannot_be_read(repo):
    locked = repo / "locked.txt"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        result = _invoke("tests", "seen", "--", *gate(GREEN, 0))
    finally:
        locked.chmod(0o600)
    assert result.exit_code == 1
    lines = [line for line in result.stderr.splitlines() if "cannot prove a tree" in line]
    assert len(lines) == 1 and "locked.txt" in lines[0]


@pytest.mark.parametrize("sub", [["run", "--"], ["seen", "--"], ["history"], ["loss"]])
def test_cli_names_a_dubious_ownership_refusal_and_not_a_missing_repository(repo, monkeypatch, sub):
    env = {**__import__("os").environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    probe = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=repo, env=env, capture_output=True)
    if probe.returncode == 0:
        pytest.skip("this git does not know GIT_TEST_ASSUME_DIFFERENT_OWNER")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    argv = [*sub, *gate(GREEN, 0)] if sub[-1] == "--" else sub
    result = _invoke("tests", *argv)
    lines = [line for line in result.stderr.splitlines() if "safe.directory" in line]
    assert len(lines) == 1, result.stderr
    assert "not in the work tree" not in result.stderr and "not in a git repository" not in result.stderr
    if sub[0] == "run":
        assert result.exit_code == 0
    else:
        assert result.exit_code == 1
