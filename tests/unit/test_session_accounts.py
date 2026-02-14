"""Unit tests for session-account tracking.

Tests cover the Database CRUD methods (record, end, query, active),
heartbeat-based staleness (60-min read-side filter), session resurrection,
deduplication via UNIQUE constraint, multi-account scenarios, the
check_same_thread safety on the watcher loop's raw sqlite3 connection,
and subagent session detection/tagging.
"""

import asyncio
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

from jacked.web.database import Database
from jacked.data.hooks.session_account_tracker import (
    _detect_subagent,
    _tag_subagent,
)


def _make_db():
    """Create an in-memory Database for testing.

    >>> db = _make_db()
    >>> db is not None
    True
    """
    return Database(":memory:")


# ------------------------------------------------------------------
# record_session_account
# ------------------------------------------------------------------


def test_record_session_account_basic():
    """Record a session-account and retrieve it.

    >>> db = _make_db()
    >>> rid = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> rid > 0
    True
    """
    db = _make_db()
    rid = db.record_session_account(
        "s1",
        account_id=1,
        email="a@b.com",
        detection_method="session_start",
        repo_path="C:\\Github\\myrepo",
    )
    assert rid > 0

    rows = db.get_session_accounts("s1")
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["account_id"] == 1
    assert rows[0]["email"] == "a@b.com"
    assert rows[0]["detection_method"] == "session_start"
    assert rows[0]["repo_path"] == "C:\\Github\\myrepo"
    assert rows[0]["ended_at"] is None


def test_record_session_account_null_account():
    """Record with unknown account (null account_id/email).

    >>> db = _make_db()
    >>> rid = db.record_session_account("s1")
    >>> rid > 0
    True
    """
    db = _make_db()
    rid = db.record_session_account("s1", detection_method="session_start")
    assert rid > 0

    rows = db.get_session_accounts("s1")
    assert len(rows) == 1
    assert rows[0]["account_id"] is None
    assert rows[0]["email"] is None


def test_record_session_account_dedup():
    """Same session+account recorded twice creates only one open row.

    The second call refreshes last_activity_at instead of inserting a duplicate.

    >>> db = _make_db()
    >>> r1 = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> r1 > 0
    True
    """
    db = _make_db()
    r1 = db.record_session_account("s1", account_id=1, email="a@b.com")
    assert r1 > 0

    time.sleep(0.01)
    r2 = db.record_session_account("s1", account_id=1, email="a@b.com")
    assert r2 > 0

    rows = db.get_session_accounts("s1")
    assert len(rows) == 1
    assert rows[0]["ended_at"] is None


def test_record_multiple_sessions_same_account():
    """Multiple sessions can use the same account.

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> _ = db.record_session_account("s2", account_id=1, email="a@b.com")
    >>> len(db.get_account_sessions(1))
    2
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/repo/a")
    db.record_session_account("s2", account_id=1, email="a@b.com", repo_path="/repo/b")

    rows = db.get_account_sessions(1)
    assert len(rows) == 2
    paths = {r["repo_path"] for r in rows}
    assert paths == {"/repo/a", "/repo/b"}


# ------------------------------------------------------------------
# end_session_account
# ------------------------------------------------------------------


def test_end_session_account_basic():
    """End a session — sets ended_at on open records.

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> db.end_session_account("s1")
    True
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com")
    result = db.end_session_account("s1")
    assert result is True

    rows = db.get_session_accounts("s1")
    assert len(rows) == 1
    assert rows[0]["ended_at"] is not None


def test_end_session_account_nonexistent():
    """Ending a nonexistent session returns False.

    >>> db = _make_db()
    >>> db.end_session_account("nonexistent")
    False
    """
    db = _make_db()
    assert db.end_session_account("nonexistent") is False


def test_end_session_account_idempotent():
    """Ending an already-ended session returns False (no open records).

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> db.end_session_account("s1")
    True
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com")
    assert db.end_session_account("s1") is True
    assert db.end_session_account("s1") is False  # Already ended


def test_end_session_account_auth_reauth_flow():
    """Simulate auth_success flow: end previous, record new, end again.

    >>> # Verified via unit test
    """
    db = _make_db()

    # Initial session start
    db.record_session_account(
        "s1", account_id=1, email="old@b.com", detection_method="session_start"
    )

    # Auth success — end old record, create new one
    db.end_session_account("s1")
    time.sleep(0.01)
    db.record_session_account(
        "s1", account_id=2, email="new@b.com", detection_method="auth_success"
    )

    rows = db.get_session_accounts("s1")
    assert len(rows) == 2

    # First record (most recent — ORDER BY detected_at DESC) is open
    assert rows[0]["email"] == "new@b.com"
    assert rows[0]["ended_at"] is None

    # Second record (older) is ended
    assert rows[1]["email"] == "old@b.com"
    assert rows[1]["ended_at"] is not None

    # Now end the session entirely
    db.end_session_account("s1")
    rows = db.get_session_accounts("s1")
    assert all(r["ended_at"] is not None for r in rows)


# ------------------------------------------------------------------
# get_active_sessions
# ------------------------------------------------------------------


def test_get_active_sessions_empty():
    """No sessions means empty list.

    >>> db = _make_db()
    >>> db.get_active_sessions()
    []
    """
    db = _make_db()
    assert db.get_active_sessions() == []


def test_get_active_sessions_basic():
    """Active sessions are returned, ended ones are not.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/repo/a")
    db.record_session_account("s2", account_id=2, email="b@c.com", repo_path="/repo/b")

    # End s1
    db.end_session_account("s1")

    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["repo_path"] == "/repo/b"


def test_get_active_sessions_includes_session_id():
    """session_id IS included in active session results for terminal tab identification.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/repo/a")

    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["session_id"] == "s1"


def test_get_active_sessions_multiple_accounts():
    """Active sessions across multiple accounts.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/repo/a")
    time.sleep(0.01)
    db.record_session_account("s2", account_id=1, email="a@b.com", repo_path="/repo/b")
    time.sleep(0.01)
    db.record_session_account("s3", account_id=2, email="c@d.com", repo_path="/repo/c")

    active = db.get_active_sessions()
    assert len(active) == 3

    # Group by account_id (same as the API does)
    by_account = {}
    for row in active:
        aid = row["account_id"]
        by_account.setdefault(aid, []).append(row)

    assert len(by_account[1]) == 2
    assert len(by_account[2]) == 1


def test_get_active_sessions_stale_cutoff():
    """Sessions with last_activity_at older than 60 min are filtered out.

    Uses COALESCE(last_activity_at, detected_at) > now - 60 minutes.

    >>> # Verified via unit test
    """
    db = _make_db()

    # Insert a session with timestamps 75 minutes ago (stale)
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=75)).isoformat()
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO session_accounts
               (session_id, account_id, email, detected_at, last_activity_at,
                detection_method, repo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "stale-sess",
                1,
                "a@b.com",
                stale_ts,
                stale_ts,
                "session_start",
                "/repo/stale",
            ),
        )

    # Insert a fresh session (within 60 min)
    db.record_session_account(
        "fresh-sess", account_id=2, email="b@c.com", repo_path="/repo/fresh"
    )

    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["repo_path"] == "/repo/fresh"


def test_get_active_sessions_near_boundary():
    """Session 59 min old is still returned (near-boundary positive case).

    Guards against format regressions in the cutoff comparison.

    >>> # Verified via unit test
    """
    db = _make_db()

    # Insert a session 59 minutes ago — just inside the 60-min window
    near_ts = (datetime.now(timezone.utc) - timedelta(minutes=59)).isoformat()
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO session_accounts
               (session_id, account_id, email, detected_at, last_activity_at,
                detection_method, repo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                "near-sess",
                1,
                "a@b.com",
                near_ts,
                near_ts,
                "session_start",
                "/repo/near",
            ),
        )

    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["repo_path"] == "/repo/near"


def test_get_active_sessions_null_account_included():
    """Sessions with null account_id are still returned by the DB query.

    The API layer filters them out, but the DB method should return them.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_session_account("s1", account_id=None, email=None, repo_path="/repo/a")

    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["account_id"] is None


# ------------------------------------------------------------------
# get_account_sessions
# ------------------------------------------------------------------


def test_get_account_sessions_limit():
    """Limit parameter caps the number of returned rows.

    >>> # Verified via unit test
    """
    db = _make_db()
    for i in range(5):
        time.sleep(0.01)
        db.record_session_account(f"s{i}", account_id=1, email="a@b.com")

    assert len(db.get_account_sessions(1, limit=3)) == 3
    assert len(db.get_account_sessions(1, limit=50)) == 5


def test_get_account_sessions_nonexistent():
    """Querying sessions for a nonexistent account returns empty list.

    >>> db = _make_db()
    >>> db.get_account_sessions(999)
    []
    """
    db = _make_db()
    assert db.get_account_sessions(999) == []


# ------------------------------------------------------------------
# check_same_thread safety (regression guard for the watcher loop)
# ------------------------------------------------------------------


def test_sqlite_check_same_thread_false():
    """Raw sqlite3 connection with check_same_thread=False works across threads.

    This guards against regression of the watcher loop bug where asyncio.to_thread()
    dispatches queries to a different thread than the one that created the connection.

    >>> # Verified via unit test — exercises cross-thread sqlite3 access
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT)")
    conn.execute("INSERT INTO t VALUES (1, 'hello')")
    conn.commit()

    async def _query_in_thread():
        return await asyncio.to_thread(
            lambda: conn.execute("SELECT val FROM t WHERE id = 1").fetchone()[0]
        )

    result = asyncio.new_event_loop().run_until_complete(_query_in_thread())
    assert result == "hello"
    conn.close()


def test_sqlite_pragma_data_version_in_thread():
    """PRAGMA data_version can be read via asyncio.to_thread on a cross-thread connection.

    This is the exact pattern used by _session_accounts_watch_loop.

    >>> # Verified via unit test
    """
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL")

    async def _read_data_version():
        return await asyncio.to_thread(
            lambda: conn.execute("PRAGMA data_version").fetchone()[0]
        )

    dv = asyncio.new_event_loop().run_until_complete(_read_data_version())
    assert isinstance(dv, int)
    conn.close()


# ------------------------------------------------------------------
# heartbeat_session
# ------------------------------------------------------------------


def test_heartbeat_session_basic():
    """Heartbeat updates last_activity_at for an active session.

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> db.heartbeat_session("s1")
    True
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com")

    # Record the initial last_activity_at
    rows = db.get_session_accounts("s1")
    initial_ts = rows[0].get("last_activity_at") or rows[0]["detected_at"]

    time.sleep(0.01)
    result = db.heartbeat_session("s1")
    assert result is True

    # Verify last_activity_at was updated
    with db._reader() as conn:
        row = conn.execute(
            "SELECT last_activity_at FROM session_accounts WHERE session_id = ?",
            ("s1",),
        ).fetchone()
        assert row[0] is not None
        assert row[0] > initial_ts


def test_heartbeat_session_nonexistent():
    """Heartbeat on nonexistent session returns False.

    >>> db = _make_db()
    >>> db.heartbeat_session("nonexistent")
    False
    """
    db = _make_db()
    assert db.heartbeat_session("nonexistent") is False


def test_heartbeat_session_ended_noop():
    """Heartbeat does nothing on an ended session.

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> db.end_session_account("s1")
    True
    >>> db.heartbeat_session("s1")
    False
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com")
    db.end_session_account("s1")
    assert db.heartbeat_session("s1") is False


# ------------------------------------------------------------------
# Session resurrection (idle → active via heartbeat)
# ------------------------------------------------------------------


def test_session_resurrection():
    """A stale session reappears after heartbeat updates last_activity_at.

    This is the core resurrection behavior: sessions fade from the
    active list when idle but come back when activity resumes.

    >>> # Verified via unit test
    """
    db = _make_db()

    # Insert a session with stale timestamps (75 min ago)
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=75)).isoformat()
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO session_accounts
               (session_id, account_id, email, detected_at, last_activity_at,
                detection_method, repo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", 1, "a@b.com", stale_ts, stale_ts, "session_start", "/repo/a"),
        )

    # Session should NOT appear as active (stale)
    assert len(db.get_active_sessions()) == 0

    # Heartbeat resurrects it
    db.heartbeat_session("s1")

    # Session should now appear as active
    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["session_id"] == "s1"


def test_record_sets_last_activity_at():
    """record_session_account sets last_activity_at to detected_at.

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com")

    with db._reader() as conn:
        row = conn.execute(
            "SELECT detected_at, last_activity_at FROM session_accounts "
            "WHERE session_id = ?",
            ("s1",),
        ).fetchone()
        assert row[0] is not None
        assert row[1] is not None
        assert row[0] == row[1]  # last_activity_at == detected_at on insert


def test_migration_adds_last_activity_at():
    """Migration adds last_activity_at column to existing session_accounts table.

    Simulates an existing DB without the column, then reinitializes.

    >>> # Verified via unit test
    """
    # Create a DB and add a row WITHOUT last_activity_at
    db = Database(":memory:")

    # Manually drop the column by recreating the table without it
    with db._writer() as conn:
        conn.execute("DROP TABLE session_accounts")
        conn.execute("""
            CREATE TABLE session_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                account_id INTEGER,
                email TEXT,
                detected_at TEXT NOT NULL,
                ended_at TEXT,
                detection_method TEXT,
                repo_path TEXT,
                UNIQUE(session_id, detected_at)
            )
        """)
        conn.execute(
            """INSERT INTO session_accounts
               (session_id, account_id, email, detected_at, detection_method)
               VALUES ('s1', 1, 'a@b.com', '2025-01-01T00:00:00+00:00', 'test')"""
        )

    # Re-run init_schema which includes migration
    db._init_schema()

    # Verify column exists and pre-existing row has NULL last_activity_at
    with db._reader() as conn:
        row = conn.execute(
            "SELECT last_activity_at FROM session_accounts WHERE session_id = 's1'"
        ).fetchone()
        assert row is not None
        assert row[0] is None  # pre-existing rows have NULL

    # COALESCE falls back to detected_at for pre-existing rows
    active = db.get_active_sessions()
    # The row has detected_at from 2025 so it's definitely stale
    assert len(active) == 0


# ------------------------------------------------------------------
# Subagent detection — _detect_subagent()
# ------------------------------------------------------------------

_SUBAGENT_ENV_KEYS = [
    "CLAUDE_CODE_PARENT_SESSION_ID",
    "CLAUDE_CODE_AGENT_TYPE",
    "CLAUDE_CODE_AGENT_NAME",
]


def _clear_subagent_env():
    """Remove all subagent env vars for a clean test state.

    >>> _clear_subagent_env()  # no-op, just clears env
    """
    for k in _SUBAGENT_ENV_KEYS:
        os.environ.pop(k, None)


def test_detect_subagent_no_env():
    """No subagent env vars set — returns (False, None, None).

    >>> _clear_subagent_env()
    >>> _detect_subagent()
    (False, None, None)
    """
    _clear_subagent_env()
    is_sub, parent, atype = _detect_subagent()
    assert is_sub is False
    assert parent is None
    assert atype is None


def test_detect_subagent_parent_only():
    """Only CLAUDE_CODE_PARENT_SESSION_ID set.

    >>> _clear_subagent_env()
    >>> os.environ["CLAUDE_CODE_PARENT_SESSION_ID"] = "abc123"
    >>> _detect_subagent()
    (True, 'abc123', None)
    """
    _clear_subagent_env()
    os.environ["CLAUDE_CODE_PARENT_SESSION_ID"] = "abc123"
    try:
        is_sub, parent, atype = _detect_subagent()
        assert is_sub is True
        assert parent == "abc123"
        assert atype is None
    finally:
        _clear_subagent_env()


def test_detect_subagent_all_three():
    """All three env vars — type takes precedence over name.

    >>> _clear_subagent_env()
    >>> os.environ["CLAUDE_CODE_PARENT_SESSION_ID"] = "abc"
    >>> os.environ["CLAUDE_CODE_AGENT_TYPE"] = "Explore"
    >>> os.environ["CLAUDE_CODE_AGENT_NAME"] = "researcher"
    >>> _detect_subagent()
    (True, 'abc', 'Explore')
    """
    _clear_subagent_env()
    os.environ["CLAUDE_CODE_PARENT_SESSION_ID"] = "abc"
    os.environ["CLAUDE_CODE_AGENT_TYPE"] = "Explore"
    os.environ["CLAUDE_CODE_AGENT_NAME"] = "researcher"
    try:
        is_sub, parent, atype = _detect_subagent()
        assert is_sub is True
        assert parent == "abc"
        assert atype == "Explore"
    finally:
        _clear_subagent_env()


def test_detect_subagent_name_only():
    """Only CLAUDE_CODE_AGENT_NAME set (no parent, no type).

    >>> _clear_subagent_env()
    >>> os.environ["CLAUDE_CODE_AGENT_NAME"] = "my-agent"
    >>> _detect_subagent()
    (True, None, 'my-agent')
    """
    _clear_subagent_env()
    os.environ["CLAUDE_CODE_AGENT_NAME"] = "my-agent"
    try:
        is_sub, parent, atype = _detect_subagent()
        assert is_sub is True
        assert parent is None
        assert atype == "my-agent"
    finally:
        _clear_subagent_env()


def test_detect_subagent_empty_strings():
    """Empty string env vars are falsy — not a subagent.

    >>> _clear_subagent_env()
    >>> os.environ["CLAUDE_CODE_AGENT_TYPE"] = ""
    >>> _detect_subagent()
    (False, None, None)
    """
    _clear_subagent_env()
    os.environ["CLAUDE_CODE_AGENT_TYPE"] = ""
    try:
        is_sub, parent, atype = _detect_subagent()
        assert is_sub is False
    finally:
        _clear_subagent_env()


# ------------------------------------------------------------------
# Subagent tagging — _tag_subagent()
# ------------------------------------------------------------------


def test_tag_subagent_not_subagent():
    """Not a subagent (no env vars) — no UPDATE executed, no error.

    >>> _clear_subagent_env()
    >>> _tag_subagent("nonexistent", "2025-01-01T00:00:00Z")
    """
    _clear_subagent_env()
    _tag_subagent("nonexistent", "2025-01-01T00:00:00Z")


def test_tag_subagent_none_detected_at():
    """None detected_at — returns immediately.

    >>> _tag_subagent("any-session", None)
    """
    _tag_subagent("any-session", None)


def test_tag_subagent_db_not_exist():
    """DB doesn't exist — returns immediately, no file created.

    >>> import os
    >>> os.environ["CLAUDE_CODE_PARENT_SESSION_ID"] = "test-parent"
    >>> _tag_subagent("test-sess", "2025-01-01T00:00:00Z")
    """
    _clear_subagent_env()
    os.environ["CLAUDE_CODE_PARENT_SESSION_ID"] = "test-parent"
    try:
        _tag_subagent("test-sess", "2025-01-01T00:00:00Z")
    finally:
        _clear_subagent_env()


# ------------------------------------------------------------------
# Schema migration for subagent columns
# ------------------------------------------------------------------


def test_migration_adds_subagent_columns():
    """Migration adds is_subagent, parent_session_id, agent_type columns.

    >>> # Verified via unit test
    """
    db = Database(":memory:")

    # Recreate table without the subagent columns
    with db._writer() as conn:
        conn.execute("DROP TABLE session_accounts")
        conn.execute("""
            CREATE TABLE session_accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                account_id INTEGER,
                email TEXT,
                detected_at TEXT NOT NULL,
                ended_at TEXT,
                last_activity_at TEXT,
                detection_method TEXT,
                repo_path TEXT,
                UNIQUE(session_id, detected_at)
            )
        """)

    # Re-run init_schema which includes the migration
    db._init_schema()

    # Verify columns exist
    with db._reader() as conn:
        cursor = conn.execute("PRAGMA table_info(session_accounts)")
        cols = {row[1] for row in cursor.fetchall()}
        assert "is_subagent" in cols
        assert "parent_session_id" in cols
        assert "agent_type" in cols


def test_get_active_sessions_returns_subagent_fields():
    """get_active_sessions() includes is_subagent, parent_session_id, agent_type.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/repo/a")

    # Tag it as subagent directly via SQL
    with db._writer() as conn:
        conn.execute(
            "UPDATE session_accounts SET is_subagent = 1, parent_session_id = 'parent-abc', "
            "agent_type = 'Explore' WHERE session_id = 's1'"
        )

    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["is_subagent"] == 1
    assert active[0]["parent_session_id"] == "parent-abc"
    assert active[0]["agent_type"] == "Explore"


def test_get_active_sessions_default_subagent_values():
    """New sessions have is_subagent=0, null parent/agent fields.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/repo/a")

    active = db.get_active_sessions()
    assert len(active) == 1
    assert active[0]["is_subagent"] == 0
    assert active[0]["parent_session_id"] is None
    assert active[0]["agent_type"] is None


# ------------------------------------------------------------------
# Session-account dedup and stale cleanup
# ------------------------------------------------------------------


def test_record_session_account_switch_closes_old():
    """Recording a session with a new account closes the old account's record.

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> _ = db.record_session_account("s1", account_id=2, email="b@b.com")
    >>> rows = db.get_session_accounts("s1")
    >>> sum(1 for r in rows if r["ended_at"] is None)
    1
    """
    db = _make_db()
    db.record_session_account("s1", account_id=1, email="a@b.com")
    time.sleep(0.01)
    db.record_session_account("s1", account_id=2, email="b@b.com")

    rows = db.get_session_accounts("s1")
    assert len(rows) == 2

    # Only one open record (the new account)
    open_rows = [r for r in rows if r["ended_at"] is None]
    assert len(open_rows) == 1
    assert open_rows[0]["account_id"] == 2

    # Old account record is ended
    closed_rows = [r for r in rows if r["ended_at"] is not None]
    assert len(closed_rows) == 1
    assert closed_rows[0]["account_id"] == 1


def test_record_session_null_then_known_account():
    """Session starts with unknown account, then gets a real one.

    The NULL account record should be closed when a known account is recorded.

    >>> db = _make_db()
    >>> _ = db.record_session_account("s1", account_id=None, email=None)
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com")
    >>> rows = db.get_session_accounts("s1")
    >>> sum(1 for r in rows if r["ended_at"] is None)
    1
    """
    db = _make_db()
    db.record_session_account("s1", account_id=None, email=None)
    time.sleep(0.01)
    db.record_session_account("s1", account_id=1, email="a@b.com")

    rows = db.get_session_accounts("s1")
    open_rows = [r for r in rows if r["ended_at"] is None]
    assert len(open_rows) == 1
    assert open_rows[0]["account_id"] == 1


def test_get_active_sessions_dedup_safety_net():
    """GROUP BY dedup in get_active_sessions collapses legacy duplicate rows.

    Even if two open rows exist for the same session+account (legacy data),
    only one result is returned.

    >>> # Verified via unit test
    """
    db = _make_db()

    # Insert two open rows for the same session+account directly (bypassing dedup)
    ts1 = datetime.now(timezone.utc).isoformat()
    ts2 = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO session_accounts
               (session_id, account_id, email, detected_at, last_activity_at,
                detection_method, repo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", 1, "a@b.com", ts1, ts1, "test", "/repo/a"),
        )
        conn.execute(
            """INSERT INTO session_accounts
               (session_id, account_id, email, detected_at, last_activity_at,
                detection_method, repo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", 1, "a@b.com", ts2, ts2, "test", "/repo/a"),
        )

    active = db.get_active_sessions()
    # GROUP BY should collapse the two rows into one
    assert len(active) == 1
    assert active[0]["session_id"] == "s1"


def test_heartbeat_scopes_to_newest_record():
    """Heartbeat only updates the most recent open record, not stale ones.

    >>> # Verified via unit test
    """
    db = _make_db()

    # Insert two open rows for the same session but different accounts (legacy state)
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO session_accounts
               (session_id, account_id, email, detected_at, last_activity_at,
                detection_method, repo_path)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            ("s1", 1, "old@b.com", old_ts, old_ts, "test", "/repo/a"),
        )
    # Record a newer session for account 2
    db.record_session_account(
        "s1", account_id=2, email="new@b.com", repo_path="/repo/a"
    )

    time.sleep(0.01)
    db.heartbeat_session("s1")

    # Check that only the newest record got updated
    with db._reader() as conn:
        rows = conn.execute(
            """SELECT account_id, last_activity_at, detected_at
               FROM session_accounts
               WHERE session_id = 's1' AND ended_at IS NULL
               ORDER BY detected_at ASC"""
        ).fetchall()

    if len(rows) == 2:
        # If old row survived (wasn't ended by record_session_account's close-stale),
        # the heartbeat should only have updated the newer row
        old_row, new_row = rows[0], rows[1]
        assert old_row[1] == old_ts  # old row's last_activity_at unchanged
        assert new_row[1] > old_ts  # new row's last_activity_at was refreshed
    else:
        # If close-stale already cleaned up, only one row should exist
        assert len(rows) == 1
        assert rows[0][0] == 2  # account_id of the newer record
