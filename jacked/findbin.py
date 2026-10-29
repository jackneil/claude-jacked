"""Robust binary lookup with fallback to known install locations.

shutil.which() only searches PATH, and PATH is often incomplete:

- the background service (launchd, systemd, Task Scheduler) runs with a
  short, system-only PATH on purpose (``jacked.service.environment``), so a
  CLI that the user installed under the home directory (``~/.local/bin/codex``,
  ``~/.local/bin/claude``) is invisible to it;
- the dashboard can run in a shell (Git Bash on Windows) that does not
  inherit paths from another shell (PowerShell).

Every lookup of a user-installed CLI (codex, claude, gh, pipx, uv, ...) from
code the service can reach goes through ``find_bin``. A guard test
(``tests/unit/test_findbin_guard.py``) fails on a new bare ``shutil.which``
for such a name.

Trust model: PATH hits are returned as-is, because PATH is the caller's own
environment. The off-PATH fallback directories are user-writable, so a
fallback candidate is returned only when ``_resolve_trusted`` can resolve it
one component at a time with nothing on the way that anyone but the current
user or root could change: every real directory, every symlink (intermediate
hops included), and the final file pass the owner and write-bit rule that
``jacked.service.spec`` applies to the service's own interpreter, and on macOS
the same extended-ACL check. A root-owned sticky directory such as ``/tmp`` is
accepted only as the parent of the user's own real directory (a container's
``HOME=/tmp/...``); a binary or symlink directly inside it is refused. The
service runs as that user, so executing a binary that only that user can
replace crosses no privilege boundary.

Usage:
    from jacked.findbin import find_bin
    uv = find_bin("uv")  # returns full path or None
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import sys
from pathlib import Path

logger = logging.getLogger(__name__)


def _home_dir() -> str:
    """Return the user's home directory. Extracted for testability."""
    return str(Path.home())


def _current_uid() -> int | None:
    """The caller's uid, or None where the platform has no uids (Windows)."""
    getuid = getattr(os, "getuid", None)
    return getuid() if getuid is not None else None


def _trusted_group_ids() -> frozenset[int]:
    """Groups whose write bit grants nothing beyond root (see service.spec)."""
    from jacked.service.spec import _trusted_group_ids as spec_trusted_groups

    return spec_trusted_groups()


def _is_bare_name(name: str) -> bool:
    """True for a command name; False for a path, an empty name, or a NUL.

    >>> _is_bare_name("codex"), _is_bare_name("../codex"), _is_bare_name("")
    (True, False, False)
    """
    if not name or "\x00" in name:
        return False
    return "/" not in name and "\\" not in name


def _untrusted_reason(status: os.stat_result, uid: int | None) -> str | None:
    """Why a path could be changed by someone other than the user, or None."""
    if status.st_uid not in (uid, 0):
        return "not owned by the current user"
    if status.st_mode & 0o002:
        return "world-writable"
    if status.st_mode & 0o020 and status.st_gid not in _trusted_group_ids():
        return "group-writable"
    return None


def _fallback_refusal(path: str) -> str | None:
    """Return why an off-PATH candidate is refused, or None when it is safe.

    The candidate is resolved one component at a time by
    ``_resolve_trusted``, so every directory, every symlink and the final
    file on the way are checked, intermediate symlink hops included (#186i).
    Windows has no POSIX permission bits; its fallback directories are
    per-user profile folders.
    """
    if os.name != "posix":
        return None
    _real, reason = _resolve_trusted(path)
    return reason


def trusted_path_refusal(path: str) -> str | None:
    """Why the absolute file ``path`` must not be executed, or None.

    The off-PATH fallback rule (``_resolve_trusted``) for callers that probe
    their own fixed locations, such as the Linux browser launcher's Snap and
    Flatpak paths. The reason never contains a path. Windows: always None.
    """
    return _fallback_refusal(path)


_MAX_SYMLINK_HOPS = 40  # Linux's ELOOP limit
_HOST_IS_DARWIN = sys.platform == "darwin"


def _secure_lstat(path: str, kind: str) -> os.stat_result:
    """``lstat`` of ``path``; on macOS also refuse a mutating extended ACL.

    ``kind`` is ``"dir"``, ``"file"`` or ``"link"``. On macOS a directory or
    file is opened with ``O_NOFOLLOW`` and its ACL read from the descriptor
    (``jacked.service.spec._darwin_secure_status``, the rule the service spec
    applies to its own interpreter); a link's ACL is read with
    ``acl_get_link_np``. A directory or file the user may traverse but not
    read (mode 0711, 0311) cannot be opened; its ACL is then read by path
    without following links, which needs only search permission. Raises
    ``OSError`` or ``ValueError``.
    """
    if not _HOST_IS_DARWIN:
        status = os.lstat(path)
        if kind != "link":
            # Linux POSIX ACLs: the same trust rule the service spec applies
            # to its interpreter (a named grant hidden behind the mask).
            from jacked.service import spec

            spec._validate_linux_acl(Path(path))
        return status
    from jacked.service import spec

    if kind != "link":
        try:
            return spec._darwin_secure_status(Path(path), directory=kind == "dir")
        except ValueError as exc:
            if not isinstance(exc.__cause__, PermissionError):
                raise
    status = os.lstat(path)
    spec._validate_darwin_link_acl(Path(path))
    return status


def _shared_sticky(status: os.stat_result) -> bool:
    """A root-owned, sticky, world-writable directory such as ``/tmp``.

    Others may create entries in it but may not rename or delete an entry
    they do not own, so a real directory owned by the user (or root) inside
    it cannot be swapped out from under the user.
    """
    return (
        status.st_uid == 0
        and bool(status.st_mode & stat.S_ISVTX)
        and bool(status.st_mode & 0o002)
    )


def _resolve_trusted(path: str) -> tuple[str | None, str | None]:
    """Resolve ``path`` one component at a time and check every step.

    A checked re-implementation of ``posixpath._joinrealpath``. Returns
    ``(real_path, None)`` when nobody but the current user or root can change
    any directory, symlink or file on the way, else ``(None, reason)``. The
    reason never contains a path (a path carries the user's home directory).

    Rules, in resolution order:

    * a symlink must be owned by the user or root; its target is resolved
      relative to the directory that holds it, with at most 40 hops;
    * a real directory must be owned by the user or root and must not be
      writable by anyone else, EXCEPT a root-owned sticky directory such as
      ``/tmp``, which is accepted only as the parent of a real directory owned
      by the user or root (a symlink or a file inside it is refused, so the
      directory that holds the binary always gets the strict rule);
    * the last component must be a regular file with the same owner and
      write-bit rule;
    * every step is read once with ``lstat`` and once through a descriptor
      opened without following links (macOS), and the two must be the same
      file, so a swap between the reads is refused.
    """
    uid = _current_uid()
    if not os.path.isabs(path) or os.path.normpath(path) != path:
        return None, "candidate path is not normalized"
    try:
        root = _secure_lstat(os.sep, "dir")
    except (OSError, ValueError):
        return None, "the root directory cannot be inspected"
    if _untrusted_reason(root, uid):
        return None, "the root directory is writable by others"

    # The resolved chain so far: (directory, is a shared sticky directory,
    # its device).
    chain: list[tuple[str, bool, int]] = [(os.sep, False, root.st_dev)]
    pending = [part for part in path.split(os.sep) if part]
    pending.reverse()  # a stack: the next component is pending[-1]
    hops = 0
    while pending:
        part = pending.pop()
        if part == ".":
            continue
        if part == "..":
            if len(chain) > 1:
                chain.pop()
            continue
        holder, holder_shared, holder_dev = chain[-1]
        entry = os.path.join(holder, part)
        try:
            first = os.lstat(entry)
        except OSError:
            return None, "a path component cannot be inspected"

        if stat.S_ISLNK(first.st_mode):
            if holder_shared:
                return None, "a symlink sits in a shared sticky directory"
            if first.st_uid not in (uid, 0):
                return None, "a symlink is not owned by the current user"
            try:
                _secure_lstat(entry, "link")
                target = os.readlink(entry)
            except (OSError, ValueError) as exc:
                return None, _acl_or(exc, "a symlink cannot be read")
            if not target:
                return None, "a symlink has an empty target"
            hops += 1
            if hops > _MAX_SYMLINK_HOPS:
                return None, "too many symlink hops"
            if os.path.isabs(target):
                del chain[1:]  # back to the root, already checked
            pending.extend(reversed([p for p in target.split(os.sep) if p]))
            continue

        is_dir = stat.S_ISDIR(first.st_mode)
        # Decide everything the lstat can decide BEFORE opening the entry: an
        # open of a FIFO swapped into the path would block.
        if not is_dir:
            if not stat.S_ISREG(first.st_mode):
                return None, "a path component is not a regular file or directory"
            if pending:
                return None, "a path component is not a directory"
            if holder_shared:
                return None, "the binary sits in a shared sticky directory"
        try:
            status = _secure_lstat(entry, "dir" if is_dir else "file")
        except (OSError, ValueError) as exc:
            return None, _acl_or(exc, "a path component cannot be opened without following links")
        if (status.st_dev, status.st_ino) != (first.st_dev, first.st_ino):
            return None, "a path component changed while it was being checked"

        if is_dir:
            if status.st_uid not in (uid, 0):
                return None, "a directory is not owned by the current user"
            if holder_shared and status.st_dev != holder_dev:
                # A mount point planted in /tmp (a user FUSE mount can report
                # any owner): only a plain directory on /tmp's own filesystem
                # is pinned by the sticky bit.
                return None, "a directory in a shared sticky directory is a mount point"
            shared = _shared_sticky(status)
            reason = _untrusted_reason(status, uid)
            if reason and not shared:
                return None, f"a directory is {reason}"
            chain.append((entry, shared, status.st_dev))
            continue

        if not stat.S_ISREG(status.st_mode):
            return None, "binary is not a regular file"
        reason = _untrusted_reason(status, uid)
        if reason:
            return None, f"binary is {reason}"
        return entry, None
    return None, "candidate is a directory"


def _acl_or(exc: BaseException, fallback: str) -> str:
    """The ACL verdict in ``exc`` (macOS or Linux) as a refusal reason, else
    ``fallback``.

    >>> _acl_or(ValueError("runtime_path has a mutating extended ACL"), "x")
    'a path component has a mutating ACL'
    >>> _acl_or(ValueError("runtime_path has a mutating POSIX ACL"), "x")
    'a path component has a mutating ACL'
    >>> _acl_or(ValueError("runtime_path ACL could not be inspected"), "x")
    "a path component's ACL cannot be inspected"
    >>> _acl_or(OSError("boom"), "x")
    'x'
    """
    text = str(exc)
    if "ACL" in text and "mutating" in text:
        return "a path component has a mutating ACL"
    if "ACL" in text and "could not be inspected" in text:
        return "a path component's ACL cannot be inspected"
    return fallback


def _clean_dir(directory: str | None, label: str) -> str | None:
    """``directory`` normalized, or None when it is unset or unsafe to join.

    A fallback directory must be absolute and free of ``..``: after a symlink
    a lexical ``..`` names a different directory than the physical one, so
    ``normpath`` cannot fold it safely. Duplicate and trailing separators are
    folded. ``label`` names the source in the (values-free) log.

    >>> _clean_dir("/home/u//.local/", "x") == os.path.normpath("/home/u/.local")
    True
    >>> _clean_dir("/home/u/../v", "x") is None, _clean_dir("rel/dir", "x") is None
    (True, True)
    """
    if not directory:
        return None
    parts = directory.replace("\\", "/").split("/")
    if not os.path.isabs(directory) or ".." in parts:
        logger.warning("find_bin: ignored %s: not an absolute path without '..'", label)
        return None
    return os.path.normpath(directory)


def find_bin(name: str, *, home: str | None = None) -> str | None:
    """Find a binary by name, searching PATH then known install locations.

    ``name`` must be a bare command name; a path-shaped name returns None.
    ``home`` overrides the home directory used for the fallback locations
    (tests use it so they never look at the real user's home).
    """
    if not _is_bare_name(name):
        return None
    found = shutil.which(name)
    if found:
        return found

    is_win = sys.platform == "win32"
    suffix = ".exe" if is_win and not name.endswith(".exe") else ""
    target = f"{name}{suffix}"
    home = _clean_dir(home if home is not None else _home_dir(), "the home directory")

    candidates: list[str] = []

    uv_tool_bin = _clean_dir(os.environ.get("UV_TOOL_BIN_DIR"), "UV_TOOL_BIN_DIR")
    if uv_tool_bin:
        candidates.append(os.path.join(uv_tool_bin, target))

    xdg_bin = _clean_dir(os.environ.get("XDG_BIN_HOME"), "XDG_BIN_HOME")
    if xdg_bin:
        candidates.append(os.path.join(xdg_bin, target))

    if home:
        # All platforms: uv's default tool bin dir. The standalone Claude Code
        # and Codex installers put their launchers here too.
        candidates.append(os.path.join(home, ".local", "bin", target))
        # All platforms: cargo bin (uv can be installed via cargo)
        candidates.append(os.path.join(home, ".cargo", "bin", target))

    if is_win:
        local_app = _clean_dir(os.environ.get("LOCALAPPDATA"), "LOCALAPPDATA")
        if local_app:
            candidates.append(os.path.join(local_app, "uv", "bin", target))
            candidates.append(os.path.join(local_app, "Programs", "claude", target))

    for path in candidates:
        if not os.path.isfile(path):
            continue
        # On Windows, os.access(path, os.X_OK) is unreliable — it can
        # return False for valid .exe files depending on UAC, security
        # policies, or cloud/network drives.  If the file exists and has
        # an executable extension, that's sufficient.
        if not (is_win or os.access(path, os.X_OK)):
            continue
        refusal = _fallback_refusal(path)
        if refusal:
            # Values-free: the name is a fixed command name; the path would
            # carry the user's home directory, so it is not logged.
            logger.warning(
                "find_bin: refused the off-PATH %s candidate: %s", name, refusal
            )
            continue
        return path

    return None
