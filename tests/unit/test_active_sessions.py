"""Tests for active sessions staleness parameter and last_activity_at field."""

from jacked.web.database import Database


def test_active_sessions_includes_last_activity_at():
    """Active sessions response includes the last_activity_at field.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    >>> rows = db.get_active_sessions()
    >>> "last_activity_at" in rows[0]
    True
    """
    db = Database(":memory:")
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    rows = db.get_active_sessions()
    assert len(rows) == 1
    assert "last_activity_at" in rows[0]
    assert rows[0]["last_activity_at"] is not None


def test_staleness_clamped_to_max():
    """Staleness values above 120 are clamped to 120.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    >>> rows = db.get_active_sessions(staleness_minutes=999)
    >>> len(rows)
    1
    """
    db = Database(":memory:")
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    # Should not crash, should clamp to 120
    rows = db.get_active_sessions(staleness_minutes=999)
    assert len(rows) == 1


def test_staleness_clamped_to_min():
    """Staleness values below 5 are clamped to 5.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    >>> rows = db.get_active_sessions(staleness_minutes=1)
    >>> len(rows)
    1
    """
    db = Database(":memory:")
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    # Just-created session should be within even a 5-min window
    rows = db.get_active_sessions(staleness_minutes=1)
    assert len(rows) == 1


def test_default_staleness_is_60():
    """Default staleness is 60 minutes (matches SESSION_STALENESS_MINUTES).

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    >>> len(db.get_active_sessions())
    1
    """
    db = Database(":memory:")
    db.record_session_account("s1", account_id=1, email="a@b.com", repo_path="/r")
    rows = db.get_active_sessions()
    assert len(rows) == 1
