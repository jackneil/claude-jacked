"""Remove the files of the retired settings integrity check (#99i).

jacked 0.111.0 to 0.111.5 kept a baseline of settings.json in
``jacked/integrity.json`` beside the settings file, with the lock file
``jacked/integrity.json.lock``. jacked does not use these files now.
``jacked install`` (an upgrade runs it) and ``jacked uninstall`` call
``report_removal`` to remove them.

The rules:

* Remove only the two names in ``LEFTOVER_NAMES``, and only when ``os.lstat``
  shows a regular file. jacked does not follow a link with one of these names,
  and it does not remove a folder with one of these names.
* Look in ``<home>/.claude/jacked`` and in ``<config folder>/jacked``
  (``claude_paths.config_dir``, so ``CLAUDE_CONFIG_DIR`` applies). The old
  writer put the baseline beside each settings file that it wrote, so both
  folders can hold one.
* Remove the ``jacked`` folder only when this cleanup removed a file from it
  and the folder is then empty. A user file in the folder stays, and so does
  the folder.
* A missing file gives no line. An error gives one warning line and does not
  stop the install or the uninstall.
"""
from __future__ import annotations

import logging
import os
import stat
from collections.abc import Mapping
from pathlib import Path

from jacked.memory import claude_paths

logger = logging.getLogger(__name__)

#: The files that the retired check left in each ``jacked`` folder.
LEFTOVER_NAMES = ("integrity.json", "integrity.json.lock")


def leftover_folders(home: Path | str, env: Mapping[str, str] | None = None) -> list[Path]:
    """Return each folder that can hold the files, with no duplicate.

    >>> [p.as_posix() for p in leftover_folders("/h", {})]
    ['/h/.claude/jacked']
    >>> [p.as_posix() for p in leftover_folders("/h", {"CLAUDE_CONFIG_DIR": "/c"})]
    ['/h/.claude/jacked', '/c/jacked']
    """
    folders: list[Path] = []
    for base in (Path(home) / ".claude", claude_paths.config_dir(home, env)):
        folder = base / "jacked"
        if folder not in folders:
            folders.append(folder)
    return folders


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


def _remove_empty_folder(folder: Path) -> None:
    """Remove ``folder`` when it is a real folder and empty. Else it stays."""
    try:
        if stat.S_ISDIR(os.lstat(folder).st_mode):
            os.rmdir(folder)
    except OSError as exc:  # not empty, missing or not removable: the folder stays
        logger.debug("retired integrity: %s stays (%s)", folder, type(exc).__name__)


def remove_leftovers(
    home: Path | str, env: Mapping[str, str] | None = None
) -> tuple[list[Path], list[tuple[Path, str]]]:
    """Remove the files. Return ``(removed paths, [(path, error class name)])``."""
    removed: list[Path] = []
    failed: list[tuple[Path, str]] = []
    for folder in leftover_folders(home, env):
        removed_here = False
        for name in LEFTOVER_NAMES:
            path = folder / name
            try:
                if _remove_file(path):
                    removed.append(path)
                    removed_here = True
            except OSError as exc:
                logger.warning("retired integrity: could not remove %s (%s)", path, type(exc).__name__)
                failed.append((path, type(exc).__name__))
        if removed_here:
            _remove_empty_folder(folder)
    return removed, failed


def report_removal(home: Path | str, console) -> None:
    """Remove the files, and print one line for each removed file and each error.

    ``console`` is a ``rich`` console. A path never wraps (``soft_wrap``), so
    the user can copy it.
    """
    from rich.markup import escape

    removed, failed = remove_leftovers(home)
    for path in removed:
        console.print(
            f"[green][OK][/green] Removed {escape(str(path))} (a file of the retired settings integrity check)",
            soft_wrap=True,
        )
    for path, error in failed:
        console.print(
            f"[yellow][WARN][/yellow] Could not remove {escape(str(path))} ({error}). Delete it yourself.",
            soft_wrap=True,
        )
