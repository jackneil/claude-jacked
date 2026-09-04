"""Private state storage and process/user identity primitives."""

from __future__ import annotations

import getpass
import json
import logging
import os
import secrets
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from jacked.service.instance_models import InstanceManifest, ProcessIdentity

logger = logging.getLogger(__name__)


def _ensure_private_directory(path: Path) -> None:
    if os.name == "nt":
        from jacked.service.windows_state import ensure_private_windows_directory

        ensure_private_windows_directory(path)
        return
    if path.exists() and path.is_symlink():
        raise ValueError("service state directory cannot be a symlink")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        status = path.stat()
        if status.st_uid != os.getuid():
            raise ValueError("service state directory has the wrong owner")
        path.chmod(0o700)


def _validate_private_file(path: Path) -> os.stat_result:
    status = path.lstat()
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        raise ValueError("manifest must be a regular private file")
    if getattr(status, "st_file_attributes", 0) & 0x400:
        raise ValueError("manifest must not be a Windows reparse point")
    if os.name == "posix":
        if status.st_uid != os.getuid() or status.st_mode & 0o077:
            raise ValueError("manifest must be owned by this user with mode 0600")
    elif os.name == "nt":
        from jacked.service.windows_security import inspect_windows_path

        if not inspect_windows_path(path).private_for(directory=False):
            raise ValueError("manifest must be a private current-user Windows file")
    return status


def _recover_interrupted_hardlink(path: Path, temp_prefix: str) -> bool:
    """Remove one validated temp alias left after atomic hard-link publication."""

    try:
        target = path.lstat()
        if (
            not stat.S_ISREG(target.st_mode)
            or target.st_nlink != 2
            or bool(getattr(target, "st_file_attributes", 0) & 0x400)
        ):
            return False
        if os.name == "posix" and (
            target.st_uid != os.getuid() or target.st_mode & 0o077
        ):
            return False
        if os.name == "nt":
            from jacked.service.windows_security import inspect_windows_path

            inspected = inspect_windows_path(path)
            if (
                inspected.is_directory
                or inspected.is_reparse_point
                or not inspected.owner_matches
                or not inspected.dacl_private
            ):
                return False
        matches = []
        for candidate in path.parent.iterdir():
            if candidate.name == path.name or not candidate.name.startswith(
                temp_prefix
            ):
                continue
            status = candidate.lstat()
            if stat.S_ISREG(status.st_mode) and (
                status.st_dev,
                status.st_ino,
            ) == (target.st_dev, target.st_ino):
                matches.append(candidate)
        if len(matches) != 1:
            return False
        matches[0].unlink()
        return path.lstat().st_nlink == 1
    except (FileNotFoundError, OSError) as exc:
        logger.warning(
            "Interrupted hardlink recovery failed for %s: %s",
            path,
            type(exc).__name__,
        )
        return False


def publish_manifest(path: Path, manifest: InstanceManifest) -> None:
    """Durably and atomically publish a private manifest."""

    _ensure_private_directory(path.parent)
    encoded = json.dumps(
        manifest.to_dict(), sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        elif os.name == "nt":
            _secure_windows_path(temp_path)
        with os.fdopen(descriptor, "wb", closefd=True) as file:
            descriptor = -1
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp_path, path)
        if os.name == "posix":
            path.chmod(0o600)
            directory_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        elif os.name == "nt":
            _secure_windows_path(path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temp_path.unlink(missing_ok=True)


def load_or_create_machine_id(path: Path) -> str:
    """Return a private install-scoped random identity without using host secrets."""

    _ensure_private_directory(path.parent)
    if path.exists() or path.is_symlink():
        _recover_interrupted_hardlink(path, f".{path.name}.")
        _validate_private_file(path)
        value = path.read_text(encoding="ascii").strip()
        if len(value) < 32 or any(
            char
            not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in value
        ):
            raise ValueError("machine identity file is invalid")
        return value
    value = secrets.token_urlsafe(32)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        if os.name == "posix":
            os.fchmod(descriptor, 0o600)
        elif os.name == "nt":
            _secure_windows_path(temporary)
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=True) as file:
            descriptor = -1
            file.write(value + "\n")
            file.flush()
            os.fsync(file.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            temporary.unlink(missing_ok=True)
            return load_or_create_machine_id(path)
        temporary.unlink()
        if os.name == "nt":
            _secure_windows_path(path)
        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def read_manifest(path: Path) -> InstanceManifest:
    before = _validate_private_file(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("manifest changed while it was being opened")
        if opened.st_size > 65_536:
            raise ValueError("manifest is too large")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as file:
            payload = json.load(file, object_pairs_hook=_strict_json_object)
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValueError("manifest must be an object")
    return InstanceManifest.from_dict(payload)


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate manifest field: {key}")
        result[key] = value
    return result


def remove_manifest_if_current(path: Path, instance_id: str) -> bool:
    try:
        current = read_manifest(path)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return False
    if current.instance_id != instance_id:
        return False
    path.unlink(missing_ok=True)
    return True


def current_user_identity() -> str:
    if os.name == "posix":
        return f"uid:{os.getuid()}"
    if os.name == "nt":
        return f"sid:{_windows_current_sid()}"
    return f"user:{getpass.getuser()}"


def _windows_current_sid() -> str:
    from jacked.service.windows_security import current_user_sid

    return current_user_sid()


def _secure_windows_path(path: Path) -> None:
    from jacked.service.windows_security import secure_windows_path

    secure_windows_path(path)


def _linux_process_identity(pid: int) -> ProcessIdentity:
    stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    closing = stat_text.rfind(")")
    if closing < 0:
        raise ProcessLookupError(pid)
    fields = stat_text[closing + 2 :].split()
    creation = fields[19]
    executable = os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
    return ProcessIdentity(
        pid=pid, creation_id=f"linux-boot-ticks:{creation}", executable=executable
    )


def _darwin_process_identity(pid: int) -> ProcessIdentity:
    result = subprocess.run(
        ["ps", "-o", "lstart=", "-o", "comm=", "-p", str(pid)],
        capture_output=True,
        text=True,
        timeout=2,
        check=False,
    )
    line = result.stdout.strip()
    if result.returncode != 0 or not line:
        raise ProcessLookupError(pid)
    parts = line.split(maxsplit=5)
    if len(parts) != 6:
        raise ProcessLookupError(pid)
    creation = " ".join(parts[:5])
    executable = os.path.realpath(parts[5])
    return ProcessIdentity(
        pid=pid, creation_id=f"darwin-lstart:{creation}", executable=executable
    )


def _windows_process_identity(pid: int) -> ProcessIdentity:
    import ctypes
    from ctypes import wintypes
    from jacked.service.windows_security import windows_libraries

    api = windows_libraries()
    query = 0x1000
    handle = api.kernel32.OpenProcess(query, False, pid)
    if not handle:
        raise ProcessLookupError(pid)
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not api.kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            raise ProcessLookupError(pid)
        buffer = ctypes.create_unicode_buffer(32768)
        size = wintypes.DWORD(len(buffer))
        if not api.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            raise ProcessLookupError(pid)
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        return ProcessIdentity(
            pid=pid,
            creation_id=f"windows-filetime:{ticks}",
            executable=os.path.realpath(buffer.value),
        )
    finally:
        api.kernel32.CloseHandle(handle)


def process_identity(pid: int) -> ProcessIdentity:
    if sys.platform.startswith("linux"):
        return _linux_process_identity(pid)
    if sys.platform == "darwin":
        return _darwin_process_identity(pid)
    if sys.platform == "win32":
        return _windows_process_identity(pid)
    raise OSError(f"unsupported platform: {sys.platform}")


def process_user_identity(pid: int) -> str:
    if sys.platform.startswith("linux"):
        return f"uid:{Path(f'/proc/{pid}').stat().st_uid}"
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ps", "-o", "uid=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        if result.returncode != 0 or not result.stdout.strip().isdigit():
            raise ProcessLookupError(pid)
        return f"uid:{int(result.stdout.strip())}"
    if sys.platform == "win32":
        # Opening another user's process token may be denied, which is itself
        # insufficient ownership evidence and therefore fails closed.
        import ctypes
        from ctypes import wintypes
        from jacked.service.windows_security import (
            windows_libraries,
            windows_token_sid,
        )

        query = 0x1000
        token_query = 0x0008
        api = windows_libraries()
        process = api.kernel32.OpenProcess(query, False, pid)
        if not process:
            raise ProcessLookupError(pid)
        token = wintypes.HANDLE()
        try:
            if not api.advapi32.OpenProcessToken(
                process, token_query, ctypes.byref(token)
            ):
                raise ProcessLookupError(pid)
            try:
                sid = windows_token_sid(token, api)
            except OSError:
                raise ProcessLookupError(pid)
            return f"sid:{sid}"
        finally:
            if token:
                api.kernel32.CloseHandle(token)
            api.kernel32.CloseHandle(process)
    raise OSError(f"unsupported platform: {sys.platform}")


def current_process_identity() -> ProcessIdentity:
    return process_identity(os.getpid())
