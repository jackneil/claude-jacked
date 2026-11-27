"""Tests for the tree identity, the machine identity and the history store of jacked.testdiet.

The tree tests drive the real git in throwaway repositories under ``tmp_path``.
The git config is hermetic, as in tests/unit/test_git_reality.py.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from jacked import git_guard
from jacked.testdiet import cleanup, globs, history, machine, tree
from jacked.testdiet.model import CaseResult, RunData, RunMeta, TreeId
from tests._testdiet import git as _git
from tests._testdiet import tree_of
from tests._testdiet import hermetic_git as _hermetic_git
from tests._testdiet import make_repo

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def hermetic_git(tmp_path, monkeypatch):
    """A hermetic git config: no system file, an empty global file, a fixed author."""
    return _hermetic_git(tmp_path, monkeypatch)


@pytest.fixture
def repo(hermetic_git):
    if shutil.which("git") is None:
        pytest.skip("this test drives the real git binary")
    return make_repo(hermetic_git / "repo")


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
    tid = tree_of(repo)
    assert tid == TreeId(commit=_git(repo, "rev-parse", "HEAD"),
                         tree=_git(repo, "rev-parse", "HEAD^{tree}"), clean=True)


def test_an_edit_that_is_not_committed_changes_the_tree_and_is_not_clean(repo):
    before = tree_of(repo)
    (repo / "README.md").write_bytes(b"changed\n")
    after = tree_of(repo)
    assert after.clean is False
    assert after.commit == before.commit
    assert after.tree != before.tree
    # The identity is the tree that a commit of this content has.
    _commit_all(repo, "second")
    assert after.tree == _git(repo, "rev-parse", "HEAD^{tree}")


def test_a_new_file_that_git_does_not_ignore_changes_the_tree(repo):
    before = tree_of(repo)
    (repo / "new.txt").write_bytes(b"new\n")
    after = tree_of(repo)
    assert after.clean is False and after.tree != before.tree


def test_an_ignored_file_does_not_change_the_tree(repo):
    (repo / ".gitignore").write_bytes(b"*.log\n")
    _commit_all(repo, "ignore logs")
    before = tree_of(repo)
    (repo / "debug.log").write_bytes(b"noise\n")
    after = tree_of(repo)
    assert after == before and after.clean is True


def test_two_commits_with_the_same_content_have_one_tree(repo):
    first = tree_of(repo)
    _git(repo, "commit", "-q", "--allow-empty", "-m", "empty")
    second = tree_of(repo)
    assert second.commit != first.commit
    assert second.tree == first.tree


def test_tree_id_does_not_change_the_real_index(repo):
    (repo / "README.md").write_bytes(b"changed\n")
    (repo / "new.txt").write_bytes(b"new\n")
    before = _index_bytes(repo)
    assert tree_of(repo).clean is False
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
    tid = tree_of(linked)
    assert tid.clean is False
    assert tid.tree != tree_of(repo).tree
    assert _index_bytes(linked) == linked_index
    assert _index_bytes(repo) == main_index
    assert _objects(repo) == objects


def test_tree_id_of_a_dirty_tree_writes_no_object_into_the_repository(repo):
    (repo / "README.md").write_bytes(b"a change that no object holds yet\n")
    (repo / "sub").mkdir()
    (repo / "sub" / "new.txt").write_bytes(b"a new file\n")
    count_before = _git(repo, "count-objects", "-v")
    files_before = _objects(repo)
    tid = tree_of(repo)
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
    assert tree_of(repo).clean is False
    assert len(envs) >= 3
    assert all(env.get("GIT_OPTIONAL_LOCKS") == "0" for env in envs)


def test_tree_id_leaves_no_temporary_folder(repo, tmp_path, monkeypatch):
    temp_root = tmp_path / "temp-root"
    temp_root.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp_root))
    (repo / "README.md").write_bytes(b"dirty, so git writes loose objects\n")
    (repo / "new.txt").write_bytes(b"new\n")
    assert tree_of(repo).clean is False
    assert list(temp_root.iterdir()) == []


def test_a_caller_folder_of_objects_stays_after_tree_state(repo, tmp_path):
    objects = tmp_path / "kept-objects"
    (repo / "README.md").write_bytes(b"dirty\n")
    tid = tree_of(repo, objects=objects)
    assert any(p.is_file() for p in objects.rglob("*"))
    cleanup.remove_tree_quietly(objects)
    assert not objects.exists()
    assert tid.clean is False


def test_remove_tree_removes_a_read_only_file(tmp_path):
    folder = tmp_path / "objects"
    (folder / "ab").mkdir(parents=True)
    loose = folder / "ab" / "cdef"
    loose.write_bytes(b"object")
    os.chmod(loose, stat.S_IREAD)
    if os.name != "nt":
        os.chmod(folder / "ab", stat.S_IREAD | stat.S_IEXEC)
    cleanup.remove_tree_quietly(folder)
    assert not folder.exists()
    cleanup.remove_tree_quietly(folder)  # a missing folder is not an error


def test_remove_tree_logs_a_path_that_it_cannot_remove(tmp_path, monkeypatch, caplog):
    folder = tmp_path / "stuck"
    folder.mkdir()
    (folder / "file").write_bytes(b"x")

    def refuse(*args, **kwargs):
        raise PermissionError(13, "refused")

    monkeypatch.setattr(os, "unlink", refuse)
    with caplog.at_level(logging.WARNING, logger="jacked.testdiet.cleanup"):
        cleanup.remove_tree_quietly(folder)
    assert any("cannot remove" in r.getMessage() and "file" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX mode bits")
def test_remove_tree_removes_a_folder_with_an_unreadable_subfolder(tmp_path, caplog):
    folder = tmp_path / "objects"
    locked = folder / "locked"
    locked.mkdir(parents=True)
    (locked / "inner").write_bytes(b"x")
    os.chmod(locked, 0)
    try:
        with caplog.at_level(logging.WARNING, logger="jacked.testdiet.cleanup"):
            cleanup.remove_tree_quietly(folder)  # must return, never raise
        assert not folder.exists() or any("cannot remove" in r.getMessage() for r in caplog.records)
        assert not folder.exists()
    finally:
        if locked.exists():
            os.chmod(locked, stat.S_IRWXU)


def test_remove_tree_never_raises(tmp_path, monkeypatch, caplog):
    folder = tmp_path / "f"
    folder.mkdir()

    def broken(*args, **kwargs):
        raise TypeError("a bug in a removal call")

    monkeypatch.setattr(shutil, "rmtree", broken)
    with caplog.at_level(logging.WARNING, logger="jacked.testdiet.cleanup"):
        cleanup.remove_tree_quietly(folder)
    assert any("TypeError" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link needs a privilege on Windows")
def test_remove_tree_does_not_change_the_mode_of_a_link_target(tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"keep")
    os.chmod(outside, 0o444)
    folder = tmp_path / "objects"
    folder.mkdir()
    (folder / "link").symlink_to(outside)
    real_unlink = os.unlink
    refused: list[str] = []

    def unlink(path, *args, **kwargs):
        if os.fspath(path).endswith("link") and not refused:
            refused.append(os.fspath(path))
            raise PermissionError(13, "refused once")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", unlink)
    cleanup.remove_tree_quietly(folder)
    assert refused and not folder.exists()
    assert outside.read_bytes() == b"keep"
    assert stat.S_IMODE(outside.stat().st_mode) == 0o444


def test_a_git_that_does_not_know_sparse_still_gives_a_tree(repo, monkeypatch):
    real = tree.git
    seen: list[tuple[str, ...]] = []

    def old_git(cwd, *args, env=None, stderr=None):
        seen.append(args)
        if "--sparse" in args:
            return 129, b""
        return real(cwd, *args, env=env, stderr=stderr)

    monkeypatch.setattr(tree, "git", old_git)
    (repo / "README.md").write_bytes(b"dirty\n")
    tid = tree_of(repo)
    assert tid is not None and tid.clean is False
    adds = [a for a in seen if "add" in a]
    assert len(adds) == 2 and "--sparse" in adds[0] and "--sparse" not in adds[1]


def test_a_failed_work_tree_read_gives_the_exit_code_in_the_reason(repo, monkeypatch, caplog):
    real = tree.git

    def failing_add(cwd, *args, env=None, stderr=None):
        if "add" in args:
            return 128, b""
        return real(cwd, *args, env=env, stderr=stderr)

    monkeypatch.setattr(tree, "git", failing_add)
    with caplog.at_level(logging.DEBUG, logger="jacked.testdiet.tree"):
        found, reason = tree.tree_state(repo)
    assert found is None and "128" in reason
    # The caller prints the reason, so the log stays at DEBUG.
    records = [r for r in caplog.records if "work tree read failed" in r.getMessage()]
    assert [r.levelno for r in records] == [logging.DEBUG] and "128" in records[0].getMessage()


def test_tree_id_is_empty_when_the_temporary_folder_cannot_be_made(repo, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError(13, "refused")

    monkeypatch.setattr(tempfile, "mkdtemp", refuse)
    assert tree_of(repo) is None


def _sparse_repo(repo: Path) -> Path:
    (repo / "in").mkdir()
    (repo / "out").mkdir()
    (repo / "in" / "a.txt").write_bytes(b"a\n")
    (repo / "out" / "b.txt").write_bytes(b"b\n")
    _commit_all(repo, "two folders")
    _git(repo, "sparse-checkout", "set", "in")
    assert not (repo / "out" / "b.txt").exists()
    return repo


def test_a_clean_sparse_checkout_has_the_head_tree(repo):
    _sparse_repo(repo)
    tid = tree_of(repo)
    assert tid == TreeId(commit=_git(repo, "rev-parse", "HEAD"),
                         tree=_git(repo, "rev-parse", "HEAD^{tree}"), clean=True)


def test_an_untracked_file_outside_the_sparse_cone_is_in_the_tree(repo, tmp_path):
    _sparse_repo(repo)
    objects = tmp_path / "objects"
    clean = tree_of(repo, objects=objects)
    (repo / "out").mkdir(exist_ok=True)
    (repo / "out" / "new.txt").write_bytes(b"new\n")
    dirty = tree_of(repo, objects=objects)
    assert dirty is not None and dirty.clean is False
    # The tracked file that the cone leaves out is not staged as deleted.
    assert tree.changed_paths(repo, clean, dirty, objects=objects) == ["out/new.txt"]


def test_a_repository_with_no_commit_has_no_tree(tmp_path, hermetic_git):
    if shutil.which("git") is None:
        pytest.skip("this test drives the real git binary")
    empty = tmp_path / "empty"
    empty.mkdir()
    _git(empty, "init", "-q")
    (empty / "a.txt").write_bytes(b"a\n")
    assert tree_of(empty) is None
    bare = tmp_path / "bare.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    assert tree_of(bare) is None
    plain = tmp_path / "plain"
    plain.mkdir()
    assert tree_of(plain) is None


def test_changed_paths_names_the_file_that_differs(repo):
    (repo / "keep.txt").write_bytes(b"keep\n")
    _commit_all(repo, "keep")
    a = tree_of(repo)
    (repo / "keep.txt").write_bytes(b"changed\n")
    (repo / "dir").mkdir()
    (repo / "dir" / "added.txt").write_bytes(b"added\n")
    _commit_all(repo, "change")
    b = tree_of(repo)
    assert tree.changed_paths(repo, a, b) == ["dir/added.txt", "keep.txt"]
    assert tree.changed_paths(repo, a, a) == []


def test_changed_paths_of_two_dirty_trees_reads_a_shared_object_folder(repo, tmp_path):
    objects = tmp_path / "tree-objects"
    objects.mkdir()
    (repo / "one.txt").write_bytes(b"one\n")
    a = tree_of(repo, objects=objects)
    (repo / "two.txt").write_bytes(b"two\n")
    b = tree_of(repo, objects=objects)
    assert tree.changed_paths(repo, a, b, objects=objects) == ["two.txt"]


def test_changed_paths_fails_when_a_tree_object_is_gone(repo):
    a = tree_of(repo)
    (repo / "README.md").write_bytes(b"dirty\n")
    b = tree_of(repo)
    with pytest.raises(tree.TreeError):
        tree.changed_paths(repo, a, b)


def test_the_inputs_fingerprint_changes_when_an_ignored_input_changes(repo):
    (repo / ".gitignore").write_bytes(b"data/\n")
    _commit_all(repo, "ignore data")
    (repo / "data").mkdir()
    cache = repo / "data" / "cache.bin"
    cache.write_bytes(b"one")
    assert globs.inputs_fingerprint(repo, []) is None
    first = globs.inputs_fingerprint(repo, ["data/*.bin"])
    assert first == globs.inputs_fingerprint(repo, ["data/*.bin"])
    tid = tree_of(repo)
    cache.write_bytes(b"one and more")
    assert tree_of(repo) == tid  # git does not see the change
    second = globs.inputs_fingerprint(repo, ["data/*.bin"])
    assert second != first
    stat = cache.stat()
    os.utime(cache, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert globs.inputs_fingerprint(repo, ["data/*.bin"]) != second
    # A glob that matches nothing is not the same as no glob.
    assert globs.inputs_fingerprint(repo, ["nothing/*"]) is not None


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


@pytest.mark.parametrize("damage", ["middle", "truncated"])
def test_a_damaged_tests_file_reads_as_none_with_one_warning(tmp_path, capsys, damage):
    meta = _meta(history.new_run_id())
    history.write_run(tmp_path, meta, _data())
    path = tmp_path / "runs" / f"{meta.id}.tests.json.gz"
    raw = bytearray(path.read_bytes())
    if damage == "middle":
        for n in range(12, len(raw) - 8):
            raw[n] ^= 0xFF
    else:
        del raw[len(raw) // 2:]
    path.write_bytes(bytes(raw))
    assert history.load_tests(tmp_path, meta.id) is None
    err = capsys.readouterr().err
    assert err.count("\n") == 1 and meta.id in err


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
    """Two real processes write 25 runs each into one store, behind a start barrier.

    It proves that the run IDs are unique and that no run holds the data of
    another run (no cross-talk). It does not prove each order of the writes.
    """
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
        deadline = time.monotonic() + 120
        while not all(ready.exists() for ready, _ in procs) and time.monotonic() < deadline:
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


def test_prune_spares_a_tests_file_with_no_meta_file_that_is_younger_than_a_day(tmp_path):
    writing = history.new_run_id()
    history.write_run(tmp_path, _meta(writing), _data())
    (tmp_path / "runs" / f"{writing}.meta.json").unlink()  # the writer has not written it yet
    complete = history.new_run_id()
    history.write_run(tmp_path, _meta(complete), _data())
    assert history.prune(tmp_path, date(2100, 1, 1)) == [complete]
    assert sorted(p.name for p in (tmp_path / "runs").iterdir()) == [f"{writing}.tests.json.gz"]


def test_prune_deletes_a_tests_file_with_no_meta_file_that_is_older_than_a_day(tmp_path):
    stopped = "20261120T100000000000Z-0000000f"
    history.write_run(tmp_path, _meta(stopped), _data())
    (tmp_path / "runs" / f"{stopped}.meta.json").unlink()
    now = datetime(2026, 11, 21, 10, 0, 1, tzinfo=timezone.utc)
    assert history.prune(tmp_path, date(2026, 11, 25), now=now) == [stopped]
    assert list((tmp_path / "runs").iterdir()) == []
    history.write_run(tmp_path, _meta(stopped), _data())
    (tmp_path / "runs" / f"{stopped}.meta.json").unlink()
    young = datetime(2026, 11, 21, 9, 59, 59, tzinfo=timezone.utc)
    assert history.prune(tmp_path, date(2026, 11, 25), now=young) == []


def test_prune_refuses_a_naive_now(tmp_path):
    history.write_run(tmp_path, _meta("20261120T100000000000Z-00000001"), None)
    with pytest.raises(ValueError):
        history.prune(tmp_path, date(2026, 11, 25), now=datetime(2026, 11, 26, 10, 0))
    with pytest.raises(ValueError):  # also with no runs folder
        history.prune(tmp_path / "empty", date(2026, 11, 25), now=datetime(2026, 11, 26, 10, 0))


def test_a_prune_failure_names_the_file_and_keeps_the_deleted_ids(tmp_path, monkeypatch):
    ids = ["20261120T100000000000Z-00000001", "20261121T100000000000Z-00000002",
           "20261122T100000000000Z-00000003"]
    for run_id in ids:
        history.write_run(tmp_path, _meta(run_id), _data())
    real_unlink = Path.unlink

    def unlink(self, *args, **kwargs):
        if self.name == f"{ids[1]}.meta.json":
            raise PermissionError(13, "refused")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    with pytest.raises(history.PruneError) as caught:
        history.prune(tmp_path, date(2026, 11, 25))
    assert caught.value.deleted == [ids[0], ids[2]]
    assert caught.value.failed == [(f"{ids[1]}.meta.json", "PermissionError")]
    assert f"{ids[1]}.meta.json" in str(caught.value)
    assert [r.id for r in history.iter_runs(tmp_path)] == [ids[1]]
    assert history.load_tests(tmp_path, ids[1]) == _data()


def test_a_tree_with_a_wrong_sub_field_reads_as_no_tree(tmp_path):
    for bad in ({"commit": "c" * 40, "tree": "d" * 40}, {"commit": "c" * 40, "tree": 5, "clean": True},
                {"commit": "not hex", "tree": "d" * 40, "clean": True},
                {"commit": "c" * 40, "tree": "d" * 40, "clean": "yes"}, "a text"):
        store = tmp_path / str(abs(hash(str(bad))))
        meta = _meta(history.new_run_id())
        history.write_run(store, meta, None)
        path = store / "runs" / f"{meta.id}.meta.json"
        raw = json.loads(path.read_bytes())
        raw["tree"] = bad
        path.write_bytes(json.dumps(raw).encode("utf-8"))
        [back] = history.iter_runs(store)
        assert back.tree is None and back.label == "gate"


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link needs a privilege on Windows")
def test_a_meta_file_that_is_not_a_regular_file_is_skipped(tmp_path, capsys):
    meta = _meta(history.new_run_id())
    history.write_run(tmp_path, meta, None)
    path = tmp_path / "runs" / f"{meta.id}.meta.json"
    path.unlink()
    path.mkdir()
    assert list(history.iter_runs(tmp_path)) == []
    assert meta.id in capsys.readouterr().err


def test_the_tests_file_holds_no_failure_message(tmp_path):
    history.write_run(tmp_path, _meta(history.new_run_id()), _data())
    raw = gzip.decompress(next((tmp_path / "runs").glob("*.tests.json.gz")).read_bytes())
    case = json.loads(raw)["tests"][1]
    assert set(case) == {"id", "file", "seconds", "state", "failure_type", "attempts"}


# --------------------------------------------------------------------------- verdicts and hashes


def _run_data(failed: int | str | None, passed: int = 3) -> RunData | None:
    if failed is None:
        return None
    if failed == "all-skipped":
        return RunData(runner="pytest", seconds_reported=None, tests=tuple(
            CaseResult(id=f"s{n}", file=None, seconds=None, state="skipped", failure_type=None,
                       attempts=1) for n in range(3)))
    tests = [CaseResult(id=f"p{n}", file=None, seconds=None, state="passed", failure_type=None,
                        attempts=1) for n in range(passed)]
    tests += [CaseResult(id=f"f{n}", file=None, seconds=None, state="failed",
                         failure_type="AssertionError", attempts=1) for n in range(failed)]
    return RunData(runner="pytest", tests=tuple(tests), seconds_reported=None)


@pytest.mark.parametrize("code,failed,expected", [(0, 0, "passed"), (1, 2, "failed"), (1, 0, "failed"),
                         (0, 2, "conflict"), (0, None, "no-data"), (None, 0, "unknown"), (None, 2, "unknown"),
                         (0, "all-skipped", "no-data")])
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


def test_the_command_sha_of_a_lone_surrogate_is_a_hash_not_an_error():
    assert re.fullmatch(r"[0-9a-f]{64}", tree.command_sha(["\ud800"], folder="."))
    assert tree.command_sha(["\ud800"], folder=".") != tree.command_sha(["\ud801"], folder=".")


def test_the_command_sha_differs_when_one_argument_is_split_in_two():
    assert tree.command_sha(["a b"], folder=".") != tree.command_sha(["a", "b"], folder=".")
    assert tree.command_sha(["pytest", "-q"], folder=".") == tree.command_sha(("pytest", "-q"), folder=".")
    assert re.fullmatch(r"[0-9a-f]{64}", tree.command_sha(["x"], folder="."))


def test_the_command_sha_holds_the_folder_and_the_join_cannot_be_moved():
    assert tree.command_sha(["pytest"], folder=".") != tree.command_sha(["pytest"], folder="sub")
    # The folder and the argv are in separate domains: no split of one text gives the other.
    assert tree.command_sha(["b", "c"], folder="a") != tree.command_sha(["c"], folder="a\0b")
    assert tree.command_sha([], folder="a") != tree.command_sha(["a"], folder="")


def test_the_work_folder_is_relative_to_the_top_and_dot_at_the_top(repo):
    (repo / "sub" / "deep").mkdir(parents=True)
    assert tree.work_folder(repo) == "."
    assert tree.work_folder(repo / "sub") == "sub"
    assert tree.work_folder(repo / "sub" / "deep") == "sub/deep"


def test_a_written_record_has_the_schema_and_a_bad_schema_reads_as_none(tmp_path):
    meta = _meta(history.new_run_id(), folder="sub", schema=1)
    history.write_run(tmp_path, meta, None)
    path = tmp_path / "runs" / f"{meta.id}.meta.json"
    raw = json.loads(path.read_bytes())
    assert (raw["schema"], raw["folder"]) == (1, "sub")
    assert list(history.iter_runs(tmp_path)) == [meta]
    for bad in ("1", True, 1.5, None):
        raw["schema"] = bad
        path.write_bytes(json.dumps(raw).encode("utf-8"))
        [back] = history.iter_runs(tmp_path)
        assert back.schema is None and back.folder == "sub"
    del raw["schema"]
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    assert next(history.iter_runs(tmp_path)).schema is None


# --------------------------------------------------------------------------- default branch


def _set_remote_head(repo: Path, commit: str) -> None:
    _git(repo, "update-ref", "refs/remotes/origin/main", commit)
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")


def test_on_default_branch_is_true_for_a_clean_tree_at_an_ancestor_of_the_remote_head(repo):
    first = _git(repo, "rev-parse", "HEAD")
    (repo / "b.txt").write_bytes(b"b\n")
    _set_remote_head(repo, _commit_all(repo, "second"))
    _git(repo, "checkout", "-q", first)
    tid = tree_of(repo)
    assert tid.commit == first and tid.clean
    assert tree.on_default_branch(repo, tid) is True


def test_on_default_branch_is_false_for_a_dirty_tree_and_for_a_commit_that_is_not_an_ancestor(repo):
    _set_remote_head(repo, _git(repo, "rev-parse", "HEAD"))
    (repo / "README.md").write_bytes(b"dirty\n")
    dirty = tree_of(repo)
    assert tree.on_default_branch(repo, dirty) is False
    _commit_all(repo, "local only")
    ahead = tree_of(repo)
    assert ahead.clean
    assert tree.on_default_branch(repo, ahead) is False


def test_on_default_branch_is_empty_with_no_remote_head(repo):
    tid = tree_of(repo)
    _git(repo, "update-ref", "refs/remotes/origin/main", tid.commit)
    assert tree.on_default_branch(repo, tid) is None


# --------------------------------------------------------------------------- machine identity


def test_the_machine_id_is_made_one_time_and_read_back(tmp_path):
    first = machine.machine_id(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    path = tmp_path / ".claude" / "jacked-tests-machine-id"
    assert path.read_bytes().decode("ascii").strip() == first
    assert machine.machine_id(tmp_path) == first
    assert machine.machine_id(tmp_path / "other") != first


def test_two_processes_that_make_the_machine_id_at_once_get_one_id(tmp_path):
    script = textwrap.dedent("""
        import sys, time
        sys.path.insert(0, sys.argv[1])
        from pathlib import Path
        from jacked.testdiet import machine
        Path(sys.argv[3]).touch()
        while not Path(sys.argv[4]).exists():
            time.sleep(0.001)
        print(machine.machine_id(Path(sys.argv[2])))
    """)
    go = tmp_path / "go"
    procs = []
    for n in range(4):
        ready = tmp_path / f"ready-{n}"
        procs.append((ready, subprocess.Popen(
            [sys.executable, "-c", script, str(REPO_ROOT), str(tmp_path / "home"), str(ready), str(go)],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, text=True)))
    try:
        deadline = time.monotonic() + 120
        while not all(ready.exists() for ready, _ in procs) and time.monotonic() < deadline:
            time.sleep(0.005)
        go.touch()
        outs = [proc.communicate(timeout=120)[0].strip() for _, proc in procs]
    finally:
        for _, proc in procs:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert [proc.returncode for _, proc in procs] == [0, 0, 0, 0]
    assert len(set(outs)) == 1 and re.fullmatch(r"[0-9a-f]{32}", outs[0])
    folder = tmp_path / "home" / ".claude"
    assert sorted(p.name for p in folder.iterdir()) == ["jacked-tests-machine-id"]


def test_the_machine_id_is_made_on_a_file_system_with_no_hard_links(tmp_path, monkeypatch):
    def no_links(*args, **kwargs):
        raise OSError(1, "Operation not permitted")

    monkeypatch.setattr(os, "link", no_links)
    first = machine.machine_id(tmp_path)
    assert re.fullmatch(r"[0-9a-f]{32}", first)
    assert machine.machine_id(tmp_path) == first
    assert sorted(p.name for p in (tmp_path / ".claude").iterdir()) == ["jacked-tests-machine-id"]


def test_the_machine_id_is_empty_when_the_file_cannot_be_read_or_made(tmp_path):
    blocker = tmp_path / "blocked"
    blocker.write_bytes(b"a file where the .claude folder must be")
    assert machine.machine_id(blocker) is None
    folder = tmp_path / "folder"
    (folder / ".claude" / "jacked-tests-machine-id").mkdir(parents=True)
    value, problem = machine.identity(folder)
    assert value is None and "Delete" not in problem and (folder / ".claude" / "jacked-tests-machine-id").is_dir()


@pytest.mark.parametrize("content", [b"not an id", b"", b"\xff\xfe", b"a" * 31])
def test_a_machine_id_file_with_bad_content_is_made_again_with_one_line(tmp_path, capsys, content):
    path = tmp_path / ".claude" / "jacked-tests-machine-id"
    path.parent.mkdir(parents=True)
    path.write_bytes(content)
    value = machine.machine_id(tmp_path)
    assert value is not None and re.fullmatch(r"[0-9a-f]{32}", value)
    assert path.read_bytes() == (value + "\n").encode("ascii")
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "made a new one" in err
    assert machine.machine_id(tmp_path) == value and capsys.readouterr().err == ""


def test_a_valid_machine_id_is_never_replaced(tmp_path, monkeypatch):
    path = tmp_path / ".claude" / "jacked-tests-machine-id"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"bad")
    other = "b" * 32
    real = machine.atomic_write_bytes

    def another_process_first(target, data, **kwargs):
        # Another process writes a valid identity between the read and the replace.
        path.write_bytes((other + "\n").encode("ascii"))
        return real(target, data, **kwargs)

    monkeypatch.setattr(machine, "atomic_write_bytes", another_process_first)
    assert machine.machine_id(tmp_path) == other
    assert path.read_bytes() == (other + "\n").encode("ascii")


def test_the_machine_id_uses_the_jacked_home_by_default(tmp_path, monkeypatch):
    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    made = machine.machine_id()
    assert (tmp_path / ".claude" / "jacked-tests-machine-id").read_bytes().decode().strip() == made


def test_the_machine_id_holds_no_part_of_the_host_name(tmp_path, monkeypatch):
    # The fake name has letters outside 0-9a-f, so random hex cannot hold it by chance.
    calls: list[str] = []

    def recorder(name):
        def fake(*args, **kwargs):
            calls.append(name)
            return "zqxhost.lan"
        return fake

    monkeypatch.setattr(socket, "gethostname", recorder("gethostname"))
    monkeypatch.setattr(socket, "getfqdn", recorder("getfqdn"))
    monkeypatch.setattr(platform, "node", recorder("node"))
    made = machine.machine_id(tmp_path)
    text = (tmp_path / ".claude" / "jacked-tests-machine-id").read_bytes().decode("ascii")
    assert calls == []
    for part in ("zqxhost.lan", "zqxhost"):
        assert part not in text
    assert re.fullmatch(r"[0-9a-f]{32}\n", text) and made == text.strip()


@pytest.mark.skipif(os.name == "nt", reason="Windows has no POSIX mode bits")
def test_the_machine_id_file_is_for_the_user_only(tmp_path):
    machine.machine_id(tmp_path)
    mode = (tmp_path / ".claude" / "jacked-tests-machine-id").stat().st_mode & 0o777
    assert mode == 0o600


@pytest.mark.skipif(os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
                    reason="needs POSIX modes and a user that is not root")
def test_a_failed_work_tree_read_raises_with_the_first_error_line_of_git(repo):
    locked = repo / "locked.txt"
    locked.write_bytes(b"x")
    locked.chmod(0)
    try:
        with pytest.raises(tree.TreeError) as caught:
            tree.read_tree_id(repo)
        assert tree_of(repo) is None
    finally:
        locked.chmod(0o600)
    assert "git add exited with" in str(caught.value)
    assert "locked.txt" in caught.value.detail


def test_a_same_size_edit_that_git_status_sees_is_never_a_clean_tree(repo):
    # The edit keeps the size, the inode and the old modification time, and
    # git does not check ctime. The real index has the time of its entries,
    # so git sees the entry as racy and reads the content. A copy of the
    # index with a new time would trust the stat data and see no change.
    old = 1_000_000_000 * 10**9
    path = repo / "same.py"
    path.write_bytes(b"[1, 2]\n")
    os.utime(path, ns=(old, old))
    _git(repo, "config", "core.trustctime", "false")
    _git(repo, "add", "same.py")
    _git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "same size")
    path.write_bytes(b"VALUES\n")
    os.utime(path, ns=(old, old))
    index = repo / ".git" / "index"
    os.utime(index, ns=(old, old))
    # Real git sees the change (with no index write, so the test state stays).
    status = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True,
                            text=True, stdin=subprocess.DEVNULL, env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"})
    assert status.stdout == " M same.py\n"
    assert os.stat(index).st_mtime_ns == old
    tid = tree_of(repo)
    assert tid is not None and tid.clean is False


@pytest.mark.skipif(os.name == "nt", reason="Git for Windows reads the creation time as ctime, so an "
                    "edit that restores the time and the size is not seen there, as with git status")
def test_a_repo_that_does_not_trust_ctime_still_sees_an_edit_that_restores_the_time(repo):
    import time

    old = 1_000_000_000 * 10**9
    path = repo / "same.py"
    path.write_bytes(b"[1, 2]\n")
    os.utime(path, ns=(old, old))
    _git(repo, "config", "core.trustctime", "false")
    _git(repo, "config", "core.checkStat", "minimal")
    _git(repo, "add", "same.py")
    _git(repo, "commit", "-q", "-m", "same size")
    time.sleep(1.1)  # the change time of the edit is in a later second than the one in the index
    path.write_bytes(b"VALUES\n")
    os.utime(path, ns=(old, old))
    tid = tree_of(repo)
    assert tid is not None and tid.clean is False


def test_the_index_copy_never_has_a_later_time_than_the_index(tmp_path):
    source = tmp_path / "index"
    source.write_bytes(b"DIRC")
    when = 1_700_000_000_123_456_789
    os.utime(source, ns=(when, when))
    copy = tmp_path / "copy"
    tree._copy_index(source, copy)
    assert copy.read_bytes() == b"DIRC"
    assert copy.stat().st_mtime_ns <= source.stat().st_mtime_ns
    assert copy.stat().st_mtime_ns >= when // 10**9 * 10**9


def test_an_index_copy_that_stays_later_after_the_floor_gets_time_zero(tmp_path, monkeypatch):
    source = tmp_path / "index"
    source.write_bytes(b"DIRC")
    when = 1_700_000_000_123_456_789
    os.utime(source, ns=(when, when))
    real_utime = os.utime

    def rounds_up(path, *args, ns=None, **kwargs):
        if ns is not None and ns != (0, 0) and Path(path) != source:
            ns = (ns[0] + 2 * 10**9, ns[1] + 2 * 10**9)
        return real_utime(path, *args, ns=ns, **kwargs)

    monkeypatch.setattr(os, "utime", rounds_up)
    copy = tmp_path / "copy"
    tree._copy_index(source, copy)
    assert copy.stat().st_mtime_ns == 0


def _index_with_rounding_utime(tmp_path, monkeypatch, refuse):
    """An index, and an ``os.utime`` that rounds the copy up and refuses the times in ``refuse``."""
    source = tmp_path / "index"
    source.write_bytes(b"DIRC")
    when = 1_700_000_000_123_456_789
    os.utime(source, ns=(when, when))
    real_utime = os.utime

    def fat_like(path, *args, ns=None, **kwargs):
        if Path(path) != source and ns in refuse:
            raise OSError(22, "Invalid argument")
        if ns is not None and ns[1] > tree._FAT_FLOOR_NS and Path(path) != source:
            ns = (ns[0] + 2 * 10**9, ns[1] + 2 * 10**9)
        return real_utime(path, *args, ns=ns, **kwargs)

    monkeypatch.setattr(os, "utime", fat_like)
    return source, tmp_path / "copy"


def test_an_index_copy_on_a_file_system_that_refuses_time_zero_gets_the_1980_floor(tmp_path, monkeypatch):
    source, copy = _index_with_rounding_utime(tmp_path, monkeypatch, refuse={(0, 0)})
    tree._copy_index(source, copy)
    assert copy.stat().st_mtime_ns == 315_532_800 * 10**9
    assert copy.stat().st_mtime_ns <= source.stat().st_mtime_ns


def test_an_index_copy_that_cannot_get_an_early_time_raises_tree_error(tmp_path, monkeypatch):
    floor = (tree._FAT_FLOOR_NS, tree._FAT_FLOOR_NS)
    source, copy = _index_with_rounding_utime(tmp_path, monkeypatch, refuse={(0, 0), floor})
    with pytest.raises(tree.TreeError, match="index copy"):
        tree._copy_index(source, copy)


# --------------------------------------------------------------------------- index bits that hide an edit


def _bits(repo: Path) -> str:
    return _git(repo, "ls-files", "-v")


def test_core_ignorestat_does_not_hide_an_edit_from_the_tree(repo):
    _git(repo, "config", "core.ignoreStat", "true")
    (repo / "c.txt").write_bytes(b"one\n")
    _commit_all(repo, "c")
    assert "h c.txt" in _bits(repo)  # git set the assume-unchanged bit
    clean = tree_of(repo)
    assert clean.clean is True
    (repo / "c.txt").write_bytes(b"a longer edit\n")
    changed = tree_of(repo)
    assert changed.tree != clean.tree and changed.clean is False
    assert "h c.txt" in _bits(repo)  # the real index keeps its bit


def test_an_assume_unchanged_bit_does_not_hide_an_edit_from_the_tree(repo):
    clean = tree_of(repo)
    _git(repo, "update-index", "--assume-unchanged", "README.md")
    (repo / "README.md").write_bytes(b"hidden edit\n")
    changed = tree_of(repo)
    assert changed.tree != clean.tree and changed.clean is False
    assert "h README.md" in _bits(repo)


def test_a_skip_worktree_bit_outside_a_sparse_checkout_does_not_hide_an_edit(repo):
    (repo / "sub").mkdir()
    (repo / "sub" / "a.txt").write_bytes(b"a\n")
    _commit_all(repo, "sub")
    clean = tree_of(repo)
    _git(repo, "update-index", "--skip-worktree", "sub/a.txt")
    (repo / "sub" / "a.txt").write_bytes(b"hidden edit\n")
    changed = tree_of(repo)
    assert changed.tree != clean.tree and changed.clean is False
    assert "S sub/a.txt" in _bits(repo)


def test_clearing_the_bits_keeps_the_early_time_of_the_index_copy(repo, monkeypatch):
    # A same-size edit in the same second: the copy must stay as old as the real index.
    _git(repo, "update-index", "--assume-unchanged", "README.md")
    times = []
    real = tree._copy_index

    def spy(source, target):
        info = real(source, target)
        times.append(info.st_mtime_ns)
        return info

    monkeypatch.setattr(tree, "_copy_index", spy)
    seen: list[int] = []
    real_git = tree.git

    def watch(cwd, *args, env=None, stderr=None, stdin=None):
        if "add" in args and env and "GIT_INDEX_FILE" in env:
            seen.append(os.stat(env["GIT_INDEX_FILE"]).st_mtime_ns)
        return real_git(cwd, *args, env=env, stderr=stderr, stdin=stdin)

    monkeypatch.setattr(tree, "git", watch)
    assert tree_of(repo) is not None
    assert seen and times and all(ns <= times[0] for ns in seen)


# --------------------------------------------------------------------------- --input folders


def test_an_input_folder_adds_every_regular_file_below_it(repo):
    data = repo / "data"
    data.mkdir()
    empty = globs.inputs_fingerprint(repo, ["data"])
    assert empty == globs.inputs_fingerprint(repo, ["data"])
    (data / "deep").mkdir()
    (data / "deep" / ".hidden.db").write_bytes(b"one")
    added = globs.inputs_fingerprint(repo, ["data"])
    assert added != empty
    (data / "deep" / ".hidden.db").write_bytes(b"one and more")
    changed = globs.inputs_fingerprint(repo, ["data"])
    assert changed not in (empty, added)
    assert globs.inputs_fingerprint(repo, ["data/*"]) not in (empty, changed)  # data/* matches the folder deep


def test_the_input_globs_that_match_no_regular_file_are_named(repo):
    (repo / "data").mkdir()
    (repo / "one.bin").write_bytes(b"x")
    sha, empty = globs.input_fingerprint(repo, ["one.bin", "data", "nothing/*"])
    assert sha == globs.inputs_fingerprint(repo, ["one.bin", "data", "nothing/*"])
    assert empty == ["data", "nothing/*"]
    assert globs.input_fingerprint(repo, []) == (None, [])


@pytest.mark.skipif(os.name == "nt", reason="a symbolic link needs a privilege on Windows")
def test_an_input_folder_does_not_follow_a_link(repo, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "big.db").write_bytes(b"x")
    data = repo / "data"
    data.mkdir()
    (data / "link").symlink_to(outside)
    before = globs.inputs_fingerprint(repo, ["data"])
    (outside / "big.db").write_bytes(b"changed")
    assert globs.inputs_fingerprint(repo, ["data"]) == before


# --------------------------------------------------------------------------- git failures that are not "no repository"


def _refuses_owner(repo: Path) -> bool:
    env = {**os.environ, "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"}
    proc = subprocess.run(["git", "-C", str(repo), "rev-parse", "--show-toplevel"], env=env,
                          capture_output=True, stdin=subprocess.DEVNULL)
    return proc.returncode != 0


def test_a_dubious_ownership_refusal_names_safe_directory(repo, monkeypatch):
    if not _refuses_owner(repo):
        pytest.skip("this git does not know GIT_TEST_ASSUME_DIFFERENT_OWNER")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    with pytest.raises(tree.GitRefused) as caught:
        tree.work_tree_top(repo)
    text = str(caught.value)
    assert "safe.directory" in text and "dubious ownership" in text
    # The caution comes first, and the trust command is conditional on it.
    caution = text.index("only when you trust its owner")
    assert caution < text.index("git config --global") and "filter drivers" in text


def test_git_that_is_not_installed_is_its_own_error(repo, monkeypatch):
    monkeypatch.setattr(tree, "find_bin", lambda name: None)
    with pytest.raises(tree.GitMissing) as caught:
        tree.work_tree_top(repo)
    assert "git is not installed" in str(caught.value)


def test_a_git_timeout_is_its_own_error(repo, monkeypatch):
    def slow(*args, **kwargs):
        raise subprocess.TimeoutExpired("git", 600)

    monkeypatch.setattr(git_guard, "run_git", slow)
    with pytest.raises(tree.GitTimeout) as caught:
        tree.work_tree_top(repo)
    assert "did not answer" in str(caught.value)


def test_a_folder_outside_a_repository_names_the_first_error_line_of_git(hermetic_git):
    plain = hermetic_git / "plain"
    plain.mkdir()
    with pytest.raises(tree.NotAWorkTree) as caught:
        tree.work_tree_top(plain)
    assert "not in the work tree of a git repository" in str(caught.value)
    assert "not a git repository" in str(caught.value).split("repository", 1)[1]


# --------------------------------------------------------------------------- stored text that is not clean


def test_a_case_file_that_is_not_utf8_is_written_and_read_back(tmp_path):
    name = os.fsdecode(b"tests/test_\xff.py")
    data = RunData(runner="pytest", seconds_reported=None, tests=(
        CaseResult(id=name + "::t", file=name, seconds=1.0, state="passed", failure_type=None, attempts=1),))
    meta = _meta(history.new_run_id(), failing=(name + "::t",))
    history.write_run(tmp_path, meta, data)
    [back] = history.iter_runs(tmp_path)
    assert back.failing == (name + "::t",)
    assert history.load_tests(tmp_path, meta.id).tests[0].file == name


@pytest.mark.parametrize("field", ["command_sha", "inputs_sha", "failing_sig"])
def test_a_hash_field_that_is_not_64_hex_reads_as_none(tmp_path, field):
    # inputs_sha is the exception: None means "no --input", so a bad value reads as INPUTS_INVALID.
    meta = _meta(history.new_run_id(), inputs_sha="e" * 64, failing_sig="f" * 64)
    history.write_run(tmp_path, meta, None)
    path = tmp_path / "runs" / f"{meta.id}.meta.json"
    raw = json.loads(path.read_bytes())
    raw[field] = "\nforged line"
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    [back] = history.iter_runs(tmp_path)
    assert getattr(back, field) == (history.INPUTS_INVALID if field == "inputs_sha" else None)
    raw[field] = "unavailable"
    path.write_bytes(json.dumps(raw).encode("utf-8"))
    [back] = history.iter_runs(tmp_path)
    assert getattr(back, field) == ("unavailable" if field == "inputs_sha" else None)


def test_a_damaged_record_line_names_the_cause(tmp_path, capsys):
    meta = _meta(history.new_run_id())
    history.write_run(tmp_path, meta, None)
    path = tmp_path / "runs" / f"{meta.id}.meta.json"
    raw = json.loads(path.read_bytes())
    raw["counts"] = "x"
    path.write_bytes(json.dumps(raw).encode("ascii"))
    assert list(history.iter_runs(tmp_path)) == []
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "counts is not a map of numbers" in err


@pytest.mark.skipif(os.name == "nt", reason="a folder in place of a file reads differently on Windows")
def test_a_meta_file_that_cannot_be_read_gives_a_cause_with_no_path(tmp_path, capsys):
    run_id = history.new_run_id()
    (tmp_path / "runs" / f"{run_id}.meta.json").mkdir(parents=True)
    assert list(history.iter_runs(tmp_path)) == []
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "Error" in err and str(tmp_path) not in err


# --------------------------------------------------------------------------- store states


def test_prune_deletes_a_temporary_file_of_a_write_that_stopped(tmp_path, capsys):
    from datetime import date, datetime, timedelta, timezone

    runs = tmp_path / "runs"
    old_id = history.new_run_id(datetime(2026, 1, 1, tzinfo=timezone.utc))
    young_id = history.new_run_id()
    runs.mkdir(parents=True)
    stale = runs / f".{old_id}.meta.json.k3j2h1.tmp"
    young = runs / f".{young_id}.tests.json.gz.a1b2c3.tmp"
    other = runs / "notes.txt"
    for path in (stale, young, other):
        path.write_bytes(b"x")
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert history.prune(tmp_path, date(2026, 6, 1), now=later, dry_run=True) == []
    assert stale.exists() and "would delete 1 temporary file" in capsys.readouterr().err
    assert history.prune(tmp_path, date(2026, 6, 1), now=later) == []
    assert not stale.exists() and young.exists() and other.exists()
    assert "deleted 1 temporary file" in capsys.readouterr().err


def test_write_run_flushes_each_file_to_disk_before_the_rename(tmp_path, monkeypatch):
    calls = []
    real = history.atomic_write_bytes
    monkeypatch.setattr(history, "atomic_write_bytes",
                        lambda path, data, **kw: calls.append((Path(path).name, kw.get("fsync"))) or
                        real(path, data, **kw))
    meta = _meta(history.new_run_id())
    data = RunData(runner="pytest", seconds_reported=None, tests=(
        CaseResult(id="t::a", file="t.py", seconds=1.0, state="passed", failure_type=None, attempts=1),))
    history.write_run(tmp_path, meta, data)
    assert [fsync for _, fsync in calls] == [True, True]


@pytest.mark.skipif(os.name == "nt", reason="a folder mode needs POSIX")
@pytest.mark.parametrize("state", ["file", "unreadable"])
def test_a_runs_path_that_cannot_be_listed_gives_one_line(tmp_path, capsys, state):
    runs = tmp_path / "runs"
    if state == "file":
        runs.write_bytes(b"not a folder")
    else:
        runs.mkdir()
        runs.chmod(0)
        if os.access(runs, os.R_OK):
            runs.chmod(0o700)
            pytest.skip("this user can read a folder with mode 0 (root)")
    try:
        assert list(history.iter_runs(tmp_path)) == []
    finally:
        if runs.is_dir():
            runs.chmod(0o700)
    err = capsys.readouterr().err
    assert len(err.splitlines()) == 1 and "cannot be read" in err and str(tmp_path) not in err


@pytest.mark.parametrize("field, value", [("seconds", "1.5"), ("seconds", True), ("state", "odd"),
                                          ("attempts", "2"), ("id", 7), ("file", 3), ("failure_type", [1])])
def test_a_tests_file_with_a_field_of_the_wrong_type_is_damaged(tmp_path, capsys, field, value):
    import gzip as _gzip

    meta = _meta(history.new_run_id())
    case = {"id": "t::a", "file": "t.py", "seconds": 1.0, "state": "passed", "failure_type": None,
            "attempts": 1, field: value}
    history.write_run(tmp_path, meta, None)
    raw = json.dumps({"runner": "pytest", "seconds_reported": None, "tests": [case]}).encode("ascii")
    (tmp_path / "runs" / f"{meta.id}.tests.json.gz").write_bytes(_gzip.compress(raw))
    assert history.load_tests(tmp_path, meta.id) is None
    assert "skipped the damaged record" in capsys.readouterr().err


def test_a_temporary_file_that_cannot_be_deleted_keeps_the_new_machine_id(tmp_path, monkeypatch, caplog):
    real = Path.unlink

    def refuse_temp(self, missing_ok=False):
        if self.name.endswith(".tmp"):
            raise PermissionError(13, "Permission denied")
        return real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_temp)
    with caplog.at_level("DEBUG", logger="jacked.testdiet.machine"):
        value = machine.machine_id(tmp_path)
    assert value is not None and re.fullmatch(r"[0-9a-f]{32}", value)
    assert machine.machine_id(tmp_path) == value
    assert any("temporary file" in record.getMessage() for record in caplog.records)


# --------------------------------------------------------------------------- ASCII only in stored values

_ARABIC_ZERO = "٠"  # a digit for str.isdigit and for re's \d without re.ASCII


def _validators():
    from jacked.testdiet import model

    good_id = history.new_run_id()
    return [
        ("RUN_ID_RE", history.RUN_ID_RE, good_id),
        ("_TEMP_RE", history._TEMP_RE, f".{good_id}.meta.json.abc123.tmp"),
        ("MACHINE_ID_RE", model.MACHINE_ID_RE, "0" * 32),
        ("_SHA256_RE", history._SHA256_RE, "0" * 64),
        ("_HEX_ID_RE", history._HEX_ID_RE, "0" * 40),
        ("LABEL_RE", model.LABEL_RE, "ci0"),
    ]


@pytest.mark.parametrize("name, pattern, good", _validators(), ids=[v[0] for v in _validators()])
def test_each_validator_of_a_stored_value_refuses_a_non_ascii_digit(name, pattern, good):
    assert pattern.fullmatch(good)
    first = good.index("0") if "0" in good else next(i for i, ch in enumerate(good) if ch.isdigit())
    bad = good[:first] + _ARABIC_ZERO + good[first + 1:]
    assert pattern.fullmatch(bad) is None, name


def test_a_run_id_with_non_ascii_digits_is_refused_by_history_prune_and_run(tmp_path, capsys):
    from datetime import date

    good = _meta(history.new_run_id())
    history.write_run(tmp_path, good, None)
    fake = _ARABIC_ZERO * 8 + "T" + "0" * 12 + "Z-abcdef12"
    assert history.RUN_ID_RE.fullmatch(fake) is None
    path = tmp_path / "runs" / f"{fake}.meta.json"
    path.write_bytes((tmp_path / "runs" / f"{good.id}.meta.json").read_bytes())
    assert [m.id for m in history.iter_runs(tmp_path)] == [good.id]
    with pytest.raises(ValueError):
        history.check_id(fake)
    with pytest.raises(ValueError):
        history.load_tests(tmp_path, fake)
    assert history.prune(tmp_path, date(2099, 1, 1), dry_run=True) == [good.id]
    assert fake not in history.run_ids(tmp_path)
    history.prune(tmp_path, date(2099, 1, 1))
    assert path.exists()
    from jacked.testdiet import views

    with pytest.raises(views.ViewError):
        views.profile(tmp_path, fake)


@pytest.mark.parametrize("bit", ["--assume-unchanged", "--skip-worktree"])
def test_a_hidden_edit_outside_the_gate_folder_is_in_the_tree(repo, bit):
    # ls-files in a subfolder lists that subfolder only; add -A hashes the whole tree.
    (repo / "sub").mkdir()
    (repo / "sub" / "x.txt").write_bytes(b"x\n")
    _commit_all(repo, "sub")
    head_tree = _git(repo, "rev-parse", "HEAD^{tree}")
    _git(repo, "update-index", bit, "README.md")
    (repo / "README.md").write_bytes(b"hidden edit at the top\n")
    before = _index_bytes(repo)
    found, _reason = tree.tree_state(repo / "sub")
    assert found.tree != head_tree and found.clean is False
    assert _index_bytes(repo) == before


def test_a_git_trace_line_is_not_the_detail(tmp_path, monkeypatch):
    plain = tmp_path / "plain"
    plain.mkdir()
    monkeypatch.setenv("GIT_TRACE", "1")
    with pytest.raises(tree.NotAWorkTree) as caught:
        tree.work_tree_top(plain)
    assert "not a git repository" in str(caught.value) and "trace:" not in str(caught.value)


def test_a_refusal_after_a_long_trace_is_still_a_refusal(repo, monkeypatch):
    if not _refuses_owner(repo):
        pytest.skip("this git does not know GIT_TEST_ASSUME_DIFFERENT_OWNER")
    monkeypatch.setenv("GIT_TEST_ASSUME_DIFFERENT_OWNER", "1")
    monkeypatch.setenv("GIT_TRACE2_PERF", "1")
    monkeypatch.setenv("GIT_TRACE", "1")
    with pytest.raises(tree.GitRefused) as caught:
        tree.work_tree_top(repo)
    assert "trace" not in str(caught.value).split("(", 1)[1].split(")", 1)[0]


def test_the_git_detail_prefers_the_error_line_and_is_cut_at_300():
    assert tree.git_detail("12:00 trace: run\nwarning: w\nfatal: f\n") == "warning: w"
    assert tree.git_detail("trace: a\n\nsome text\n") == "trace: a"
    assert tree.git_detail("") == ""
    assert len(tree.git_detail("fatal: " + "x" * 5000)) == 300
    assert len(tree.git_detail("x" * 5000)) == 300


def test_tree_state_is_the_one_entry_for_the_identity():
    import inspect

    # tree_id and its warn-once flag had no caller outside the tests after M8.
    assert not hasattr(tree, "tree_id") and not hasattr(tree, "_warned_add")
    assert "warn" not in inspect.signature(tree.tree_state).parameters
    assert "tree_id" not in (tree.__doc__ or "").replace("read_tree_id", "")


@pytest.mark.skipif(os.name == "nt", reason="a file name with a line break is POSIX only")
def test_a_cleanup_warning_shows_a_line_break_in_a_name_as_an_escape(tmp_path, monkeypatch, caplog):
    from jacked.testdiet import cleanup

    folder = tmp_path / "work"
    folder.mkdir()
    (folder / "a\nb").write_bytes(b"x")
    real_unlink = os.unlink

    def refuse(path, *args, **kwargs):
        if "\n" in os.fsdecode(path if not isinstance(path, int) else ""):
            raise PermissionError(13, "refused")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(os, "unlink", refuse)
    with caplog.at_level(logging.WARNING, logger="jacked.testdiet.cleanup"):
        cleanup.remove_tree_quietly(folder)
    lines = [r.getMessage() for r in caplog.records if "cannot remove" in r.getMessage()]
    assert lines and all("\n" not in line for line in lines)
    assert any("a\\nb" in line for line in lines)


def test_two_repairs_of_one_bad_machine_id_end_with_one_identity(tmp_path, monkeypatch):
    # g-L2: writer A passes its validity check and pauses; then B starts. With no lock, B passes its
    # check too, A writes, and B replaces A's valid identity. With the lock, B waits and reads A's.
    import threading

    from jacked.testdiet import machine

    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / machine.MACHINE_ID_NAME).write_bytes(b"cut by a power loss\n")
    a_checked, b_checked, a_done = threading.Event(), threading.Event(), threading.Event()
    real_write = machine.atomic_write_bytes

    def paced(path, data, **kwargs):
        check = kwargs["before_replace"]

        def before():
            check()
            if threading.current_thread().name == "A":
                a_checked.set()
                b_checked.wait(2)  # no lock: B arrives; with the lock: B waits, and this times out
            else:
                b_checked.set()
                a_done.wait(10)

        return real_write(path, data, **{**kwargs, "before_replace": before})

    monkeypatch.setattr(machine, "atomic_write_bytes", paced)
    got: dict[str, str | None] = {}

    def repair(name):
        got[name] = machine.identity(home)[0]
        if name == "A":
            a_done.set()

    first = threading.Thread(target=repair, args=("A",), name="A")
    first.start()
    assert a_checked.wait(10)
    second = threading.Thread(target=repair, args=("B",), name="B")
    second.start()
    first.join(20)
    second.join(20)
    on_disk = machine.identity(home)[0]
    assert got["A"] is not None and got["A"] == got["B"] == on_disk, (got, on_disk)
