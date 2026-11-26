"""A trace of a jacked process that could not import its CLI.

When ``from jacked.cli import main`` fails, the process ends before it can
log anything. Under a supervisor or under ``pythonw.exe`` the traceback goes
nowhere. On 2026-11-26 that hid an outage: every Windows service start
stopped at ``import asyncio``, and no file said why.

``jacked/__main__.py`` calls :func:`record_current` when that import fails,
and then raises the error again. The log is
``~/.claude/jacked-boot-failure.log``, with owner-only access. The file
stays under ``MAX_BYTES``: the oldest entries go first, and one line says
that older entries were dropped. ``jacked service status`` prints the time
and the last line of the newest entry.

This module uses the standard library only, because it must work when the
rest of jacked cannot import.
"""

from __future__ import annotations

import os
import tempfile
import time
import traceback
from pathlib import Path

LOG_NAME = "jacked-boot-failure.log"
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
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{LOG_NAME}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", errors="replace") as handle:
            handle.write(text)
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


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


def describe_latest(*, path: Path | None = None) -> str | None:
    """Return one status line for the newest entry, or None."""
    found = latest(path=path)
    if found is None:
        return None
    stamp, last = found
    return f"{last} (at {stamp}, see {path or log_path()})"
