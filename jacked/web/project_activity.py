"""The projects that jacked recorded from its own session activity.

Two tables record a project path for each event: ``command_usage`` and
``hook_executions``. A writer can keep a path with back slashes (Windows), so
each query here changes a back slash to a forward slash before it compares or
groups paths. ``Database`` calls these functions with a read connection.
"""

from __future__ import annotations

import sqlite3
from typing import Iterable, Optional

# The tables that record a project path for each event.
ACTIVITY_TABLES = ("command_usage", "hook_executions")

_SUMMARY_SQL = """
SELECT
    repo_path,
    SUM(cmd_total) as commands_run,
    SUM(hook_total) as hook_executions,
    MAX(last_ts) as last_activity,
    MIN(first_ts) as first_seen,
    unique_sessions
FROM (
    SELECT REPLACE(repo_path, char(92), '/') as repo_path,
           COUNT(*) as cmd_total, 0 as hook_total,
           MAX(timestamp) as last_ts,
           MIN(timestamp) as first_ts,
           COUNT(DISTINCT session_id) as unique_sessions
    FROM command_usage
    WHERE repo_path IS NOT NULL AND repo_path != ''
    GROUP BY REPLACE(repo_path, char(92), '/')

    UNION ALL

    SELECT REPLACE(repo_path, char(92), '/') as repo_path,
           0 as cmd_total, COUNT(*) as hook_total,
           MAX(timestamp) as last_ts,
           MIN(timestamp) as first_ts,
           COUNT(DISTINCT session_id) as unique_sessions
    FROM hook_executions
    WHERE repo_path IS NOT NULL AND repo_path != ''
    GROUP BY REPLACE(repo_path, char(92), '/')
)
GROUP BY repo_path
ORDER BY last_activity DESC
LIMIT ?
"""


def activity_summary(conn: sqlite3.Connection, limit: int) -> list[dict]:
    """Return the per-project activity rows, the most recent project first."""
    return [dict(row) for row in conn.execute(_SUMMARY_SQL, (limit,)).fetchall()]


def recorded_project(conn: sqlite3.Connection, candidates: Iterable[str]) -> Optional[str]:
    """Return the recorded path that is equal to one of ``candidates``.

    The comparison is exact and case-sensitive, after each back slash in the
    recorded path and in each candidate changes to a forward slash. The
    return value is the recorded path with forward slashes, the same text
    that ``activity_summary`` gives.
    ``None`` means that no table records any of the candidates. The age of the
    activity has no effect.

    Each query first uses the ``repo_path`` index for an exact match. Only when
    that finds nothing does it compare the slash-changed text of each row, for
    a path that a writer kept with back slashes.

    >>> conn = sqlite3.connect(":memory:")
    >>> for table in ACTIVITY_TABLES:
    ...     _ = conn.execute(f"CREATE TABLE {table} (repo_path TEXT)")
    >>> _ = conn.execute("INSERT INTO hook_executions VALUES ('C:\\\\work\\\\repo')")
    >>> recorded_project(conn, ["C:/work/repo"])
    'C:/work/repo'
    >>> recorded_project(conn, ["c:/work/repo", "C:/work"]) is None
    True
    """
    forward = (c.replace("\\", "/") for c in candidates if isinstance(c, str) and c)
    wanted = list(dict.fromkeys(forward))
    if not wanted:
        return None
    marks = ", ".join("?" for _ in wanted)
    for table in ACTIVITY_TABLES:
        # The table names come from ACTIVITY_TABLES, never from a request.
        exact = conn.execute(
            f"SELECT repo_path FROM {table} WHERE repo_path IN ({marks}) LIMIT 1",
            wanted,
        ).fetchone()
        if exact is not None:
            return exact[0]
    for table in ACTIVITY_TABLES:
        found = conn.execute(
            f"SELECT REPLACE(repo_path, char(92), '/') FROM {table} "
            f"WHERE instr(repo_path, char(92)) > 0 "
            f"AND REPLACE(repo_path, char(92), '/') IN ({marks}) LIMIT 1",
            wanted,
        ).fetchone()
        if found is not None:
            return found[0]
    return None
