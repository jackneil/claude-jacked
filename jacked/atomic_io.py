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

import logging
import os
import stat
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


def _describe_refusal(refusal: OSError) -> str:
    """Name a refusal for a log line: its class and its error code.

    The Windows code (``winerror``) is used when there is one, else the
    ``errno``. The exception text is never used, because it can hold a path.

    >>> _describe_refusal(PermissionError(13, "Access is denied"))
    'PermissionError errno=13'
    >>> held = PermissionError(13, "Access is denied")
    >>> held.winerror = 32
    >>> _describe_refusal(held)
    'PermissionError winerror=32'
    """
    winerror = getattr(refusal, "winerror", None)
    if winerror is not None:
        return f"{type(refusal).__name__} winerror={winerror}"
    return f"{type(refusal).__name__} errno={refusal.errno}"


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
    ``_pause_schedule(retry_budget)`` (ten tries in 0.9 s by default). After
    the last try, the last refusal is raised again unchanged. A retried
    replace logs one DEBUG line with the file name, the retry count, and the
    class and error code of the last refusal (never its text). On POSIX the
    replace runs once, and a ``PermissionError`` is a real refusal that is
    not retried.

    ``retry_budget`` is the most time, in seconds, the retries may wait. Use
    0 for a write that must never wait (a cache). ``write_through`` asks
    Windows to flush the rename to disk before it returns. POSIX callers that
    need the rename durable sync the parent directory themselves.

    ``before_retry`` runs after each pause, before the next try. A
    compare-and-swap writer uses it to check its precondition again, and it
    raises to stop the replace when the target changed during the pause.
    That exception propagates unchanged. A ``PermissionError`` from
    ``before_retry`` is different: the check could not read the file, for
    the same reason the replace failed. It counts as one more refusal of the
    same budget, so the retry pauses and runs ``before_retry`` again before
    the next replace.
    """
    if sys.platform != "win32":
        _replace_once(src, dst, write_through=write_through)
        return
    pauses = _pause_schedule(retry_budget)
    retries = 0
    last_refusal: PermissionError | None = None
    while True:
        try:
            if retries and before_retry is not None:
                before_retry()
            _replace_once(src, dst, write_through=write_through)
        except PermissionError as refusal:
            pause = next(pauses, None)
            if pause is None:
                logger.debug(
                    "Could not replace %s after %d retries (%s)",
                    Path(dst).name,
                    retries,
                    _describe_refusal(refusal),
                )
                raise
            last_refusal = refusal
            retries += 1
            time.sleep(pause)
            continue
        if last_refusal is not None:
            logger.debug(
                "Replaced %s after %d retries (last refusal: %s)",
                Path(dst).name,
                retries,
                _describe_refusal(last_refusal),
            )
        return


def _remove_temp(tmp_name: str, retry_budget: float = RETRY_BUDGET_SECONDS) -> None:
    """Remove the temp file of a failed write. This is best effort.

    On Windows a scanner or an indexer can hold the new temp file for a
    short time, so a refused unlink is tried again with the pauses of the
    caller's retry budget (the default replace budget when none is given).
    A budget of 0 gives one attempt on every platform, so a write that must
    never wait does not wait in its cleanup either. When the refusal lasts
    longer, or the unlink fails another way, the temp file stays and this
    function returns quietly: the cleanup must never replace the error that
    stopped the write. POSIX gets one attempt.
    """
    pauses = _pause_schedule(retry_budget)
    while True:
        try:
            os.unlink(tmp_name)
            return
        except PermissionError:
            pause = next(pauses, None) if sys.platform == "win32" else None
            if pause is None:
                return
            time.sleep(pause)
        except OSError:
            return


def _set_mode_bits(fd: int, tmp_name: str, mode: int) -> None:
    """Give the open temp file the POSIX permission bits ``mode``.

    ``os.fchmod`` changes the file that ``fd`` (from mkstemp) holds, with no
    path lookup, so a link that another process puts at the temp name cannot
    redirect the change. A build with no ``os.fchmod`` (Windows before Python
    3.13) uses the path.
    """
    if hasattr(os, "fchmod"):
        os.fchmod(fd, mode)
    else:
        os.chmod(tmp_name, mode)


def _set_mode(fd: int, tmp_name: str, mode: int, *, strict: bool, name: str) -> None:
    """Give the open temp file the permission bits ``mode``, and on Windows a
    private ACL.

    When the mode bits are refused, a ``strict`` write stops: the error
    propagates. A tolerant write logs the refusal at DEBUG (the file
    ``name``, the error class and its code) and goes on.

    On Windows a mode bit does not change who can open the file: mkstemp
    gives the file the ACL of its folder. So on Windows the file also gets a
    private ACL (the current user only). That step always runs after the
    mode step, whatever the result of the mode step. On Windows the ACL is
    the only protection the file has, so a refused ACL stops the write even
    for a tolerant caller; a storage that cannot carry a private ACL cannot
    hold a secret. The ACL goes through the temp file's name
    (``SetNamedSecurityInfoW``), and so does the mode on a build without
    ``os.fchmod``: a process that can put a link at that name can already
    read the folder's files, so the name is not a new exposure.
    """
    try:
        _set_mode_bits(fd, tmp_name, mode)
    except OSError as exc:
        if strict:
            raise
        logger.debug(
            "Could not set the mode of %s (%s); the write goes on",
            name,
            _describe_refusal(exc),
        )
    if os.name == "nt":
        # Imported here, as the service writers do: this module imports only
        # the standard library at load time, so it has no import cycle.
        from jacked.service.windows_security import secure_windows_path

        secure_windows_path(Path(tmp_name))


def _keep_mode(fd: int, tmp_name: str, target: Path) -> None:
    """Give the temp file the permission bits of the file that it replaces (POSIX).

    mkstemp makes the temp file 0o600, so without this step each rewrite of a
    0o644 file (a MEMORY.md, a settings file) leaves it 0o600. Only a regular
    file at ``target`` gives its bits (``lstat``: a symlink that the write
    replaces gives none), and only the nine permission bits (never setuid,
    setgid or sticky). A new file stays 0o600. Windows is not changed here:
    its protection is the ACL, which the ``mode`` argument sets. A refused
    chmod is logged at DEBUG and the write goes on; the result is then
    0o600, never looser.
    """
    if os.name == "nt":
        return
    try:
        st = os.lstat(target)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.debug("Could not read the mode of %s (%s); it becomes 0o600", target.name, _describe_refusal(exc))
        return
    bits = stat.S_IMODE(st.st_mode) & 0o777
    if not stat.S_ISREG(st.st_mode) or bits == 0o600:
        return
    try:
        _set_mode_bits(fd, tmp_name, bits)
    except OSError as exc:
        logger.debug("Could not keep the mode of %s (%s); it becomes 0o600", target.name, _describe_refusal(exc))


def _write_atomically(
    path,
    payload,
    *,
    binary: bool,
    fsync: bool,
    follow_symlinks: bool,
    mode: int | None,
    mode_strict: bool,
    retry_budget: float,
    before_replace: Callable[[], None] | None = None,
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
    # mkstemp creates the temp file owner-only (0o600); with no ``mode``, a
    # rewrite keeps the permission bits of the file it replaces.
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        handle = os.fdopen(fd, "wb") if binary else os.fdopen(fd, "w", encoding="utf-8")
        with handle:
            if mode is not None:
                # Before the bytes land, so a secret never sits in a file
                # with looser permissions than the caller asked for.
                _set_mode(
                    handle.fileno(), tmp_name, mode, strict=mode_strict, name=target.name
                )
            else:
                _keep_mode(handle.fileno(), tmp_name, target)
            handle.write(payload)
            if fsync:
                handle.flush()
                os.fsync(handle.fileno())
        if before_replace is not None:
            before_replace()
        replace_with_retry(tmp_name, target, retry_budget=retry_budget, before_retry=before_replace)
    except BaseException:
        # Any failure (a KeyboardInterrupt mid-write included) removes the
        # temp file, so a crash never leaves a stray temp or a partial target.
        # The cleanup follows the caller's wait policy (its retry budget).
        _remove_temp(tmp_name, retry_budget)
        raise


def atomic_write_bytes(
    path,
    data: bytes,
    *,
    fsync: bool = False,
    follow_symlinks: bool = False,
    mode: int | None = None,
    mode_strict: bool = True,
    retry_budget: float = RETRY_BUDGET_SECONDS,
    before_replace: Callable[[], None] | None = None,
) -> None:
    """Atomically replace ``path`` with ``data``, byte for byte.

    The parent directory is created when it is missing. ``fsync`` flushes the
    bytes to disk before the replace. ``mode`` sets the permission bits of the
    result (0o600 for a credential file); on Windows it also gives the result
    a private ACL (the current user only). With no ``mode``, a rewrite on
    POSIX keeps the permission bits of the regular file that it replaces, and
    a new file is 0o600. ``follow_symlinks`` writes through a
    link at ``path`` to the file it points at; by default the link itself is
    replaced by a plain file. ``retry_budget`` is passed to
    ``replace_with_retry``.

    ``before_replace`` is a compare-and-swap check. It runs after the temp
    file is complete, immediately before the replace, and again before each
    retry of the replace (``before_retry``). It raises to stop the write: the
    temp file is removed, the target does not change, and the exception
    propagates.

    By default a failure to set the mode (the chmod, or the private ACL on
    Windows) stops the write before the replace, and the error propagates.
    ``mode_strict=False`` is for storage that creates and replaces files but
    refuses chmod (ENOTSUP). Its tolerance covers the POSIX mode bits only:
    a refused chmod is logged at DEBUG (the file name, the error class and
    its code) and the write goes on. The temp file is created owner-only
    (0o600), so a tolerated failure never leaves the result looser than
    0o600: with ``mode=0o600`` it is exactly the mode the caller asked for.
    On Windows the private ACL is still required: a refused ACL stops a
    tolerant write too, because the ACL is the only protection the file has
    there.
    """
    _write_atomically(
        path,
        data,
        binary=True,
        fsync=fsync,
        follow_symlinks=follow_symlinks,
        mode=mode,
        mode_strict=mode_strict,
        retry_budget=retry_budget,
        before_replace=before_replace,
    )


def atomic_write_text(
    path,
    text: str,
    *,
    follow_symlinks: bool = False,
    fsync: bool = False,
    mode: int | None = None,
    mode_strict: bool = True,
    retry_budget: float = RETRY_BUDGET_SECONDS,
    before_replace: Callable[[], None] | None = None,
) -> None:
    """Atomically replace ``path`` with ``text`` encoded as UTF-8.

    The file is written in text mode, so Windows writes its native line
    endings. Use ``atomic_write_bytes`` when the bytes must match exactly.
    The keyword arguments are the same as for ``atomic_write_bytes``: for
    example, ``mode_strict=False`` tolerates a refused chmod only, and on
    Windows the private ACL is still required.
    """
    _write_atomically(
        path,
        text,
        binary=False,
        fsync=fsync,
        follow_symlinks=follow_symlinks,
        mode=mode,
        mode_strict=mode_strict,
        retry_budget=retry_budget,
        before_replace=before_replace,
    )
