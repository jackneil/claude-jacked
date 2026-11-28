"""Remove the files of the retired settings integrity check (#99i).

jacked 0.111.0 to 0.111.5 kept a baseline of settings.json in
``jacked/integrity.json`` beside the settings file, with the lock file
``jacked/integrity.json.lock``. The MEMORY.md quarantine of the same versions
kept one lock file for each memory folder that it checked, in
``jacked/locks/quarantine-<project>.lock``. jacked does not use
these files now. ``jacked install`` (an upgrade runs it) and
``jacked uninstall`` call ``report_removal`` to remove them.

The rules:

* Remove only the two names in ``LEFTOVER_NAMES`` and the lock files whose
  names start with ``quarantine-`` and end with ``.lock``. Remove a file only
  when ``os.lstat`` shows a regular file. jacked does not follow a link, and it
  does not remove a folder that has the name of a file.
* Read a ``jacked`` or ``locks`` folder only when ``os.lstat`` shows a real
  folder: not a symlink, and not a Windows junction or other reparse point
  (``regular_file.is_reparse_point``). A linked folder is left alone, and
  nothing in it is removed.
* Look in ``<home>/.claude/jacked`` and in ``<config folder>/jacked``
  (``claude_paths.config_dir``, so ``CLAUDE_CONFIG_DIR`` applies), with the
  same rules for each. The old writer put the baseline beside each settings
  file that it wrote, so both folders can hold one. Two paths that resolve to
  the same real folder count as one folder. A link or a junction never takes
  the place of the real folder that it points at: it is skipped, and the real
  folder is cleaned through its own path.
* Look for the lock files in the ``locks`` folder of each of these folders.
  Remove ``locks`` when it is empty after that: nothing else uses it.
* Remove a ``jacked`` folder only when this cleanup removed something from it
  and the folder is then empty. A user file or ``issue-queue`` in the folder
  keeps it.
* A missing file or folder gives no line. Each removed baseline file gives one
  line, and the lock files give one count line. An error gives one warning
  line and does not stop the install or the uninstall. A folder that cannot be
  read (any error but a missing folder) gives one warning line too.
"""
from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jacked.memory import claude_paths
from jacked.regular_file import is_reparse_point

logger = logging.getLogger(__name__)

#: The files that the retired check left in each ``jacked`` folder.
LEFTOVER_NAMES = ("integrity.json", "integrity.json.lock")
#: The name pattern of the lock files that the retired quarantine left.
LOCK_PREFIX = "quarantine-"
LOCK_SUFFIX = ".lock"


@dataclass
class Removal:
    """What ``remove_leftovers`` did. Each failure is ``(path, error class name)``.

    ``locks_folders`` holds each ``locks`` folder that gave up at least one
    lock file. ``unchecked`` holds each ``jacked`` or ``locks`` folder that
    could not be read, so nothing in it was removed.
    """

    files: list[Path] = field(default_factory=list)
    failed: list[tuple[Path, str]] = field(default_factory=list)
    locks_folders: list[Path] = field(default_factory=list)
    locks_removed: int = 0
    locks_failed: list[tuple[Path, str]] = field(default_factory=list)
    unchecked: list[tuple[Path, str]] = field(default_factory=list)


def _is_link(path: Path) -> bool:
    """True when ``os.lstat`` shows a symlink or a Windows reparse point (a junction).

    A missing path or an error returns False. ``remove_leftovers`` inspects
    the folder again and records an error itself.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(info.st_mode) or is_reparse_point(info)


def leftover_folders(home: Path | str, env: Mapping[str, str] | None = None) -> list[Path]:
    """Return each folder that can hold the two files.

    A real folder appears once, even when two paths resolve to it. A link or
    a junction claims no place: it stays in the list (``remove_leftovers``
    skips it with an INFO line), and the real folder that it points at keeps
    its own place, so that folder is cleaned once.

    >>> [p.as_posix() for p in leftover_folders("/h", {})]
    ['/h/.claude/jacked']
    >>> [p.as_posix() for p in leftover_folders("/h", {"CLAUDE_CONFIG_DIR": "/c"})]
    ['/h/.claude/jacked', '/c/jacked']
    >>> [p.as_posix() for p in leftover_folders("/h", {"CLAUDE_CONFIG_DIR": "/h/.claude"})]
    ['/h/.claude/jacked']
    """
    folders: list[Path] = []
    seen: set[str] = set()
    for base in (Path(home) / ".claude", claude_paths.config_dir(home, env)):
        folder = base / "jacked"
        if folder in folders:
            continue
        if _is_link(folder):
            folders.append(folder)
            continue
        key = os.path.normcase(os.path.realpath(folder))
        if key not in seen:
            seen.add(key)
            folders.append(folder)
    return folders


def is_lock_name(name: str) -> bool:
    """True for the name of a lock file of the retired quarantine.

    >>> is_lock_name("quarantine--Users-me-repo.lock"), is_lock_name("quarantine-.txt"), is_lock_name("other.lock")
    (True, False, False)
    """
    return name.startswith(LOCK_PREFIX) and name.endswith(LOCK_SUFFIX) and len(name) > len(LOCK_PREFIX + LOCK_SUFFIX)


def _remove_file(path: Path) -> bool:
    """Remove ``path`` when it is a regular file. Return True when it was removed.

    A missing path returns False. A link, a folder or another file type stays,
    and the log records it. Every other ``OSError`` goes to the caller.
    """
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(mode):
        logger.info("retired integrity: %s is not a regular file, so it stays", path)
        return False
    try:
        os.unlink(path)
    except FileNotFoundError:
        return False
    return True


def _is_real_folder(path: Path) -> bool:
    """True when ``os.lstat`` shows a folder that is not a link or a junction.

    On Windows, ``lstat`` reports a directory junction as a folder
    (``S_IFDIR``), so the reparse-point attribute decides. A missing path
    returns False. Every other ``OSError`` goes to the caller.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISDIR(info.st_mode) and not is_reparse_point(info)


def _check_folder(path: Path, result: Removal) -> bool:
    """True when ``path`` is a real folder that the cleanup can read.

    A missing path returns False with no record. A link, a junction or another
    file type returns False, and the log records it. An error records the
    folder in ``result.unchecked``.
    """
    try:
        if _is_real_folder(path):
            return True
    except OSError as exc:
        logger.warning("retired integrity: could not read %s (%s)", path, type(exc).__name__)
        result.unchecked.append((path, type(exc).__name__))
        return False
    if os.path.lexists(path):
        logger.info("retired integrity: %s is a link, a junction or not a folder, so it stays", path)
    return False


def _remove_empty_folder(folder: Path) -> bool:
    """Remove ``folder`` when it is a real folder and empty. Return True when it went."""
    try:
        if not _is_real_folder(folder):
            return False
        os.rmdir(folder)
    except OSError as exc:  # not empty, gone or not removable: the folder stays
        logger.debug("retired integrity: %s stays (%s)", folder, type(exc).__name__)
        return False
    return True


def _remove_locks(locks: Path, result: Removal) -> bool:
    """Remove the quarantine lock files in ``locks``, then ``locks`` when it is empty.

    Return True when this removed anything. A linked or junctioned ``locks``
    folder is not read, so nothing outside the jacked folder is removed. A
    ``locks`` folder that another install removed first is not an error.
    """
    if not _check_folder(locks, result):
        return False
    before = result.locks_removed
    try:
        names = sorted(entry.name for entry in os.scandir(locks))
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("retired integrity: could not read %s (%s)", locks, type(exc).__name__)
        result.unchecked.append((locks, type(exc).__name__))
        return False
    for name in names:
        if not is_lock_name(name):
            continue
        try:
            if _remove_file(locks / name):
                result.locks_removed += 1
        except OSError as exc:
            result.locks_failed.append((locks / name, type(exc).__name__))
    if result.locks_removed > before:
        result.locks_folders.append(locks)
    return _remove_empty_folder(locks) or result.locks_removed > before


def remove_leftovers(home: Path | str, env: Mapping[str, str] | None = None) -> Removal:
    """Remove the files of the retired check and return what was done."""
    result = Removal()
    for folder in leftover_folders(home, env):
        if not _check_folder(folder, result):
            continue
        removed_here = False
        for name in LEFTOVER_NAMES:
            path = folder / name
            try:
                if _remove_file(path):
                    result.files.append(path)
                    removed_here = True
            except OSError as exc:
                logger.warning("retired integrity: could not remove %s (%s)", path, type(exc).__name__)
                result.failed.append((path, type(exc).__name__))
        if _remove_locks(folder / "locks", result):
            removed_here = True
        if removed_here:
            _remove_empty_folder(folder)
    for path, error in result.locks_failed:
        logger.warning("retired integrity: could not remove %s (%s)", path, error)
    return result


def _plural(count: int, word: str) -> str:
    """``"1 file"`` or ``"2 files"``.

    >>> _plural(1, "file"), _plural(3, "file")
    ('1 file', '3 files')
    """
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def report_removal(home: Path | str, console) -> None:
    """Remove the files and print what happened.

    One line for each removed baseline file, one count line for the lock files,
    one warning line for each baseline error, one for the lock errors together,
    and one for each folder that could not be read. ``console`` is a ``rich``
    console. A path never wraps (``soft_wrap``), so the user can copy it.
    """
    from rich.markup import escape

    result = remove_leftovers(home)
    for path in result.files:
        console.print(
            f"[green][OK][/green] Removed {escape(str(path))} (a file of the retired settings integrity check)",
            soft_wrap=True,
        )
    for path, error in result.failed:
        console.print(
            f"[yellow][WARN][/yellow] Could not remove {escape(str(path))} ({error}). Delete it yourself.",
            soft_wrap=True,
        )
    if result.locks_removed:
        removed_from = " and ".join(escape(str(folder)) for folder in result.locks_folders)
        console.print(
            f"[green][OK][/green] Removed {_plural(result.locks_removed, 'old quarantine lock file')} "
            f"from {removed_from}",
            soft_wrap=True,
        )
    if result.locks_failed:
        failed_in = " and ".join(escape(str(folder)) for folder in dict.fromkeys(p.parent for p, _ in result.locks_failed))
        errors = ", ".join(sorted({error for _, error in result.locks_failed}))
        console.print(
            f"[yellow][WARN][/yellow] Could not remove "
            f"{_plural(len(result.locks_failed), 'old quarantine lock file')} in {failed_in} ({errors}). "
            "Delete them yourself.",
            soft_wrap=True,
        )
    for path, error in result.unchecked:
        console.print(
            f"[yellow][WARN][/yellow] Could not read {escape(str(path))} ({error}). "
            "jacked did not remove the old files in it.",
            soft_wrap=True,
        )
