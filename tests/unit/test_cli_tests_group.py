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
from tests._testdiet import fixture_reports, hermetic_git, make_repo, skip_or_fail_in_ci

GREEN = fixture_reports("pytest_rerun")[0]
RED = fixture_reports("pytest")[0]
_GATE = "import shutil, sys; shutil.copyfile(sys.argv[1], sys.argv[2]); sys.exit(int(sys.argv[3]))"


def gate(report: Path, code: int) -> list[str]:
    return [sys.executable, "-c", _GATE, str(report), "{junit}", str(code)]


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if shutil.which("git") is None:
        skip_or_fail_in_ci("this test drives the real git binary")
    hermetic_git(tmp_path, monkeypatch)
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
    assert hit.exit_code == 0 and "folder sub" in hit.stdout and hit.stdout.startswith("passed before: ")
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
    lines = [line for line in result.stderr.splitlines() if "cannot prove this tree" in line]
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


def test_cli_run_gives_130_and_no_aborted_line_for_an_interrupt_before_the_gate(repo, monkeypatch):
    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(tree, "tree_state", interrupt)
    result = _invoke("tests", "run", "--", *gate(GREEN, 0))
    assert result.exit_code == 130 and "Aborted" not in result.output


def test_cli_prune_dry_run_deletes_nothing_and_both_print_the_kept_count(repo):
    for code in (0, 0):
        _invoke("tests", "run", "--", *gate(GREEN, code))
    store = tree.store_dir(repo)
    dry = _invoke("tests", "prune", "--dry-run", "--before", "2099-01-01")
    assert dry.exit_code == 0
    assert sum(line.startswith("would delete ") for line in dry.stdout.splitlines()) == 2
    assert "0 runs stay" in dry.stdout and len(list(history.iter_runs(store))) == 2
    real = _invoke("tests", "prune", "--before", "2000-01-01")
    assert "2 runs stay" in real.stdout + real.stderr
    real = _invoke("tests", "prune", "--before", "2099-01-01")
    assert real.stdout.count("deleted ") == 2 and "0 runs stay" in real.stdout


def test_cli_wording_of_help_and_errors(repo):
    loss_help = _invoke("tests", "loss", "--help").stdout
    assert "exit 1" in loss_help and "it exits 0." not in loss_help
    assert all(len(line) <= 80 for line in loss_help.splitlines())
    started = _invoke("tests", "record", "--junit", str(GREEN), "--started", "yesterday")
    assert started.exit_code == 2 and "Invalid isoformat" not in started.output
    assert "2026-11-26T09:00:00+00:00" in started.output
    assert "views" in _invoke("tests", "run", "--help").stdout.split("--label", 1)[1].split("--", 1)[0]
    empty = _invoke("tests", "profile")
    assert empty.exit_code == 1 and "jacked tests run" in empty.stderr
    _invoke("tests", "run", "--", *gate(GREEN, 0))
    unknown = _invoke("tests", "profile", "--machine", "ci")
    assert unknown.exit_code == 1 and "local" in unknown.stderr


def test_cli_seen_outside_a_repository_prints_one_line(tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    result = _invoke("tests", "seen", "--", "pytest")
    assert result.exit_code == 1 and len(result.stderr.splitlines()) == 1
    assert "this tree" not in result.stderr



def test_cli_seen_prints_the_escaped_message_of_an_unknown_error(repo, monkeypatch):
    from jacked.testdiet import record as _record

    def broken(*args, **kwargs):
        raise RuntimeError("bad\nline")

    monkeypatch.setattr(_record, "seen_answer", broken)
    result = _invoke("tests", "seen", "--", *gate(GREEN, 0))
    assert result.exit_code == 1 and len(result.stderr.splitlines()) == 1
    assert "RuntimeError: bad\\nline" in result.stderr


def test_cli_record_with_no_machine_says_the_run_is_filed_under_this_machine(repo):
    result = _invoke("tests", "record", "--junit", str(GREEN))
    lines = [line for line in result.stderr.splitlines() if "this machine" in line]
    assert result.exit_code == 0 and len(lines) == 1 and "--machine" in lines[0]
    assert "this machine" not in _invoke("tests", "record", "--junit", str(GREEN), "--machine", "ci").stderr


def test_cli_history_prints_the_size_line_by_default(repo):
    _invoke("tests", "run", "--", *gate(GREEN, 0))
    out = _invoke("tests", "history").stdout
    assert out.splitlines()[-1].startswith("size of the history: ") and out.rstrip().endswith(" bytes")


def test_the_cli_choices_and_defaults_come_from_the_model():
    from jacked.cli import main
    from jacked.testdiet import model

    group = main.commands["tests"]
    for name in ("run", "record"):
        params = {p.name: p for p in group.commands[name].params}
        assert tuple(params["kind"].type.choices) == model.KINDS
        assert tuple(params["runner"].type.choices) == tuple(r for r in model.RUNNERS if r != "unknown")
    top = next(p for p in group.commands["profile"].params if p.name == "top")
    assert top.default == model.DEFAULT_TOP


def test_cli_profile_refuses_a_run_id_with_non_ascii_digits(repo):
    _invoke("tests", "run", "--", *gate(GREEN, 0))
    fake = "٠" * 8 + "T" + "0" * 12 + "Z-abcdef12"
    result = _invoke("tests", "profile", "--run", fake)
    assert result.exit_code == 1 and "a run ID must be" in result.stderr


@pytest.mark.parametrize("bad", ["broken", 7, ""])
def test_cli_seen_refuses_a_record_whose_input_fingerprint_is_not_valid(repo, bad):
    import json as _json

    argv = gate(GREEN, 0)
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    assert _invoke("tests", "seen", "--", *argv).exit_code == 0  # control: the record proves the tree
    store = tree.store_dir(repo)
    [path] = (store / history.RUNS).glob("*.meta.json")
    raw = _json.loads(path.read_bytes())
    raw["inputs_sha"] = bad
    path.write_bytes(_json.dumps(raw).encode("ascii"))
    result = _invoke("tests", "seen", "--", *argv)
    assert result.exit_code == 1 and "--input fingerprint" in result.stderr and "not valid" in result.stderr


def test_a_run_id_with_an_impossible_date_is_not_a_run_file(repo):
    # Month 13 passes the pattern, but its time does not parse.
    assert _invoke("tests", "run", "--", *gate(GREEN, 0)).exit_code == 0
    runs = tree.store_dir(repo) / history.RUNS
    [real] = runs.glob("*" + history.META_SUFFIX)
    fake = "20261399T999999999999Z-0a1b2c3d"
    assert history.RUN_ID_RE.fullmatch(fake)
    meta = runs / f"{fake}{history.META_SUFFIX}"
    temp = runs / f".{fake}{history.META_SUFFIX}.x1y2.tmp"
    meta.write_bytes(real.read_bytes())
    temp.write_bytes(b"{}")
    for argv in (["history", "--days", "1"], ["prune", "--before", "2099-01-01"]):
        result = _invoke("tests", *argv)
        assert result.exception is None or isinstance(result.exception, SystemExit), (argv, result.exception)
        assert "Traceback" not in result.output + result.stderr
    assert meta.exists() and temp.exists()
    assert fake not in history.run_ids(tree.store_dir(repo))
    with pytest.raises(ValueError):
        history.check_id(fake)
    assert _invoke("tests", "profile", "--run", fake).exit_code == 1


def test_seen_reads_the_store_one_time_so_a_damaged_record_warns_one_time(repo):
    argv = gate(GREEN, 0)
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    runs = tree.store_dir(repo) / history.RUNS
    (runs / f"{history.new_run_id()}{history.META_SUFFIX}").write_bytes(b"{not json")
    (repo / "README.md").write_bytes(b"a new tree, so seen misses and names the nearest record\n")
    result = _invoke("tests", "seen", "--", *argv)
    assert result.exit_code == 1
    assert result.stderr.count("skipped the damaged record") == 1, result.stderr


def test_seen_with_a_history_folder_that_cannot_be_read_does_not_say_the_history_is_empty(repo):
    store = tree.store_dir(repo)
    store.mkdir(parents=True, exist_ok=True)
    (store / history.RUNS).write_bytes(b"a file, not a folder")
    result = _invoke("tests", "seen", "--", *gate(GREEN, 0))
    assert result.exit_code == 1
    assert result.stderr.count("No run is read") == 1, result.stderr  # the warning of iter_runs
    assert "no run of this tree is in the history" not in result.stderr
    assert "the history cannot be read, so no record can prove this tree" in result.stderr


# --------------------------------------------------------------------------- o-L4: each miss line ends with what to do

def test_a_no_data_miss_names_the_repair_and_the_next_action(repo):
    argv = [sys.executable, "-c", "pass"]  # exit 0, no report: verdict no-data
    assert _invoke("tests", "run", "--", *argv).exit_code == 0
    result = _invoke("tests", "seen", "--", *argv)
    assert result.exit_code == 1
    [line] = [line for line in result.stderr.splitlines() if "does not prove the tree" in line]
    assert "Put {junit} in the command, or give --junit GLOB." in line
    assert line.endswith("Run the gate with jacked tests run.")


def test_an_any_machine_miss_ends_with_the_next_action(repo):
    argv = gate(RED, 1)
    _invoke("tests", "run", "--", *argv)
    result = _invoke("tests", "seen", "--any-machine", "--", *argv)
    assert result.exit_code == 1
    [line] = [line for line in result.stderr.splitlines() if "no machine's newest run" in line]
    assert line.endswith("Run the gate with jacked tests run.")


def test_run_and_seen_print_one_text_when_git_cannot_read_the_tree(repo, monkeypatch):
    from jacked.testdiet import model

    problem = "git cannot read the work tree (git add exited with 128: fatal: x)"
    monkeypatch.setattr(tree, "tree_state", lambda *a, **k: (None, problem))
    run_err = _invoke("tests", "run", "--", *gate(GREEN, 0)).stderr
    seen_err = _invoke("tests", "seen", "--", *gate(GREEN, 0)).stderr
    run_line = [line for line in run_err.splitlines() if problem in line]
    seen_line = [line for line in seen_err.splitlines() if problem in line]
    assert run_line and run_line == seen_line, (run_line, seen_line)
    assert run_line[0] == model.PREFIX + tree.no_tree_line(problem)


def test_seen_json_writes_the_miss_reason_to_stderr_and_null_to_stdout(repo):
    argv = gate(RED, 1)
    _invoke("tests", "run", "--", *argv)
    result = _invoke("tests", "seen", "--json", "--", *argv)
    assert result.exit_code == 1 and result.stdout.strip() == "null"
    assert "does not prove the tree" in result.stderr


def test_a_store_that_cannot_be_found_prints_one_line(repo, monkeypatch):
    def refuse(top):
        raise OSError(5, "no store")

    monkeypatch.setattr(tree, "store_dir", refuse)
    result = _invoke("tests", "run", "--", *gate(GREEN, 0))
    assert result.exit_code == 0
    lines = [line for line in result.stderr.splitlines() if "not recorded" in line]
    assert len(lines) == 1, result.stderr
