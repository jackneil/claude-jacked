"""Which Claude Code session runs this process, found by process ancestry.

The session tracker hook records each Claude Code session's process id
(``session_accounts.pid``: the hook's parent, the ``claude`` process). A
command that a session runs (an agent's Bash tool, for example) descends from
that process, so walking this process's ancestors and matching them against
the tracker's open rows names the account of the calling session, with no
credential-store read.

POSIX only: Linux reads ``/proc/<pid>/stat``, macOS asks ``ps``. Windows (or
a missing ``ps``) gives no ancestors, and callers treat the session as
unknown.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

# Enough for claude -> shell -> launcher -> python and a few wrappers; the
# walk also stops at pid 1 and on a loop.
_MAX_DEPTH = 16
_PS_TIMEOUT_SECONDS = 2.0


def _parent_of(pid: int) -> Optional[int]:
    """The parent pid of *pid*, or None when it cannot be read."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        try:
            data = stat.read_text(encoding="utf-8", errors="replace")
            # Fields after the command name, which can hold spaces and ")".
            fields = data[data.rindex(")") + 2:].split()
            return int(fields[1])
        except (OSError, ValueError, IndexError):
            return None
    try:
        result = subprocess.run(
            ["ps", "-o", "ppid=", "-p", str(pid)],
            capture_output=True, text=True, timeout=_PS_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    try:
        return int(result.stdout.strip())
    except ValueError:
        return None


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


def calling_session_account(db) -> Optional[int]:
    """The account id of the Claude Code session that runs this process.

    None when no ancestor is a session the tracker recorded (a plain
    terminal, a cron job, Windows), or when the tracker has no account for
    it. *db* is a :class:`jacked.web.database.Database`.
    """
    pids = ancestor_pids()
    if not pids:
        return None
    return db.find_open_session_account(pids)
