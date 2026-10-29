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
    if capture:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace")
    elif relay:
        kwargs.update(stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
    else:
        kwargs.update(stdout=log_fh, stderr=log_fh)

    proc = subprocess.Popen(argv, **kwargs)
    relay_thread = None
    if relay:
        relay_thread = threading.Thread(
            target=_relay_lines, args=(proc.stdout, log_fh), daemon=True
        )
        relay_thread.start()
    try:
        if capture:
            stdout, stderr = proc.communicate(timeout=timeout)
        else:
            proc.wait(timeout=timeout)
            stdout = stderr = None
    except subprocess.TimeoutExpired:
        _stop_tree(proc, interrupted=False)
        stdout, stderr = _drain(proc) if capture else (None, None)
        raise subprocess.TimeoutExpired(argv, timeout, output=stdout, stderr=stderr) from None
    except BaseException:
        _stop_tree(proc, interrupted=True)
        raise
    finally:
        if relay_thread is not None:
            relay_thread.join(timeout=_GRACE_SECONDS)
    _reap_leftovers(proc)
    return subprocess.CompletedProcess(argv, proc.returncode, stdout, stderr)


def _relay_lines(stream, log_fh) -> None:
    for line in stream:
        try:
            sys.stdout.write(line)
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


def _stop_tree(proc, *, interrupted: bool) -> None:
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
    else:
        _signal_group(proc.pid, signal.SIGINT if interrupted else signal.SIGTERM)
        try:
            proc.wait(timeout=_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        _signal_group(proc.pid, signal.SIGKILL)
    try:
        proc.wait(timeout=_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        logger.warning("Process %s did not exit after SIGKILL", proc.pid)


def _reap_leftovers(proc) -> None:
    """POSIX: stop group members still alive after the child exited (a worker
    the child started and did not wait for, or one an inner timeout left)."""
    if sys.platform != "win32":
        _signal_group(proc.pid, signal.SIGKILL)


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass
