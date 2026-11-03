"""The one atomic file writer for jacked, with a Windows-aware replace.

Every jacked file write that must never leave a half-written file goes
through this module: write a writer-unique temp file beside the target, then
replace the target in one step. A reader sees the old file or the new one,
never a mix of the two.

On Windows the replace fails with ``PermissionError`` (WinError 5 or 32)
while another handle holds the target: a concurrent replace, a reader, a
search indexer or an antivirus scan. ``replace_with_retry`` retries that
refusal for a short bounded time, then re-raises. POSIX ``rename`` never
fails that way, so POSIX gets exactly one attempt.

This module imports only the standard library, so the credential stores and
the service supervisors can use it with no import cycle.
"""

from __future__ import annotations

import contextlib
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# The default retry budget: ten tries with pauses of 10 ms doubling, each
# pause capped at 150 ms, for 0.9 s of waiting in total.
RETRY_BUDGET_SECONDS = 1.0
_FIRST_PAUSE_MS = 10
_MAX_PAUSE_MS = 150

_MOVEFILE_REPLACE_EXISTING = 0x1
_MOVEFILE_WRITE_THROUGH = 0x8


def _pause_schedule(budget_seconds: float):
    """Yield the pause (seconds) before each retry, within ``budget_seconds``.

    Pauses start at 10 ms and double, each capped at 150 ms. The schedule
    stops before a pause would take the total past the budget.

    >>> list(_pause_schedule(RETRY_BUDGET_SECONDS))
    [0.01, 0.02, 0.04, 0.08, 0.15, 0.15, 0.15, 0.15, 0.15]
    >>> list(_pause_schedule(0))
    []
    """
    budget_ms = round(budget_seconds * 1000)
    total_ms = 0
    pause_ms = _FIRST_PAUSE_MS
    while total_ms + pause_ms <= budget_ms:
        total_ms += pause_ms
        yield pause_ms / 1000
        pause_ms = min(pause_ms * 2, _MAX_PAUSE_MS)


def _move_file_write_through(src, dst) -> None:
    """Replace ``dst`` with ``src`` and flush the rename to disk (Windows only)."""
    import ctypes
    from ctypes import wintypes

    move_file = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
    move_file.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file.restype = wintypes.BOOL
    flags = _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH
    if not move_file(os.fspath(src), os.fspath(dst), flags):
        # WinError 5 and 32 surface as PermissionError, which the caller retries.
        raise ctypes.WinError(ctypes.get_last_error())


def _replace_once(src, dst, *, write_through: bool) -> None:
    """One replace attempt: the write-through move on Windows when asked."""
    if write_through and sys.platform == "win32":
        _move_file_write_through(src, dst)
    else:
        os.replace(src, dst)


def replace_with_retry(
    src,
    dst,
    *,
    retry_budget: float = RETRY_BUDGET_SECONDS,
    write_through: bool = False,
    before_retry: Callable[[], None] | None = None,
) -> None:
    """Replace ``dst`` with ``src`` in one step.

    On Windows a ``PermissionError`` is retried with the pauses of
    ``_pause_schedule(retry_budget)`` (ten tries in 0.9 s by default) and
    re-raised after the last try. A retried replace logs one DEBUG line with
    the file name and the retry count. On POSIX the replace runs once, and a
    ``PermissionError`` is a real refusal that is not retried.

    ``retry_budget`` is the most time, in seconds, the retries may wait. Use
    0 for a write that must never wait (a cache). ``write_through`` asks
    Windows to flush the rename to disk before it returns. POSIX callers that
    need the rename durable sync the parent directory themselves.
    ``before_retry`` runs after each pause, before the next try. A
    compare-and-swap writer uses it to check the target again, and it raises
    to stop the replace when the target changed during the pause.
    """
    if sys.platform != "win32":
        _replace_once(src, dst, write_through=write_through)
        return
    pauses = _pause_schedule(retry_budget)
    retries = 0
    while True:
        try:
            _replace_once(src, dst, write_through=write_through)
        except PermissionError:
            pause = next(pauses, None)
            if pause is None:
                logger.debug(
                    "Could not replace %s after %d retries", Path(dst).name, retries
                )
                raise
            retries += 1
            time.sleep(pause)
            if before_retry is not None:
                before_retry()
            continue
        if retries:
            logger.debug("Replaced %s after %d retries", Path(dst).name, retries)
        return


def _write_atomically(
    path,
    payload,
    *,
    binary: bool,
    fsync: bool,
    follow_symlinks: bool,
    mode: int | None,
    retry_budget: float,
) -> None:
    # A symlink at ``path`` is replaced by a plain file unless the caller
    # opts in: following it lets a repo-shipped link (lessons.md pointing at
    # ~/.bashrc) turn a save into a write outside the project. The user's own
    # global CLAUDE.md, often a link to a dotfiles copy, opts in.
    target = Path(os.path.realpath(path)) if follow_symlinks else Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Writer-unique: the CLI, the dashboard service and hook processes can
    # all write the same file, and a shared temp name would let one writer
    # replace the target with another writer's half-written temp file.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        handle = os.fdopen(fd, "wb") if binary else os.fdopen(fd, "w", encoding="utf-8")
        with handle:
            if mode is not None:
                # Before the bytes land, so a secret never sits in a file
                # with looser permissions than the caller asked for.
                os.chmod(tmp_name, mode)
            handle.write(payload)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        replace_with_retry(tmp_name, target, retry_budget=retry_budget)
    except BaseException:
        # Any failure (a KeyboardInterrupt mid-write included) removes the
        # temp file, so a crash never leaves a stray temp or a partial target.
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def atomic_write_bytes(
    path,
    data: bytes,
    *,
    fsync: bool = False,
    follow_symlinks: bool = False,
    mode: int | None = None,
    retry_budget: float = RETRY_BUDGET_SECONDS,
) -> None:
    """Atomically replace ``path`` with ``data``, byte for byte.

    The parent directory is created when it is missing. ``fsync`` flushes the
    bytes to disk before the replace. ``mode`` sets the permission bits of the
    result (0o600 for a credential file). ``follow_symlinks`` writes through a
    link at ``path`` to the file it points at; by default the link itself is
    replaced by a plain file. ``retry_budget`` is passed to
    ``replace_with_retry``.
    """
    _write_atomically(
        path,
        data,
        binary=True,
        fsync=fsync,
        follow_symlinks=follow_symlinks,
        mode=mode,
        retry_budget=retry_budget,
    )


def atomic_write_text(
    path,
    text: str,
    *,
    follow_symlinks: bool = False,
    fsync: bool = False,
    mode: int | None = None,
    retry_budget: float = RETRY_BUDGET_SECONDS,
) -> None:
    """Atomically replace ``path`` with ``text`` encoded as UTF-8.

    The file is written in text mode, so Windows writes its native line
    endings. Use ``atomic_write_bytes`` when the bytes must match exactly.
    The keyword arguments are the same as for ``atomic_write_bytes``.
    """
    _write_atomically(
        path,
        text,
        binary=False,
        fsync=fsync,
        follow_symlinks=follow_symlinks,
        mode=mode,
        retry_budget=retry_budget,
    )
