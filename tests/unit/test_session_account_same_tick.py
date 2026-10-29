"""Two account detections for one session in the same clock tick (#194pr).

session_accounts is UNIQUE(session_id, detected_at) and inserts are
INSERT OR IGNORE: on a coarse clock (Windows, about 15.6 ms) the second
detection reused the first one's timestamp and was silently dropped right
after the first record was ended, leaving the session with no open record.
The Windows CI run of the module doctests caught it.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

from jacked.web.database import Database

FROZEN = datetime(2026, 10, 29, 5, 0, 0, tzinfo=timezone.utc)


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return FROZEN


def test_database_writer_keeps_the_second_detection_in_one_tick(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    with patch("jacked.web.database.datetime", _FrozenDatetime):
        db.record_session_account("sess-1", account_id=1, email="a@b.com", detection_method="x")
        db.record_session_account("sess-1", account_id=2, email="b@b.com", detection_method="x")
    rows = db.get_session_accounts("sess-1")
    open_rows = [r for r in rows if r["ended_at"] is None]
    assert len(rows) == 2
    assert [r["account_id"] for r in open_rows] == [2]


def test_hook_store_writer_keeps_the_second_detection_in_one_tick(tmp_path):
    from jacked import session_tracking_store as store

    db_path = tmp_path / "t.db"
    db = Database(str(db_path))
    with patch.object(store, "datetime", _FrozenDatetime):
        store.record_session(db_path, "sess-1", 1, "a@b.com", "x", None, None)
        store.record_session(db_path, "sess-1", 2, "b@b.com", "x", None, None)
    rows = db.get_session_accounts("sess-1")
    assert len(rows) == 2
    assert [r["account_id"] for r in rows if r["ended_at"] is None] == [2]
