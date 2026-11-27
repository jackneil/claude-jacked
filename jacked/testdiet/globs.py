"""The glob expansion of ``--junit`` and ``--input``: one rule for both options.

A relative glob starts at the top of the work tree. ``**`` matches folders
at all depths, and a hidden file matches. ``input_entries`` adds every
regular file below a matched folder, and it never follows a symbolic link.
``inputs_fingerprint`` hashes those files for a record.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import stat
from collections.abc import Sequence
from pathlib import Path


def matches(top: Path, pattern: str) -> list[str]:
    """Return the names that one glob matches, sorted. A relative name is relative to ``top``."""
    return sorted(glob.glob(pattern, root_dir=top, recursive=True, include_hidden=True))


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
