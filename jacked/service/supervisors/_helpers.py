"""Private serialization helpers for native supervisor artifacts."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def privately_controlled(path: Path, status: os.stat_result) -> bool:
    """True when the current user owns ``path`` and no one else can write it.

    ``status`` is the caller's ``lstat`` of ``path``. Group write is accepted
    only for the caller's Linux user-private group. Debian and Ubuntu give
    such users umask 002, so every directory they create is group-writable,
    and that group holds only them (the same rule as
    ``jacked.service.spec._trusted_group_ids``). The ACL is read too, since
    mode bits cannot show it: on Linux the group bits show the MASK, so a
    named grant to another user would hide behind the private group; on
    macOS an extended ACL entry can grant another user write on its own.
    """
    from jacked.service import spec

    if status.st_uid != os.getuid() or status.st_mode & 0o002:
        return False
    if status.st_mode & 0o020:
        private = spec._user_private_group_id()
        if private is None or status.st_gid != private:
            return False
    try:
        spec._validate_linux_acl(path)
        if spec._HOST_IS_DARWIN:
            opened = spec._darwin_secure_status(
                path, directory=stat.S_ISDIR(status.st_mode)
            )
            if (opened.st_dev, opened.st_ino) != (status.st_dev, status.st_ino):
                return False  # the path changed since the caller's lstat
    except (OSError, ValueError):
        return False
    return True


def make_private_dirs(path: Path) -> None:
    """Create ``path`` and every missing ancestor with mode 0700.

    ``Path.mkdir(mode=..., parents=True)`` applies the mode to the leaf only,
    so under umask 002 the ancestors it creates would be group-writable.
    Directories that already exist are left as they are.
    """
    missing = []
    current = path
    while not os.path.lexists(current) and current.parent != current:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            directory.mkdir(mode=0o700)
        except FileExistsError:
            pass


def atomic_write(path: Path, content: bytes) -> None:
    make_private_dirs(path.parent)
    descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        elif os.name == "nt":
            from jacked.service.instance_storage import _secure_windows_path

            _secure_windows_path(temporary)
        with os.fdopen(descriptor, "wb", closefd=True) as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temporary, path)
        if os.name == "posix":
            path.chmod(0o600)
        elif os.name == "nt":
            _secure_windows_path(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def systemd_quote(value: str) -> str:
    if "\x00" in value or "\n" in value:
        raise ValueError("systemd argument contains an invalid character")
    return (
        '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'
    )


def windows_quote(value: str) -> str:
    if '"' in value or "\x00" in value or "\r" in value or "\n" in value:
        raise ValueError("Windows launcher path contains an unsupported character")
    return f'"{value}"'
