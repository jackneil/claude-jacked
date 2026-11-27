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

from jacked.public_errors import os_error_reason
from jacked.testdiet import globs, history, machine, reports, tree
from jacked.testdiet.cleanup import remove_tree_quietly
from jacked.testdiet.model import SCHEMA, RunMeta, TreeId, emit
from jacked.testdiet.process import EXIT_INTERRUPTED, Signals, Stopped, start_and_wait
from jacked.testdiet.record import INPUTS_UNAVAILABLE, LOCAL, check_options, guard, now_utc, repeat_notice

logger = logging.getLogger(__name__)

#: The line before the gate when the run has no report source.
#: The ends of a failed-step line: what the failure costs.
NOT_RECORDED = "The run is not recorded."
NO_PROOF = "This run cannot prove a tree."
NO_REPORT_SOURCE = ("the command has no {junit} argument, and the run has no --junit option. "
                    "So the run has no report, and it cannot prove a tree. "
                    "Put {junit} in the command, or give --junit GLOB.")


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
    check_options(kind, label, runner)
    cwd = Path(cwd)
    signals = Signals()
    signals.install()
    try:
        return _run_gate(argv, cwd, signals, junit, inputs, kind, label, runner, strict)
    except KeyboardInterrupt:
        if signals.code is not None:
            return signals.code  # the gate is done: its exit code stays the result
        emit("the run got an interrupt before the gate started. The gate did not run, and the run "
             "is not recorded.")
        return EXIT_INTERRUPTED
    finally:
        signals.restore()


def _run_gate(argv: list[str], cwd: Path, signals: Signals, junit: Sequence[str],
              inputs: Sequence[str], kind: str | None, label: str | None, runner: str | None,
              strict: bool) -> int:
    clock0 = time.monotonic()
    top = guard("find the work tree", lambda: _top_or_none(cwd), None, then=NOT_RECORDED)
    store = guard("find the store", lambda: tree.store_dir(top), None, then=NOT_RECORDED) if top is not None else None
    if top is None or store is None:
        if top is not None:
            emit("the history store cannot be found. The gate runs, and the run is not recorded.")
        report_dir, _report, child_argv = _placeholder(argv)
        try:
            return start_and_wait(child_argv, cwd, signals)
        finally:
            if report_dir is not None:
                remove_tree_quietly(report_dir)

    objects = guard("make a temporary folder", lambda: Path(tempfile.mkdtemp(prefix="jacked-objects-")),
                     None)
    report_dir: Path | None = None
    try:
        before = guard("read the tree", lambda: _read_tree(cwd, objects), None, then=NO_PROOF)
        folder = guard("find the gate folder", lambda: tree.work_folder(cwd), None, then=NO_PROOF)
        # No folder, no command identity: such a run can never prove a tree.
        sha = tree.command_sha(argv, folder=folder) if folder is not None else None
        inputs_sha = guard("hash the input files", lambda: hash_inputs(top, inputs),
                           INPUTS_UNAVAILABLE if inputs else None, then=NO_PROOF)
        report_dir, report, child_argv = _placeholder(argv)
        if not junit and not any(reports.JUNIT_PLACEHOLDER in arg for arg in argv):
            emit(NO_REPORT_SOURCE)
        # None (not empty) when the hash step fails: then no fixed report is read,
        # because an old report must not become this run.
        fixed_before = guard("hash the fixed reports",
                             lambda: reports.hash_fixed(top, junit),
                             None, then="No --junit report is read.") if junit else {}

        started = now_utc()
        load_start = _load()
        cpu_start = guard("read the processor time", _child_cpu, None)
        clock = time.monotonic()
        code = start_and_wait(child_argv, cwd, signals)
        wall = time.monotonic() - clock
        cpu_end = guard("read the processor time", _child_cpu, None)
        load_end = _load()
        def spent() -> float:
            return max(0.0, time.monotonic() - clock0 - wall)

        # What the post-run steps know. A stop keeps the values that are read.
        known: dict = {"data": None, "changed": None, "written": False, "meta": None, "inputs": None}

        def build() -> RunMeta:
            data = known["data"]
            return RunMeta(
                id=history.new_run_id(), started=started, source="run",
                wall_seconds=round(wall, 3), exit_code=code, verdict=history.verdict_of(code, data),
                kind=kind, label=label, runner=runner or (data.runner if data else None),
                command_sha=sha, inputs_sha=inputs_sha, machine=LOCAL, machine_id=machine.machine_id(),
                tree=before, tree_changed=known["changed"],
                on_default_branch=(tree.on_default_branch(cwd, before)
                                   if before and signals.stopped is None else None),
                counts=history.counts_of(data), seconds_reported=data.seconds_reported if data else None,
                load_start=load_start, load_end=load_end,
                child_cpu_seconds=(round(cpu_end - cpu_start, 3)
                                   if cpu_start is not None and cpu_end is not None else None),
                failing=history.failing_of(data), flaky=history.flaky_of(data),
                failing_sig=history.failing_signature(data.tests) if data else None,
                folder=folder, schema=SCHEMA, test_seconds=history.sum_test_seconds(data),
                interrupted=signals.interrupted, inputs_changed=known["inputs"],
            )

        def write() -> RunMeta:
            # One meta for each run: a second write of it replaces the same files.
            meta = known["meta"] = build()
            notice = guard("read the earlier records", lambda: repeat_notice(store, meta), None)
            if _write_record(store, meta, known["data"], spent()):
                known["written"] = True
            if notice:
                # jacked built each line, and each stored value in it went through one_line.
                for line in notice.split("\n"):
                    emit(line)
            return meta

        try:
            try:
                signals.raise_ok = True
                if signals.stopped is not None:
                    raise Stopped(signals.stopped)
                known["data"] = guard("read the report",
                                      lambda: reports.read_reports(top, report, junit, fixed_before, runner), None,
                                      then="The run has no test data.")
                if report_dir is not None:
                    remove_tree_quietly(report_dir)
                    report_dir = None
                after = guard("read the tree", lambda: _read_tree(cwd, objects, quiet=before is None), None,
                              then=NO_PROOF)
                known["changed"] = changed = _tree_changed(before, after)
                known["inputs"] = guard("hash the input files again",
                                        lambda: _inputs_changed(top, inputs, inputs_sha), None, then=NO_PROOF)
                if changed:
                    guard("list the changed paths",
                           lambda: _print_changes(cwd, before, after, objects), None)
                # No stop from here on: a signal between the tests file and the meta
                # file, or after write_run, would leave an orphan or a second record.
                signals.raise_ok = False
                meta = guard("write the record", write, None, then=NOT_RECORDED)
            finally:
                signals.raise_ok = False
        except Stopped as stop:
            emit(f"the run got signal {stop.args[0]} after the gate stopped. The recorder skips its "
                 "other steps and writes the record.")
            if not known["written"]:
                guard("write the record", lambda: _write_record(store, build(), known["data"], spent()), None,
                      then=NOT_RECORDED)
            return code
        except KeyboardInterrupt:
            # The gate is done. An interrupt now stops only the recorder, as a SIGTERM does.
            emit("the run got an interrupt after the gate stopped. The recorder skips its other steps "
                 "and writes the record, which cannot prove a tree. The exit code is the code of the gate.")
            if not known["written"]:
                meta = known["meta"] or build()
                guard("write the record", lambda: _write_record(store, meta, known["data"], spent()), None,
                      then=NOT_RECORDED)
            return code
        data = known["data"]
        verdict = meta.verdict if meta is not None else guard(
            "find the verdict", lambda: history.verdict_of(code, data), None)
        if verdict == "conflict":
            failing = len(history.failing_of(data))
            tests = "test" if failing == 1 else "tests"
            suite = data.suite_failures if data is not None else 0
            at_suite = f", and {suite} that only a suite counts" if suite else ""
            emit(f"the gate returned 0, but the report holds failures ({failing} failed or error {tests}"
                 f"{at_suite}). "
                 + ("--strict makes the exit code 1." if strict else
                    "This run is not a pass, and seen does not accept it. Give --strict to get exit 1."))
            if strict:
                return 1
        return code
    finally:
        if report_dir is not None:
            remove_tree_quietly(report_dir)
        if objects is not None:
            remove_tree_quietly(objects)


def _inputs_changed(top: Path, inputs: Sequence[str], before: str | None) -> bool | None:
    """True when the gate changed the ``--input`` files, and one line says so. None with no fingerprint."""
    if not inputs or before is None or before == INPUTS_UNAVAILABLE:
        return None
    if globs.inputs_fingerprint(top, inputs) == before:
        return False
    emit("the gate changed the --input files, so this run cannot prove a tree. Make the gate write "
         "them outside the --input globs.")
    return True


def _placeholder(argv: list[str]) -> tuple[Path | None, Path | None, list[str]]:
    """The report folder, the report path and the child argv. A folder that cannot be made is logged."""
    return reports.placeholder(argv, lambda: guard("make the report folder", reports.make_report_folder, None,
                                                   then="The gate gets {junit} as typed, so the run has no report."))


def _write_record(store: Path, meta: RunMeta, data, recorder: float) -> bool:
    """Write the record and print the closing line. A store that cannot be written gives one line.

    ``recorder`` is the time of the recorder itself in seconds: the run so far, less the gate.
    """
    try:
        history.write_run(store, meta, data)
    except OSError as exc:
        logger.debug("write_run: the store cannot be written", exc_info=True)
        emit(f"cannot write the history store {store} ({os_error_reason(exc)}), so the run is not "
             "recorded. A sandbox that makes the .git folder read-only gives this error.")
        return False
    emit(f"recorded {meta.id}, verdict {meta.verdict}, recorder {recorder:.1f} s.")
    return True


def _read_tree(cwd: Path, objects: Path | None, *, quiet: bool = False) -> TreeId | None:
    """The tree identity. When there is none, one line says why, unless ``quiet``."""
    found, problem = tree.tree_state(cwd, objects=objects)
    if found is None and not quiet:
        emit(f"{problem}, so this run cannot prove a tree. Run git status to see the cause.")
    return found


def hash_inputs(top: Path, inputs: Sequence[str]) -> str | None:
    """The ``--input`` fingerprint. Each glob that matches no regular file gives one line."""
    sha, empty = globs.input_fingerprint(top, inputs)
    for pattern in empty:
        emit(f"no regular file matches the --input pattern {pattern!r}. "
             "It is in the fingerprint as a pattern with no file.")
    return sha


def _top_or_none(cwd: Path) -> Path | None:
    """The top of the work tree, or None. With None, one line names the git failure."""
    try:
        return tree.work_tree_top(cwd)
    except tree.TreeError as exc:
        emit(f"{exc}. The gate runs, and the run is not recorded.")
        return None


def _tree_changed(before: TreeId | None, after: TreeId | None) -> bool | None:
    """False only when both identities exist and are equal. None when one of them is missing."""
    if before is None or after is None:
        return None
    return before != after


def _print_changes(cwd: Path, before: TreeId, after: TreeId, objects: Path | None) -> None:
    if before.tree == after.tree:
        emit("the gate changed the HEAD commit. The content of the tree is the same. "
             "This run cannot prove a tree. Make the gate keep HEAD as it is.")
        return
    try:
        paths = tree.changed_paths(cwd, before, after, objects=objects)
    except tree.TreeError:
        emit("the gate changed the work tree. The changed paths are not available. "
             "This run cannot prove a tree. Run git status to see the changed paths.")
        return
    count = "1 path" if len(paths) == 1 else f"{len(paths)} paths"
    emit(f"the gate changed {count} in the work tree. This run cannot prove a tree:")
    for path in paths:
        emit(f"  {path}")
    emit("add these paths to .gitignore, or make the gate write them outside the work tree.")
