"""Tests for the tree identity, the machine identity and the history store of jacked.testdiet.

The tree tests drive the real git in throwaway repositories under ``tmp_path``.
The git config is hermetic, as in tests/unit/test_git_reality.py.
"""

from __future__ import annotations

import gzip
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import textwrap
import time
from datetime import date
from pathlib import Path

import pytest

from jacked import git_guard
from jacked.testdiet import history, tree
from jacked.testdiet.model import CaseResult, RunData, RunMeta, TreeId
from tests._testdiet import make_repo

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def hermetic_git(tmp_path, monkeypatch):
    """A hermetic git config: no system file, an empty global file, a fixed author."""
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
    return make_repo(hermetic_git / "repo")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", "-C", str(repo), "-c", "commit.gpgsign=false", *args],
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)
    assert proc.returncode == 0, f"git {args[0]} failed: {proc.stderr}"
    return proc.stdout.strip()


def _commit_all(repo: Path, message: str) -> str:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _objects(repo: Path) -> set[str]:
    """Every loose object file and pack file of the repository."""
    root = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "objects"))
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _index_bytes(cwd: Path) -> bytes:
    return Path(_git(cwd, "rev-parse", "--path-format=absolute", "--git-path", "index")).read_bytes()


# --------------------------------------------------------------------------- tree identity


def test_a_clean_tree_is_clean_and_has_the_head_tree(repo):
    tid = tree.tree_id(repo)
    assert tid == TreeId(commit=_git(repo, "rev-parse", "HEAD"),
                         tree=_git(repo, "rev-parse", "HEAD^{tree}"), clean=True)


def test_an_edit_that_is_not_committed_changes_the_tree_and_is_not_clean(repo):
    before = tree.tree_id(repo)
    (repo / "README.md").write_bytes(b"changed\n")
    after = tree.tree_id(repo)
    assert after.clean is False
    assert after.commit == before.commit
    assert after.tree != before.tree
    # The identity is the tree that a commit of this content has.
    _commit_all(repo, "second")
    assert after.tree == _git(repo, "rev-parse", "HEAD^{tree}")


def test_a_new_file_that_git_does_not_ignore_changes_the_tree(repo):
    before = tree.tree_id(repo)
    (repo / "new.txt").write_bytes(b"new\n")
    after = tree.tree_id(repo)
    assert after.clean is False and after.tree != before.tree


def test_an_ignored_file_does_not_change_the_tree(repo):
    (repo / ".gitignore").write_bytes(b"*.log\n")
    _commit_all(repo, "ignore logs")
    before = tree.tree_id(repo)
    (repo / "debug.log").write_bytes(b"noise\n")
    after = tree.tree_id(repo)
    assert after == before and after.clean is True


def test_two_commits_with_the_same_content_have_one_tree(repo):
    first = tree.tree_id(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    second = tree.tree_id(repo)
    assert second.commit != first.commit
    assert second.tree == first.tree


def test_tree_id_does_not_change_the_real_index(repo):
    (repo / "README.md").write_bytes(b"changed\n")
    (repo / "new.txt").write_bytes(b"new\n")
    before = _index_bytes(repo)
    assert tree.tree_id(repo).clean is False
    assert _index_bytes(repo) == before
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                            text=True, stdin=subprocess.DEVNULL).stdout
    assert status == " M README.md\n?? new.txt\n"


def test_tree_id_in_a_linked_worktree_does_not_change_its_index(repo, hermetic_git):
    linked = hermetic_git / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    (linked / "README.md").write_bytes(b"in the linked worktree\n")
    (linked / "extra.txt").write_bytes(b"extra\n")
    main_index = _index_bytes(repo)
    linked_index = _index_bytes(linked)
    objects = _objects(repo)
    tid = tree.tree_id(linked)
    assert tid.clean is False
    assert tid.tree != tree.tree_id(repo).tree
    assert _index_bytes(linked) == linked_index
    assert _index_bytes(repo) == main_index
    assert _objects(repo) == objects


def test_tree_id_of_a_dirty_tree_writes_no_object_into_the_repository(repo):
    (repo / "README.md").write_bytes(b"a change that no object holds yet\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "new.txt").write_bytes(b"a new file\n")
    count_before = _git(repo, "count-objects", "-v")
    files_before = _objects(repo)
    tid = tree.tree_id(repo)
    assert tid.clean is False
    assert _git(repo, "count-objects", "-v") == count_before
    assert _objects(repo) == files_before
    assert subprocess.run(["git", "-C", str(repo), "cat-file", "-e", tid.tree],
                          capture_output=True).returncode != 0


def test_each_git_call_of_tree_id_has_optional_locks_off(repo, monkeypatch):
    real_run = git_guard.run_git
    envs: list[dict] = []

    def spy(argv, **kwargs):
        envs.append(dict(kwargs["env"]))
        return real_run(argv, **kwargs)

    monkeypatch.setattr(git_guard, "run_git", spy)
    (repo / "README.md").write_bytes(b"dirty\n")
    assert tree.tree_id(repo).clean is False
    assert len(envs) >= 3
    assert all(env.get("GIT_OPTIONAL_LOCKS") == "0" for env in envs)


def test_a_repository_with_no_commit_has_no_tree(tmp_path, hermetic_git):
    if shutil.which("git") is None:
        pytest.skip("this test drives the real git binary")
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    (empty / "a.txt").write_bytes(b"a\n")
    assert tree.tree_id(empty) is None
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    assert tree.tree_id(bare) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert tree.tree_id(plain) is None


def test_changed_paths_names_the_file_that_differs(repo):
    (repo / "keep.txt").write_bytes(b"keep\n")
    _commit_all(repo, "keep")
    a = tree.tree_id(repo)
    (repo / "keep.txt").write_bytes(b"changed\n")
    (repo / "dir").mkdir()
    (repo / "dir" / "added.txt").write_bytes(b"added\n")
    _commit_all(repo, "change")
    b = tree.tree_id(repo)
    assert tree.changed_paths(repo, a, b) == ["dir/added.txt", "keep.txt"]
    assert tree.changed_paths(repo, a, a) == []


def test_changed_paths_of_two_dirty_trees_reads_a_shared_object_folder(repo, tmp_path):
    objects = tmp_path / "tree-objects"
    objects.mkdir()
    (repo / "one.txt").write_bytes(b"one\n")
    a = tree.tree_id(repo, objects=objects)
    (repo / "two.txt").write_bytes(b"two\n")
    b = tree.tree_id(repo, objects=objects)
    assert tree.changed_paths(repo, a, b, objects=objects) == ["two.txt"]


def test_changed_paths_fails_when_a_tree_object_is_gone(repo):
    a = tree.tree_id(repo)
    (repo / "README.md").write_bytes(b"dirty\n")
    b = tree.tree_id(repo)
    with pytest.raises(tree.TreeError):
        tree.changed_paths(repo, a, b)


def test_the_inputs_fingerprint_changes_when_an_ignored_input_changes(repo):
    (repo / ".gitignore").write_bytes(b"data/\n")
    _commit_all(repo, "ignore data")
    (repo / "data").mkdir()
    cache = repo / "data" / "cache.bin"
    cache.write_bytes(b"one")
    assert tree.inputs_fingerprint(repo, []) is None
    first = tree.inputs_fingerprint(repo, ["data/*.bin"])
    assert first == tree.inputs_fingerprint(repo, ["data/*.bin"])
    tid = tree.tree_id(repo)
    cache.write_bytes(b"one and more")
    assert tree.tree_id(repo) == tid  # git does not see the change
    second = tree.inputs_fingerprint(repo, ["data/*.bin"])
    assert second != first
    stat = cache.stat()
    os.utime(cache, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert tree.inputs_fingerprint(repo, ["data/*.bin"]) != second
    # A glob that matches nothing is not the same as no glob.
    assert tree.inputs_fingerprint(repo, ["nothing/*"]) is not None


def test_two_worktrees_of_one_clone_share_one_store(repo, hermetic_git):
    linked = hermetic_git / "linked"
    _git(repo, "worktree", "add", "-q", "-b", "side", str(linked))
    assert tree.store_dir(linked) == tree.store_dir(repo)
    assert tree.store_dir(repo) == tree.common_dir(repo) / "jacked-tests"
    assert tree.store_dir(repo / ".") == tree.store_dir(repo)


def test_the_store_is_outside_the_work_tree(repo):
    store = tree.store_dir(repo).resolve()
    assert store.is_relative_to((repo / ".git").resolve())
    history.write_run(store, _meta(history.new_run_id()), None)
    assert _git(repo, "status", "--porcelain", "--untracked-files=all") == ""


# --------------------------------------------------------------------------- the store


def _meta(run_id: str, **changes) -> RunMeta:
    values = dict(
        id=run_id, started="2026-11-26T10:00:00Z", source="run", wall_seconds=12.5, exit_code=0,
        verdict="passed", kind="full", label="gate", runner="pytest", command_sha="a" * 64,
        inputs_sha=None, machine="local", machine_id="0" * 32,
        tree=TreeId(commit="c" * 40, tree="d" * 40, clean=True), tree_changed=False,
        on_default_branch=None, counts={"passed": 2, "failed": 0}, seconds_reported=11.0,
        load_start=1.5, load_end=2.0, child_cpu_seconds=30.25, failing=(), flaky=("t::b",),
        failing_sig=None,
    )
    values.update(changes)
    return RunMeta(**values)


def _data() -> RunData:
    return RunData(runner="pytest", seconds_reported=11.0, tests=(
        CaseResult(id="t::a", file="t.py", seconds=1.0, state="passed", failure_type=None, attempts=1),
        CaseResult(id="t::b", file="t.py", seconds=None, state="flaky", failure_type="AssertionError",
                   attempts=2),
    ))


def test_a_run_comes_back_the_same(tmp_path):
    meta = _meta(history.new_run_id())
    history.write_run(tmp_path, meta, _data())
    assert list(history.iter_runs(tmp_path)) == [meta]
    assert history.load_tests(tmp_path, meta.id) == _data()


def test_a_value_that_is_absent_comes_back_as_none_not_zero(tmp_path):
    meta = _meta(history.new_run_id(), wall_seconds=None, exit_code=None, verdict="unknown",
                 counts=None, seconds_reported=None, load_start=None, load_end=None,
                 child_cpu_seconds=None, tree=None, tree_changed=None, machine_id=None)
    history.write_run(tmp_path, meta, None)
    [back] = history.iter_runs(tmp_path)
    assert back == meta
    for name in ("wall_seconds", "exit_code", "counts", "seconds_reported", "load_start",
                 "load_end", "child_cpu_seconds", "tree", "tree_changed", "machine_id"):
        assert getattr(back, name) is None, name
    assert history.load_tests(tmp_path, meta.id) is None
    raw = json.loads(next((tmp_path / "runs").glob("*.meta.json")).read_bytes())
    assert raw["wall_seconds"] is None and raw["counts"] is None


def test_a_meta_file_with_a_field_of_a_later_version_is_read(tmp_path):
    meta = _meta(history.new_run_id())
    history.write_run(tmp_path, meta, None)
    path = tmp_path / "runs" / f"{meta.id}.meta.json"
    raw = json.loads(path.read_bytes())
    raw["a_field_of_a_later_version"] = {"x": 1}
    raw["tree"]["a_later_tree_field"] = True
    del raw["load_end"], raw["flaky"]
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    [back] = history.iter_runs(tmp_path)
    assert back.load_end is None and back.flaky == ()
    assert back.tree == meta.tree and back.label == "gate"


def test_a_run_with_no_meta_file_does_not_exist(tmp_path):
    meta = _meta(history.new_run_id())
    history.write_run(tmp_path, meta, _data())
    (tmp_path / "runs" / f"{meta.id}.meta.json").unlink()
    assert list(history.iter_runs(tmp_path)) == []
    assert (tmp_path / "runs" / f"{meta.id}.tests.json.gz").is_file()


def test_the_tests_file_is_written_before_the_meta_file(tmp_path, monkeypatch):
    order: list[str] = []
    real = history.atomic_write_bytes

    def spy(path, data, **kwargs):
        order.append(Path(path).name.split(".", 1)[1])
        return real(path, data, **kwargs)

    monkeypatch.setattr(history, "atomic_write_bytes", spy)
    history.write_run(tmp_path, _meta(history.new_run_id()), _data())
    assert order == ["tests.json.gz", "meta.json"]


def test_two_processes_write_runs_at_once_and_both_are_readable(tmp_path):
    script = textwrap.dedent("""
        import sys
        import time
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from jacked.testdiet import history
        from jacked.testdiet.model import CaseResult, RunData, RunMeta
        store, label = Path(sys.argv[2]), sys.argv[3]
        Path(sys.argv[4]).touch()
        while not Path(sys.argv[5]).exists():
            time.sleep(0.005)
        for n in range(25):
            meta = RunMeta(id=history.new_run_id(), started="2026-11-26T10:00:00Z", source="run",
                wall_seconds=1.0, exit_code=0, verdict="passed", kind=None, label=label, runner="pytest",
                command_sha=None, inputs_sha=None, machine="local", machine_id=None, tree=None,
                tree_changed=None, on_default_branch=None, counts={"passed": n}, seconds_reported=None,
                load_start=None, load_end=None, child_cpu_seconds=None, failing=(), flaky=(),
                failing_sig=None)
            data = RunData(runner="pytest", seconds_reported=None, tests=(CaseResult(
                id=f"{label}::{n}", file=None, seconds=None, state="passed", failure_type=None,
                attempts=1),))
            history.write_run(store, meta, data)
    """)
    go = tmp_path / "go"
    procs = []
    for label in ("one", "two"):
        ready = tmp_path / f"ready-{label}"
        procs.append((ready, subprocess.Popen(
            [sys.executable, "-c", script, str(REPO_ROOT), str(tmp_path / "store"), label, str(ready),
             str(go)], stdin=subprocess.DEVNULL)))
    try:
        while not all(ready.exists() for ready, _ in procs):
            if any(proc.poll() is not None for _, proc in procs):
                break
            time.sleep(0.005)
        go.touch()
        codes = [proc.wait(timeout=120) for _, proc in procs]
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert codes == [0, 0]
    runs = list(history.iter_runs(tmp_path / "store"))
    assert sorted(r.label for r in runs) == ["one"] * 25 + ["two"] * 25
    assert len({r.id for r in runs}) == 50
    for run in runs:
        [case] = history.load_tests(tmp_path / "store", run.id).tests
        assert case.id == f"{run.label}::{run.counts['passed']}"


def test_a_damaged_meta_file_is_skipped_and_the_others_are_listed(tmp_path, capsys):
    ids = ["20261126T100000000000Z-00000001", "20261126T100001000000Z-00000002",
           "20261126T100002000000Z-00000003"]
    for run_id in ids:
        history.write_run(tmp_path, _meta(run_id), None)
    (tmp_path / "runs" / f"{ids[1]}.meta.json").write_bytes(b'{"id": "trunc')
    listed = [r.id for r in history.iter_runs(tmp_path)]
    assert listed == [ids[2], ids[0]]
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and ids[1] in err


def test_runs_are_listed_newest_first(tmp_path):
    ids = ["20261124T090000000000Z-0000000a", "20261126T080000000000Z-0000000b",
           "20261125T230000000000Z-0000000c"]
    for run_id in ids:
        history.write_run(tmp_path, _meta(run_id), None)
    assert [r.id for r in history.iter_runs(tmp_path)] == sorted(ids, reverse=True)
    assert list(history.iter_runs(tmp_path / "missing")) == []


def test_a_run_id_is_fixed_width_utc_time_and_random_hex(tmp_path):
    run_id = history.new_run_id()
    assert re.fullmatch(r"\d{8}T\d{12}Z-[0-9a-f]{8}", run_id)
    with pytest.raises(ValueError):
        history.write_run(tmp_path, _meta("../escape"), None)
    with pytest.raises(ValueError):
        history.load_tests(tmp_path, "../escape")


def test_prune_deletes_only_runs_before_the_date_and_names_them(tmp_path):
    old = ["20261120T100000000000Z-00000001", "20261124T235959999999Z-00000002"]
    new = ["20261125T000000000000Z-00000003", "20261126T100000000000Z-00000004"]
    for run_id in old + new:
        history.write_run(tmp_path, _meta(run_id), _data())
    stranger = tmp_path / "runs" / "notes.txt"
    stranger.write_bytes(b"not a run")
    assert history.prune(tmp_path, date(2026, 11, 25)) == old
    assert [r.id for r in history.iter_runs(tmp_path)] == sorted(new, reverse=True)
    left = sorted(p.name for p in (tmp_path / "runs").iterdir())
    assert left == sorted(["notes.txt"] + [f"{i}.{s}" for i in new for s in ("meta.json", "tests.json.gz")])
    assert history.prune(tmp_path, date(2026, 11, 25)) == []


def test_the_tests_file_holds_no_failure_message(tmp_path):
    history.write_run(tmp_path, _meta(history.new_run_id()), _data())
    raw = gzip.decompress(next((tmp_path / "runs").glob("*.tests.json.gz")).read_bytes())
    case = json.loads(raw)["tests"][1]
    assert set(case) == {"id", "file", "seconds", "state", "failure_type", "attempts"}


# --------------------------------------------------------------------------- verdicts and hashes


def _run_data(failed: int | None, passed: int = 3) -> RunData | None:
    if failed is None:
        return None
    tests = [CaseResult(id=f"p{n}", file=None, seconds=None, state="passed", failure_type=None,
                        attempts=1) for n in range(passed)]
    tests += [CaseResult(id=f"f{n}", file=None, seconds=None, state="failed",
                         failure_type="AssertionError", attempts=1) for n in range(failed)]
    return RunData(runner="pytest", tests=tuple(tests), seconds_reported=None)


@pytest.mark.parametrize("code,failed,expected", [(0, 0, "passed"), (1, 2, "failed"), (1, 0, "failed"),
                         (0, 2, "conflict"), (0, None, "no-data"), (None, 0, "unknown"), (None, 2, "unknown")])
def test_verdict_of(code, failed, expected):  # failed None means no report
    assert history.verdict_of(code, _run_data(failed)) == expected


def test_a_run_with_no_test_is_not_passed():
    empty = RunData(runner="pytest", tests=(), seconds_reported=0.0)
    assert history.verdict_of(0, empty) == "no-data"
    error = RunData(runner="pytest", seconds_reported=None, tests=(CaseResult(
        id="e", file=None, seconds=None, state="error", failure_type="ImportError", attempts=1),))
    assert history.verdict_of(0, error) == "conflict"
    flaky = RunData(runner="pytest", seconds_reported=None, tests=(CaseResult(
        id="f", file=None, seconds=None, state="flaky", failure_type="AssertionError", attempts=2),))
    assert history.verdict_of(0, flaky) == "passed"


def test_the_failing_signature_differs_when_the_failure_type_differs():
    def case(id_, ftype, state="failed"):
        return CaseResult(id=id_, file=None, seconds=None, state=state, failure_type=ftype, attempts=1)

    a = history.failing_signature([case("t1", "AssertionError"), case("t2", "KeyError")])
    assert re.fullmatch(r"[0-9a-f]{64}", a)
    assert a == history.failing_signature([case("t2", "KeyError"), case("t1", "AssertionError")])
    assert a != history.failing_signature([case("t1", "TypeError"), case("t2", "KeyError")])
    assert history.failing_signature([case("t1", "x", state="passed")]) is None
    assert history.failing_signature([]) is None


def test_the_command_sha_differs_when_one_argument_is_split_in_two():
    assert tree.command_sha(["a b"]) != tree.command_sha(["a", "b"])
    assert tree.command_sha(["pytest", "-q"]) == tree.command_sha(("pytest", "-q"))
    assert re.fullmatch(r"[0-9a-f]{64}", tree.command_sha(["x"]))


# --------------------------------------------------------------------------- default branch


def _set_remote_head(repo: Path, commit: str) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/main", commit)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def test_on_default_branch_is_true_for_a_clean_tree_at_an_ancestor_of_the_remote_head(repo):
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_bytes(b"b\n")
    _set_remote_head(repo, _commit_all(repo, "second"))
    _git(repo, "checkout", "-q", first)
    tid = tree.tree_id(repo)
    assert tid.commit == first and tid.clean
    assert tree.on_default_branch(repo, tid) is True


def test_on_default_branch_is_false_for_a_dirty_tree_and_for_a_commit_that_is_not_an_ancestor(repo):
    _set_remote_head(repo, _git(repo, "rev-parse", "HEAD"))
    (repo / "README.md").write_bytes(b"dirty\n")
    dirty = tree.tree_id(repo)
    assert tree.on_default_branch(repo, dirty) is False
    _commit_all(repo, "local only")
    ahead = tree.tree_id(repo)
    assert ahead.clean
    assert tree.on_default_branch(repo, ahead) is False


def test_on_default_branch_is_empty_with_no_remote_head(repo):
    tid = tree.tree_id(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", tid.commit)
    assert tree.on_default_branch(repo, tid) is None


# --------------------------------------------------------------------------- machine identity


def test_the_machine_id_is_made_one_time_and_read_back(tmp_path):
    first = tree.machine_id(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    path = tmp_path / ".claude" / "jacked-tests-machine-id"
    assert path.read_bytes().decode("ascii").strip() == first
    assert tree.machine_id(tmp_path) == first
    assert tree.machine_id(tmp_path / "other") != first


def test_the_machine_id_is_empty_when_the_file_cannot_be_read_or_made(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_bytes(b"a file where the .claude folder must be")
    assert tree.machine_id(blocker) is None
    damaged = tmp_path / "damaged"
    (damaged / ".claude").mkdir(parents=True)
    (damaged / ".claude" / "jacked-tests-machine-id").write_bytes(b"not an id")
    assert tree.machine_id(damaged) is None


def test_the_machine_id_uses_the_jacked_home_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    made = tree.machine_id()
    assert (tmp_path / ".claude" / "jacked-tests-machine-id").read_bytes().decode().strip() == made


def test_the_machine_id_holds_no_part_of_the_host_name(tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "gethostname", lambda: "abc123def")
    made = tree.machine_id(tmp_path)
    text = (tmp_path / ".claude" / "jacked-tests-machine-id").read_bytes().decode("ascii")
    for part in ("abc123def", "abc", "123", "def", socket.gethostname()):
        assert part not in text
    assert re.fullmatch(r"[0-9a-f]{32}\n?", text) and made == text.strip()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX mode bits")
def test_the_machine_id_file_is_for_the_user_only(tmp_path):
    tree.machine_id(tmp_path)
    mode = (tmp_path / ".claude" / "jacked-tests-machine-id").stat().st_mode & 0o777
    assert mode == 0o600
