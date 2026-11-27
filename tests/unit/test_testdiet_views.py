"""Tests for jacked.testdiet.views and the commands profile, history and prune."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from jacked.cli import main
from jacked.testdiet import history, record, tree, views
from jacked.testdiet import machine as identity
from tests._testdiet import git as _git
from tests._testdiet import skip_or_fail_in_ci
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
    hermetic_git,
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
        skip_or_fail_in_ci("this test drives the real git binary")
    hermetic_git(tmp_path, monkeypatch)
    path = make_repo(tmp_path / "repo")
    monkeypatch.chdir(path)
    return path


def _invoke(*args: str):
    return CliRunner().invoke(main, list(args), catch_exceptions=False)


def _ids(store: Path) -> list[str]:
    return [m.id for m in history.iter_runs(store)]


def test_files_for_half_counts_the_fewest_files_that_hold_half_the_seconds(store):
    assert views.files_for_half([10, 5, 3, 1, 1]) == 1
    assert views.files_for_half([1, 5, 10, 1, 3]) == 1
    assert views.files_for_half([5, 5, 5, 5]) == 2
    assert views.files_for_half([0.0, 0.0, None]) is None  # no time to split
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
    text = views.render_profile(result)
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
        print(views.render_profile(p), views.to_json(p), views.render_history(h),
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
    # The red runs hold no default-branch value: the count is empty, not 0.
    assert (group["trees_unknown"], group["on_default_branch"], group["on_default_branch_unknown"]) == \
        (0, None, 3)
    assert "on the default branch: not recorded (3 runs)," in views.render_history(result)
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
    assert "test seconds: no time recorded\n" in views.render_profile(empty) + "\n"
    assert json.loads(views.to_json(empty))["total_test_seconds"] is None


def test_profile_says_no_kind_for_a_run_that_has_no_kind(store):
    # A record without --kind has no kind; the header must say so, never "kind ,".
    run_id = store_run(store, "20261126T090000", exit_code=0, tests=[make_case("k::t", "k.py", 1.0)], kind=None)
    result = views.profile(store, run_id)
    assert result["kind"] is None
    header = next(x for x in views.render_profile(result).splitlines() if x.startswith("run: "))
    assert "kind not given, verdict passed" in header
    assert "kind ," not in header
    assert json.loads(views.to_json(result))["kind"] is None


def test_cli_prune_prints_each_run_that_it_deletes(repo, monkeypatch):
    store = tree.store_dir(repo)
    old = [store_run(store, when, exit_code=0, tests=[make_case("t::t", "t.py", 1.0)])
           for when in ("20200101T080000", "20201231T235959")]
    kept = [store_run(store, when, exit_code=0, tests=None) for when in ("20210101T000000", "20261125T080000")]
    out = _invoke("tests", "prune", "--before", "2021-01-01")
    assert out.exit_code == 0
    assert out.output.splitlines() == [*(f"deleted {run_id}" for run_id in sorted(old)),
                                       "2 runs stay in the history."]
    assert sorted(_ids(store)) == sorted(kept)
    again = _invoke("tests", "prune", "--before", "2021-01-01")
    assert again.exit_code == 0 and "Nothing is deleted" in again.output
    assert _invoke("tests", "prune", "--before", "2021-13-01").exit_code == 2

    def refuse(store, before, *, now=None, dry_run=False):
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
    assert (empty.exit_code, empty.output) == (0, "the history has no run.\nsize of the history: 0 bytes\n")
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


def test_cli_history_prints_the_size_of_the_store(store, repo):
    shutil.copytree(store / history.RUNS, tree.store_dir(repo) / history.RUNS)
    size = sum(p.stat().st_size for p in (store / history.RUNS).iterdir())
    assert _invoke("tests", "history").output.splitlines()[-1] == f"size of the history: {size} bytes"
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


def _edit_meta(store: Path, run_id: str, **changes) -> None:
    """Change fields of a stored meta file. A value of ``...`` removes the field."""
    path = store / history.RUNS / f"{run_id}{history.META_SUFFIX}"
    raw = json.loads(path.read_bytes())
    for name, value in changes.items():
        if value is ...:
            raw.pop(name, None)
        else:
            raw[name] = value
    path.write_bytes(json.dumps(raw).encode("utf-8"))


def test_profile_of_a_run_with_no_tests_file_has_empty_counts(store):
    no_report = next(m.id for m in history.iter_runs(store) if m.verdict == "no-data")
    for run_id in (no_report, _damaged(store)):
        result = views.profile(store, run_id)
        data = json.loads(views.to_json(result))
        assert (data["file_count"], data["tests_without_seconds"], data["files_for_half"],
                data["total_test_seconds"]) == (None, None, None, None)
        assert data["files"] == [] and data["tests"] == []
        text = views.render_profile(result)
        assert "the run has no tests file." in text
        assert " of 0" not in text and "None" not in text


def _damaged(store: Path) -> str:
    run_id = store_run(store, "20261119T080000", exit_code=0, tests=[make_case("t::t", "t.py", 1.0)])
    (store / history.RUNS / f"{run_id}{history.TESTS_SUFFIX}").write_bytes(b"not gzip")
    return run_id


def test_a_bad_machine_label_or_identity_reads_as_empty_and_the_run_stays(repo):
    store = tree.store_dir(repo)
    case = [make_case("t::t", "t.py", 1.0)]
    no_label = store_run(store, "20261126T080000", exit_code=0, tests=case)
    newline = store_run(store, "20261125T080000", exit_code=0, tests=case)
    bad_id = store_run(store, "20261124T080000", exit_code=1, tests=case)
    _edit_meta(store, no_label, machine=...)
    _edit_meta(store, newline, machine="a\nb")
    _edit_meta(store, bad_id, machine_id="NOT-HEX")
    read = {m.id: (m.machine, m.machine_id) for m in history.iter_runs(store)}
    assert read == {no_label: (None, LOCAL_ID), newline: (None, LOCAL_ID), bad_id: ("local", None)}
    result = views.history(store)
    assert (result["machine"], result["runs"], result["store_runs"]) == (None, 2, 3)
    assert result["other_machines"] == ["local"]
    out = _invoke("tests", "history")
    assert out.exit_code == 0 and "the history has no run" not in out.output
    assert "machine: (no label) (the machine of the newest full run)" in out.output
    assert "a\nb" not in out.output and "\nb\n" not in out.output
    # The run with no identity is its own machine; its other machines hold "(no label)".
    other = views.history(store, machine="local")
    assert other["runs"] == 1 and other["other_machines"] == ["(no label)"]
    assert "(no label)" in views.render_history(other)
    assert views.profile(store, no_label)["machine"] is None


def test_a_line_break_in_a_test_id_cannot_add_a_line(store):
    store_run(store, "20261126T080000", exit_code=1,
              tests=[make_case("t::x\nmachine: ci (fake)", "t\nf.py", 1.0, "failed")])
    for text in (views.render_profile(views.profile(store)), views.render_history(views.history(store))):
        assert not any(line.startswith("machine: ci") for line in text.splitlines())
        assert "t::x\\nmachine: ci (fake)" in text


def test_profile_of_a_given_run_says_when_its_label_has_two_identities(store):
    store_run(store, "20261126T080000", exit_code=0, machine_id=LOCAL_TWIN_ID,
              tests=[make_case("t::t", "t.py", 1.0)])
    old = _ids(store)[1]
    notes = views.profile(store, old)["notes"]
    assert notes[0] == "machine: local (the machine of the run that you gave)"
    assert notes[1] == ("the label local has 2 machine identities. "
                        "This view uses the identity of the run that you gave.")


def test_a_failing_set_with_no_tree_has_no_tree_count(store):
    red = [make_case("x::y", "x.py", 1.0, "failed")]
    store_run(store, "20261126T080000", exit_code=1, tests=red, tree=None, on_default_branch=True)
    store_run(store, "20261126T090000", exit_code=1, tests=red, tree=None)
    group = next(s for s in views.history(store)["failing_sets"] if s["ids"] == ["x::y"])
    assert (group["trees"], group["trees_unknown"]) == (None, 2)
    assert (group["on_default_branch"], group["on_default_branch_unknown"]) == (1, 1)


def test_no_command_prints_a_machine_id(store, repo):
    store_run(store, "20261119T080000", exit_code=0, machine_id=LOCAL_TWIN_ID,
              tests=[make_case("t::t", "t.py", 1.0)])
    shutil.copytree(store / history.RUNS, tree.store_dir(repo) / history.RUNS)
    ci_run = next(m.id for m in history.iter_runs(store) if m.machine == "ci")
    real = identity.machine_id()
    commands = [
        ("profile",), ("profile", "--json"), ("profile", "--machine", "ci"),
        ("profile", "--machine", "ci", "--json"), ("profile", "--run", ci_run, "--json"),
        ("profile", "--run", ci_run, "--machine", "local"), ("profile", "--machine", "nobody"),
        ("profile", "--run", "20200101T000000000000Z-00000000"), ("profile", "--run", "bad"),
        ("history",), ("history", "--json"), ("history", "--machine", "ci"),
        ("history", "--machine", "ci", "--json"), ("history", "--machine", "nobody"),
        ("history", "--days", "1", "--json"), ("prune", "--before", "2000-01-01"),
    ]
    for command in commands:
        out = _invoke("tests", *command).output
        for value in (LOCAL_ID, LOCAL_TWIN_ID, real):
            assert not value or value not in out, command
        assert "machine_id" not in out, command


def _one_line(text: str, start: str) -> str:
    (line,) = [x for x in text.splitlines() if x.lstrip().startswith(start)]
    return line


def test_profile_says_no_time_recorded_when_no_file_has_seconds(store):
    only = store_run(store, "20261126T080000", exit_code=0,
                     tests=[make_case("n::t", "n.py", None), make_case("m::t", "m.py", None)])
    result = views.profile(store, only)
    assert (result["files_for_half"], result["total_test_seconds"]) == (None, None)
    text = views.render_profile(result)
    assert _one_line(text, "files that hold half") == "files that hold half the seconds: no time recorded"
    assert json.loads(views.to_json(result))["files_for_half"] is None


def test_profile_says_every_file_took_0_seconds_when_the_times_are_0(store):
    only = store_run(store, "20261126T080000", exit_code=0,
                     tests=[make_case("n::t", "n.py", 0.0), make_case("m::t", "m.py", 0.0)])
    result = views.profile(store, only)
    assert (result["files_for_half"], result["total_test_seconds"]) == (None, 0.0)
    text = views.render_profile(result)
    assert _one_line(text, "files that hold half") == "files that hold half the seconds: every file took 0 seconds"
    assert _one_line(text, "test seconds") == "test seconds: 0"


def test_profile_says_every_file_with_a_time_took_0_seconds_when_some_files_have_none(store):
    only = store_run(store, "20261126T080000", exit_code=0,
                     tests=[make_case("n::t", "n.py", 0.0), make_case("m::t", "m.py", None)])
    result = views.profile(store, only)
    assert (result["files_for_half"], result["total_test_seconds"]) == (None, 0.0)
    line = _one_line(views.render_profile(result), "files that hold half")
    assert line == "files that hold half the seconds: every file with a time took 0 seconds"


def test_profile_table_rows_with_no_seconds_say_no_time(store):
    text = views.render_profile(views.profile(store))
    rows = [x.split() for x in text.splitlines() if x.lstrip().startswith("no time ")]
    assert rows == [["no", "time", "1", "tests/test_f.py"], ["no", "time", "tests/test_f.py::test_no_time"]]


def test_profile_repeat_line_with_no_wall_time_says_no_time_recorded(store):
    result = views.profile(store)
    assert result["repeats"]
    result["repeats"][0].update(wall_min=None, wall_max=None, test_seconds_min=None, test_seconds_max=None)
    tree = result["repeats"][0]["tree"][:12]
    (line,) = [x for x in views.render_profile(result).splitlines() if f", {tree} " in x]
    assert "wall no time recorded, test seconds no time recorded," in line
    assert " -" not in line and "- " not in line


def test_history_size_line_of_a_folder_that_cannot_be_read(store):
    result = views.history(store)
    result["size_bytes"] = None
    line = _one_line(views.render_history(result), "size of the history")
    assert line == "size of the history: the folder cannot be read"
    result["size_bytes"] = 0
    assert _one_line(views.render_history(result), "size of") == "size of the history: 0 bytes"


def test_history_failing_set_counts_that_only_some_runs_hold(store):
    result = views.history(store)
    group = result["failing_sets"][0]
    group.update(trees=1, trees_unknown=2, on_default_branch=1, on_default_branch_unknown=2)
    line = views.render_history(result)
    assert "trees: 1 (2 not recorded), on the default branch: 1 (2 not recorded)," in line
    group.update(trees=None, trees_unknown=3, on_default_branch=0, on_default_branch_unknown=0)
    line = views.render_history(result)
    assert "trees: not recorded (3 runs), on the default branch: 0," in line


def test_profile_reads_the_test_seconds_of_a_repeat_run_from_its_record(store, monkeypatch):
    from jacked.testdiet import history as _history

    read: list[str] = []
    real = _history.load_tests
    monkeypatch.setattr(_history, "load_tests", lambda s, run_id: read.append(run_id) or real(s, run_id))
    result = views.profile(store)
    assert read == [result["run"]]  # only the profiled run's own tests file
    assert result["repeats"] and result["repeats"][0]["test_seconds_min"] is not None


def test_a_record_with_no_stored_test_seconds_reads_its_tests_file(store, monkeypatch):
    import json as _json

    from jacked.testdiet import history as _history

    for path in (store / _history.RUNS).glob("*.meta.json"):
        raw = _json.loads(path.read_bytes())
        raw.pop("test_seconds", None)
        raw.pop("schema", None)
        path.write_bytes(_json.dumps(raw).encode("utf-8"))
    read: list[str] = []
    real = _history.load_tests
    monkeypatch.setattr(_history, "load_tests", lambda s, run_id: read.append(run_id) or real(s, run_id))
    result = views.profile(store)
    assert len(read) > 1 and result["repeats"][0]["test_seconds_min"] is not None


def test_a_written_record_holds_the_sum_of_its_test_seconds(store):
    by_id = {m.id: m for m in history.iter_runs(store)}
    for run_id, meta in by_id.items():
        data = history.load_tests(store, run_id)
        expected = None if data is None else views._sum(c.seconds for c in data.tests)
        assert meta.test_seconds == expected, run_id


def test_a_run_that_the_user_stopped_is_not_the_default_profile_run(tmp_path):
    from jacked.testdiet import history as _history

    store = tmp_path / "s"
    kept = store_run(store, "20261120T080000", exit_code=0, kind="full", tests=[make_case("a::t", "a.py", 1.0)])
    stopped = store_run(store, "20261121T080000", exit_code=130, kind="full",
                        tests=[make_case("a::t", "a.py", 0.1)])
    path = store / _history.RUNS / f"{stopped}.meta.json"
    import json as _json

    raw = _json.loads(path.read_bytes())
    raw["interrupted"] = True
    path.write_bytes(_json.dumps(raw).encode("ascii"))
    assert views.profile(store)["run"] == kept


def test_history_of_an_empty_store_has_the_keys_of_a_full_one(store, tmp_path):
    empty = tmp_path / "empty-store"
    empty.mkdir()
    assert set(views.history(empty)) == set(views.history(store))


def test_a_record_with_no_start_time_verdict_or_source_prints_text(tmp_path):
    import json as _json

    from jacked.testdiet import record as _record

    store = tmp_path / "s"
    red = [make_case("a::t", "a.py", 1.0, "failed")]
    first = store_run(store, "20261120T080000", exit_code=1, kind="full", tests=red)
    now = store_run(store, "20261121T080000", exit_code=1, kind="full", tests=red)
    for run_id in (first, now):
        path = store / history.RUNS / f"{run_id}.meta.json"
        raw = _json.loads(path.read_bytes())
        for key in ("started", "verdict", "source"):
            raw.pop(key)
        path.write_bytes(_json.dumps(raw).encode("ascii"))
    texts = [views.render_profile(views.profile(store)), views.render_history(views.history(store))]
    meta = next(m for m in history.iter_runs(store) if m.id == now)
    texts += [_record.seen_line(meta), _record.repeat_notice(store, meta)]
    for text in texts:
        assert " ," not in text and "started ," not in text and not any(line.endswith(" ") for line in
                                                                        text.splitlines())
    assert "no start time recorded" in texts[0] and "verdict not recorded" in texts[0]
    assert "no start time recorded" in texts[2] and "source not recorded" in texts[3]
