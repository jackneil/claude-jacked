"""Run a gate command, record the run, and return the exit code of the gate.

``run_gate`` returns the exit code of the gate command. A recorder error
never changes that code: each step of the recorder is guarded, the error is
logged and one line goes to standard error. Only ``strict`` changes the
code, and only for a conflict (exit code 0 and failures in the report).

The order of ``run_gate``: read the tree; hash the typed command; make the
report folder when an argument holds ``{junit}``; start the child; wait;
read the report; remove the report folder; read the tree a second time;
write the record.
"""

from __future__ import annotations

import logging
import os
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

from jacked.testdiet import history, machine, reports, tree
from jacked.testdiet.cleanup import remove_tree_quietly
from jacked.testdiet.model import SCHEMA, RunMeta, TreeId, emit
from jacked.testdiet.process import Signals, Stopped, start_and_wait
from jacked.testdiet.record import (
    _LOCAL,
    INPUTS_UNAVAILABLE,
    _check_options,
    _counts,
    _failing,
    _flaky,
    _guard,
    _now,
    repeat_notice,
    work_tree_top,
)

logger = logging.getLogger(__name__)


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
    recorded. ``strict`` returns 1 for a conflict. A SIGTERM or a SIGHUP at
    any point goes to the gate while it runs; after it stops, the recorder
    skips its remaining steps, still writes the record, and removes its
    temporary folders.
    """
    argv = [str(arg) for arg in argv]
    if not argv:
        raise ValueError("the gate command is empty")
    _check_options(kind, label, runner)
    cwd = Path(cwd)
    signals = Signals()
    signals.install()
    try:
        return _run_gate(argv, cwd, signals, junit, inputs, kind, label, runner, strict)
    finally:
        signals.restore()


def _run_gate(argv: list[str], cwd: Path, signals: Signals, junit: Sequence[str],
              inputs: Sequence[str], kind: str | None, label: str | None, runner: str | None,
              strict: bool) -> int:
    top = _guard("find the work tree", lambda: _top_or_none(cwd), None)
    store = _guard("find the store", lambda: tree.store_dir(top), None) if top is not None else None
    if top is None or store is None:
        if top is None:
            emit("this folder is not in a git repository. The gate runs, and the run is not recorded.")
        else:
            emit("the history store cannot be found. The gate runs, and the run is not recorded.")
        report_dir, _report, child_argv = _placeholder(argv)
        try:
            return start_and_wait(child_argv, cwd, signals)
        finally:
            if report_dir is not None:
                remove_tree_quietly(report_dir)

    objects = _guard("make a temporary folder", lambda: Path(tempfile.mkdtemp(prefix="jacked-objects-")),
                     None)
    report_dir: Path | None = None
    try:
        before = _guard("read the tree", lambda: tree.tree_id(cwd, objects=objects), None)
        folder = _guard("find the gate folder", lambda: tree.work_folder(cwd), None)
        # No folder, no command identity: such a run can never prove a tree.
        sha = tree.command_sha(argv, folder=folder) if folder is not None else None
        inputs_sha = _guard("hash the input files", lambda: tree.inputs_fingerprint(top, inputs),
                            INPUTS_UNAVAILABLE if inputs else None)
        report_dir, report, child_argv = _placeholder(argv)
        # None (not empty) when the hash step fails: then no fixed report is read,
        # because an old report must not become this run.
        fixed_before = _guard("hash the fixed reports",
                              lambda: reports.hash_fixed(top, junit),
                              None) if junit else {}

        started = _now()
        load_start = _load()
        cpu_start = _guard("read the processor time", _child_cpu, None)
        clock = time.monotonic()
        code = start_and_wait(child_argv, cwd, signals)
        wall = time.monotonic() - clock
        cpu_end = _guard("read the processor time", _child_cpu, None)
        load_end = _load()
        # What the post-run steps know. A stop keeps the values that are read.
        known: dict = {"data": None, "changed": None, "written": False}

        def build() -> RunMeta:
            data = known["data"]
            return RunMeta(
                id=history.new_run_id(), started=started, source="run",
                wall_seconds=round(wall, 3), exit_code=code, verdict=history.verdict_of(code, data),
                kind=kind, label=label, runner=runner or (data.runner if data else None),
                command_sha=sha, inputs_sha=inputs_sha, machine=_LOCAL, machine_id=machine.machine_id(),
                tree=before, tree_changed=known["changed"],
                on_default_branch=(tree.on_default_branch(cwd, before)
                                   if before and signals.stopped is None else None),
                counts=_counts(data), seconds_reported=data.seconds_reported if data else None,
                load_start=load_start, load_end=load_end,
                child_cpu_seconds=(round(cpu_end - cpu_start, 3)
                                   if cpu_start is not None and cpu_end is not None else None),
                failing=_failing(data), flaky=_flaky(data),
                failing_sig=history.failing_signature(data.tests) if data else None,
                folder=folder, schema=SCHEMA,
            )

        def write() -> RunMeta:
            meta = build()
            notice = _guard("read the earlier records", lambda: repeat_notice(store, meta), None)
            history.write_run(store, meta, known["data"])
            known["written"] = True
            if notice:
                for line in notice.splitlines():
                    emit(line)
            return meta

        try:
            try:
                signals.raise_ok = True
                if signals.stopped is not None:
                    raise Stopped(signals.stopped)
                known["data"] = _guard("read the report",
                                       lambda: reports.read_reports(top, report, junit, fixed_before, runner), None)
                if report_dir is not None:
                    remove_tree_quietly(report_dir)
                    report_dir = None
                after = _guard("read the tree", lambda: tree.tree_id(cwd, objects=objects), None)
                known["changed"] = changed = _tree_changed(before, after)
                if changed:
                    _guard("list the changed paths",
                           lambda: _print_changes(cwd, before, after, objects), None)
                # No stop from here on: a signal between the tests file and the meta
                # file, or after write_run, would leave an orphan or a second record.
                signals.raise_ok = False
                meta = _guard("write the record", write, None)
            finally:
                signals.raise_ok = False
        except Stopped as stop:
            emit(f"the run got signal {stop.args[0]} after the gate stopped. The recorder skips its "
                 "other steps and writes the record.")
            if not known["written"]:
                _guard("write the record", lambda: history.write_run(store, build(), known["data"]), None)
            return code
        except KeyboardInterrupt:
            # The gate is done. An interrupt now stops only the recorder.
            emit("the run got an interrupt after the gate stopped. The recorder stops; "
                 "the exit code is the code of the gate.")
            return code
        data = known["data"]
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
            remove_tree_quietly(report_dir)
        if objects is not None:
            remove_tree_quietly(objects)


def _placeholder(argv: list[str]) -> tuple[Path | None, Path | None, list[str]]:
    """The report folder, the report path and the child argv. A folder that cannot be made is logged."""
    return reports.placeholder(argv, lambda: _guard("make the report folder", reports.make_report_folder, None))


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
