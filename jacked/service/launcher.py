"""Immutable, content-addressed launcher installation."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from pathlib import Path


POSIX_LAUNCHER_SOURCE = b"""#!/bin/sh
set -eu
runtime=$1
shift
case "$runtime" in
    /*) ;;
    *) exit 64 ;;
esac
[ -f "$runtime" ] || exit 66
[ -x "$runtime" ] || exit 77
exec "$runtime" "$@"
"""


def verify_launcher(path: Path, expected_sha256: str) -> bool:
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            return False
        if os.name == "posix" and (
            status.st_uid != os.getuid() or status.st_mode & 0o077
        ):
            return False
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == expected_sha256


def install_versioned_launcher(
    root: Path,
    *,
    version: str,
    name: str,
    content: bytes,
    expected_sha256: str,
    executable: bool = False,
) -> Path:
    """Install once into a stable version slot; never rewrite altered files."""

    if not version or any(char in version for char in "/\\\x00"):
        raise ValueError("invalid launcher version")
    if not name or Path(name).name != name:
        raise ValueError("invalid launcher name")
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected_sha256:
        raise ValueError("launcher source hash does not match expected source hash")
    if root.exists() and root.is_symlink():
        raise ValueError("launcher root cannot be a symlink")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        from jacked.service.instance_storage import _secure_windows_path

        _secure_windows_path(root)
    slot = root / version
    if slot.exists() and slot.is_symlink():
        raise ValueError("launcher version slot cannot be a symlink")
    slot.mkdir(mode=0o700, exist_ok=True)
    if os.name == "nt":
        _secure_windows_path(slot)
    if os.name == "posix":
        if root.stat().st_uid != os.getuid() or slot.stat().st_uid != os.getuid():
            raise ValueError("launcher directories have the wrong owner")
        root.chmod(0o700)
        slot.chmod(0o700)
    target = slot / name
    if target.exists() or target.is_symlink():
        if verify_launcher(target, expected_sha256):
            return target
        raise ValueError("refusing to overwrite a foreign or altered launcher")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{name}.", dir=slot)
    temporary = Path(temp_name)
    mode = 0o700 if executable else 0o600
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb", closefd=True) as file:
            descriptor = -1
            file.write(content)
            file.flush()
            os.fsync(file.fileno())
        # Hard-link publication fails atomically if a target appeared after
        # our check. Unlinking the temporary name leaves one immutable link.
        try:
            os.link(temporary, target)
        except FileExistsError as exc:
            raise ValueError("launcher target appeared during installation") from exc
        temporary.unlink()
        if os.name == "nt":
            _secure_windows_path(target)
        if not verify_launcher(target, expected_sha256):
            target.unlink(missing_ok=True)
            raise ValueError("installed launcher failed verification")
        return target
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)
