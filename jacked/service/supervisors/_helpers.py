"""Private serialization helpers for native supervisor artifacts."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def privately_controlled(status: os.stat_result) -> bool:
    """True when the current user owns the path and no one else can write it.

    Group write is accepted only for the caller's Linux user-private group.
    Debian and Ubuntu give such users umask 002, so every directory they
    create is group-writable, and that group holds only them (the same rule
    as ``jacked.service.spec._trusted_group_ids``).
    """
    from jacked.service.spec import _user_private_group_id

    if status.st_uid != os.getuid() or status.st_mode & 0o002:
        return False
    if status.st_mode & 0o020:
        private = _user_private_group_id()
        return private is not None and status.st_gid == private
    return True


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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
