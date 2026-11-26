"""A trace of a jacked process that could not import its CLI.

When ``from jacked.cli import main`` fails, the process ends before it can
log anything. Under a supervisor or under ``pythonw.exe`` the traceback goes
nowhere. On 2026-11-26 that hid an outage: every Windows service start
stopped at ``import asyncio``, and no file said why.

``jacked/__main__.py`` calls :func:`record_current` when that import fails,
and then raises the error again. The log is
``~/.claude/jacked-boot-failure.log``. On macOS and Linux it has mode 0600.
On Windows jacked then sets the private ACL of the service files (owner
only), best effort: when that fails, one line on stderr says so, the file
keeps the ACL of ``~/.claude``, and the real boot error still goes up.

When the service reaches "Service ready", :func:`record_started` appends one
"started OK" line, when the newest entry is a failure. ``jacked service
status`` prints the time and the last line of the newest failure only when
no "started OK" line comes after it. The file stays under ``MAX_BYTES`` of
UTF-8: the oldest entries go first, and one line says that older entries
were dropped.

On macOS and Linux this module uses the standard library and
``jacked.atomic_io`` (which uses the standard library only), because it
must work when the rest of jacked cannot import. On Windows it also imports
``jacked.service.windows_security`` for the ACL, inside a try.
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

LOG_NAME = "jacked-boot-failure.log"
_IS_WINDOWS = os.name == "nt"
#: The size limit of the log, in UTF-8 bytes. A crash loop must not fill the disk.
MAX_BYTES = 1_000_000
ENTRY_MARK = "=== jacked boot failure at "
OK_MARK = "=== jacked started OK at "
DROPPED_LINE = "=== older entries were dropped to keep this file under 1 MB ===\n"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"


def log_path() -> Path:
    """Return the path of the boot-failure log."""
    return Path(os.path.expanduser("~")) / ".claude" / LOG_NAME


def _size(text: str) -> int:
    return len(text.encode("utf-8"))


def _entries(text: str) -> list[str]:
    entries: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith((ENTRY_MARK, OK_MARK)):
            entries.append(line)
        elif entries:
            entries[-1] += line
    return entries


def _warn(text: str) -> None:
    try:
        sys.stderr.write(f"jacked: {text}\n")
    except Exception:  # noqa: BLE001 - no stderr under pythonw.exe
        pass


def _write_private(path: Path, text: str) -> None:
    from jacked.atomic_io import atomic_write_text  # standard library only

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text, mode=0o600, mode_strict=False)
    if _IS_WINDOWS:
        try:
            from jacked.service.windows_security import secure_windows_path

            secure_windows_path(path)
        except Exception as exc:  # noqa: BLE001 - never mask the boot error
            _warn(f"could not set the private ACL of {path} ({type(exc).__name__})")


def _stamp(now: float | None) -> str:
    return time.strftime(_STAMP, time.gmtime(time.time() if now is None else now))


def _read(path: Path) -> tuple[str, list[str]]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return "", []
    return text, _entries(text)


def _append(entry: str, path: Path) -> None:
    old, entries = _read(path)
    entries = entries + [entry]
    dropped = old.startswith(DROPPED_LINE)
    while len(entries) > 1 and _size("".join(entries)) + _size(DROPPED_LINE) > MAX_BYTES:
        entries.pop(0)
        dropped = True
    if _size(entry) + _size(DROPPED_LINE) > MAX_BYTES:
        room = MAX_BYTES - _size(DROPPED_LINE) - 1
        entries = [entry.encode("utf-8")[:room].decode("utf-8", errors="ignore") + "\n"]
    _write_private(path, (DROPPED_LINE if dropped else "") + "".join(entries))


def record(trace: str, *, path: Path | None = None, now: float | None = None) -> None:
    """Append one failure entry with ``trace`` and a UTC time, then trim the log.

    A failed write prints one line on stderr and raises the error again.

    >>> import tempfile, pathlib
    >>> log = pathlib.Path(tempfile.mkdtemp()) / LOG_NAME
    >>> record("Traceback\\nImportError: x\\n", path=log, now=0)
    >>> latest(path=log)
    ('1970-01-01T00:00:00Z', 'ImportError: x')
    """
    path = log_path() if path is None else path
    try:
        _append(f"{ENTRY_MARK}{_stamp(now)} ===\n{trace.rstrip()}\n", path)
    except Exception as exc:
        _warn(f"could not write the boot-failure log {path} ({type(exc).__name__})")
        raise


def record_started(*, path: Path | None = None, now: float | None = None) -> None:
    """Append one "started OK" line when the newest entry is a failure. Never raise.

    This is a read, then a write, with no lock. A failure that a second
    jacked process writes in the same moment can be lost. That needs two
    processes at once: one that starts the service, and one that cannot
    import its CLI. That is two installs, one of them broken, at one time.
    """
    path = log_path() if path is None else path
    try:
        _old, entries = _read(path)
        if entries and entries[-1].startswith(ENTRY_MARK):
            _append(f"{OK_MARK}{_stamp(now)} ===\n", path)
    except Exception:  # noqa: BLE001 - a log line must never stop the service
        pass


def record_current() -> None:
    """Record the exception that is being handled now."""
    record(traceback.format_exc())


def _summary(entry: str) -> tuple[str, str]:
    lines = [line.strip() for line in entry.splitlines() if line.strip()]
    stamp = lines[0][len(ENTRY_MARK):].removesuffix("===").strip()
    return stamp, lines[-1] if len(lines) > 1 else ""


def latest(*, path: Path | None = None) -> tuple[str, str] | None:
    """Return the time and the last line of the newest failure entry, or None."""
    _text, entries = _read(log_path() if path is None else path)
    failures = [entry for entry in entries if entry.startswith(ENTRY_MARK)]
    return _summary(failures[-1]) if failures else None


def describe_latest(*, path: Path | None = None) -> str | None:
    """Return one status line for the newest failure, or None.

    A failure with a "started OK" line after it is history: the service
    started after it, so it gives None.

    >>> import tempfile, pathlib
    >>> log = pathlib.Path(tempfile.mkdtemp()) / LOG_NAME
    >>> record("Traceback\\nImportError: x\\n", path=log, now=0)
    >>> describe_latest(path=log).startswith("ImportError: x (at 1970-01-01T00:00:00Z")
    True
    >>> record_started(path=log, now=1)
    >>> describe_latest(path=log) is None
    True
    """
    path = log_path() if path is None else path
    _text, entries = _read(path)
    if not entries or not entries[-1].startswith(ENTRY_MARK):
        return None
    # One read only: a second read can find the file gone.
    stamp, last = _summary(entries[-1])
    return f"{last} (at {stamp}, see {path})"
