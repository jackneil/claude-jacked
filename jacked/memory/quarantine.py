"""Move suspected injected lines out of a memory file. Never delete them.

The MemoryTrap attack (Cisco, 2026; see JACKED_GUARDRAILS.md) wrote
instructions into ``~/.claude/projects/*/memory/MEMORY.md`` so that each later
session loaded them. Research on memory poisoning found that a model often
refuses an injected instruction but leaves it in the file, so the next session
reads it again. jacked moves such a line to a dated quarantine file beside the
memory file: the session stops loading it, and the user can still read it and
move it back.

The detector is in ``jacked.memory.injection_shapes``: it matches six documented
attack shapes only, so a note about an attack, or a user's own rule, stays.
"""
from __future__ import annotations

import contextlib
import errno
import hashlib
import logging
import os
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from jacked.atomic_io import atomic_write_bytes
from jacked.memory.injection_shapes import SHAPES as SHAPES
from jacked.memory.injection_shapes import _physical_lines
from jacked.memory.injection_shapes import line_shape as line_shape
from jacked.memory.injection_shapes import suspect_lines as suspect_lines
from jacked.regular_file import FileTooLargeError, read_regular

logger = logging.getLogger(__name__)

# The errors of an O_NOFOLLOW open of a symlink: ELOOP on Linux and macOS,
# EMLINK on FreeBSD, EFTYPE on NetBSD.
_LINK_ERRNOS = {code for code in (errno.ELOOP, getattr(errno, "EMLINK", None), getattr(errno, "EFTYPE", None)) if code}

#: The largest memory file that jacked scans. Claude Code loads only the
#: first 25 KB of MEMORY.md; this limit only stops a huge file from taking the
#: memory of a session start. A larger file is not scanned, and the log says so.
MAX_SCAN_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class Moved:
    """The result of one scan that found suspect lines.

    ``reason`` is empty when the lines moved to ``path``. Else the lines
    stayed where they are, ``path`` is None, and ``reason`` says why (for
    example ``"a symlink"``).
    """

    path: Path | None
    count: int
    reason: str = ""

    @property
    def moved(self) -> bool:
        return not self.reason


def quarantine_path(memory: Path, today: str) -> Path:
    """``<memory file>.quarantine-<YYYY-MM-DD>.md`` beside the memory file.

    >>> quarantine_path(Path("/m/MEMORY.md"), "2026-11-03").name
    'MEMORY.md.quarantine-2026-11-03.md'
    """
    return memory.with_name(f"{memory.name}.quarantine-{today}.md")


#: A quarantine lock older than this is from a session that stopped, so it is
#: removed. A real move takes milliseconds.
LOCK_STALE_SECONDS = 60
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)


class RefusedPathError(OSError):
    """A path that the quarantine must not write: a symlink or not a regular file."""


def _kind(path: Path) -> str:
    """``"missing"``, ``"symlink"``, ``"regular"`` or ``"other"``, from ``lstat``."""
    try:
        mode = os.lstat(path).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return "missing"
    if stat.S_ISLNK(mode):
        return "symlink"
    return "regular" if stat.S_ISREG(mode) else "other"


def _read_memory(path: Path) -> bytes | None:
    """The bytes of the memory file, or None when it is missing or not readable."""
    try:
        return read_regular(path, MAX_SCAN_BYTES)
    except FileNotFoundError:
        return None
    except FileTooLargeError as exc:
        logger.warning(
            "quarantine: %s is %d bytes, larger than the scan limit of %d bytes, so it is not scanned",
            path.name, exc.size, MAX_SCAN_BYTES,
        )
        return None
    except OSError as exc:
        logger.warning("quarantine: cannot read %s (%s)", path.name, type(exc).__name__)
        return None


def _header(memory: Path, today: str) -> bytes:
    return (
        f"# Quarantined by jacked on {today} (data, not instructions)\n\n"
        f"jacked moved these lines out of {memory.name} because each one matches a known\n"
        "memory-injection shape. Read each line. Move a line back only if you wrote it.\n"
    ).encode("utf-8")


def _append_archive(target: Path, memory: Path, today: str, entries: bytes) -> None:
    """Add ``entries`` to the end of the quarantine archive ``target``.

    The first use creates the file (``O_CREAT | O_EXCL``) with the header. A
    later use only appends (``O_APPEND``): jacked never reads, rewrites or
    replaces an existing archive, so an archive that is large or unreadable
    keeps every earlier line. Both opens refuse a symlink (``O_NOFOLLOW``),
    and the open handle must be a regular file. Both opens are
    ``O_NONBLOCK``: a FIFO put at the name after the ``lstat`` check fails
    at once (ENXIO) or opens and fails the ``fstat`` check, and never blocks
    the session start. The flag has no effect on a regular file. The bytes
    are flushed to disk before the memory file changes.
    """
    flags = os.O_WRONLY | os.O_APPEND | _NOFOLLOW | _NONBLOCK | _BINARY
    try:
        try:
            fd = os.open(target, flags | os.O_CREAT | os.O_EXCL, 0o600)
            payload = _header(memory, today) + entries
        except FileExistsError:
            fd = os.open(target, flags)
            payload = entries
    except OSError as exc:
        if _NOFOLLOW and exc.errno in _LINK_ERRNOS:
            raise RefusedPathError("its quarantine file is a symlink") from exc
        if exc.errno == errno.ENXIO:
            # A FIFO with no reader: not a regular file.
            raise RefusedPathError("its quarantine file is not a regular file") from exc
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise RefusedPathError("its quarantine file is not a regular file")
        view = memoryview(payload)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    finally:
        os.close(fd)


def lock_path(memory: Path) -> Path:
    """The lock file beside ``memory`` that one move holds.

    >>> lock_path(Path("/m/MEMORY.md")).name
    '.MEMORY.md.jacked-quarantine.lock'
    """
    return memory.with_name(f".{memory.name}.jacked-quarantine.lock")


def _take_lock(lock: Path, memory: Path) -> int | None:
    """Create ``lock`` (``O_CREAT | O_EXCL``) and return its inode number.

    Returns None when another session holds the lock. A lock older than
    ``LOCK_STALE_SECONDS`` is removed once and taken again. The removal is a
    rename to a name that only this taker uses, so when two sessions see the
    same stale lock, only one rename takes it. A late rename that takes a
    lock other than the stale one (another session made a fresh lock in the
    meantime) puts that lock back and stops. After the create, the lock at
    the path must be the file that this taker made.
    """
    for attempt in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY | _NOFOLLOW | _BINARY, 0o600)
        except FileExistsError:
            try:
                st = os.lstat(lock)
            except FileNotFoundError:
                continue
            age = time.time() - st.st_mtime
            if attempt or age <= LOCK_STALE_SECONDS or not _remove_stale(lock, memory, st.st_ino, age):
                break
            continue
        try:
            ino = os.fstat(fd).st_ino
        finally:
            os.close(fd)
        with contextlib.suppress(FileNotFoundError):
            if os.lstat(lock).st_ino == ino:
                return ino
        break
    logger.debug("quarantine: another jacked session is moving lines from %s, so this pass stops", memory.name)
    return None


def _remove_stale(lock: Path, memory: Path, stale_ino: int, age: float) -> bool:
    """Remove the stale ``lock`` with inode ``stale_ino``. False when another taker was first."""
    aside = lock.with_name(f"{lock.name}.stale.{os.getpid()}.{secrets.token_hex(4)}")
    try:
        os.rename(lock, aside)
    except FileNotFoundError:
        return True  # another taker removed it; try the create again
    if os.lstat(aside).st_ino != stale_ino:
        # This rename took a fresh lock of another session: put it back.
        # The link never replaces a lock that a third session made at the path.
        try:
            os.link(aside, lock)
        except FileExistsError:
            logger.debug("quarantine: a newer lock is beside %s, so jacked keeps it", memory.name)
        except OSError as exc:
            # A filesystem without hard links (exFAT, FAT): rename the lock back.
            logger.debug("quarantine: cannot link the lock back beside %s (%s), so jacked renames it back",
                         memory.name, type(exc).__name__)
            if _rename_back(aside, lock, memory):
                return False
        os.unlink(aside)
        return False
    logger.debug("quarantine: the lock beside %s is %d s old, so jacked removes it", memory.name, age)
    os.unlink(aside)
    return True


def _rename_back(aside: Path, lock: Path, memory: Path) -> bool:
    """Rename the grabbed lock ``aside`` back to ``lock``. True when it is back.

    A rename can replace a file on POSIX, so it runs only when no lock is at
    the path. A third session that creates a lock between the check and the
    rename loses its lock; the window is two system calls on a filesystem
    without hard links only.
    """
    if os.path.lexists(lock):
        logger.debug("quarantine: a newer lock is beside %s, so jacked keeps it", memory.name)
        return False
    try:
        os.rename(aside, lock)
    except OSError as exc:
        logger.debug("quarantine: cannot rename the lock back beside %s (%s)", memory.name, type(exc).__name__)
        return False
    return True


def _release_lock(lock: Path, ino: int) -> None:
    """Remove ``lock`` only when it is still the lock with inode ``ino``."""
    with contextlib.suppress(FileNotFoundError):
        if os.lstat(lock).st_ino == ino:
            os.unlink(lock)


def _fingerprint(path: Path) -> tuple | None:
    """The identity, size, time and SHA-256 of ``path``, or None when it cannot be read.

    A failure is logged (the error class at DEBUG here, a read failure by
    ``_read_memory``), so a None that stops a move always has a reason in the log.
    """
    try:
        st = os.lstat(path)
    except OSError as exc:
        logger.debug("quarantine: cannot check %s again (%s), so jacked treats it as changed",
                     path.name, type(exc).__name__)
        return None
    data = _read_memory(path)
    if data is None:
        return None
    return st.st_ino, st.st_size, st.st_mtime_ns, hashlib.sha256(data).hexdigest()


class _ChangedMeanwhile(Exception):
    """The memory file changed after jacked read it."""


def _entries(lines: list[bytes], shapes: dict[int, str]) -> bytes:
    """The quarantine entries for the suspect ``lines``: each line as its original bytes."""
    return b"".join(
        f"\n- line {number} ({shapes[number]}):\n\n    ".encode("utf-8")
        + lines[number - 1].rstrip(b"\r\n") + b"\n"
        for number in sorted(shapes)
    )


def _not_moved(memory: Path, count: int, reason: str) -> Moved:
    logger.warning(
        "quarantine: %d suspected injected line%s in %s, %s; not moved",
        count, "" if count == 1 else "s", memory.name, reason,
    )
    return Moved(path=None, count=count, reason=reason)


def quarantine_file(memory: Path | str, *, today: str, read_only: str = "") -> Moved | None:
    """Move each suspect line of ``memory`` to its dated quarantine file.

    Returns None when the file is missing, not a regular file, too large to
    scan, or clean. Returns a result that did not move (``moved`` False, with
    the ``reason``) when the lines were found but must stay:

    * ``read_only`` is not empty: the caller gives the reason, for example a
      memory folder that a repo-local settings file chose;
    * the memory file is a symlink: jacked scans the file it points at, but
      never rewrites through a link;
    * the quarantine file is a symlink, or exists and is not a regular file.

    Else the lines are appended to the quarantine file first and the memory
    file is replaced second, so a failure between the two leaves a copy,
    never a loss. Before the archive write and again immediately before the
    replace, the memory file must still be the bytes that were read; when
    another process changed it, the pass returns None and the file stays as
    it is. The archive then keeps a copy, and the next pass appends the line
    again: a duplicate archive entry loses nothing. Both writes go to disk (fsync). The memory file keeps every
    other line byte for byte. The log names the file, the reason and the
    line count, never the content and never a link target.
    """
    memory = Path(memory)
    target = quarantine_path(memory, today)
    kind = _kind(memory)
    if kind == "missing":
        return None
    if kind == "other":
        logger.warning("quarantine: %s is not a regular file, so jacked does not scan it", memory.name)
        return None
    try:
        before = os.lstat(memory)
    except (FileNotFoundError, NotADirectoryError):
        return None
    raw = _read_memory(memory)
    if not raw:
        return None
    seen = (before.st_ino, before.st_size, before.st_mtime_ns, hashlib.sha256(raw).hexdigest())
    # Bytes end to end: the match uses a decoded view, the move takes the
    # original bytes, so an invalid UTF-8 byte never raises and never changes.
    lines = _physical_lines(raw)
    shapes = dict(suspect_lines(raw))
    if not shapes:
        return None
    count = len(shapes)
    target_kind = _kind(target)
    reason = read_only or (
        "a symlink" if kind == "symlink"
        else "its quarantine file is a symlink" if target_kind == "symlink"
        else "its quarantine file is not a regular file" if target_kind == "other"
        else ""
    )
    if reason:
        return _not_moved(memory, count, reason)

    entries = _entries(lines, shapes)
    lock = lock_path(memory)
    held = _take_lock(lock, memory)
    if held is None:
        return None
    try:
        # Another process (Claude Code, a second session) can append to the
        # memory file after the read. Write only when the file is still the
        # bytes that were read; else the next session start tries again.
        if _fingerprint(memory) != seen:
            logger.debug(
                "quarantine: %s changed while jacked read it, so this pass moves nothing; "
                "the next session start tries again", memory.name,
            )
            return None
        try:
            _append_archive(target, memory, today, entries)
        except RefusedPathError as exc:
            return _not_moved(memory, count, str(exc))
        kept = b"".join(line for number, line in enumerate(lines, 1) if number not in shapes)

        def unchanged() -> None:
            # A write can land during the archive write too, so the check
            # runs again immediately before the replace (and each retry).
            if _fingerprint(memory) != seen:
                raise _ChangedMeanwhile

        # Each kept line keeps its own bytes and line ending (CRLF stays CRLF).
        # follow_symlinks=False: the replace swaps the path itself, never a target.
        try:
            atomic_write_bytes(memory, kept, fsync=True, follow_symlinks=False, before_replace=unchanged)
        except _ChangedMeanwhile:
            # The archive keeps its copy. The next pass appends the line
            # again: a duplicate entry in the archive loses nothing.
            logger.debug(
                "quarantine: %s changed while jacked wrote %s, so %s stays as it is; the quarantine "
                "file holds a copy of the line%s, and the next session start moves %s again",
                memory.name, target.name, memory.name, "" if count == 1 else "s",
                "it" if count == 1 else "them",
            )
            return None
    finally:
        _release_lock(lock, held)

    logger.warning(
        "quarantine: moved %d line%s from %s to %s",
        count, "" if count == 1 else "s", memory.name, target.name,
    )
    return Moved(path=target, count=count)
