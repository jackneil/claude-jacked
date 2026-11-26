"""Tests for jacked.testdiet.views and the commands profile, history and prune."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from jacked.cli import main
from jacked.testdiet import history, record, tree, views
from tests._testdiet import (
    COMMAND,
    FAILING_ID,
    LOCAL_ID,
    LOCAL_TWIN_ID,
    REPORT_ROOT,
    TREE_1,
    TREE_2,
    make_case,
    fill_store,
    fixture_project,
    fixture_reports,
    make_repo,
    store_run,
)

RUNNERS = ["pytest", "vitest", "jest", "playwright", "go", "nextest"]


@pytest.fixture
def store(tmp_path) -> Path:
    return fill_store(tmp_path / "store")


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
    monkeypatch.chdir(path)
    return path


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def _git(repo: Path, *args: str) -> None:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, proc.stderr


def _ids(store: Path) -> list[str]:
    return [m.id for m in history.iter_runs(store)]


def test_files_for_half_counts_the_fewest_files_that_hold_half_the_seconds(store):
    assert views.files_for_half([10, 5, 3, 1, 1]) == 1
    assert views.files_for_half([1, 5, 10, 1, 3]) == 1
    assert views.files_for_half([5, 5, 5, 5]) == 2
    result = views.profile(store)
    assert [r["seconds"] for r in result["files"]] == [10.0, 5.0, 3.0, 1.0, 1.0, None]
    assert result["files_for_half"] == 1
    assert result["file_count"] == 6
    assert "files that hold half the seconds: 1 of 6" in views.render_profile(result)


def test_profile_uses_the_machine_of_the_newest_full_run_and_prints_it(store):
    result = views.profile(store)
    newest_full = next(m for m in history.iter_runs(store) if m.kind == "full")
    assert (result["machine"], result["run"]) == ("local", newest_full.id)
    assert "machine: local (the machine of the newest full run)" in views.render_profile(result)
    # A newer targeted run of ci does not move the view. A newer full run does.
    store_run(store, "20261126T080000", exit_code=0, tests=[make_case("ci::t", "t.py", 1.0)],
              kind="targeted", machine="ci", machine_id=None, source="record", command_sha=None)
    assert views.profile(store)["machine"] == "local"
    ci_id = store_run(store, "20261126T090000", exit_code=0, tests=[make_case("ci::t", "t.py", 1.0)],
                      machine="ci", machine_id=None, source="record", command_sha=None)
    result = views.profile(store)
    assert (result["machine"], result["run"]) == ("ci", ci_id)
    assert views.history(store)["machine"] == "ci"


def test_profile_reads_a_record_of_a_different_machine_with_the_machine_flag(store):
    result = views.profile(store, machine="ci")
    ci_full = [m for m in history.iter_runs(store) if m.machine == "ci" and m.kind == "full"]
    assert result["machine"] == "ci" and result["run"] == ci_full[0].id
    assert result["total_test_seconds"] == 1.5
    assert {r["file"] for r in result["files"]} == {"tests/test_calc.py"}
    assert "machine: ci (the machine label that you gave)" in views.render_profile(result)
    with pytest.raises(views.ViewError, match="no run of the machine nobody"):
        views.profile(store, machine="nobody")
    local_run = views.profile(store)["run"]
    with pytest.raises(views.ViewError, match="a run of the machine local"):
        views.profile(store, local_run, machine="ci")


def test_a_group_never_mixes_two_machines_with_one_label(store):
    # A second identity with the label "local" (for example a container on
    # the same clone) ran the same tree and command, and it ran last.
    twin = store_run(store, "20261126T080000", exit_code=0, machine_id=LOCAL_TWIN_ID, wall=500.0,
                     tests=[make_case("tests/test_a.py::test_one", "tests/test_a.py", 50.0)])
    result = views.profile(store)
    assert result["run"] == twin and result["machine"] == "local"
    assert result["repeats"] == []  # the 3 runs of TREE_1 of the first identity are not joined
    assert any("2 machine identities" in note for note in result["notes"])
    seen = views.history(store)
    assert seen["runs"] == 1 and seen["verdicts"]["passed"] == 1 and seen["repeated_green"] == []
    assert seen["other_machines"] == ["local (another identity)", "ci"]
    # The first identity is still one group with its own numbers.
    old = views.profile(store, _ids(store)[1])
    assert {r["tree"]: r["wall_max"] for r in old["repeats"]} == {TREE_1: 120.0, TREE_2: 100.0}


def test_repeats_need_two_runs_of_one_tree_and_one_command(store):
    result = views.profile(store)
    by_tree = {r["tree"]: r for r in result["repeats"]}
    assert set(by_tree) == {TREE_1, TREE_2}
    one = by_tree[TREE_1]
    assert (one["command_sha"], one["runs"]) == (COMMAND, 3)
    assert (one["wall_min"], one["wall_max"]) == (90.0, 120.0)
    assert (one["test_seconds_min"], one["test_seconds_max"]) == (1.0, 20.0)
    # A second command on TREE_1, run one time, is a group of one: no repeat.
    store_run(store, "20261125T090000", exit_code=0, command_sha="e5" * 32, kind="targeted",
              tests=[make_case("x::t", "x.py", 1.0)])
    assert {(r["tree"], r["command_sha"]) for r in views.profile(store)["repeats"]} == \
        {(TREE_1, COMMAND), (TREE_2, COMMAND)}
    # The ci records have no command SHA: no repeat group.
    assert views.profile(store, machine="ci")["repeats"] == []


def test_setup_share_is_none_for_a_junit_record(store):
    for machine in (None, "ci"):
        result = views.profile(store, machine=machine)
        assert "setup_share" in result and result["setup_share"] is None
        assert "setup share" not in views.render_profile(result)
        assert json.loads(views.to_json(result))["setup_share"] is None


def test_json_output_is_not_shortened_and_the_text_says_what_it_hides(store, repo):
    result = views.profile(store, top=2)
    assert len(result["files"]) == 6 and len(result["tests"]) == 7
    assert result["hidden_rows"] == {"files": 4, "tests": 5}
    text = views.render_profile(result, 2)
    assert "4 more files are not shown" in text and "5 more tests are not shown" in text
    assert "tests/test_c.py" not in text
    # The command: --json holds every row, also with --top.
    shutil.copytree(store / history.RUNS, tree.store_dir(repo) / history.RUNS)
    out = _invoke("tests", "profile", "--top", "2", "--json")
    assert out.exit_code == 0
    data = json.loads(out.output)
    assert len(data["files"]) == 6 and len(data["tests"]) == 7
    assert data["hidden_rows"] == {"files": 0, "tests": 0}
    short = _invoke("tests", "profile", "--top", "2")
    assert "4 more files are not shown" in short.output


def test_no_view_prints_a_machine_id(store, capsys):
    store_run(store, "20261119T080000", exit_code=0, machine_id=LOCAL_TWIN_ID,
              tests=[make_case("t::t", "t.py", 1.0)])
    for machine in (None, "ci"):
        p = views.profile(store, machine=machine, top=None)
        h = views.history(store, machine=machine)
        print(views.render_profile(p), views.to_json(p), views.render_history(h, size=True),
              views.to_json(h))
    out, err = capsys.readouterr()
    for text in (out, err):
        assert LOCAL_ID not in text and LOCAL_TWIN_ID not in text
        assert "machine_id" not in text


def test_history_groups_red_runs_by_failing_set(store):
    result = views.history(store)
    assert len(result["failing_sets"]) == 1
    group = result["failing_sets"][0]
    assert (group["runs"], group["trees"], group["ids"]) == (3, 1, [FAILING_ID])
    assert (group["first"], group["last"]) == ("2026-11-22T08:00:00+00:00", "2026-11-23T08:00:00+00:00")
    assert group["on_default_branch"] == 0
    assert FAILING_ID in views.render_history(result)
    # The ci record with the same set is a run of a different machine.
    ci = views.history(store, machine="ci")["failing_sets"]
    assert [(s["sig"], s["runs"]) for s in ci] == [(group["sig"], 1)]


def test_history_lists_a_tree_that_ran_green_two_times(store):
    result = views.history(store)
    assert result["repeated_green"] == [{"tree": TREE_1, "command_sha": COMMAND, "runs": 3}]
    assert TREE_2 not in json.dumps(result["repeated_green"])  # red runs are not green
    assert f"3 runs, tree {TREE_1[:12]}" in views.render_history(result)
    assert views.history(store, machine="ci")["repeated_green"] == []


def test_history_counts_each_verdict(store):
    local = views.history(store)
    assert local["verdicts"] == {"passed": 3, "failed": 3, "conflict": 0, "no-data": 0, "unknown": 0}
    assert local["labelled_full_runs"] == 5 and local["runs"] == 6
    assert local["failing_counts"] == [0, 1, 1, 1, 0, 0]
    assert local["flaky"] == [{"id": "tests/test_calc.py::test_flaky", "runs": 1}]
    ci = views.history(store, machine="ci")
    assert ci["verdicts"] == {"passed": 1, "failed": 0, "conflict": 1, "no-data": 1, "unknown": 1}
    assert ci["failing_counts"] == [1, 0, 0]  # the run with no report has no count
    assert "verdicts: passed 3, failed 3, conflict 0, no-data 0, unknown 0" in views.render_history(local)
    # --days keeps the runs of the last N days by the run-ID time.
    now = datetime(2026, 11, 25, 12, tzinfo=timezone.utc)
    assert views.history(store, days=3, now=now)["verdicts"]["failed"] == 1
    with pytest.raises(ValueError):
        views.history(store, days=1, now=datetime(2026, 11, 25))


def test_a_test_with_no_seconds_is_not_counted_as_zero_seconds(store):
    result = views.profile(store)
    assert result["total_test_seconds"] == 20.0
    assert result["tests_without_seconds"] == 1
    assert result["files"][-1] == {"file": "tests/test_f.py", "seconds": None, "tests": 1}
    assert result["tests"][-1] == {"id": "tests/test_f.py::test_no_time", "seconds": None}
    line = next(x for x in views.render_profile(result).splitlines() if "test_f.py " in x + " ")
    assert "0" not in line.replace("tests/test_f.py", "")
    only = store_run(store, "20261126T080000", exit_code=0, tests=[make_case("n::t", "n.py", None)])
    empty = views.profile(store, only)
    assert (empty["total_test_seconds"], empty["files_for_half"]) == (None, None)
    assert "test seconds: \n" in views.render_profile(empty) + "\n"
    assert json.loads(views.to_json(empty))["total_test_seconds"] is None


def test_cli_prune_prints_each_run_that_it_deletes(repo, monkeypatch):
    store = tree.store_dir(repo)
    old = [store_run(store, when, exit_code=0, tests=[make_case("t::t", "t.py", 1.0)])
           for when in ("20200101T080000", "20201231T235959")]
    kept = [store_run(store, when, exit_code=0, tests=None) for when in ("20210101T000000", "20261125T080000")]
    out = _invoke("tests", "prune", "--before", "2021-01-01")
    assert out.exit_code == 0
    assert out.output.splitlines() == [f"deleted {run_id}" for run_id in sorted(old)]
    assert sorted(_ids(store)) == sorted(kept)
    again = _invoke("tests", "prune", "--before", "2021-01-01")
    assert again.exit_code == 0 and "Nothing is deleted" in again.output
    assert _invoke("tests", "prune", "--before", "2021-13-01").exit_code == 2

    def refuse(store, before, *, now=None):
        raise history.PruneError([kept[0]], [(f"{kept[1]}.meta.json\x1b[2J", "PermissionError")])

    monkeypatch.setattr(history, "prune", refuse)
    failed = _invoke("tests", "prune", "--before", "2027-01-01")
    assert failed.exit_code == 1
    assert f"deleted {kept[0]}" in failed.output
    assert f"cannot delete {kept[1]}.meta.json[2J (PermissionError)" in failed.output
    assert "\x1b" not in failed.output


def test_cli_views_outside_a_work_tree_and_with_no_run(repo, tmp_path, monkeypatch):
    # An empty history is not an error for history. It is for profile.
    empty = _invoke("tests", "history")
    assert (empty.exit_code, empty.output) == (0, "the history has no run.\n")
    data = json.loads(_invoke("tests", "history", "--json").output)
    assert (data["machine"], data["runs"], data["failing_sets"]) == (None, 0, [])
    assert set(data["verdicts"].values()) == {0}
    empty = _invoke("tests", "profile")
    assert empty.exit_code == 1 and "the history has no run" in empty.output
    # An unknown --run or --machine is an error for each command, empty store or not.
    for command in (("profile", "--run", "nope"), ("profile", "--machine", "ci"),
                    ("history", "--machine", "ci")):
        result = _invoke("tests", *command)
        assert result.exit_code == 1 and len(result.output.splitlines()) == 1
    store_run(tree.store_dir(repo), "20261125T080000", exit_code=0, tests=None)
    for command in (("profile", "--run", "20261125T080000000000Z-00000000"),
                    ("profile", "--machine", "ci"), ("history", "--machine", "ci")):
        result = _invoke("tests", *command)
        assert result.exit_code == 1 and len(result.output.splitlines()) == 1
    assert _invoke("tests", "history", "--machine", "a b").exit_code == 2
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    for command in (("profile",), ("history",), ("prune", "--before", "2021-01-01")):
        result = _invoke("tests", *command)
        assert result.exit_code == 1 and "not in the work tree" in result.output


def test_cli_history_prints_the_size_with_the_size_flag(store, repo):
    shutil.copytree(store / history.RUNS, tree.store_dir(repo) / history.RUNS)
    size = sum(p.stat().st_size for p in (store / history.RUNS).iterdir())
    assert f"size of the history: {size} bytes" in _invoke("tests", "history", "--size").output
    assert "size of the history" not in _invoke("tests", "history").output
    data = json.loads(_invoke("tests", "history", "--json", "--machine", "ci").output)
    assert data["machine"] == "ci" and data["size_bytes"] == size


@pytest.mark.parametrize("runner", RUNNERS)
def test_a_real_report_goes_through_record_and_profile(repo, runner):
    shutil.copytree(fixture_project(runner), repo, dirs_exist_ok=True)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "sample project")
    meta = record.record_files(fixture_reports(runner), cwd=repo, kind="full", label=None, runner=None,
                               commit=None, exit_code=1, wall_seconds=None, machine=None,
                               report_root=REPORT_ROOT)
    out = _invoke("tests", "profile", "--json")
    assert out.exit_code == 0
    result = json.loads(out.output)
    assert (result["run"], result["machine"], result["verdict"]) == (meta.id, "local", "failed")
    assert result["total_test_seconds"] > 0.4
    assert result["files"] and all(r["file"] and (repo / r["file"]).is_file() for r in result["files"])
    assert sum(r["tests"] for r in result["files"]) == len(result["tests"]) == sum(meta.counts.values())
    slept = [r for r in result["tests"] if "sleeps_half_a_second" in r["id"]]
    assert len(slept) == 1 and slept[0]["seconds"] >= 0.4
    assert result["files_for_half"] is not None and 1 <= result["files_for_half"] <= result["file_count"]
    text = _invoke("tests", "profile")
    assert text.exit_code == 0 and "machine: local (the machine of the newest full run)" in text.output
