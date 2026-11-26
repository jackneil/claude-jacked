"""Run a gate command and record it, record a report that was not wrapped, and ask if a tree passed.

``run_gate`` returns the exit code of the gate command. A recorder error
never changes that code: each step of the recorder is guarded, the error is
logged and one line goes to standard error. Only ``strict`` changes the
code, and only for a conflict (exit code 0 and failures in the report).

The order of ``run_gate``: read the tree; hash the typed command; make the
report folder when an argument holds ``{junit}``; start the child; wait;
read the report; remove the report folder; read the tree a second time;
write the record.

Each message goes to standard error and starts with ``jacked tests: ``.
Control characters (except the newline and the tab) are removed from a
message before the print, because a test ID or a file name is free text.
"""

from __future__ import annotations

import glob
import hashlib
import logging
import os
import re
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from jacked.regular_file import open_regular_fd
from jacked.testdiet import history, junit, tree
from jacked.testdiet.model import RUNNERS, STATES, RunData, RunMeta, TreeId

logger = logging.getLogger(__name__)

#: The text in a gate argument that ``run_gate`` replaces with the path of a private report file.
JUNIT_PLACEHOLDER = "{junit}"
#: The pattern of a label, a machine label and a runner name.
LABEL_RE = re.compile(r"[A-Za-z0-9._-]+")
KINDS = ("full", "targeted")
PREFIX = "jacked tests: "
#: The exit code of a command that cannot start (the shell convention).
EXIT_NOT_STARTED = 127
_LOCAL = "local"
_SIGNAL_BASE = 128

T = TypeVar("T")


def clean_text(text: str) -> str:
    """Remove each control character from ``text``, except the newline and the tab.

    The control characters are the Unicode category Cc: C0, DEL and C1 (C1
    holds CSI, U+009B, which a terminal reads as ESC ``[``).

    >>> clean_text("a\\x1b[31mb\\u009bc\\td")
    'a[31mbc\\td'
    """
    return "".join(ch for ch in text if ch in "\n\t" or unicodedata.category(ch) != "Cc")


def emit(message: str) -> None:
    """Print one message on standard error, with the prefix and with no control characters."""
    print(PREFIX + clean_text(message), file=sys.stderr, flush=True)


def _guard(step: str, func: Callable[[], T], default: T) -> T:
    """Run one recorder step. An error is logged and printed, and ``default`` is the result."""
    try:
        return func()
    except Exception as exc:  # noqa: BLE001 -- a recorder error never stops or changes the gate
        logger.warning("recorder step %s failed: %s", step, type(exc).__name__, exc_info=True)
        emit(f"the recorder step '{step}' failed ({type(exc).__name__}). The gate result is not changed.")
        return default


def check_label(value: str | None, what: str = "label") -> str | None:
    """Return ``value`` when it is None or matches ``[A-Za-z0-9._-]+``. Else raise ``ValueError``."""
    if value is not None and not (isinstance(value, str) and LABEL_RE.fullmatch(value)):
        raise ValueError(f"the {what} must match [A-Za-z0-9._-]+")
    return value


def _check_options(kind: str | None, label: str | None, runner: str | None) -> None:
    if kind is not None and kind not in KINDS:
        raise ValueError("the kind must be full or targeted")
    check_label(label)
    if runner is not None and runner not in RUNNERS:
        raise ValueError("the runner must be one of " + ", ".join(RUNNERS))


def work_tree_top(cwd: Path) -> Path:
    """Return the top folder of the work tree that holds ``cwd``. Raise ``TreeError`` outside one."""
    code, out = tree._git(Path(cwd), "rev-parse", "--show-toplevel")
    lines = tree._lines(out)
    if code != 0 or len(lines) != 1:
        raise tree.TreeError("the folder is not in the work tree of a git repository")
    return Path(lines[0])


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _load() -> float | None:
    """The 1-minute load average, or None where the system does not give it."""
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        return None


def _child_cpu() -> float | None:
    """The processor seconds of the waited-for child processes, or None on Windows."""
    try:
        import resource
    except ImportError:
        return None
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def _sha_of(path: Path) -> str | None:
    """The SHA-256 of a regular file, or None when it is not one (a FIFO is never opened for a read)."""
    try:
        fd = open_regular_fd(path)
    except OSError:
        return None
    digest = hashlib.sha256()
    try:
        while block := os.read(fd, 1 << 16):
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest()


def _matches(top: Path, globs: Sequence[str]) -> list[Path]:
    """The paths that the globs match. A relative glob starts at ``top``. No path is listed two times."""
    found: dict[str, Path] = {}
    for pattern in globs:
        for name in sorted(glob.glob(pattern, root_dir=top, recursive=True, include_hidden=True)):
            path = Path(name) if os.path.isabs(name) else top / name
            found.setdefault(os.path.normpath(path), path)
    return list(found.values())


def _counts(data: RunData | None) -> dict[str, int] | None:
    if data is None:
        return None
    return {state: sum(1 for case in data.tests if case.state == state) for state in STATES}


def _failing(data: RunData | None) -> tuple[str, ...]:
    if data is None:
        return ()
    return tuple(sorted(case.id for case in data.tests if case.state in ("failed", "error")))


def _flaky(data: RunData | None) -> tuple[str, ...]:
    if data is None:
        return ()
    return tuple(sorted(case.id for case in data.tests if case.state == "flaky"))


def _start_and_wait(argv: list[str], cwd: Path) -> int:
    """Start the gate, wait for it, and return its exit code.

    The child gets the standard streams of this process, and no shell. A
    command that cannot start gives 127. A child that a signal stops gives
    128 plus the signal number. An interrupt of this process does not stop
    the wait: the child gets the interrupt from the terminal too, and its
    exit code is the result.
    """
    try:
        proc = subprocess.Popen(argv, cwd=cwd)
    except (OSError, ValueError) as exc:
        emit(f"the gate command cannot start ({type(exc).__name__}). The exit code is 127.")
        return EXIT_NOT_STARTED
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            emit("the run got an interrupt. It waits for the gate command to stop.")
    return _SIGNAL_BASE - code if code < 0 else code


def run_gate(argv: Sequence[str], *, cwd: Path, junit: Sequence[str] = (), inputs: Sequence[str] = (),
             kind: str | None = None, label: str | None = None, runner: str | None = None,
             strict: bool = False) -> int:
    """Run the gate command ``argv`` in ``cwd``, record the run, and return the exit code of the gate.

    ``junit`` holds globs of reports at fixed paths. A match is read only when
    it is new or its content changed during the run. ``inputs`` holds globs of
    files that the gate reads and that git does not see; their fingerprint
    goes into the record. A relative glob starts at the top of the work tree.
    An argument that holds ``{junit}`` gets the path of a private report file
    in its place. ``kind`` is "full" or "targeted", a label for the views only.

    A bad ``kind``, ``label`` or ``runner`` raises ``ValueError`` before the
    gate starts. Outside a git repository the gate runs and nothing is
    recorded. ``strict`` returns 1 for a conflict.
    """
    argv = [str(arg) for arg in argv]
    if not argv:
        raise ValueError("the gate command is empty")
    _check_options(kind, label, runner)
    cwd = Path(cwd)

    top = _guard("find the work tree", lambda: _top_or_none(cwd), None)
    store = _guard("find the store", lambda: tree.store_dir(top), None) if top is not None else None
    if top is None or store is None:
        emit("this folder is not in a git repository. The gate runs, and the run is not recorded.")
        report_dir, _report, child_argv = _placeholder(argv)
        try:
            return _start_and_wait(child_argv, cwd)
        finally:
            if report_dir is not None:
                tree.remove_tree(report_dir)

    objects = _guard("make a temporary folder", lambda: Path(tempfile.mkdtemp(prefix="jacked-objects-")),
                     None)
    report_dir: Path | None = None
    try:
        before = _guard("read the tree", lambda: tree.tree_id(cwd, objects=objects), None)
        sha = tree.command_sha(argv)
        inputs_sha = _guard("hash the input files", lambda: tree.inputs_fingerprint(top, inputs), None)
        report_dir, report, child_argv = _placeholder(argv)
        fixed_before = _guard("hash the fixed reports",
                              lambda: {os.path.normpath(p): _sha_of(p) for p in _matches(top, junit)}, {})

        started = _now()
        load_start = _load()
        cpu_start = _guard("read the processor time", _child_cpu, None)
        clock = time.monotonic()
        code = _start_and_wait(child_argv, cwd)
        wall = time.monotonic() - clock
        cpu_end = _guard("read the processor time", _child_cpu, None)
        load_end = _load()

        data = _guard("read the report",
                      lambda: _read_reports(top, report, junit, fixed_before, runner), None)
        if report_dir is not None:
            tree.remove_tree(report_dir)
            report_dir = None
        after = _guard("read the tree", lambda: tree.tree_id(cwd, objects=objects), None)
        changed = _tree_changed(before, after)
        if changed:
            _guard("list the changed paths", lambda: _print_changes(cwd, before, after, objects), None)

        def write() -> RunMeta:
            meta = RunMeta(
                id=history.new_run_id(), started=started, source="run",
                wall_seconds=round(wall, 3), exit_code=code, verdict=history.verdict_of(code, data),
                kind=kind, label=label, runner=runner or (data.runner if data else None),
                command_sha=sha, inputs_sha=inputs_sha, machine=_LOCAL, machine_id=tree.machine_id(),
                tree=before, tree_changed=changed,
                on_default_branch=tree.on_default_branch(cwd, before) if before else None,
                counts=_counts(data), seconds_reported=data.seconds_reported if data else None,
                load_start=load_start, load_end=load_end,
                child_cpu_seconds=(round(cpu_end - cpu_start, 3)
                                   if cpu_start is not None and cpu_end is not None else None),
                failing=_failing(data), flaky=_flaky(data),
                failing_sig=history.failing_signature(data.tests) if data else None,
            )
            notice = _guard("read the earlier records", lambda: repeat_notice(store, meta), None)
            history.write_run(store, meta, data)
            if notice:
                for line in notice.splitlines():
                    emit(line)
            return meta

        meta = _guard("write the record", write, None)
        verdict = meta.verdict if meta is not None else _guard(
            "find the verdict", lambda: history.verdict_of(code, data), None)
        if verdict == "conflict":
            failing = len(_failing(data))
            emit(f"the gate returned 0, but the report holds failures ({failing} failed or error tests)."
                 + (" --strict makes the exit code 1." if strict else ""))
            if strict:
                return 1
        return code
    finally:
        if report_dir is not None:
            tree.remove_tree(report_dir)
        if objects is not None:
            tree.remove_tree(objects)


def _placeholder(argv: list[str]) -> tuple[Path | None, Path | None, list[str]]:
    """Make the private report folder when an argument holds ``{junit}``.

    Return the folder, the report path and the argv of the child. When the
    folder cannot be made, the child gets ``argv`` as it is.
    """
    if not any(JUNIT_PLACEHOLDER in arg for arg in argv):
        return None, None, argv
    folder = _guard("make the report folder", lambda: Path(tempfile.mkdtemp(prefix="jacked-junit-")), None)
    if folder is None:
        return None, None, argv
    report = folder / "junit.xml"
    return folder, report, [arg.replace(JUNIT_PLACEHOLDER, str(report)) for arg in argv]


def _top_or_none(cwd: Path) -> Path | None:
    try:
        return work_tree_top(cwd)
    except tree.TreeError:
        return None


def _tree_changed(before: TreeId | None, after: TreeId | None) -> bool | None:
    """False only when both identities exist and are equal. None when one of them is missing."""
    if before is None or after is None:
        return None
    return before != after


def _print_changes(cwd: Path, before: TreeId, after: TreeId, objects: Path | None) -> None:
    if before.tree == after.tree:
        emit("the gate changed the HEAD commit. The content of the tree is the same. "
             "This run cannot prove a tree.")
        return
    try:
        paths = tree.changed_paths(cwd, before, after, objects=objects)
    except tree.TreeError:
        emit("the gate changed the work tree. The changed paths are not available. "
             "This run cannot prove a tree.")
        return
    emit(f"the gate changed {len(paths)} path(s) in the work tree. This run cannot prove a tree:")
    for path in paths:
        emit(f"  {path}")


def _read_reports(top: Path, report: Path | None, globs: Sequence[str],
                  fixed_before: dict[str, str | None], runner: str | None) -> RunData | None:
    """Read the report at the placeholder path and each fixed report that is new or changed.

    The read is all or nothing: when one file cannot be read, the run has no
    data. A report that is left out could hold a failure.
    """
    paths: list[Path] = []
    if report is not None:
        if os.path.lexists(report):
            paths.append(report)
        else:
            emit("the gate wrote no report at the {junit} path.")
    for path in _matches(top, globs):
        key = os.path.normpath(path)
        now = _sha_of(path)
        if now is None:
            emit(f"did not read {path}: it is not a regular file.")
        elif key in fixed_before and fixed_before[key] == now:
            emit(f"did not read {path}: its content did not change during the run.")
        else:
            paths.append(path)
    if globs and not any(_matches(top, [g]) for g in globs):
        emit("no file matches the --junit pattern(s).")
    if not paths:
        return None
    try:
        return junit.read_junit(paths, root=top, runner=runner)
    except (junit.JUnitError, OSError) as exc:
        emit(f"cannot read the report, so the run has no test data: {exc}")
        return None


def record_files(junit_paths: Sequence[Path], *, cwd: Path, kind: str | None, label: str | None,
                 runner: str | None, commit: str | None, exit_code: int | None,
                 wall_seconds: float | None, machine: str | None,
                 report_root: str | None) -> RunMeta:
    """Record the JUnit files of a run that was not wrapped, and return the record.

    The source is "record". With no ``commit``, the record has no tree. With
    a ``commit``, the tree is the tree of that commit, clean. ``machine`` is a
    label for a different machine: the record then has no machine identity.
    With no ``exit_code`` the verdict is "unknown". A file that cannot be
    read raises ``JUnitError`` and nothing is recorded. Outside a git
    repository, ``TreeError``.
    """
    _check_options(kind, label, runner)
    check_label(machine, "machine label")
    if wall_seconds is not None and not wall_seconds >= 0:
        raise ValueError("the wall time must be 0 or more seconds")
    cwd = Path(cwd)
    top = work_tree_top(cwd)
    store = tree.store_dir(top)
    tree_value = _commit_tree(top, commit) if commit is not None else None
    data = junit.read_junit([Path(p) for p in junit_paths], root=top, report_root=report_root,
                            runner=runner)
    meta = RunMeta(
        id=history.new_run_id(), started=_now(), source="record", wall_seconds=wall_seconds,
        exit_code=exit_code, verdict=history.verdict_of(exit_code, data), kind=kind, label=label,
        runner=runner or data.runner, command_sha=None, inputs_sha=None,
        machine=machine or _LOCAL, machine_id=None if machine else tree.machine_id(),
        tree=tree_value, tree_changed=None,
        on_default_branch=tree.on_default_branch(top, tree_value) if tree_value else None,
        counts=_counts(data), seconds_reported=data.seconds_reported, load_start=None,
        load_end=None, child_cpu_seconds=None, failing=_failing(data), flaky=_flaky(data),
        failing_sig=history.failing_signature(data.tests),
    )
    history.write_run(store, meta, data)
    return meta


def _commit_tree(top: Path, commit: str) -> TreeId:
    """The clean tree identity of ``commit``. A name that is not a commit raises ``TreeError``."""
    found = []
    for suffix in ("^{commit}", "^{tree}"):
        code, out = tree._git(top, "rev-parse", "--verify", "-q", "--end-of-options", commit + suffix)
        lines = tree._lines(out)
        if code != 0 or len(lines) != 1:
            raise tree.TreeError("the commit is not in this repository")
        found += lines
    return TreeId(commit=found[0], tree=found[1], clean=True)


def seen(store: Path, tree_value: TreeId, command_sha: str, *, inputs_sha: str | None,
         machine_id: str | None, any_machine: bool = False) -> RunMeta | None:
    """Return the newest run that proves this tree passed this command on this machine, or None.

    A run proves it when all of these are true: its source is "run"; its
    verdict is "passed"; ``tree_changed`` is False (not empty); the tree
    hash, the command SHA and the inputs fingerprint are equal; and the
    machine identity is equal and not empty. ``any_machine`` removes the
    last condition. The commit is not compared, so a second commit with the
    same content is the same tree.
    """
    if tree_value is None or not command_sha:
        return None
    if not any_machine and not machine_id:
        return None
    for meta in history.iter_runs(store):
        if (meta.source == "run" and meta.verdict == "passed" and meta.tree_changed is False
                and meta.tree is not None and meta.tree.tree == tree_value.tree
                and meta.command_sha == command_sha and meta.inputs_sha == inputs_sha
                and (any_machine or meta.machine_id == machine_id)):
            return meta
    return None


def repeat_notice(store: Path, meta: RunMeta) -> str | None:
    """Return the facts about earlier records with the failing set of ``meta``, or None.

    The text gives the number of earlier records with the same failing
    signature, the first and the last date, the number of trees, and how
    many of the records are on the default branch. Then the failing IDs and
    one line for each earlier record. It does not say whose defect it is.
    The result is None for a run with no failing set, or with a failing set
    that no earlier record holds.
    """
    if not meta.failing_sig:
        return None
    earlier = [m for m in history.iter_runs(store)
               if m.id != meta.id and m.failing_sig == meta.failing_sig]
    if not earlier:
        return None
    earlier.sort(key=lambda m: m.id)
    trees = {m.tree.tree for m in earlier if m.tree is not None}
    no_tree = sum(1 for m in earlier if m.tree is None)
    default = sum(1 for m in earlier if m.on_default_branch is True)
    first, last = earlier[0].started[:10], earlier[-1].started[:10]
    records = "record" if len(earlier) == 1 else "records"
    tree_word = "tree" if len(trees) == 1 else "trees"
    summary = (f"the same failing set is in {len(earlier)} earlier {records}, from {first} to {last}, "
               f"on {len(trees)} {tree_word}")
    if no_tree:
        summary += f" ({no_tree} with no tree)"
    summary += f", {default} of them on the default branch."
    lines = [summary, f"the failing set has {len(meta.failing)} test(s):"]
    lines += [f"  {test_id}" for test_id in meta.failing]
    lines.append("the earlier records:")
    for m in earlier:
        place = m.tree.commit[:12] if m.tree is not None else "no tree"
        flag = ", default branch" if m.on_default_branch is True else ""
        lines.append(f"  {m.id} {m.started} {m.source} {place}{flag}")
    return "\n".join(lines)
