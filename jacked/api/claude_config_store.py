"""Read and write the Claude Code global config safely.

This module owns the file side of the ``oauthAccount`` publish:

- ``claude_config_path`` finds the file that Claude Code reads, with the
  same rule as Claude Code 2.1.296 (``<home>/.claude/.config.json`` when it
  exists, else ``<home>/.claude.json``);
- a directory lock that is compatible with proper-lockfile, the lock
  library that Claude Code uses for the same file;
- ``write_config_identity``, which reads under the lock, fails closed on a
  file it cannot read, compares and swaps on the whole file, writes
  atomically with fsync, and verifies the identity after the write.

Logs from this module contain row ids and field names. They never contain
an email address or a token.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from jacked.api.credential_helpers import _safe_replace

logger = logging.getLogger(__name__)

CONFIG_FILENAME = ".claude.json"
LEGACY_CONFIG_RELATIVE = Path(".claude") / ".config.json"
LOCK_SUFFIX = ".lock"
# Claude Code adds a suffix to its config name for a custom OAuth endpoint.
# jacked cannot know that file's contract, so it does not write then.
CUSTOM_OAUTH_ENV = "CLAUDE_CODE_CUSTOM_OAUTH_URL"
# proper-lockfile (Claude Code) treats a lock directory as stale after 10 s.
STALE_LOCK_SECONDS = 10.0
# A hold longer than half the stale limit could let Claude Code take the
# lock over. The writer gives up instead of replacing the file that late.
MAX_HOLD_SECONDS = STALE_LOCK_SECONDS / 2
_LOCK_POLL_SECONDS = 0.05
_RELEASE_ATTEMPTS = 5
_RELEASE_BACKOFF_SECONDS = 0.05
# Windows antivirus and indexers hold new files briefly. The replace gets
# a longer retry budget there (about 3 s in total).
_REPLACE_RETRIES = 6
_REPLACE_DELAY_SECONDS = 0.1
_VERIFY_READS = 3
_VERIFY_DELAY_SECONDS = 0.05
TMP_PREFIX = ".claude_json_tmp_"

WRITTEN = "written"
UNCHANGED = "unchanged"
CAS_ABORTED = "cas_aborted"
LOCKED = "locked"
UNREADABLE = "unreadable"
SYMLINK = "symlink"
WRITE_FAILED = "write_failed"
POST_VERIFY_MISMATCH = "post_verify_mismatch"
UNSUPPORTED = "unsupported"
WRITE_STATUSES = frozenset(
    {
        WRITTEN,
        UNCHANGED,
        CAS_ABORTED,
        LOCKED,
        UNREADABLE,
        SYMLINK,
        WRITE_FAILED,
        POST_VERIFY_MISMATCH,
        UNSUPPORTED,
    }
)
_SUCCESS_STATUSES = frozenset({WRITTEN, UNCHANGED})


@dataclass(frozen=True)
class WriteResult:
    """The outcome of one config write.

    ``status`` is one of ``WRITE_STATUSES``. ``mode`` is ``"merge"`` or
    ``"replace"`` when the block was applied, else None.
    """

    status: str
    mode: str | None = field(default=None)

    @property
    def ok(self) -> bool:
        """True when the config holds the block after the call.

        >>> WriteResult("written").ok, WriteResult("locked").ok
        (True, False)
        """
        return self.status in _SUCCESS_STATUSES


# ---------------------------------------------------------------------------
# Path
# ---------------------------------------------------------------------------


def claude_config_path(home: Path | str, *, scoped: bool = False) -> Path | None:
    """Return the config file that Claude Code reads, or None.

    Claude Code 2.1.296 reads ``<config dir>/.config.json`` when that file
    exists, else ``<CLAUDE_CONFIG_DIR or home>/.claude.json``. For the global
    config, ``home`` is the user's home and the config dir is
    ``<home>/.claude``. With ``scoped``, ``home`` is a ``CLAUDE_CONFIG_DIR``
    directory and is also the config dir. With a custom OAuth endpoint the
    name has a suffix that jacked does not support, so the result is None.

    >>> import tempfile
    >>> d = Path(tempfile.mkdtemp())
    >>> claude_config_path(d) == d / ".claude.json"
    True
    """
    if os.environ.get(CUSTOM_OAUTH_ENV):
        return None
    home = Path(home)
    legacy = home / ".config.json" if scoped else home / LEGACY_CONFIG_RELATIVE
    if legacy.exists():
        return legacy
    return home / CONFIG_FILENAME


# ---------------------------------------------------------------------------
# Lock (proper-lockfile compatible: an EMPTY <config>.lock directory)
# ---------------------------------------------------------------------------


def _lock_path_for(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + LOCK_SUFFIX)


def _is_windows() -> bool:
    """True on Windows, where a lock directory can be "delete pending".

    >>> isinstance(_is_windows(), bool)
    True
    """
    return os.name == "nt" or sys.platform == "win32"


def _clear_stale_lock(lock_path: Path) -> bool:
    """Remove an empty lock directory that is older than the stale limit.

    Return True when the caller can try ``mkdir`` again at once. A lock
    directory that is not empty is not ours, so it is never removed.

    The lock is examined again immediately before ``rmdir``. When its
    ``st_mtime`` or ``st_ino`` changed after the age check, another process
    replaced it, and it is not removed. A small race remains between that
    second check and ``rmdir``. The whole-file compare-and-swap and the
    post-write verify cover that race.
    """
    try:
        first = os.lstat(lock_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    age = time.time() - first.st_mtime
    if age <= STALE_LOCK_SECONDS:
        return False
    try:
        second = os.lstat(lock_path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if second.st_mtime != first.st_mtime or second.st_ino != first.st_ino:
        logger.debug("Claude config lock was replaced during the stale check; kept")
        return False
    try:
        os.rmdir(lock_path)
    except FileNotFoundError:
        return True
    except OSError:
        logger.debug("Stale Claude config lock is not an empty directory; treated as held")
        return False
    logger.info("Removed a stale Claude config lock (age %.0f s)", age)
    return True


def _acquire_lock(lock_path: Path, timeout: float) -> int | None:
    """Create the lock directory and return its inode, or None on timeout.

    Raise OSError for a failure other than "the lock exists". On Windows,
    ``mkdir`` raises PermissionError while a removed lock directory is
    still "delete pending". That is a held lock that goes away soon, so the
    call retries until the deadline. It never clears such a lock as stale.
    """
    deadline = time.monotonic() + max(float(timeout), 0.0)
    retried_stale = False
    while True:
        try:
            os.mkdir(lock_path)
            try:
                return os.lstat(lock_path).st_ino
            except OSError:
                return 0
        except FileExistsError:
            # After a stale lock is cleared, always try once more, even when
            # the timeout is zero.
            if _clear_stale_lock(lock_path) and (
                not retried_stale or time.monotonic() < deadline
            ):
                retried_stale = True
                continue
        except PermissionError:
            if not _is_windows():
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        time.sleep(min(_LOCK_POLL_SECONDS, remaining))


def _release_lock(lock_path: Path, inode: int) -> None:
    """Remove the lock directory only when it is still the one we created.

    Another process can take a lock over when it judges it stale. Removing
    that lock would break the other holder, so a changed inode is left in
    place. Removal has a bounded retry for Windows scanners.
    """
    for attempt in range(_RELEASE_ATTEMPTS):
        try:
            current = os.lstat(lock_path).st_ino
        except FileNotFoundError:
            logger.warning(
                "Claude config lock was removed by another process before release"
            )
            return
        except OSError:
            current = inode
        if inode and current != inode:
            logger.warning(
                "Claude config lock was taken over by another process; left in place"
            )
            return
        try:
            os.rmdir(lock_path)
            return
        except FileNotFoundError:
            return
        except OSError:
            if attempt < _RELEASE_ATTEMPTS - 1:
                time.sleep(_RELEASE_BACKOFF_SECONDS * (attempt + 1))
    logger.warning("Could not release the Claude config lock")


# ---------------------------------------------------------------------------
# Read and write
# ---------------------------------------------------------------------------


def _stat_signature(path: Path) -> tuple | None:
    """Return a change signature for ``path``, or None when it is absent."""
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return None
    return (st.st_mtime_ns, st.st_size, st.st_ino)


def _read_config_for_write(config_path: Path) -> dict | None:
    """Read the config under the lock.

    An absent file gives an empty dict. A file that cannot be read or that
    is not a JSON object gives None (fail closed: never start fresh over it).
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(config_path: Path, data: dict) -> None:
    """Write ``data`` to ``config_path`` through a private, fsynced temp file."""
    fd, tmp = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=TMP_PREFIX, suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(
            tmp, str(config_path), retries=_REPLACE_RETRIES, delay=_REPLACE_DELAY_SECONDS
        )
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


Applier = Callable[[dict], str]


def _write_locked(
    config_path: Path,
    apply: Applier,
    expected: dict | None,
    account_id: int,
    started: float,
) -> WriteResult:
    """Read, compare-and-swap and write. Call this only under the lock."""
    if config_path.is_symlink():
        return WriteResult(SYMLINK)
    signature = _stat_signature(config_path)
    config = _read_config_for_write(config_path)
    if config is None:
        return WriteResult(UNREADABLE)
    if expected is not None and config.get("oauthAccount") != expected:
        return WriteResult(CAS_ABORTED)
    had_block = "oauthAccount" in config
    before = copy.deepcopy(config.get("oauthAccount"))
    mode = apply(config)
    if had_block and config["oauthAccount"] == before:
        return WriteResult(UNCHANGED, mode)
    # Whole-file compare-and-swap. A writer that does not honor the lock
    # (Claude Code's contention fallback, its storage-v5 backend) may have
    # changed any key since the read. Replacing the file now would undo it.
    if _stat_signature(config_path) != signature:
        logger.info(
            "Account %d: the Claude config changed during the write; not replaced",
            account_id,
        )
        return WriteResult(CAS_ABORTED, mode)
    if time.monotonic() - started > MAX_HOLD_SECONDS:
        logger.warning(
            "Account %d: the Claude config lock was held too long; not replaced",
            account_id,
        )
        return WriteResult(LOCKED, mode)
    try:
        _atomic_write_json(config_path, config)
    except Exception as exc:  # noqa: BLE001 - every write error is reported
        logger.warning(
            "Account %d: could not write the Claude config (%s)",
            account_id,
            type(exc).__name__,
        )
        return WriteResult(WRITE_FAILED, mode)
    return WriteResult(WRITTEN, mode)


def write_config_identity(
    home: Path | str,
    apply: Applier,
    verify: Callable[[dict | None], list[str]],
    *,
    account_id: int,
    expected: dict | None,
    lock_timeout: float,
    scoped: bool = False,
) -> WriteResult:
    """Apply ``apply`` to the config under the lock and verify the result.

    ``apply`` changes the parsed config and returns the mode it used.
    ``verify`` takes the ``oauthAccount`` read back after the write and
    returns the names of the fields that differ from what was written.
    """
    config_path = claude_config_path(home, scoped=scoped)
    if config_path is None:
        logger.warning(
            "Account %d: Claude Code uses a custom OAuth config name (%s is set); "
            "the identity is not published",
            account_id,
            CUSTOM_OAUTH_ENV,
        )
        return WriteResult(UNSUPPORTED)
    if config_path.is_symlink():
        return WriteResult(SYMLINK)
    lock_path = _lock_path_for(config_path)
    try:
        inode = _acquire_lock(lock_path, lock_timeout)
    except OSError as exc:
        logger.warning(
            "Account %d: could not create the Claude config lock (%s)",
            account_id,
            type(exc).__name__,
        )
        return WriteResult(WRITE_FAILED)
    if inode is None:
        return WriteResult(LOCKED)
    started = time.monotonic()
    try:
        result = _write_locked(config_path, apply, expected, account_id, started)
    finally:
        _release_lock(lock_path, inode)
    if result.status != WRITTEN:
        return result
    diverged = verify(_verify_read(home, scoped=scoped))
    if diverged:
        logger.warning(
            "Account %d: post-verify-mismatch after the Claude config write "
            "(fields: %s); another writer changed the identity",
            account_id,
            ", ".join(diverged),
        )
        return WriteResult(POST_VERIFY_MISMATCH, result.mode)
    return result


def _verify_read(home: Path | str, *, scoped: bool = False) -> dict | None:
    """Read the identity back, retrying a torn or missing read briefly."""
    for attempt in range(_VERIFY_READS):
        block = read_config_identity(home, scoped=scoped)
        if block is not None:
            return block
        if attempt < _VERIFY_READS - 1:
            time.sleep(_VERIFY_DELAY_SECONDS)
    return None


def read_config_identity(home: Path | str, *, scoped: bool = False) -> dict | None:
    """Return the ``oauthAccount`` block of the Claude config, or None.

    None means no supported config, a missing file, a symlink, a file that
    is not a JSON object, or no ``oauthAccount`` object. The read does not
    take the lock.
    """
    config_path = claude_config_path(home, scoped=scoped)
    if config_path is None or config_path.is_symlink():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("oauthAccount")
    return block if isinstance(block, dict) else None


def sweep_stale_temp_files(home: Path | str, *, older_than: float = 60.0) -> int:
    """Remove temp files that an interrupted write left beside the config.

    Return the number removed. Only files with the writer's own prefix that
    are older than ``older_than`` seconds are touched.
    """
    config_path = claude_config_path(home)
    if config_path is None:
        return 0
    removed = 0
    cutoff = time.time() - older_than
    try:
        candidates = list(config_path.parent.glob(TMP_PREFIX + "*.json"))
    except OSError:
        return 0
    for candidate in candidates:
        try:
            if candidate.is_symlink() or candidate.stat().st_mtime > cutoff:
                continue
            candidate.unlink()
            removed += 1
        except OSError:
            continue
    if removed:
        logger.info("Removed %d stale Claude config temp file(s)", removed)
    return removed
