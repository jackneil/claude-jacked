"""Canonical identity contract for the jacked background service.

The generation is content-addressed.  Supervisor code may act on an artifact
only when its owner marker and generation match this object exactly.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any


class SupervisorKind(str, Enum):
    LAUNCHD = "launchd"
    SYSTEMD_USER = "systemd-user"
    TASK_SCHEDULER = "task-scheduler"
    MANUAL = "manual"


_PYTHON_ENTRYPOINT_RE = re.compile(
    r"(?:python|pypy|graalpy)(?:\d+(?:\.\d+)*)?[a-z]*"
)
_DARWIN_ACL_TYPE_EXTENDED = 0x100
_DARWIN_ACL_EXTENDED_ALLOW = 1
_DARWIN_MUTATING_ACL_MASK = sum(1 << bit for bit in (2, 4, 5, 6, 8, 10, 12, 13))
_HOST_IS_DARWIN = sys.platform == "darwin"


@lru_cache(maxsize=1)
def _darwin_acl_api():
    library = ctypes.CDLL("libc.dylib", use_errno=True)
    library.acl_get_fd_np.argtypes = [ctypes.c_int, ctypes.c_int]
    library.acl_get_fd_np.restype = ctypes.c_void_p
    library.acl_get_link_np.argtypes = [ctypes.c_char_p, ctypes.c_int]
    library.acl_get_link_np.restype = ctypes.c_void_p
    library.acl_get_entry.argtypes = [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    library.acl_get_entry.restype = ctypes.c_int
    library.acl_get_tag_type.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_int),
    ]
    library.acl_get_tag_type.restype = ctypes.c_int
    library.acl_get_permset_mask_np.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint64),
    ]
    library.acl_get_permset_mask_np.restype = ctypes.c_int
    library.acl_free.argtypes = [ctypes.c_void_p]
    library.acl_free.restype = ctypes.c_int
    return library


def _darwin_acl_allows_mutation(acl: int | None) -> bool:
    if not acl:
        if ctypes.get_errno() == errno.ENOENT:
            return False
        raise ValueError("runtime_path ACL could not be inspected")
    library = _darwin_acl_api()
    try:
        entry = ctypes.c_void_p()
        entry_id = 0
        while True:
            ctypes.set_errno(0)
            result = library.acl_get_entry(acl, entry_id, ctypes.byref(entry))
            if result != 0:
                if ctypes.get_errno() == errno.EINVAL:
                    return False
                raise ValueError("runtime_path ACL entry could not be inspected")
            tag = ctypes.c_int()
            mask = ctypes.c_uint64()
            if library.acl_get_tag_type(entry, ctypes.byref(tag)) != 0 or (
                library.acl_get_permset_mask_np(entry, ctypes.byref(mask)) != 0
            ):
                raise ValueError("runtime_path ACL entry could not be inspected")
            if (
                tag.value == _DARWIN_ACL_EXTENDED_ALLOW
                and mask.value & _DARWIN_MUTATING_ACL_MASK
            ):
                return True
            entry_id = -1
    finally:
        library.acl_free(acl)


def _darwin_secure_status(path: Path, *, directory: bool) -> os.stat_result:
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    if directory:
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ValueError("runtime_path could not be opened without links") from exc
    try:
        status = os.fstat(descriptor)
        library = _darwin_acl_api()
        ctypes.set_errno(0)
        acl = library.acl_get_fd_np(descriptor, _DARWIN_ACL_TYPE_EXTENDED)
        if _darwin_acl_allows_mutation(acl):
            raise ValueError("runtime_path has a mutating extended ACL")
        return status
    finally:
        os.close(descriptor)


def _validate_darwin_link_acl(path: Path) -> None:
    if not _HOST_IS_DARWIN:
        return
    library = _darwin_acl_api()
    ctypes.set_errno(0)
    acl = library.acl_get_link_np(
        os.fsencode(path), _DARWIN_ACL_TYPE_EXTENDED
    )
    if _darwin_acl_allows_mutation(acl):
        raise ValueError("runtime_path symlink has a mutating extended ACL")


# Groups whose members can already act as root on the host. macOS ships
# ``%admin ALL=(ALL) ALL`` in /etc/sudoers and Homebrew makes its prefix
# admin group-writable by design, so every Homebrew-installed Python lives
# under such a directory. Linux distributions use ``sudo`` or ``wheel``.
_ROOT_EQUIVALENT_GROUP_NAMES = ("root", "wheel", "admin", "sudo")


def _user_private_group_id() -> int | None:
    """Return the caller's primary gid when no other account can use it.

    Linux creates one group per user (same name, no other members, no other
    account with it as primary group). Writes granted to that group reach
    only the user, so it is as trusted as the user. macOS ``staff`` fails
    every one of these tests, which keeps staff-writable paths untrusted.
    """
    import grp
    import pwd

    uid = os.getuid()
    try:
        user = pwd.getpwuid(uid)
        group = grp.getgrgid(user.pw_gid)
    except KeyError:
        return None
    if group.gr_name != user.pw_name:
        return None
    if any(member != user.pw_name for member in group.gr_mem):
        return None
    for other in pwd.getpwall():
        if other.pw_gid == group.gr_gid and other.pw_uid != uid:
            return None
    return group.gr_gid


@lru_cache(maxsize=1)
def _trusted_group_ids() -> frozenset[int]:
    """Groups a writable bit may grant without opening a privilege boundary.

    A directory writable only by principals who can already become root is
    not an escalation path: any of them could replace it with ``sudo``.
    """
    import grp

    trusted = {0}
    for name in _ROOT_EQUIVALENT_GROUP_NAMES:
        try:
            trusted.add(grp.getgrnam(name).gr_gid)
        except KeyError:
            continue
    private = _user_private_group_id()
    if private is not None:
        trusted.add(private)
    return frozenset(trusted)


@lru_cache(maxsize=1)
def _trusted_owner_ids() -> frozenset[int]:
    """Root, the caller, and every member of a root-equivalent group."""
    import grp
    import pwd

    trusted = {0, os.getuid()}
    root_equivalent_gids = set()
    for name in _ROOT_EQUIVALENT_GROUP_NAMES:
        try:
            group = grp.getgrnam(name)
        except KeyError:
            continue
        root_equivalent_gids.add(group.gr_gid)
        for member in group.gr_mem:
            try:
                trusted.add(pwd.getpwnam(member).pw_uid)
            except KeyError:
                continue
    for user in pwd.getpwall():
        if user.pw_gid in root_equivalent_gids:
            trusted.add(user.pw_uid)
    return frozenset(trusted)


def _trusted_posix_owner(owner: int) -> bool:
    return owner in _trusted_owner_ids()


def _writable_by_untrusted_principal(
    status: os.stat_result, *, directory: bool
) -> bool:
    """True when a principal outside the trust set could mutate the path.

    World-writable is always untrusted unless the sticky bit protects a
    directory. Group-writable is untrusted unless the group is
    root-equivalent or the caller's private group (see
    ``_trusted_group_ids``).
    """
    sticky = directory and bool(status.st_mode & stat.S_ISVTX)
    if status.st_mode & 0o002 and not sticky:
        return True
    if status.st_mode & 0o020 and not sticky:
        return status.st_gid not in _trusted_group_ids()
    return False


def _validate_posix_directory_chain(path: Path) -> None:
    """Require every directory to resist replacement by another local user."""

    current = path
    while True:
        try:
            status = (
                _darwin_secure_status(current, directory=True)
                if _HOST_IS_DARWIN
                else current.lstat()
            )
        except OSError as exc:
            raise ValueError("runtime_path directory chain is incomplete") from exc
        if (
            not stat.S_ISDIR(status.st_mode)
            or not _trusted_posix_owner(status.st_uid)
            or _writable_by_untrusted_principal(status, directory=True)
        ):
            raise ValueError("runtime_path has an untrusted writable directory")
        if current.parent == current:
            return
        current = current.parent


def _validate_posix_file(path: Path, name: str, *, executable: bool = False) -> None:
    try:
        status = (
            _darwin_secure_status(path, directory=False)
            if _HOST_IS_DARWIN
            else path.lstat()
        )
    except OSError as exc:
        raise ValueError(f"{name} must be a trusted regular file") from exc
    if (
        not stat.S_ISREG(status.st_mode)
        or not _trusted_posix_owner(status.st_uid)
        or _writable_by_untrusted_principal(status, directory=False)
    ):
        raise ValueError(f"{name} must be a trusted regular file")
    if executable and not status.st_mode & 0o111:
        raise ValueError(f"{name} must be executable")


@dataclass(frozen=True)
class ServiceSpec:
    """Everything that must match before lifecycle control is authorized."""

    service_id: str
    protocol_version: int
    build_version: str
    runtime_path: str
    launcher_path: str
    launcher_sha256: str
    supervisor: SupervisorKind
    arguments: tuple[str, ...]
    runtime_target_path: str | None = None
    schema_version: int = 1
    owner: str = "claude-jacked"

    def __post_init__(self) -> None:
        if not self.service_id or not self.build_version:
            raise ValueError("service_id and build_version are required")
        if self.protocol_version < 1 or self.schema_version < 1:
            raise ValueError("protocol and schema versions must be positive")
        resolved_runtime = os.path.realpath(self.runtime_path)
        runtime_target = self.runtime_target_path or resolved_runtime
        self._validate_path(runtime_target, "runtime_target_path")
        if runtime_target != resolved_runtime:
            raise ValueError("runtime_target_path does not match runtime_path")
        object.__setattr__(self, "runtime_target_path", runtime_target)
        self._validate_runtime_path(self.runtime_path, runtime_target)
        self._validate_path(self.launcher_path, "launcher_path")
        if len(self.launcher_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.launcher_sha256
        ):
            raise ValueError("launcher_sha256 must be a lowercase SHA-256 digest")
        if not self.arguments or self.arguments[0] != "-I":
            raise ValueError("service Python must start in isolated mode (-I)")
        if any("\x00" in value for value in self.arguments):
            raise ValueError("arguments cannot contain NUL")

    @staticmethod
    def _validate_path(value: str, name: str) -> None:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"{name} must be absolute")
        normalized = os.path.normpath(value)
        if normalized != value:
            raise ValueError(f"{name} must be normalized")

    @classmethod
    def _validate_runtime_path(cls, value: str, target: str) -> None:
        cls._validate_path(value, "runtime_path")
        if target == value:
            return
        if os.name != "posix":
            raise ValueError("runtime_path symlinks are not supported on this platform")
        path = Path(value)
        venv_config = path.parent.parent / "pyvenv.cfg"
        if (
            os.path.realpath(path.parent) != str(path.parent)
            or not path.is_symlink()
            or not _PYTHON_ENTRYPOINT_RE.fullmatch(path.name)
        ):
            raise ValueError(
                "runtime_path symlink must be a virtualenv Python entrypoint"
            )
        link_status = path.lstat()
        if not _trusted_posix_owner(link_status.st_uid):
            raise ValueError("runtime_path symlink has an untrusted owner")
        _validate_darwin_link_acl(path)
        _validate_posix_directory_chain(path.parent)
        _validate_posix_file(venv_config, "pyvenv.cfg")
        target_path = Path(target)
        if not _PYTHON_ENTRYPOINT_RE.fullmatch(target_path.name):
            raise ValueError("runtime_path target must be a Python executable")
        _validate_posix_directory_chain(target_path.parent)
        _validate_posix_file(target_path, "runtime_path target", executable=True)

    def constructor_fields(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "protocol_version": self.protocol_version,
            "build_version": self.build_version,
            "runtime_path": self.runtime_path,
            "launcher_path": self.launcher_path,
            "launcher_sha256": self.launcher_sha256,
            "supervisor": self.supervisor,
            "arguments": self.arguments,
            "runtime_target_path": self.runtime_target_path,
            "schema_version": self.schema_version,
            "owner": self.owner,
        }

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.constructor_fields()
        payload["supervisor"] = self.supervisor.value
        payload["arguments"] = list(self.arguments)
        return payload

    @property
    def generation(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def runtime_target_matches(self) -> bool:
        """Return whether the runtime still resolves to its bound target file."""

        try:
            return os.path.samefile(self.runtime_path, self.runtime_target_path)
        except (OSError, TypeError):
            return False

    def artifact_marker(self) -> dict[str, str | int]:
        return {
            "owner": self.owner,
            "service_id": self.service_id,
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "generation": self.generation,
        }

    def matches_artifact_marker(self, marker: dict[str, Any]) -> bool:
        return marker == self.artifact_marker()
