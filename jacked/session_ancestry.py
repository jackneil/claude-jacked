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
tracked process id must still belong to that session: a newer process that
reuses the id of a session that is gone does not count, and the walk moves
past it.

The hook records a process token with each row (``session_accounts.pid_token``,
see ``process_tokens``). The token does not change when the wall clock steps,
so it decides ownership: the same token is the same process, a different
token is a reused id. A row with no token (written before tokens existed, or
where the token could not be read) falls back to the start time, and that
can only confirm: a start at or before the row time is the same process. A
later start is unknown, because a wall-clock step (a resumed virtual machine,
an NTP correction) moves a live process the same way as a reused id.

The process table reads live in ``jacked.process_identity``. Ancestry is
POSIX only: Linux reads ``/proc``, macOS asks ``ps``. Windows (or a missing
``ps``) gives no ancestors, and callers treat the session as unknown.

The same ownership rule tells whether a tracked session still runs at all
(``session_processes_running``): its process is alive AND is still the
process that wrote the tracker row. Two consumers use that verdict and must
agree: the credential holder set (``tracked_holder_sessions``) and the
dashboard process sweeper. The check is cheap on every platform: one
liveness probe per process id, then the tokens (and, for rows with no token,
the start times) of the live ones in one pass each (``/proc`` on Linux, the
kernel on Windows, one ``ps`` for all ids elsewhere).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from jacked.process_identity import (
    ancestor_pids,
    process_start,
    process_starts,
    process_token,
    process_tokens,
)

logger = logging.getLogger(__name__)

# A session's process starts before the hook writes its tracker row. The
# slack covers the one-second resolution of ps and small clock steps.
_START_SLACK_SECONDS = 5.0


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


def _recorded_token(row) -> Optional[str]:
    """The process token stored with a tracker row, or None."""
    token = row.get("pid_token") if isinstance(row, dict) else None
    return token if isinstance(token, str) and token else None


def _row_ownership(row, start: Optional[float], token: Optional[str]) -> Optional[bool]:
    """Whether a live process (its *start* and *token*) still runs a tracker row.

    The token decides when the row and the process both have one: equal is
    the same process, different is a reused id. Otherwise the start time can
    only confirm (True for a start at or before the row time). A later start
    gives None, never False: a wall-clock step and a reused id look the same.

    >>> row = {"pid_token": "linux-boot-ticks:9", "detected_at": "2026-11-01T00:00:00+00:00"}
    >>> _row_ownership(row, 9e9, "linux-boot-ticks:9"), _row_ownership(row, 1.0, "linux-boot-ticks:7")
    (True, False)
    >>> _row_ownership({"detected_at": "2026-11-01T00:00:00+00:00"}, 9e9, None) is None
    True
    """
    recorded = _recorded_token(row)
    if recorded is not None and token is not None:
        return token == recorded
    written = _row_time(row.get("detected_at"))
    if start is None or written is None:
        return None
    return True if start <= written + _START_SLACK_SECONDS else None


def _owns_row(pid: int, row) -> Optional[bool]:
    """True when *pid* is still the process that wrote the tracker *row*."""
    token = process_token(pid) if _recorded_token(row) else None
    start = process_start(pid) if token is None else None
    return _row_ownership(row, start, token)


def tracked_pid(row) -> Optional[int]:
    """The usable process id of a tracker row, or None (no id, or id 0/1).

    >>> [tracked_pid(r) for r in ({"pid": 4242}, {"pid": 1}, {"pid": True}, {})]
    [4242, None, None, None]
    """
    pid = row.get("pid") if isinstance(row, dict) else None
    if isinstance(pid, int) and not isinstance(pid, bool) and pid > 1:
        return pid
    return None


def session_processes_running(rows) -> list[Optional[bool]]:
    """Whether the recorded process of each tracker row still runs its session.

    One answer per row, in order. True: the process is alive and is still
    the process that wrote the row. False: the process ended, or a newer
    process reuses its id (its token differs). None: the answer is not known
    (the row has no usable id, the liveness probe failed, or the row has no
    token to compare and the start does not confirm it). Each id is probed
    once. The tokens of the live ids are read in one pass, and the start
    times of the live ids with no token to compare in one more.

    >>> session_processes_running([{"pid": None}, {}])
    [None, None]
    """
    from jacked.service.process import process_liveness

    pids = [tracked_pid(row) for row in rows]
    alive: dict[int, Optional[bool]] = {}
    for pid in {p for p in pids if p is not None}:
        try:
            alive[pid] = process_liveness(pid)
        except Exception:  # noqa: BLE001 - an unknown answer, never a crash
            alive[pid] = None
    live_rows = [
        (row, pid) for row, pid in zip(rows, pids)
        if pid is not None and alive[pid] is True
    ]
    tokens = _read_batch(process_tokens, {
        pid for row, pid in live_rows if _recorded_token(row)
    })
    starts = _read_batch(process_starts, {
        pid for row, pid in live_rows
        if not (_recorded_token(row) and pid in tokens)
    })
    verdicts: list[Optional[bool]] = []
    for row, pid in zip(rows, pids):
        state = None if pid is None else alive[pid]
        if state is True:
            state = _row_ownership(row, starts.get(pid), tokens.get(pid))
        verdicts.append(state)
    return verdicts


def _read_batch(reader, pids: set[int]) -> dict:
    """``reader(sorted(pids))``, or {} when there are no pids or the read fails."""
    if not pids:
        return {}
    try:
        return reader(sorted(pids))
    except Exception:  # noqa: BLE001 - an unknown answer, never a crash
        logger.debug("Process facts could not be read", exc_info=True)
        return {}


def tracked_holder_sessions(db, window_minutes: int) -> list[dict]:
    """The open tracked sessions that may hold their account's refresh token.

    A session counts while it was active in the last *window_minutes*. After
    that it counts while its recorded process still runs it, or while that is
    not known (fail closed): a laptop that slept all night keeps its sessions,
    and they spend their refresh tokens when they wake. An idle session with
    no process id, or whose process ended or was replaced, does not count.
    *db* is a :class:`jacked.web.database.Database`.
    """
    sessions = list(db.get_active_sessions(staleness_minutes=window_minutes))
    idle = [
        row for row in db.get_stale_open_sessions(staleness_minutes=window_minutes)
        if tracked_pid(row) is not None
    ]
    running = session_processes_running(idle)
    return sessions + [row for row, state in zip(idle, running) if state is not False]


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
            owned = _owns_row(pid, row)
            if owned is None:
                return None  # the nearest session cannot be verified: claim nothing
            if owned:
                return row.get("account_id")
        return None
    except Exception:  # noqa: BLE001 - advisory: never fail the caller
        logger.debug("Session ancestry lookup failed", exc_info=True)
        return None
