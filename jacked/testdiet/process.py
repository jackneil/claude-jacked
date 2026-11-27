"""Start the gate command, wait for it, and send it the stop signals that this process gets.

``start_and_wait`` gives the exit code of the gate. ``Signals`` holds the
SIGTERM and SIGHUP handlers of one run (POSIX, main thread only).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from pathlib import Path

from jacked.findbin import find_on_path
from jacked.testdiet.model import emit

#: The exit code of a command that cannot start (the shell convention).
EXIT_NOT_STARTED = 127


_SIGNAL_BASE = 128


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


class Stopped(BaseException):
    """A SIGTERM or a SIGHUP came after the gate stopped. The recorder stops its steps."""


class Signals:
    """The SIGTERM and SIGHUP handlers of one run, on POSIX, in the main thread.

    The handlers are set before the gate starts and stay until the run ends.
    A signal before the gate exists is kept, then sent on when it exists.
    While the gate runs, a signal goes to it one time for each signal
    number, and only while it still runs. After the gate stops, the first
    signal stops the recorder: it raises ``Stopped`` only inside the
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
        """Set the handlers. A signal that this process ignores (``nohup``) stays ignored.

        The gate then inherits the ignore, as it does with ``nohup <gate>``, and
        that signal is not sent on.
        """
        if os.name == "nt" or threading.current_thread() is not threading.main_thread():
            return
        for signum in _FORWARDED:
            try:
                if signal.getsignal(signum) is signal.SIG_IGN:
                    continue
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
                    raise Stopped(signum)
            return
        if self.proc is None:
            self.pending.append(signum)
        else:
            self._send(signum)


def start_and_wait(argv: list[str], cwd: Path, signals: Signals) -> int:
    """Start the gate, wait for it, and return its exit code.

    The child gets the standard streams of this process, and no shell. A
    command that cannot start gives 127. A child that a signal stops gives
    128 plus the signal number. An interrupt of this process does not stop
    the wait: the child gets the interrupt from the terminal too, and its
    exit code is the result. On POSIX, a SIGTERM or a SIGHUP of this process
    goes to the child (see ``Signals``), and the wait goes on.
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
