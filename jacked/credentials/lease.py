"""Serialized credential switch lease abstraction."""

from __future__ import annotations

import logging
import os
import stat
import threading
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import ContextManager, Iterator, Protocol

logger = logging.getLogger(__name__)


class SwitchLease(Protocol):
    def acquire(self) -> ContextManager[bool]: ...


# Why the last ``acquire`` yielded False. ``REFUSAL_HELD`` is another holder;
# ``REFUSAL_UNUSABLE`` is a lock file that could not be opened at all, which
# cannot prove that nobody is switching and so refuses fail-closed. A caller
# that reports the refusal to a person reads ``refusal`` right after a False.
REFUSAL_HELD = "held"
REFUSAL_UNUSABLE = "unusable"


class ProcessSwitchLease:
    """Nonblocking in-process lease; service adapters may supply an OS lease."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.refusal: str | None = None

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        self.refusal = None if acquired else REFUSAL_HELD
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()


class FileSwitchLease:
    """Nonblocking cross-process lease held on one advisory lock file.

    A thread lock only serializes one interpreter. ``jacked launch`` runs in
    its own process while the dashboard service runs in another, and both call
    ``activate_account``. Without an OS lock the two interleave their authority,
    mirror and identity writes and the account that wins is undefined. POSIX
    uses ``fcntl.flock``; Windows uses ``msvcrt.locking``. Both release when the
    handle closes, so a crashed holder never leaves a stale lease.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self.refusal: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        handle = self._open()
        if handle is None:
            # Fail closed: an unusable lock file cannot prove that no other
            # process is switching, and a refused switch is always recoverable.
            self.refusal = REFUSAL_UNUSABLE
            yield False
            return
        acquired = False
        try:
            acquired = _lock(handle)
            self.refusal = None if acquired else REFUSAL_HELD
            yield acquired
        finally:
            if acquired:
                _unlock(handle, self._path.name)
            handle.close()

    def _open(self):
        # The log lines name the lease file and the exception class only: the
        # text of an OSError carries the full path, and the update lock uses
        # this class too, so the message must not say "credential switch".
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                os.chmod(self._path.parent, 0o700)
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self._path, flags, 0o600)
        except OSError as exc:
            logger.warning(
                "Could not open the lease %s (%s)", self._path.name, type(exc).__name__
            )
            return None
        try:
            if stat.S_ISREG(os.fstat(descriptor).st_mode):
                if os.name != "nt":
                    os.fchmod(descriptor, 0o600)
                return os.fdopen(descriptor, "r+b")
            reason = "not a regular file"
        except OSError as exc:
            reason = type(exc).__name__
        os.close(descriptor)
        logger.warning("Could not open the lease %s (%s)", self._path.name, reason)
        return None


def _lock(handle) -> bool:
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle, name: str) -> None:
    """Release the lock on ``handle``. ``name`` is the lease file name."""
    try:
        if os.name == "nt":  # pragma: no cover - Windows
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError as exc:
        logger.warning("Could not release the lease %s (%s)", name, type(exc).__name__)


class CompositeSwitchLease:
    """Hold several leases together; release all of them if any one refuses.

    The in-process lease and the cross-process lease answer different
    questions. A switch must hold both, because two threads of one process
    share a file lock and cannot be separated by it.
    """

    def __init__(self, *leases: SwitchLease) -> None:
        self._leases = leases
        self.refusal: str | None = None

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        self.refusal = None
        with ExitStack() as stack:
            for lease in self._leases:
                if not stack.enter_context(lease.acquire()):
                    # The first lease that refused says why; a lease that
                    # does not report a reason was simply held.
                    self.refusal = getattr(lease, "refusal", None) or REFUSAL_HELD
                    yield False
                    return
            yield True
