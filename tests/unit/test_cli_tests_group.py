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
from jacked.testdiet import history, tree
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
    assert "machine_id" not in data and tree.machine_id() not in as_json.output
    assert json.loads(_invoke("tests", "seen", "--json", "--", *gate(RED, 0)).stdout) is None


def test_cli_seen_from_a_sub_folder_uses_the_top_of_the_work_tree_for_inputs(repo, monkeypatch):
    (repo / "data.bin").write_bytes(b"x")
    argv = gate(GREEN, 0)
    assert _invoke("tests", "run", "--input", "data.bin", "--", *argv).exit_code == 0
    monkeypatch.chdir(repo / "sub")
    assert _invoke("tests", "seen", "--input", "data.bin", "--", *argv).exit_code == 0


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
