"""The glob expansion of ``--junit`` and ``--input``: one rule for both options.

A relative glob starts at the top of the work tree. ``**`` matches folders
at all depths, and a hidden file matches. ``input_entries`` adds every
regular file below a matched folder, and it never follows a symbolic link.
``inputs_fingerprint`` hashes those files for a record.
"""

from __future__ import annotations

import fnmatch
import glob
import hashlib
import json
import os
import re
import stat
from collections.abc import Sequence
from pathlib import Path


_MAGIC = re.compile(r"[*?[]")
_SEPARATORS = re.compile(r"[\\/]" if os.name == "nt" else r"/")


def matches(top: Path, pattern: str) -> list[str]:
    """Return the names that one glob matches, sorted. A relative name is relative to ``top``.

    The names are the names of ``glob.glob(pattern, root_dir=top,
    recursive=True, include_hidden=True)``, but ``**`` never goes into a
    symbolic link to a folder: two links to ``.`` would make that walk
    endless. There is no limit on the count or the depth.
    """
    if "**" not in pattern:
        return sorted(glob.glob(pattern, root_dir=top, include_hidden=True))
    parts = [part for part in _SEPARATORS.split(pattern) if part]
    absolute = os.path.isabs(pattern)
    fixed = []
    while parts and not _MAGIC.search(parts[0]):
        fixed.append(parts.pop(0))
    prefix = os.path.join(*fixed) if fixed else ""
    if absolute:
        anchor = pattern[: len(pattern) - len(pattern.lstrip("\\/"))] or os.sep
        prefix = os.path.join(os.path.splitdrive(pattern)[0] + anchor, prefix)
    base = prefix if absolute else os.path.join(top, prefix) if prefix else str(top)
    dirs_only = bool(_SEPARATORS.match(pattern[-1:]))
    found = [os.path.join(prefix, rel) for rel in _walk_matches(base, parts, dirs_only)]
    if prefix and os.path.isdir(base) and all(part == "**" for part in parts):
        found.append(os.path.join(prefix, ""))  # glob gives "dir/" for "dir/**"
    return sorted(found)


def _walk_matches(base: str, parts: list[str], dirs_only: bool) -> list[str]:
    """The paths below ``base`` (relative to it) that the glob components ``parts`` match.

    With ``dirs_only`` (a pattern that ends with a separator), only folders
    match, and each name ends with a separator, as glob gives them.
    """
    if not os.path.isdir(base):
        return []
    found: list[str] = []
    for here, dirs, files in os.walk(base, followlinks=False):
        rel_here = os.path.relpath(here, base)
        for name in dirs if dirs_only else dirs + files:
            rel = name if rel_here == "." else os.path.join(rel_here, name)
            if _match_parts(_SEPARATORS.split(rel), parts):
                found.append(os.path.join(rel, "") if dirs_only else rel)
    return found


def _match_parts(path: list[str], parts: list[str]) -> bool:
    """Match path components with glob components. ``**`` matches zero or more components."""
    if not parts:
        return not path
    if parts[0] == "**":
        return any(_match_parts(path[k:], parts[1:]) for k in range(len(path) + 1))
    return bool(path) and fnmatch.fnmatch(path[0], parts[0]) and _match_parts(path[1:], parts[1:])


def full_path(top: Path, name: str) -> Path:
    """The path of a name that ``matches`` gave."""
    return Path(name) if os.path.isabs(name) else Path(top) / name


def _entry(name: str, info: os.stat_result) -> list:
    return [Path(name).as_posix(), info.st_size, info.st_mtime_ns]


def _folder_files(folder: Path, name: str) -> list[list]:
    """Each regular file below ``folder``, at all depths, with no link followed."""
    found: list[list] = []
    for here, dirs, files in os.walk(folder, followlinks=False):
        rel = os.path.relpath(here, folder)
        for file in files:
            try:
                info = os.lstat(os.path.join(here, file))
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append(_entry(os.path.join(name, rel, file) if rel != "." else
                                    os.path.join(name, file), info))
    return found


def input_entries(top: Path, patterns: Sequence[str]) -> list[list]:
    """Return ``[pattern, files]`` for each glob, where each file is ``[path, size, mtime_ns]``.

    A matched regular file gives itself. A matched folder (not a link to a
    folder) gives every regular file below it. Each list of files is sorted.
    """
    entries: list[list] = []
    for pattern in patterns:
        found: list[list] = []
        for name in matches(top, pattern):
            path = full_path(top, name)
            try:
                info = os.stat(path)
            except OSError:
                continue
            if stat.S_ISREG(info.st_mode):
                found.append(_entry(name, info))
            elif stat.S_ISDIR(info.st_mode) and not os.path.islink(path):
                found += _folder_files(path, name)
        entries.append([pattern, sorted(found)])
    return entries


def inputs_fingerprint(cwd: Path, patterns: Sequence[str]) -> str | None:
    """Return a SHA-256 of the files that ``patterns`` match, or None when no glob is given.

    A relative glob starts at ``cwd``. ``**`` matches folders at all depths,
    and a hidden file matches. A matched folder adds every regular file
    below it, with no link followed. Each file gives its path, its size and
    its modification time in nanoseconds, not its content. A glob that
    matches no file is in the hash too.
    """
    return input_fingerprint(cwd, patterns)[0]


def input_fingerprint(cwd: Path, patterns: Sequence[str]) -> tuple[str | None, list[str]]:
    """Return ``inputs_fingerprint`` and the globs that match no regular file."""
    if not patterns:
        return None, []
    entries = input_entries(Path(cwd), patterns)
    text = json.dumps(entries, ensure_ascii=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest(), [p for p, files in entries if not files]
