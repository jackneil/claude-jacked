"""Immutable, content-addressed launcher installation."""

from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path


POSIX_LAUNCHER_SOURCE = b"""#!/bin/sh
set -eu
runtime=$1
expected=$2
shift 2
case "$runtime" in
    /*) ;;
    *) exit 64 ;;
esac
case "$expected" in
    /*) ;;
    *) exit 64 ;;
esac
[ -f "$runtime" ] || exit 66
[ -x "$runtime" ] || exit 77
[ -f "$expected" ] || exit 66
[ -x "$expected" ] || exit 77
[ "$runtime" -ef "$expected" ] || exit 78
exec "$runtime" "$@"
"""


@dataclass(frozen=True)
class LauncherInstall:
    version: str
    name: str
    content: bytes
    expected_sha256: str
    executable: bool = False


def verify_launcher(path: Path, expected_sha256: str) -> bool:
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            return False
        if os.name == "posix" and (
            status.st_uid != os.getuid() or status.st_mode & 0o077
        ):
            return False
        if os.name == "nt":
            from jacked.service.windows_security import inspect_windows_path

            if not inspect_windows_path(path).private_for(directory=False):
                return False
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == expected_sha256


def _validate_install(request: LauncherInstall) -> None:
    if not request.version or any(char in request.version for char in "/\\\x00"):
        raise ValueError("invalid launcher version")
    if not request.name or Path(request.name).name != request.name:
        raise ValueError("invalid launcher name")
    actual = hashlib.sha256(request.content).hexdigest()
    if actual != request.expected_sha256:
        raise ValueError("launcher source hash does not match expected source hash")


def _prepare_slot(root: Path, version: str) -> Path:
    from jacked.service.instance_storage import _ensure_private_directory

    _ensure_private_directory(root)
    slot = root / version
    _ensure_private_directory(slot)
    if os.name == "posix":
        if root.stat().st_uid != os.getuid() or slot.stat().st_uid != os.getuid():
            raise ValueError("launcher directories have the wrong owner")
        root.chmod(0o700)
        slot.chmod(0o700)
    return slot


def _publish_launcher(
    slot: Path, target: Path, request: LauncherInstall
) -> Path:
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{request.name}.", dir=slot
    )
    temporary = Path(temp_name)
    mode = 0o700 if request.executable else 0o600
    try:
        if os.name == "posix":
            os.fchmod(descriptor, mode)
        elif os.name == "nt":
            from jacked.service.instance_storage import _secure_windows_path

            _secure_windows_path(temporary)
        with os.fdopen(descriptor, "wb", closefd=True) as file:
            descriptor = -1
            file.write(request.content)
            file.flush()
            os.fsync(file.fileno())
        os.link(temporary, target)
        temporary.unlink()
        if os.name == "nt":
            _secure_windows_path(target)
        if verify_launcher(target, request.expected_sha256):
            return target
        target.unlink(missing_ok=True)
        raise ValueError("installed launcher failed verification")
    except FileExistsError as exc:
        raise ValueError("launcher target appeared during installation") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _sweep_orphaned_temps(slot: Path, prefix: str) -> None:
    """Remove temp files a hard exit left in the slot.

    A publish that dies between ``mkstemp`` and ``os.link`` (a preflight
    timeout exits without unwinding) leaves a same-user 0600 temp file. It is
    never resolved as a launcher, so this is hygiene: remove regular files
    with the temp prefix that are not hard-linked to the target.
    """
    try:
        entries = list(slot.iterdir())
    except OSError:
        return
    for entry in entries:
        if not entry.name.startswith(prefix):
            continue
        try:
            status = entry.lstat()
            if stat.S_ISREG(status.st_mode) and status.st_nlink == 1:
                entry.unlink()
        except OSError:
            continue


def install_versioned_launcher(root: Path, request: LauncherInstall) -> Path:
    """Install once into a stable version slot; never rewrite altered files."""

    _validate_install(request)
    slot = _prepare_slot(root, request.version)
    target = slot / request.name
    _sweep_orphaned_temps(slot, f".{request.name}.")
    if target.exists() or target.is_symlink():
        from jacked.service.instance_storage import _recover_interrupted_hardlink

        _recover_interrupted_hardlink(target, f".{request.name}.")
        if verify_launcher(target, request.expected_sha256):
            return target
        raise ValueError("refusing to overwrite a foreign or altered launcher")
    return _publish_launcher(slot, target, request)
