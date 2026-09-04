"""Explicit recovery for invalid private service ownership state."""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from jacked.service.instance_models import ServicePaths


def _unsafe_manifest(path: Path) -> bool:
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or bool(getattr(status, "st_file_attributes", 0) & 0x400)
    ):
        return True
    if os.name == "posix":
        return status.st_uid != os.getuid() or bool(status.st_mode & 0o077)
    if os.name == "nt":
        from jacked.service.windows_security import inspect_windows_path

        return not inspect_windows_path(path).private_for(directory=False)
    return False


def _unsafe_control(path: Path) -> bool:
    if not (path.exists() or path.is_symlink()):
        return False
    if os.name != "posix":
        return False
    status = path.lstat()
    return not stat.S_ISSOCK(status.st_mode) or status.st_uid != os.getuid()


def quarantine_invalid_ownership(paths: ServicePaths) -> Path | None:
    """Move a safely-owned invalid manifest aside while holding the v2 lease."""

    from jacked.service.instance import ServiceLease, read_manifest

    lease = ServiceLease(paths.lease)
    lease.acquire()
    try:
        try:
            read_manifest(paths.manifest)
            return None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            pass
        if _unsafe_manifest(paths.manifest):
            raise ValueError(
                f"unsafe manifest requires manual backup: {paths.manifest}"
            )
        if _unsafe_control(paths.control):
            raise ValueError(
                f"unsafe control path requires manual backup: {paths.control}"
            )
        backup = paths.manifest.with_name(
            f"{paths.manifest.name}.invalid-{time.time_ns()}"
        )
        os.replace(paths.manifest, backup)
        if paths.control.exists() or paths.control.is_symlink():
            paths.control.unlink()
        return backup
    finally:
        lease.release()
