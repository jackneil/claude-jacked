"""Cross-platform serialization for native supervisor mutations."""

from __future__ import annotations

import os
import stat
import threading
from pathlib import Path
from typing import Any


class TransitionBusy(RuntimeError):
    """Another process or thread is changing this native service."""


class SupervisorTransitionLease:
    """A non-blocking service-scoped lock that does not alter parent modes."""

    _held: set[str] = set()
    _guard = threading.Lock()

    def __init__(self, artifact: Path, service_id: str):
        self.path = artifact.parent / f".{service_id}.transition.lock"
        self.handle: Any | None = None

    def __enter__(self):
        _ensure_safe_parent(self.path.parent)
        key = os.path.realpath(self.path)
        with self._guard:
            if key in self._held:
                raise TransitionBusy("another native transition is active")
            self._held.add(key)
        try:
            self.handle = _open_lock(self.path)
            _lock(self.handle)
        except BaseException:
            if self.handle is not None:
                self.handle.close()
                self.handle = None
            with self._guard:
                self._held.discard(key)
            raise
        return self

    def __exit__(self, *_args):
        key = os.path.realpath(self.path)
        try:
            if self.handle is not None:
                _unlock(self.handle)
                self.handle.close()
                self.handle = None
        finally:
            with self._guard:
                self._held.discard(key)


def _ensure_safe_parent(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    status = path.lstat()
    if not stat.S_ISDIR(status.st_mode):
        raise OSError("supervisor directory is not a directory")
    if os.name == "posix" and (
        status.st_uid != os.getuid() or status.st_mode & 0o022
    ):
        raise OSError("supervisor directory is not privately controlled")


def _open_lock(path: Path):
    if os.name == "nt":
        handle = open(path, "a+b")
        from jacked.service.instance_storage import _secure_windows_path

        _secure_windows_path(path)
        return handle
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    status = os.fstat(descriptor)
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or status.st_uid != os.getuid()
    ):
        os.close(descriptor)
        raise OSError("unsafe transition lease")
    os.fchmod(descriptor, 0o600)
    return os.fdopen(descriptor, "a+b")


def _lock(handle) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            handle.write(b"\0")
            handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        raise TransitionBusy("another native transition is active") from exc


def _unlock(handle) -> None:
    if os.name == "nt":
        import msvcrt

        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
