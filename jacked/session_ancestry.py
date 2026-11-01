"""Which Claude Code session runs this process, found by process ancestry.

The session tracker hook records each Claude Code session's process id
(``session_accounts.pid``: the hook's parent, the ``claude`` process). A
command that a session runs (an agent's Bash tool, for example) descends from
that process, so walking this process's ancestors and matching them against
the tracker's open rows names the account of the calling session, with no
credential-store read.

Two rules keep the answer honest. The NEAREST tracked ancestor decides: a
session started inside another session is its own session, so an unknown
account there gives no answer, never the outer session's account. And a
tracked process id must still belong to that session: a process that started
after the tracker row was written reuses the id of a session that is gone,
and the walk moves past it.

POSIX only: Linux reads ``/proc``, macOS asks ``ps``. Windows (or a missing
``ps``) gives no ancestors, and callers treat the session as unknown.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Enough for claude -> shell -> launcher -> python and a few wrappers; the
# walk also stops at pid 1 and on a loop.
_MAX_DEPTH = 16
_PS_TIMEOUT_SECONDS = 2.0
# A session's process starts before the hook writes its tracker row. The
# slack covers the one-second resolution of ps and small clock steps.
_START_SLACK_SECONDS = 5.0
_boot_time: Optional[float] = None


def _proc_stat(pid: int) -> Optional[list[str]]:
    """Fields 3 and later of ``/proc/<pid>/stat``; None without /proc or access.

    Any OSError counts as "cannot read": with ``hidepid`` a root-owned
    ancestor raises PermissionError, which ``Path.exists`` does not swallow.
    """
    try:
        data = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if ")" not in data:
        return None
    # The command name (field 2) can hold spaces and ")".
    return data[data.rindex(")") + 2:].split()


def _ps(pid: int, field: str) -> Optional[str]:
    """One ``ps`` output field for *pid*, or None when ps cannot answer."""
    try:
        result = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip() or None


def _parent_of(pid: int) -> Optional[int]:
    """The parent pid of *pid*, or None when it cannot be read."""
    fields = _proc_stat(pid)
    text = fields[1] if fields and len(fields) > 1 else _ps(pid, "ppid")
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _etime_seconds(text: Optional[str]) -> Optional[int]:
    """Seconds in a ``ps`` elapsed time, ``[[dd-]hh:]mm:ss``, or None.

    >>> _etime_seconds("05:03"), _etime_seconds("01:00:00"), _etime_seconds("2-00:00:01")
    (303, 3600, 172801)
    >>> _etime_seconds("soon"), _etime_seconds(None), _etime_seconds("1:2:3:4")
    (None, None, None)
    """
    if not text:
        return None
    days, _, clock = text.strip().rpartition("-")
    parts = clock.split(":")
    try:
        values = [int(days or 0)] + [0] * (3 - len(parts)) + [int(p) for p in parts]
    except ValueError:
        return None
    if len(values) != 4:
        return None
    d, h, m, s = values
    return ((d * 24 + h) * 60 + m) * 60 + s


def _linux_boot_time() -> Optional[float]:
    """The ``btime`` line of ``/proc/stat`` (epoch seconds), read once."""
    global _boot_time
    if _boot_time is None:
        try:
            lines = Path("/proc/stat").read_text(encoding="utf-8").splitlines()
            _boot_time = next(float(x.split()[1]) for x in lines if x.startswith("btime "))
        except (OSError, ValueError, IndexError, StopIteration):
            return None
    return _boot_time


def process_start(pid: int) -> Optional[float]:
    """When *pid* started, in epoch seconds, or None when it cannot be read.

    >>> import os, time
    >>> start = process_start(os.getpid())
    >>> start is None or start <= time.time() + 1
    True
    """
    fields = _proc_stat(pid)
    if fields is not None:
        boot = _linux_boot_time()
        try:
            ticks = int(fields[19])  # field 22: starttime, in clock ticks since boot
            hz = os.sysconf("SC_CLK_TCK")
        except (IndexError, ValueError, OSError, AttributeError):
            return None
        return boot + ticks / hz if boot is not None and hz > 0 else None
    seconds = _etime_seconds(_ps(pid, "etime"))
    return None if seconds is None else time.time() - seconds


def ancestor_pids(pid: Optional[int] = None, max_depth: int = _MAX_DEPTH) -> list[int]:
    """The pids of this process's ancestors, nearest first.

    *pid* is the first ancestor to report (default: this process's parent).
    Empty on a platform without POSIX process ids.

    >>> import os
    >>> pids = ancestor_pids()
    >>> os.name != "posix" or os.getppid() <= 1 or pids[0] == os.getppid()
    True
    >>> ancestor_pids(pid=1)
    []
    """
    if os.name != "posix":
        return []
    current = os.getppid() if pid is None else pid
    out: list[int] = []
    for _ in range(max_depth):
        if current is None or current <= 1 or current in out:
            break
        out.append(current)
        current = _parent_of(current)
    return out


def _row_time(detected_at) -> Optional[float]:
    """Epoch seconds of a tracker row's ``detected_at`` (ISO; UTC when naive).

    >>> _row_time("2026-11-01T00:00:00+00:00"), _row_time("2026-11-01T00:00:00"), _row_time("x")
    (1793491200.0, 1793491200.0, None)
    """
    try:
        moment = datetime.fromisoformat(str(detected_at).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.timestamp()


def _owns_row(pid: int, detected_at) -> Optional[bool]:
    """True when *pid* is still the process that wrote the tracker row.

    False for a reused pid (its process started after the row was written);
    None when the start time or the row time cannot be read.
    """
    start = process_start(pid)
    written = _row_time(detected_at)
    if start is None or written is None:
        return None
    return start <= written + _START_SLACK_SECONDS


def calling_session_account(db) -> Optional[int]:
    """The account id of the Claude Code session that runs this process.

    The nearest tracked ancestor decides (see the module docstring). None
    when no ancestor is a live tracked session (a plain terminal, a cron
    job, Windows), when the nearest session has no account or cannot be
    verified, and on any error: the answer is advisory and must never fail
    the command that asks. *db* is a :class:`jacked.web.database.Database`.
    """
    try:
        pids = ancestor_pids()
        sessions = db.find_open_sessions(pids) if pids else {}
        for pid in pids:
            row = sessions.get(pid)
            if row is None:
                continue
            owned = _owns_row(pid, row.get("detected_at"))
            if owned is None:
                return None  # the nearest session cannot be verified: claim nothing
            if owned:
                return row.get("account_id")
        return None
    except Exception:  # noqa: BLE001 - advisory: never fail the caller
        logger.debug("Session ancestry lookup failed", exc_info=True)
        return None
