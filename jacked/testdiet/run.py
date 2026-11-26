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

import glob
import hashlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from jacked.findbin import find_on_path
from jacked.regular_file import open_regular_fd
from jacked.testdiet import history, junit, tree
from jacked.testdiet.model import RunData, RunMeta, TreeId
from jacked.testdiet.record import (
    _LOCAL,
    INPUTS_UNAVAILABLE,
    _check_options,
    _counts,
    _failing,
    _flaky,
    _guard,
    _now,
    emit,
    repeat_notice,
    work_tree_top,
)

logger = logging.getLogger(__name__)

#: The text in a gate argument that ``run_gate`` replaces with the path of a private report file.
JUNIT_PLACEHOLDER = "{junit}"


#: The exit code of a command that cannot start (the shell convention).
EXIT_NOT_STARTED = 127


_SIGNAL_BASE = 128


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


def _program(argv: list[str]) -> list[str]:
    """Return the argv to start. On Windows, a bare program name is resolved first.

    Popen with no shell finds only ``.exe`` files on Windows.
    ``find_on_path`` searches PATH only, as a shell does, and honors
    PATHEXT, so ``npx`` finds ``npx.cmd``. A name with a path separator, a
    name that is not on PATH, and every name on other systems stay as typed.
    """
    if sys.platform != "win32" or not argv or "/" in argv[0] or "\\" in argv[0]:
        return argv
    found = find_on_path(argv[0])
    return [found, *argv[1:]] if found else argv


# The signals that a supervisor sends to stop a process (docker stop,
# systemd, kill <pid>). The run sends them on to the gate and keeps waiting.
_FORWARDED = tuple(getattr(signal, name) for name in ("SIGTERM", "SIGHUP") if hasattr(signal, name))


class _Stopped(BaseException):
    """A SIGTERM or a SIGHUP came after the gate stopped. The recorder stops its steps."""


class _Signals:
    """The SIGTERM and SIGHUP handlers of one run, on POSIX, in the main thread.

    The handlers are set before the gate starts and stay until the run ends.
    A signal before the gate exists is kept, then sent on when it exists.
    While the gate runs, a signal goes to it one time for each signal
    number, and only while it still runs. After the gate stops, the first
    signal stops the recorder: it raises ``_Stopped`` only inside the
    post-run steps (``raise_ok``), and is only noted at all other points.
    """

    def __init__(self) -> None:
        self.proc: subprocess.Popen | None = None
        self.pending: list[int] = []
        self.sent: set[int] = set()
        self.child_done = False
        self.raise_ok = False
        self.stopped: int | None = None
        self._old: dict = {}

    def install(self) -> None:
        if os.name == "nt" or threading.current_thread() is not threading.main_thread():
            return
        for signum in _FORWARDED:
            try:
                self._old[signum] = signal.signal(signum, self._handle)
            except (ValueError, OSError):
                continue

    def restore(self) -> None:
        for signum, handler in self._old.items():
            try:
                signal.signal(signum, handler if handler is not None else signal.SIG_DFL)
            except (ValueError, OSError):
                continue
        self._old = {}

    def started(self, proc: subprocess.Popen) -> None:
        self.proc = proc
        pending, self.pending = self.pending, []
        for signum in pending:
            self._send(signum)

    def _send(self, signum: int) -> None:
        if self.proc is None or signum in self.sent or self.proc.poll() is not None:
            return
        self.sent.add(signum)
        try:
            self.proc.send_signal(signum)
        except OSError:
            pass

    def _handle(self, signum, frame) -> None:
        if self.child_done:
            if self.stopped is None:
                self.stopped = signum
                if self.raise_ok:
                    raise _Stopped(signum)
            return
        if self.proc is None:
            self.pending.append(signum)
        else:
            self._send(signum)


def _start_and_wait(argv: list[str], cwd: Path, signals: _Signals) -> int:
    """Start the gate, wait for it, and return its exit code.

    The child gets the standard streams of this process, and no shell. A
    command that cannot start gives 127. A child that a signal stops gives
    128 plus the signal number. An interrupt of this process does not stop
    the wait: the child gets the interrupt from the terminal too, and its
    exit code is the result. On POSIX, a SIGTERM or a SIGHUP of this process
    goes to the child (see ``_Signals``), and the wait goes on.
    """
    name = os.path.basename(argv[0]) if argv else ""
    try:
        proc = subprocess.Popen(_program(argv), cwd=cwd)
    except (OSError, ValueError) as exc:
        signals.child_done = True
        emit(f"the gate command {name!r} cannot start ({type(exc).__name__}). The exit code is 127.")
        return EXIT_NOT_STARTED
    signals.started(proc)
    while True:
        try:
            code = proc.wait()
            break
        except KeyboardInterrupt:
            emit("the run got an interrupt. It waits for the gate command to stop.")
    signals.child_done = True
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
    signals = _Signals()
    signals.install()
    try:
        return _run_gate(argv, cwd, signals, junit, inputs, kind, label, runner, strict)
    finally:
        signals.restore()


def _run_gate(argv: list[str], cwd: Path, signals: _Signals, junit: Sequence[str],
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
            return _start_and_wait(child_argv, cwd, signals)
        finally:
            if report_dir is not None:
                tree.remove_tree(report_dir)

    objects = _guard("make a temporary folder", lambda: Path(tempfile.mkdtemp(prefix="jacked-objects-")),
                     None)
    report_dir: Path | None = None
    try:
        before = _guard("read the tree", lambda: tree.tree_id(cwd, objects=objects), None)
        sha = tree.command_sha(argv)
        inputs_sha = _guard("hash the input files", lambda: tree.inputs_fingerprint(top, inputs),
                            INPUTS_UNAVAILABLE if inputs else None)
        report_dir, report, child_argv = _placeholder(argv)
        # None (not empty) when the hash step fails: then no fixed report is read,
        # because an old report must not become this run.
        fixed_before = _guard("hash the fixed reports",
                              lambda: {os.path.normpath(p): _sha_of(p) for p in _matches(top, junit)},
                              None) if junit else {}

        started = _now()
        load_start = _load()
        cpu_start = _guard("read the processor time", _child_cpu, None)
        clock = time.monotonic()
        code = _start_and_wait(child_argv, cwd, signals)
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
                command_sha=sha, inputs_sha=inputs_sha, machine=_LOCAL, machine_id=tree.machine_id(),
                tree=before, tree_changed=known["changed"],
                on_default_branch=(tree.on_default_branch(cwd, before)
                                   if before and signals.stopped is None else None),
                counts=_counts(data), seconds_reported=data.seconds_reported if data else None,
                load_start=load_start, load_end=load_end,
                child_cpu_seconds=(round(cpu_end - cpu_start, 3)
                                   if cpu_start is not None and cpu_end is not None else None),
                failing=_failing(data), flaky=_flaky(data),
                failing_sig=history.failing_signature(data.tests) if data else None,
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
                    raise _Stopped(signals.stopped)
                known["data"] = _guard("read the report",
                                       lambda: _read_reports(top, report, junit, fixed_before, runner), None)
                if report_dir is not None:
                    tree.remove_tree(report_dir)
                    report_dir = None
                after = _guard("read the tree", lambda: tree.tree_id(cwd, objects=objects), None)
                known["changed"] = changed = _tree_changed(before, after)
                if changed:
                    _guard("list the changed paths",
                           lambda: _print_changes(cwd, before, after, objects), None)
                meta = _guard("write the record", write, None)
            finally:
                signals.raise_ok = False
        except _Stopped as stop:
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
                  fixed_before: dict[str, str | None] | None, runner: str | None) -> RunData | None:
    """Read the report at the placeholder path and each fixed report that is new or changed.

    The read is all or nothing: when one file cannot be read, the run has no
    data. A report that is left out could hold a failure. ``fixed_before``
    is None when the fixed reports were not hashed before the run: then no
    fixed report is read.
    """
    paths: list[Path] = []
    if report is not None:
        if os.path.lexists(report):
            paths.append(report)
        else:
            emit("the gate wrote no report at the {junit} path.")
    if fixed_before is None:
        emit("did not read the --junit reports: they were not hashed before the run.")
        globs = ()
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
