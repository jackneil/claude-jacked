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
# <membership.h>: mbr_uuid_to_id reports which kind of id a qualifier names.
_DARWIN_ID_TYPE_UID = 0
_DARWIN_ID_TYPE_GID = 1


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
    library.acl_get_qualifier.argtypes = [ctypes.c_void_p]
    library.acl_get_qualifier.restype = ctypes.c_void_p
    library.mbr_uuid_to_id.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_int),
    ]
    library.mbr_uuid_to_id.restype = ctypes.c_int
    return library


def _darwin_entry_is_trusted(library, entry) -> bool:
    """True when an ACL entry names a principal that is already trusted.

    An allow entry for the user, root, a root-equivalent group member, or a
    trusted group grants nothing those principals could not already do, so it
    is not a way in for anyone else. A qualifier that cannot be resolved is
    untrusted (fail closed).
    """
    qualifier = library.acl_get_qualifier(entry)
    if not qualifier:
        return False
    try:
        ident = ctypes.c_uint32()
        id_type = ctypes.c_int()
        if library.mbr_uuid_to_id(qualifier, ctypes.byref(ident), ctypes.byref(id_type)) != 0:
            return False
        if id_type.value == _DARWIN_ID_TYPE_UID:
            return ident.value in _trusted_owner_ids()
        if id_type.value == _DARWIN_ID_TYPE_GID:
            return ident.value in _trusted_group_ids()
        return False
    finally:
        library.acl_free(qualifier)


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
                and not _darwin_entry_is_trusted(library, entry)
            ):
                return True
            entry_id = -1
    finally:
        library.acl_free(acl)


def _darwin_secure_status(path: Path, *, directory: bool) -> os.stat_result:
    # O_NONBLOCK: a FIFO swapped into the path must not hang the open.
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_NONBLOCK
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


def _root_equivalent_group_names() -> tuple[str, ...]:
    """Groups whose members can already act as root on THIS host.

    Root-equivalence is a sudoers property, not a group-name property, so the
    list is scoped per platform to what the platform itself guarantees:

    - macOS ships ``%admin ALL=(ALL) ALL`` and Homebrew makes its prefix admin
      group-writable by design, so every Homebrew-installed Python lives under
      such a directory. ``wheel`` is root's group there.
    - Debian-family Linux ships ``%sudo``; RHEL, Fedora, SUSE and Amazon Linux
      ship ``%wheel``. Arch, Alpine, Void and Gentoo ship ``wheel`` with the
      sudoers grant commented out, so ``wheel`` is trusted only on the families
      that enable it. A Linux ``admin`` group is an ordinary group and is never
      trusted by name.
    """
    if _HOST_IS_DARWIN:
        return ("wheel", "admin")
    if sys.platform.startswith("linux"):
        families = _linux_family_ids()
        names = ["sudo"]
        if families & {"rhel", "fedora", "centos", "rocky", "almalinux", "amzn", "suse", "opensuse", "sles"}:
            names.append("wheel")
        return tuple(names)
    return ()


def _linux_family_ids() -> frozenset[str]:
    """``ID`` and ``ID_LIKE`` tokens from /etc/os-release, lowercase."""
    release = Path("/etc/os-release")
    try:
        status = release.stat()
        # A mis-permissioned container could let a local user rewrite the
        # family; fail closed (no wheel grant) unless root owns it read-only.
        if status.st_uid != 0 or status.st_mode & 0o022:
            return frozenset()
        text = release.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    tokens: set[str] = set()
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in {"ID", "ID_LIKE"}:
            tokens.update(value.strip().strip('"').lower().split())
    return frozenset(tokens)


def _user_private_group_id() -> int | None:
    """Return the caller's primary gid when it is a Linux user-private group.

    Linux creates one group per user: same name as the user, no other members.
    Writes granted to that group reach only the user, so it is as trusted as
    the user. The check is bounded on purpose: it never enumerates the user
    database, which can block for a directory-service timeout on hosts bound
    to LDAP or Active Directory. macOS never has such groups (``staff``).
    """
    if _HOST_IS_DARWIN:
        return None
    import grp
    import pwd

    uid = os.getuid()
    try:
        user = pwd.getpwuid(uid)
        group = grp.getgrgid(user.pw_gid)
    except KeyError:
        return None
    if group.gr_name != user.pw_name or group.gr_gid < 1000:
        return None
    if any(member != user.pw_name for member in group.gr_mem):
        return None
    return group.gr_gid


def _trusted_group_ids() -> frozenset[int]:
    """Groups a writable bit may grant without opening a privilege boundary.

    A directory writable only by principals who can already become root is
    not an escalation path: any of them could replace it with ``sudo``.
    Computed on every validation (three call sites per boot); a cache would
    only let a demoted administrator's files stay trusted for the tray's life.
    """
    import grp

    trusted = {0}
    for name in _root_equivalent_group_names():
        try:
            trusted.add(grp.getgrnam(name).gr_gid)
        except KeyError:
            continue
    private = _user_private_group_id()
    if private is not None:
        trusted.add(private)
    return frozenset(trusted)


def _trusted_owner_ids() -> frozenset[int]:
    """Root, the caller, and the listed members of root-equivalent groups.

    Only ``gr_mem`` is consulted. Users whose PRIMARY group is root-equivalent
    are not enumerated: that needs ``pwd.getpwall()``, which walks every
    directory-service record and can block a boot gate. Such a user is trusted
    through the group-writable rule instead when the path is group-writable.
    """
    import grp
    import pwd

    trusted = {0, os.getuid()}
    for name in _root_equivalent_group_names():
        try:
            group = grp.getgrnam(name)
        except KeyError:
            continue
        for member in group.gr_mem:
            try:
                trusted.add(pwd.getpwnam(member).pw_uid)
            except KeyError:
                continue
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


# Linux POSIX ACL entry tags and the write bit (linux/posix_acl_xattr.h).
_POSIX_ACL_XATTR = "system.posix_acl_access"
_ACL_USER = 0x02
_ACL_GROUP = 0x08
_ACL_MASK = 0x10
_ACL_WRITE = 0x02


_ACL_USER_OBJ = 0x01
_ACL_GROUP_OBJ = 0x04
_ACL_OTHER = 0x20
_ACL_KNOWN_TAGS = frozenset(
    {_ACL_USER_OBJ, _ACL_USER, _ACL_GROUP_OBJ, _ACL_GROUP, _ACL_MASK, _ACL_OTHER}
)


def _posix_acl_grants_untrusted_write(
    raw: bytes,
    owners: frozenset[int] | None = None,
    groups: frozenset[int] | None = None,
) -> bool:
    """True when a named entry of a Linux access ACL lets an untrusted user or
    group write. The mode bits cannot show this: with an ACL mask, the group
    bits are the MASK, so a grant to another user can sit behind a trusted
    owning group (#185i fixed the same gap on macOS).

    ``raw`` is the ``system.posix_acl_access`` value: a little-endian 4-byte
    version (2), then 8-byte entries (tag u16, perm u16, id u32). A named
    entry's effective permissions are its own AND the mask's. A value the
    kernel would not store (another version, no entries, an unknown tag, two
    masks, or named entries with no mask) is untrusted. ``owners`` and
    ``groups`` default to the trusted sets of this host.

    >>> import struct
    >>> def acl(*entries, version=2): return struct.pack("<I", version) + b"".join(struct.pack("<HHI", *e) for e in entries)
    >>> base = [(0x01, 7, 0xFFFFFFFF), (0x04, 5, 0xFFFFFFFF), (0x20, 5, 0xFFFFFFFF)]
    >>> me = frozenset({0, 1000})
    >>> _posix_acl_grants_untrusted_write(acl(*base), me, me)
    False
    >>> _posix_acl_grants_untrusted_write(acl(*base, (0x02, 7, 4242), (0x10, 7, 0xFFFFFFFF)), me, me)
    True
    >>> _posix_acl_grants_untrusted_write(acl(*base, (0x02, 7, 4242), (0x10, 5, 0xFFFFFFFF)), me, me)
    False
    >>> _posix_acl_grants_untrusted_write(acl(*base, (0x02, 7, 1000), (0x10, 7, 0xFFFFFFFF)), me, me)
    False
    >>> _posix_acl_grants_untrusted_write(b"bad", me, me)
    True
    >>> _posix_acl_grants_untrusted_write(acl(), me, me)
    True
    >>> _posix_acl_grants_untrusted_write(acl(*base, version=1), me, me)
    True
    >>> _posix_acl_grants_untrusted_write(acl(*base, (0x40, 0, 0)), me, me)
    True
    >>> _posix_acl_grants_untrusted_write(acl(*base, (0x02, 5, 4242)), me, me)
    True
    >>> _posix_acl_grants_untrusted_write(acl(*base, (0x10, 5, 0), (0x10, 5, 0)), me, me)
    True
    """
    import struct

    if len(raw) < 12 or (len(raw) - 4) % 8:
        return True
    if struct.unpack_from("<I", raw, 0)[0] != 2:
        return True
    entries = [struct.unpack_from("<HHI", raw, offset) for offset in range(4, len(raw), 8)]
    tags = [tag for tag, _, _ in entries]
    if any(tag not in _ACL_KNOWN_TAGS for tag in tags) or tags.count(_ACL_MASK) > 1:
        return True
    named = any(tag in (_ACL_USER, _ACL_GROUP) for tag in tags)
    if named and _ACL_MASK not in tags:
        return True
    mask = next((perm for tag, perm, _ in entries if tag == _ACL_MASK), 0o7)
    for tag, perm, principal in entries:
        if tag not in (_ACL_USER, _ACL_GROUP) or not (perm & mask & _ACL_WRITE):
            continue
        if tag == _ACL_USER:
            owners = _trusted_owner_ids() if owners is None else owners
            if principal not in owners:
                return True
        else:
            groups = _trusted_group_ids() if groups is None else groups
            if principal not in groups:
                return True
    return False


def _validate_linux_acl(path: Path) -> None:
    """Refuse a Linux path whose access ACL lets an untrusted principal write.

    No ACL (ENODATA) or a filesystem without them (ENOTSUP) passes; any other
    read failure fails closed. Off Linux this is a no-op (macOS ACLs are read
    by ``_darwin_secure_status``). Symlinks carry no access ACL on Linux.
    """
    getxattr = getattr(os, "getxattr", None)
    if not sys.platform.startswith("linux") or getxattr is None:
        return
    try:
        raw = getxattr(os.fspath(path), _POSIX_ACL_XATTR, follow_symlinks=False)
    except OSError as exc:
        if exc.errno in (errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP):
            return
        raise ValueError("runtime_path ACL could not be inspected") from exc
    if _posix_acl_grants_untrusted_write(raw):
        raise ValueError("runtime_path has a mutating POSIX ACL")


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
        _validate_linux_acl(current)
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
    _validate_linux_acl(path)
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
