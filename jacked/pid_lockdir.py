"""A cross-process lock made from a directory and the pid of its holder.

``mkdir`` is atomic on Windows, macOS and Linux (``flock`` does not exist on
Windows). The holder makes the directory, then writes its pid into the
``pid`` file in it. The Codex swap lock and the Grok switch lock use these
rules:

* A lock whose holder process is dead is stale.
* A lock with no readable pid is stale only when the directory is older than
  ``NO_PID_GRACE_SECONDS``. A holder writes its pid immediately after the
  ``mkdir``, so a new lock with no pid belongs to a holder in that gap.
* A holder that cannot write its pid removes its directory and does not hold
  the lock. Another process cannot tell that lock from a crash.
* A holder removes the lock only while the pid file holds its own pid. If
  another process broke the lock as stale and took it, the lock stays.

The pid file holds only the pid. Older jacked versions read it as an
integer, so its format must not change.
"""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path
from typing import Callable, Optional

PID_FILE = "pid"
# The longest time between the mkdir and the pid write of a live holder,
# with a large margin for a slow disk.
NO_PID_GRACE_SECONDS = 10.0


def _write_pid(path: Path) -> None:
    path.write_text(str(os.getpid()), encoding="utf-8")


def _read_pid(lock_dir: Path) -> Optional[int]:
    try:
        return int((lock_dir / PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def try_acquire(lock_dir: Path) -> bool:
    """Make ``lock_dir`` and write this process's pid in it.

    Returns False when the directory exists. Raises ``OSError`` when the pid
    cannot be written: the directory is removed first, so nobody holds it.
    """
    try:
        os.mkdir(lock_dir)
    except FileExistsError:
        return False
    try:
        _write_pid(lock_dir / PID_FILE)
    except OSError:
        shutil.rmtree(lock_dir, ignore_errors=True)
        raise
    return True


def _default_liveness(pid: int) -> Optional[bool]:
    from jacked.service.process import process_liveness

    return process_liveness(pid)


def is_stale(
    lock_dir: Path,
    liveness: Optional[Callable[[int], Optional[bool]]] = None,
    *,
    now: Optional[float] = None,
) -> bool:
    """True when the holder of ``lock_dir`` is gone.

    A dead holder makes the lock stale. An unknown liveness does not. A lock
    with no readable pid is stale only after ``NO_PID_GRACE_SECONDS``.

    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp()) / "lock"
    >>> d.mkdir()
    >>> is_stale(d), is_stale(d, now=time.time() + 60)
    (False, True)
    >>> _ = (d / PID_FILE).write_text("4242")
    >>> is_stale(d, lambda pid: False), is_stale(d, lambda pid: None)
    (True, False)
    """
    holder = _read_pid(lock_dir)
    if holder is None:
        try:
            age = (now if now is not None else time.time()) - lock_dir.stat().st_mtime
        except OSError:
            return False  # The lock went away. The next mkdir decides.
        return age > NO_PID_GRACE_SECONDS
    probe = liveness or _default_liveness
    try:
        return probe(holder) is False
    except (OSError, SystemError):
        return False


def release(lock_dir: Path) -> None:
    """Remove ``lock_dir`` only while its pid file holds this process's pid."""
    if _read_pid(lock_dir) == os.getpid():
        shutil.rmtree(lock_dir, ignore_errors=True)
