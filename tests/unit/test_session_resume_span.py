"""A resumed session gets a new tracker span for its new process (CRED-3).

A Claude Code session killed without SessionEnd keeps its open row with pid
P1. ``claude --resume`` then runs the same session id in a new process P2.
Both writers once found the open row and only refreshed its activity, so the
row kept P1. The sweeper saw P1 dead, closed the row, and the account left
the holder set while P2 was alive and held the refresh token.

Now a record from a usable pid that differs from the open row's pid (or the
same pid with a different process token) ends that row and opens a new span
with the new pid, its token, and ``detected_at`` = now.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import jacked.session_ancestry as anc
from jacked import session_tracking_store as store
from jacked.web.database import Database


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "resume.db"))
    yield database
    database.close()


def _age(db, session_id, hours):
    seen = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db._writer() as conn:
        conn.execute(
            "UPDATE session_accounts SET detected_at = ?, last_activity_at = ? "
            "WHERE session_id = ? AND ended_at IS NULL",
            (seen, seen, session_id),
        )


def _rows(db, session_id):
    with db._reader() as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT id, pid, pid_token, detected_at, ended_at FROM session_accounts "
                "WHERE session_id = ? ORDER BY id", (session_id,),
            )
        ]


def _open(rows):
    return [r for r in rows if r["ended_at"] is None]


# ---------------------------------------------------------------------------
# Database.record_session_account
# ---------------------------------------------------------------------------


def test_db_resume_in_a_new_process_opens_a_new_span(db):
    db.record_session_account("s", account_id=4, detection_method="session_start",
                              pid=4100, pid_token="tok-a")
    _age(db, "s", 9)
    db.record_session_account("s", account_id=4, detection_method="session_start",
                              pid=5200, pid_token="tok-b")
    rows = _rows(db, "s")
    assert len(rows) == 2
    assert rows[0]["pid"] == 4100 and rows[0]["ended_at"] is not None
    (current,) = _open(rows)
    assert current["pid"] == 5200 and current["pid_token"] == "tok-b"
    fresh = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert current["detected_at"] > fresh


def test_db_same_process_refreshes_the_open_span(db):
    """SessionStart after /compact runs in the same process: one span."""
    first = db.record_session_account("s", account_id=4, pid=4100, pid_token="tok-a")
    again = db.record_session_account("s", account_id=4, pid=4100, pid_token="tok-a")
    assert again == first
    assert len(_rows(db, "s")) == 1


def test_db_same_pid_with_a_new_token_is_a_new_process(db):
    db.record_session_account("s", account_id=4, pid=4100, pid_token="tok-a")
    db.record_session_account("s", account_id=4, pid=4100, pid_token="tok-z")
    rows = _rows(db, "s")
    assert len(rows) == 2 and _open(rows)[0]["pid_token"] == "tok-z"


def test_db_a_missing_token_is_filled_not_rotated(db):
    """A row written before tokens existed gains the token of its own process."""
    db.record_session_account("s", account_id=4, pid=4100)
    db.record_session_account("s", account_id=4, pid=4100, pid_token="tok-a")
    rows = _rows(db, "s")
    assert len(rows) == 1 and rows[0]["pid_token"] == "tok-a"


@pytest.mark.parametrize("pid", [None, 0, 1, True])
def test_db_an_unusable_pid_never_rotates(db, pid):
    db.record_session_account("s", account_id=4, pid=4100, pid_token="tok-a")
    db.record_session_account("s", account_id=4, pid=pid)
    rows = _rows(db, "s")
    assert len(rows) == 1 and rows[0]["pid"] == 4100 and rows[0]["ended_at"] is None


def test_db_a_row_with_no_pid_takes_the_new_process(db):
    db.record_session_account("s", account_id=4)
    db.record_session_account("s", account_id=4, pid=5200, pid_token="tok-b")
    assert [r["pid"] for r in _open(_rows(db, "s"))] == [5200]


def test_resumed_session_stays_a_holder_after_the_old_process_dies(db, monkeypatch):
    """End to end: P1 dead, P2 alive. The sweeper's verdict keeps the session."""
    from jacked.api import credential_helpers as ch

    db.record_session_account("s", account_id=4, pid=4100, pid_token="tok-a")
    _age(db, "s", 9)
    db.record_session_account("s", account_id=4, pid=5200, pid_token="tok-b")
    _age(db, "s", 9)  # the resumed process is idle too
    monkeypatch.setattr(
        "jacked.service.process.process_liveness", {4100: False, 5200: True}.get
    )
    monkeypatch.setattr(anc, "process_tokens", lambda pids: {5200: "tok-b"})
    monkeypatch.setattr(anc, "process_starts", lambda pids: {})
    assert ch.live_holder_account_ids(db, {"claudeAiOauth": {}}) == {4}


# ---------------------------------------------------------------------------
# session_tracking_store.record_session (the hook's writer)
# ---------------------------------------------------------------------------


def _store_rows(path, session_id):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        return [
            dict(r) for r in conn.execute(
                "SELECT * FROM session_accounts WHERE session_id = ? ORDER BY id",
                (session_id,),
            )
        ]
    finally:
        conn.close()


def test_store_resume_in_a_new_process_opens_a_new_span(tmp_path):
    path = tmp_path / "jacked.db"
    Database(str(path)).close()
    store.record_session(path, "s", 4, "a@b.com", "session_start", None, 4100,
                         pid_token="tok-a")
    store.record_session(path, "s", 4, "a@b.com", "session_start", None, 5200,
                         pid_token="tok-b")
    rows = _store_rows(path, "s")
    assert [r["pid"] for r in rows] == [4100, 5200]
    assert rows[0]["ended_at"] is not None and rows[1]["ended_at"] is None
    assert rows[1]["pid_token"] == "tok-b"


def test_store_same_process_refreshes_the_open_span(tmp_path):
    path = tmp_path / "jacked.db"
    Database(str(path)).close()
    store.record_session(path, "s", 4, "a@b.com", "session_start", None, 4100,
                         pid_token="tok-a")
    store.record_session(path, "s", 4, "a@b.com", "session_start", None, 4100,
                         pid_token="tok-a")
    assert len(_store_rows(path, "s")) == 1


def test_store_writes_to_a_schema_without_the_new_columns(tmp_path):
    """The hook can run against a database that an older jacked created."""
    path = tmp_path / "old.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE session_accounts (
               id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL,
               account_id INTEGER, email TEXT, detected_at TEXT NOT NULL,
               ended_at TEXT, last_activity_at TEXT, detection_method TEXT,
               repo_path TEXT, pid INTEGER, UNIQUE(session_id, detected_at))"""
    )
    conn.commit()
    conn.close()
    assert store.record_session(path, "s", 4, "a@b.com", "x", None, 4100,
                                pid_token="tok-a", credential_scope="global")
    assert store.record_session(path, "s", 4, "a@b.com", "x", None, 5200,
                                pid_token="tok-b")
    rows = _store_rows(path, "s")
    assert [r["pid"] for r in rows] == [4100, 5200]
    assert rows[0]["ended_at"] is not None and rows[1]["ended_at"] is None


def test_the_hook_records_the_token_of_its_parent(monkeypatch):
    from jacked.data.hooks import session_account_tracker as hook

    seen = {}
    monkeypatch.setattr(hook, "process_token", lambda pid: f"tok-{pid}")
    monkeypatch.setattr(hook, "record_session", lambda *a, **k: seen.update(k, args=a))
    hook._record_session("s", 4, "a@b.com", "session_start", None, 4100)
    assert seen["pid_token"] == "tok-4100"
    hook._record_session("s", 4, "a@b.com", "session_start", None, 1)
    assert seen["pid_token"] is None
