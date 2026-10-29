"""Run a child process as a whole tree: stop it and everything it started.

``subprocess.run`` stops only its direct child on a timeout or an interrupt.
A child that starts its own workers (``jacked packs update`` -> npx -> node,
``uv tool install``) leaves those workers running, and they keep writing
files after the caller gave up (#178i). ``run_tree`` puts the child in its
own process group (POSIX session, Windows process group) and, on a timeout
or any interrupt, stops the whole group before it raises again. On POSIX it
also removes any group member still alive after the child exits.

The own group also keeps a closed terminal's SIGHUP and a Ctrl-C away from
the child: the caller decides when the child stops. With ``relay=True`` the
child's output goes through this process, so a closed terminal can never
make the child fail on a write.
"""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import subprocess
import sys
import threading
from typing import IO, Any

from jacked.winproc import NO_WINDOW

logger = logging.getLogger(__name__)

# How long a group gets to exit after the polite signal before SIGKILL.
_GRACE_SECONDS = 5


class _Terminated(BaseException):
    """SIGTERM arrived while a tree ran (see ``_terminate_as_exception``)."""


@contextlib.contextmanager
def _terminate_as_exception():
    """While a tree runs, turn SIGTERM to this process into ``_Terminated``.

    A jacked process that runs a tree is often a tree member itself (the
    updater runs ``jacked packs update``, which runs npx). Each tree gets its
    own session, so the outer stop reaches this process but not the inner
    tree, and Python's default SIGTERM exits without cleanup: the inner tree
    would keep running. Only the main thread can set a handler, and a handler
    the caller already installed (the upgrade guard) stays in charge.
    """
    if sys.platform == "win32" or threading.current_thread() is not threading.main_thread():
        yield
        return
    try:
        previous = signal.getsignal(signal.SIGTERM)
    except (OSError, ValueError):
        yield
        return
    if previous is not signal.SIG_DFL:
        yield
        return

    def _raise(_signum, _frame):
        raise _Terminated()

    signal.signal(signal.SIGTERM, _raise)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_tree(
    argv: list[str],
    *,
    timeout: float | None = None,
    capture: bool = False,
    relay: bool = False,
    log_fh: IO | None = None,
    env: dict[str, str] | None = None,
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run ``argv`` in its own process group and return its result.

    ``capture``: collect stdout and stderr as text (like ``capture_output``).
    ``relay``: copy each output line to this process's stdout (errors from a
    closed terminal are ignored) and to ``log_fh`` when given.
    Otherwise stdout and stderr go to ``log_fh`` (or are inherited when it is
    None). stdin is always /dev/null: the child never waits for input.

    Raises ``subprocess.TimeoutExpired`` after the whole group is stopped,
    ``OSError`` when the child cannot start, and re-raises any interrupt
    (KeyboardInterrupt, a signal turned into an exception) after the group
    is stopped.
    """
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "env": env, "cwd": cwd}
    if sys.platform == "win32":
        kwargs["creationflags"] = NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    # uv, npx and jacked write UTF-8; the locale code page (cp1252 on
    # Windows) would mangle anything outside ASCII.
    text = {"text": True, "encoding": "utf-8", "errors": "replace"}
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, **text)
    elif relay:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, **text)
    else:
        kwargs.update(stdout=log_fh, stderr=log_fh)

    proc = subprocess.Popen(argv, **kwargs)
    group = _own_group(proc)
    relay_thread = None
    if relay:
        relay_thread = threading.Thread(
            target=_relay_lines, args=(proc.stdout, log_fh), daemon=True
        )
        relay_thread.start()
    try:
        with _terminate_as_exception():
            if capture:
                stdout, stderr = proc.communicate(timeout=timeout)
            else:
                proc.wait(timeout=timeout)
                stdout = stderr = None
    except subprocess.TimeoutExpired:
        _stop_tree(proc, group, interrupted=False)
        stdout, stderr = _drain(proc) if capture else (None, None)
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    except _Terminated:
        # Finish inside the outer stop's grace period, then exit the way the
        # default SIGTERM action would have.
        _stop_tree(proc, group, interrupted=False, grace=_GRACE_SECONDS / 2)
        raise SystemExit(128 + signal.SIGTERM) from None
    except BaseException:
        _stop_tree(proc, group, interrupted=True)
        raise
    finally:
        if relay_thread is not None:
            relay_thread.join(timeout=_GRACE_SECONDS)
    _reap_leftovers(group)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _own_group(proc) -> int | None:
    """The child's process group id, or None when it is not provably a
    group of its own.

    Read right after Popen returns: the child has already called setsid
    (Popen waits for the exec), so a real child leads a group whose id is its
    pid. Anything else (a mocked Popen, whose pid coerces to 1, or a child
    that shares our group) gets None, and no group signal is ever sent.
    This matters: killpg(1) is kill(-1) on Linux, which signals every
    process the user owns.
    """
    if sys.platform == "win32":
        return None
    pid = getattr(proc, "pid", None)
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 1:
        return None
    try:
        group = os.getpgid(pid)
    except OSError:
        return None
    if group != pid or group == os.getpgrp():
        return None
    return group


def _relay_lines(stream, log_fh) -> None:
    for line in stream:
        try:
            try:
                sys.stdout.write(line)
            except UnicodeEncodeError:
                # A console code page that cannot show a character: show
                # the line with a replacement, never drop it.
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                sys.stdout.write(line.encode(enc, "replace").decode(enc))
            sys.stdout.flush()
        except (OSError, ValueError):
            pass  # a closed terminal: the child must keep running
        if log_fh is not None:
            try:
                log_fh.write(line)
                log_fh.flush()
            except (OSError, ValueError):
                pass


def _drain(proc) -> tuple[str | None, str | None]:
    try:
        return proc.communicate(timeout=_GRACE_SECONDS)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None, None


def _stop_tree(
    proc, group: int | None, *, interrupted: bool, grace: float = _GRACE_SECONDS
) -> None:
    """Stop the child and every process in its group, then reap the child.

    A Ctrl-C reaches the group as SIGINT first (what the terminal would have
    sent), a timeout as SIGTERM; whatever is still alive after the grace
    period gets SIGKILL. Windows has no group signal: ``taskkill /T /F``
    stops the tree.
    """
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=30,
                creationflags=NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            proc.kill()
    elif group is None:
        proc.kill()  # no verified group: stop only the child
    else:
        _signal_group(group, signal.SIGINT if interrupted else signal.SIGTERM)
        try:
            proc.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
        _signal_group(group, signal.SIGKILL)
    try:
        proc.wait(timeout=_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("Process %s did not exit after SIGKILL", proc.pid)


def _reap_leftovers(group: int | None) -> None:
    """POSIX: stop group members still alive after the child exited (a worker
    the child started and did not wait for, or one an inner timeout left)."""
    if group is not None:
        _signal_group(group, signal.SIGKILL)


def _signal_group(pgid: int, sig: int) -> None:
    # Last line of defense: never a broadcast (0 or 1) and never our own group.
    if pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
