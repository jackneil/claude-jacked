"""A trace of a jacked process that could not import its CLI.

When ``from jacked.cli import main`` fails, the process ends before it can
log anything. Under a supervisor or under ``pythonw.exe`` the traceback goes
nowhere. On 2026-11-26 that hid an outage: every Windows service start
stopped at ``import asyncio``, and no file said why.

``jacked/__main__.py`` calls :func:`record_current` when that import fails,
and then raises the error again. The log is
``~/.claude/jacked-boot-failure.log``. On macOS and Linux it has mode 0600.
On Windows jacked then sets the private ACL of the service files (owner
only), best effort: when that fails, the file keeps the ACL of
``~/.claude``, and the real boot error still goes up. The file
stays under ``MAX_BYTES``: the oldest entries go first, and one line says
that older entries were dropped. ``jacked service status`` prints the time
and the last line of the newest entry.

This module uses the standard library and ``jacked.atomic_io`` (which also
uses the standard library only), because it must work when the rest of
jacked cannot import.
"""

from __future__ import annotations

import calendar
import os
import time
import traceback
from pathlib import Path

LOG_NAME = "jacked-boot-failure.log"
_IS_WINDOWS = os.name == "nt"
#: The size limit of the log. A crash loop must not fill the disk.
MAX_BYTES = 1_000_000
ENTRY_MARK = "=== jacked boot failure at "
DROPPED_LINE = "=== older entries were dropped to keep this file under 1 MB ===\n"


def log_path() -> Path:
    """Return the path of the boot-failure log."""
    return Path(os.path.expanduser("~")) / ".claude" / LOG_NAME


def _entries(text: str) -> list[str]:
    entries: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith(ENTRY_MARK):
            entries.append(line)
        elif entries:
            entries[-1] += line
    return entries


def _write_private(path: Path, text: str) -> None:
    from jacked.atomic_io import atomic_write_text  # standard library only

    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text, mode=0o600, mode_strict=False)
    if _IS_WINDOWS:
        try:
            from jacked.service.windows_security import secure_windows_path

            secure_windows_path(path)
        except Exception:  # noqa: BLE001 - never mask the boot error
            pass


def record(trace: str, *, path: Path | None = None, now: float | None = None) -> None:
    """Append one entry with ``trace`` and a UTC time, then trim the log.

    >>> import tempfile, pathlib
    >>> log = pathlib.Path(tempfile.mkdtemp()) / LOG_NAME
    >>> record("Traceback\\nImportError: x\\n", path=log, now=0)
    >>> latest(path=log)
    ('1970-01-01T00:00:00Z', 'ImportError: x')
    """
    path = log_path() if path is None else path
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if now is None else now))
    entry = f"{ENTRY_MARK}{stamp} ===\n{trace.rstrip()}\n"
    try:
        old = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        old = ""
    entries = _entries(old) + [entry]
    dropped = old.startswith(DROPPED_LINE)
    while len(entries) > 1 and len("".join(entries)) + len(DROPPED_LINE) > MAX_BYTES:
        entries.pop(0)
        dropped = True
    if len(entry) + len(DROPPED_LINE) > MAX_BYTES:
        entries = [entry[: MAX_BYTES - len(DROPPED_LINE) - 1] + "\n"]
    _write_private(path, (DROPPED_LINE if dropped else "") + "".join(entries))


def record_current() -> None:
    """Record the exception that is being handled now."""
    record(traceback.format_exc())


def latest(*, path: Path | None = None) -> tuple[str, str] | None:
    """Return the time and the last line of the newest entry, or None."""
    path = log_path() if path is None else path
    try:
        entries = _entries(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    if not entries:
        return None
    lines = [line.strip() for line in entries[-1].splitlines() if line.strip()]
    stamp = lines[0][len(ENTRY_MARK):].removesuffix("===").strip()
    return stamp, lines[-1] if len(lines) > 1 else ""


def describe_latest(*, path: Path | None = None, after_file: Path | None = None) -> str | None:
    """Return one status line for the newest entry, or None.

    ``after_file`` is the service manifest: a live service writes it when it
    starts. An entry older than that file is history, not the current state,
    so it gives None.
    """
    found = latest(path=path)
    if found is None:
        return None
    stamp, last = found
    if after_file is not None:
        try:
            started = after_file.stat().st_mtime
            failed = calendar.timegm(time.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ"))
        except (OSError, ValueError):
            started = failed = None
        if started is not None and failed < started:
            return None
    return f"{last} (at {stamp}, see {path or log_path()})"
