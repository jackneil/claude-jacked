"""Small SQLite operations used by the standalone session hook."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

HEARTBEAT_THROTTLE_SECONDS = 300

# Optional evidence columns of a span, in insert order. An older schema can
# lack any of them; the writer stores only the columns the table has.
SPAN_FIELDS = (
    "credential_scope",
    "observed_at",
    "evidence",
    "observation_state",
    "credential_revision",
    "launch_nonce",
    "event_idempotency_key",
    "pid_token",
)


def usable_pid(pid) -> bool:
    """True for a process id that names a session process (an int above 1).

    >>> [usable_pid(p) for p in (4242, 1, 0, None, True, "7")]
    [True, False, False, False, False, False]
    """
    return isinstance(pid, int) and not isinstance(pid, bool) and pid > 1


def _new_process(stored_pid, stored_token, pid, token) -> bool:
    """True when a record from (*pid*, *token*) comes from a different process
    than the open span (*stored_pid*, *stored_token*).

    >>> _new_process(4100, "a", 5200, "b"), _new_process(None, None, 5200, None)
    (True, True)
    >>> _new_process(4100, "a", 4100, "z"), _new_process(4100, None, 4100, "a")
    (True, False)
    >>> _new_process(4100, "a", None, None), _new_process(4100, "a", 1, None)
    (False, False)
    """
    if not usable_pid(pid):
        return False
    if stored_pid != pid:
        return True
    return bool(stored_token and token and stored_token != token)


def write_span(
    conn: sqlite3.Connection,
    session_id: str,
    account_id: int | None,
    email: str | None,
    method: str | None,
    repo_path: str | None,
    pid: int | None,
    fields: dict,
    *,
    force_new_span: bool = False,
) -> tuple[int, str]:
    """Insert or refresh the open span of one session; return (row id, time).

    Call inside the writer transaction. Both writers (the hook's
    ``record_session`` and ``Database.record_session_account``) use it, so
    they cannot disagree. Rules, in order:

    1. ``force_new_span`` ends every open span of the session. Otherwise a
       known account ends the open spans of other accounts.
    2. An open span of the same account is refreshed (activity time and the
       evidence in *fields*), UNLESS the record comes from a different
       process (``_new_process``). ``claude --resume`` runs a session again
       in a new process: that span ends and a new one starts, with the new
       pid and ``detected_at`` = now. A refresh would keep the old pid, and
       the sweeper would close the span when the old process is gone.
    3. Otherwise a new span is inserted.

    >>> c = sqlite3.connect(":memory:")
    >>> _ = c.execute(
    ...     "CREATE TABLE session_accounts (id INTEGER PRIMARY KEY, session_id TEXT, "
    ...     "account_id INTEGER, email TEXT, detected_at TEXT, ended_at TEXT, "
    ...     "last_activity_at TEXT, detection_method TEXT, repo_path TEXT, pid INTEGER, "
    ...     "pid_token TEXT, UNIQUE(session_id, detected_at))")
    >>> first, _ = write_span(c, "s", 4, None, "x", None, 4100, {"pid_token": "a"})
    >>> write_span(c, "s", 4, None, "x", None, 4100, {"pid_token": "a"})[0] == first
    True
    >>> resumed, _ = write_span(c, "s", 4, None, "x", None, 5200, {"pid_token": "b"})
    >>> c.execute("SELECT pid FROM session_accounts WHERE ended_at IS NULL").fetchall()
    [(5200,)]
    """
    ts = datetime.now(timezone.utc).isoformat()
    columns = {row[1] for row in conn.execute("PRAGMA table_info(session_accounts)")}
    present = [name for name in SPAN_FIELDS if name in columns]
    values = [fields.get(name) for name in present]
    if force_new_span:
        conn.execute(
            "UPDATE session_accounts SET ended_at = ? "
            "WHERE session_id = ? AND ended_at IS NULL",
            (ts, session_id),
        )
    elif account_id is not None:
        conn.execute(
            """UPDATE session_accounts SET ended_at = ?
               WHERE session_id = ? AND ended_at IS NULL
                 AND (account_id != ? OR account_id IS NULL)""",
            (ts, session_id, account_id),
        )
    existing = None
    if not force_new_span:
        token_column = "pid_token" if "pid_token" in columns else "NULL"
        existing = conn.execute(
            f"""SELECT id, pid, {token_column} FROM session_accounts
                WHERE session_id = ? AND account_id IS ? AND ended_at IS NULL
                ORDER BY detected_at DESC LIMIT 1""",
            (session_id, account_id),
        ).fetchone()
    if existing and _new_process(existing[1], existing[2], pid, fields.get("pid_token")):
        conn.execute(
            "UPDATE session_accounts SET ended_at = ? WHERE id = ?", (ts, existing[0])
        )
        existing = None
    if existing:
        sets = "".join(f", {name} = COALESCE(?, {name})" for name in present)
        conn.execute(
            f"UPDATE session_accounts SET last_activity_at = ?{sets} WHERE id = ?",
            (ts, *values, existing[0]),
        )
        return existing[0], ts
    ts = unique_detected_at(conn, session_id, ts)
    names = ", ".join(
        ["session_id", "account_id", "email", "detected_at", "last_activity_at",
         "detection_method", "repo_path", "pid", *present]
    )
    marks = ", ".join("?" * (8 + len(present)))
    cursor = conn.execute(
        f"INSERT OR IGNORE INTO session_accounts ({names}) VALUES ({marks})",
        (session_id, account_id, email, ts, ts, method, repo_path, pid, *values),
    )
    return cursor.lastrowid or 0, ts


def unique_detected_at(conn: sqlite3.Connection, session_id: str, ts: str) -> str:
    """``ts``, or one microsecond past the session's latest ``detected_at``.

    ``session_accounts`` is ``UNIQUE(session_id, detected_at)`` and the insert
    is ``INSERT OR IGNORE``. Two detections for one session in the same clock
    tick (Windows' clock is coarse) gave the second record the first one's
    timestamp: it was silently dropped after the first was ended, and the
    session was left with no open record. Call inside the writer transaction.

    >>> c = sqlite3.connect(":memory:")
    >>> _ = c.execute("CREATE TABLE session_accounts (session_id TEXT, detected_at TEXT)")
    >>> _ = c.execute("INSERT INTO session_accounts VALUES ('s', '2026-10-29T05:00:00+00:00')")
    >>> unique_detected_at(c, "s", "2026-10-29T05:00:00+00:00")
    '2026-10-29T05:00:00.000001+00:00'
    >>> unique_detected_at(c, "s", "2026-10-29T06:00:00+00:00")
    '2026-10-29T06:00:00+00:00'
    >>> unique_detected_at(c, "other", "2026-10-29T05:00:00+00:00")
    '2026-10-29T05:00:00+00:00'
    """
    from datetime import timedelta

    row = conn.execute(
        "SELECT MAX(detected_at) FROM session_accounts WHERE session_id = ?",
        (session_id,),
    ).fetchone()
    latest = row[0] if row else None
    if not latest:
        return ts
    try:
        latest_dt = datetime.fromisoformat(latest)
        ts_dt = datetime.fromisoformat(ts)
    except ValueError:
        return ts
    if ts_dt > latest_dt:
        return ts
    return (latest_dt + timedelta(microseconds=1)).isoformat()


def match_snapshot_account(
    db_path: Path, snapshot: dict | None
) -> tuple[int | None, str | None]:
    """Match a resolved identity by account id, email, organization and provider."""
    if not db_path.exists() or not isinstance(snapshot, dict):
        return None, None
    observed = snapshot.get("observed")
    if snapshot.get("state") != "resolved" or not isinstance(observed, dict):
        return None, None
    account_id = observed.get("account_id")
    email = observed.get("email")
    organization_id = observed.get("organization_id")
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        return None, None
    if not isinstance(email, str) or not email:
        return None, None
    if organization_id is not None and not isinstance(organization_id, str):
        return None, None
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            row = conn.execute(
                "SELECT id, email FROM accounts "
                "WHERE id = ? AND LOWER(email) = LOWER(?) "
                "AND organization_uuid = ? AND is_deleted = 0 "
                "AND COALESCE(provider, 'claude') = 'claude' LIMIT 1",
                (account_id, email, organization_id or ""),
            ).fetchone()
            return (row[0], row[1]) if row else (None, None)
        finally:
            conn.close()
    except Exception:
        return None, None


def record_session(
    db_path: Path,
    session_id: str,
    account_id: int | None,
    email: str | None,
    method: str,
    repo_path: str | None,
    pid: int | None = None,
    *,
    credential_scope: str | None = None,
    observed_at: str | None = None,
    evidence: str | None = None,
    observation_state: str | None = None,
    credential_revision: str | None = None,
    launch_nonce: str | None = None,
    event_idempotency_key: str | None = None,
    pid_token: str | None = None,
    force_new_span: bool = False,
) -> str | None:
    """Insert or refresh one session configuration span (see ``write_span``)."""
    if not db_path.exists():
        return None
    fields = {
        "credential_scope": credential_scope,
        "observed_at": observed_at,
        "evidence": evidence,
        "observation_state": observation_state,
        "credential_revision": credential_revision,
        "launch_nonce": launch_nonce,
        "event_idempotency_key": event_idempotency_key,
        "pid_token": pid_token,
    }
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute("BEGIN IMMEDIATE")
            _, ts = write_span(
                conn, session_id, account_id, email, method, repo_path, pid,
                fields, force_new_span=force_new_span,
            )
            conn.commit()
            return ts
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            return None
        finally:
            conn.close()
    except Exception:
        return None


def tag_subagent(
    db_path: Path,
    session_id: str,
    detected_at: str | None,
    parent_session_id: str | None,
    agent_type: str | None,
) -> None:
    if not detected_at or not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                """UPDATE session_accounts
                   SET is_subagent = 1, parent_session_id = ?, agent_type = ?
                   WHERE session_id = ? AND detected_at = ?""",
                (parent_session_id, agent_type, session_id, detected_at),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def end_session(db_path: Path, session_id: str) -> None:
    if not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                "UPDATE session_accounts SET ended_at = ? "
                "WHERE session_id = ? AND ended_at IS NULL",
                (datetime.now(timezone.utc).isoformat(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass


def heartbeat_session(db_path: Path, session_id: str) -> None:
    if not db_path.exists():
        return
    try:
        now = datetime.now(timezone.utc)
        conn = sqlite3.connect(str(db_path), timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            row = conn.execute(
                "SELECT last_activity_at FROM session_accounts "
                "WHERE session_id = ? AND ended_at IS NULL "
                "ORDER BY detected_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
            if not row:
                return
            if row[0]:
                try:
                    previous = datetime.fromisoformat(row[0])
                    if previous.tzinfo is None:
                        previous = previous.replace(tzinfo=timezone.utc)
                    if (now - previous).total_seconds() < HEARTBEAT_THROTTLE_SECONDS:
                        return
                except (TypeError, ValueError):
                    pass
            conn.execute(
                """UPDATE session_accounts SET last_activity_at = ?
                   WHERE id = (
                       SELECT id FROM session_accounts
                       WHERE session_id = ? AND ended_at IS NULL
                       ORDER BY detected_at DESC LIMIT 1
                   )""",
                (now.isoformat(), session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception:
        pass
