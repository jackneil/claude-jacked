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
fallback candidate is returned only when the candidate (a symlink included),
its resolved target file, and EVERY ancestor directory of both can be changed
by nobody but the current user or root (the same owner and write-bit rule
that ``jacked.service.spec`` applies to the service's own interpreter). A
root-owned sticky directory such as ``/tmp`` is accepted only as a PARENT of a
real directory (not a symlink) owned by the user or root: the sticky bit
stops other users from swapping an entry they do not own. Components are read
with ``lstat``, so a symlink is judged by its own owner. The candidate's own
directory never gets the exemption, and a path with ``..`` is refused. The service runs as that
user, so executing a binary that only that user can replace crosses no
privilege boundary.

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


def _sticky_root_dir(status: os.stat_result) -> bool:
    """True for a root-owned directory with the sticky bit (``/tmp``)."""
    return (
        stat.S_ISDIR(status.st_mode)
        and bool(status.st_mode & stat.S_ISVTX)
        and status.st_uid == 0
    )


def _fallback_refusal(path: str) -> str | None:
    """Return why an off-PATH candidate is refused, or None when it is safe.

    Checks the directory that holds the candidate, the directory that holds
    its resolved target (``~/.local/bin/codex`` is a symlink into
    ``~/.codex/packages``), and the target file. Windows has no POSIX
    permission bits; its fallback directories are per-user profile folders.
    """
    if os.name != "posix":
        return None
    # The walk below reads path components lexically; the kernel resolves
    # "a/../b" THROUGH "a". A path with "..", "." or doubled separators
    # would be checked as one path and executed as another.
    if os.path.normpath(path) != path:
        return "candidate path is not normalized"
    uid = _current_uid()
    real = os.path.realpath(path)
    # The link itself: a replaced symlink redirects the lookup even when
    # every directory and the target are sound.
    try:
        link_status = os.lstat(path)
    except OSError:
        return "candidate cannot be inspected"
    if stat.S_ISLNK(link_status.st_mode):
        # A symlink's permission bits are ignored by the kernel (Linux
        # reports 0777); who may replace it is decided by its directory,
        # checked below. Its OWNER still matters: a link planted by another
        # user is refused even inside a sound directory.
        if link_status.st_uid not in (uid, 0):
            return "symlink is not owned by the current user"
    try:
        status = os.stat(real)
    except OSError:
        return "binary cannot be inspected"
    if not stat.S_ISREG(status.st_mode):
        return "binary is not a regular file"
    reason = _untrusted_reason(status, uid)
    if reason:
        return f"binary is {reason}"
    # Every ancestor of the candidate and of its target (same rule as
    # jacked.service.spec): a writable ancestor lets someone swap a whole
    # directory for a symlink. A root-owned sticky parent (/tmp) is exempt
    # only above a real directory the user or root owns (see
    # _untrusted_ancestor).
    for label, start in (("directory", os.path.dirname(path)),
                         ("target directory", os.path.dirname(real))):
        reason = _untrusted_ancestor(start, uid)
        if reason:
            return f"{label} {reason}"
    return None


def _untrusted_ancestor(directory: str, uid: int | None) -> str | None:
    """Why ``directory`` or one of its parents is not trusted, or None.

    Walks from ``directory`` up to the filesystem root with ``lstat``, so a
    symlink component is judged as the link it is, not as its target: a
    link must be owned by the user or root (its mode bits mean nothing). A
    real directory must pass the strict owner and write-bit rule, except
    that a root-owned sticky PARENT such as ``/tmp`` is accepted, because
    the sticky bit stops other users from renaming or removing an entry
    they do not own. That holds only for an entry that is a real directory
    owned by the user or root, so every entry whose REAL containing
    directory is a sticky world- or group-writable one must be exactly
    that: a link there is refused even when its own lstat looks sound
    (macOS reaches ``/private/tmp`` through the root-owned ``/tmp`` link).
    ``directory`` itself never gets the exemption.
    """
    seen: set[str] = set()
    start = os.path.abspath(directory)
    current = start
    while current not in seen:
        seen.add(current)
        try:
            status = os.lstat(current)
        except OSError:
            return "cannot be inspected"
        label = "is" if current == start else "ancestor is"
        if stat.S_ISLNK(status.st_mode):
            # Whoever owns a link can repoint it; its permission bits are
            # ignored by the kernel.
            if status.st_uid not in (uid, 0):
                return f"{label} a symlink not owned by the current user"
        else:
            reason = _untrusted_reason(status, uid)
            if reason and current != start and _sticky_root_dir(status):
                reason = None  # the entry below it is held to the rule below
            if reason:
                return f"{label} {reason}"
        parent = os.path.dirname(current)
        if parent == current:
            break
        try:
            container = os.stat(os.path.realpath(parent))
        except OSError:
            return "cannot be inspected"
        if _sticky_root_dir(container) and container.st_mode & 0o022:
            owned_dir = (
                stat.S_ISDIR(status.st_mode)
                and not stat.S_ISLNK(status.st_mode)
                and status.st_uid in (uid, 0)
            )
            if not owned_dir:
                return f"{label} not a directory owned by the current user below a shared sticky directory"
        current = parent
    return None


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
    home = home if home is not None else _home_dir()

    candidates: list[str] = []

    uv_tool_bin = os.environ.get("UV_TOOL_BIN_DIR")
    if uv_tool_bin:
        candidates.append(os.path.join(uv_tool_bin, target))

    xdg_bin = os.environ.get("XDG_BIN_HOME")
    if xdg_bin:
        candidates.append(os.path.join(xdg_bin, target))

    # All platforms: uv's default tool bin dir. The standalone Claude Code and
    # Codex installers put their launchers here too.
    candidates.append(os.path.join(home, ".local", "bin", target))

    # All platforms: cargo bin (uv can be installed via cargo)
    candidates.append(os.path.join(home, ".cargo", "bin", target))

    if is_win:
        local_app = os.environ.get("LOCALAPPDATA", "")
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
