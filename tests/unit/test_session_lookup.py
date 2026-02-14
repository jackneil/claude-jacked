"""Tests for session suffix lookup in Database."""

from jacked.web.database import Database


def test_suffix_too_short_returns_empty():
    """Suffixes under 8 chars are rejected server-side.

    >>> db = Database(":memory:")
    >>> db.lookup_session_by_suffix("short")
    []
    >>> db.lookup_session_by_suffix("1234567")
    []
    """
    db = Database(":memory:")
    assert db.lookup_session_by_suffix("abc") == []
    assert db.lookup_session_by_suffix("1234567") == []


def test_suffix_match_returns_rows():
    """Valid 8+ char suffix returns matching session records.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("sess-uuid-abcd1234", account_id=1, email="a@b.com", repo_path="/repo")
    >>> results = db.lookup_session_by_suffix("abcd1234")
    >>> len(results)
    1
    >>> results[0]["email"]
    'a@b.com'
    """
    db = Database(":memory:")
    db.record_session_account(
        "sess-uuid-abcd1234", account_id=1, email="a@b.com", repo_path="/repo"
    )
    results = db.lookup_session_by_suffix("abcd1234")
    assert len(results) == 1
    assert results[0]["email"] == "a@b.com"
    assert results[0]["session_id"] == "sess-uuid-abcd1234"


def test_no_match_returns_empty():
    """Suffix with no matching sessions returns empty list.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("sess-uuid-abcd1234", account_id=1, email="a@b.com", repo_path="/repo")
    >>> db.lookup_session_by_suffix("xxxxxxxx")
    []
    """
    db = Database(":memory:")
    db.record_session_account(
        "sess-uuid-abcd1234", account_id=1, email="a@b.com", repo_path="/repo"
    )
    assert db.lookup_session_by_suffix("xxxxxxxx") == []


def test_wildcard_percent_escaped():
    """Percent signs in suffix are escaped — no broad matching.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("sess-1234-abcd5678", account_id=1, email="a@b.com", repo_path="/r")
    >>> db.lookup_session_by_suffix("%%%%5678")
    []
    """
    db = Database(":memory:")
    db.record_session_account(
        "sess-1234-abcd5678", account_id=1, email="a@b.com", repo_path="/r"
    )
    # Should NOT match because % is escaped, not treated as wildcard
    results = db.lookup_session_by_suffix("%%%%5678")
    assert results == []


def test_wildcard_underscore_escaped():
    """Underscores in suffix are escaped — no single-char wildcard matching.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("sess-1234-abcd5678", account_id=1, email="a@b.com", repo_path="/r")
    >>> db.lookup_session_by_suffix("____5678")
    []
    """
    db = Database(":memory:")
    db.record_session_account(
        "sess-1234-abcd5678", account_id=1, email="a@b.com", repo_path="/r"
    )
    results = db.lookup_session_by_suffix("____5678")
    assert results == []


def test_backslash_escaped():
    r"""Backslashes in suffix are escaped — no escape-sequence injection.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("sess-1234-abcd5678", account_id=1, email="a@b.com", repo_path="/r")
    >>> db.lookup_session_by_suffix("\\\\\\\\5678")
    []
    """
    db = Database(":memory:")
    db.record_session_account(
        "sess-1234-abcd5678", account_id=1, email="a@b.com", repo_path="/r"
    )
    results = db.lookup_session_by_suffix("\\\\5678")
    assert results == []


def test_multi_match_respects_limit():
    """Multiple matches are capped at the limit parameter.

    >>> db = Database(":memory:")
    >>> for i in range(15):
    ...     _ = db.record_session_account(f"sess-{i:04d}-suffix99", account_id=1, email="a@b.com", repo_path="/r")
    >>> len(db.lookup_session_by_suffix("suffix99", limit=10))
    10
    """
    db = Database(":memory:")
    for i in range(15):
        db.record_session_account(
            f"sess-{i:04d}-suffix99", account_id=1, email="a@b.com", repo_path="/r"
        )
    results = db.lookup_session_by_suffix("suffix99", limit=10)
    assert len(results) == 10


def test_lookup_returns_last_activity_at():
    """Results include the coalesced last_activity_at field.

    >>> db = Database(":memory:")
    >>> _ = db.record_session_account("sess-uuid-abcd1234", account_id=1, email="a@b.com", repo_path="/r")
    >>> results = db.lookup_session_by_suffix("abcd1234")
    >>> "last_activity_at" in results[0]
    True
    """
    db = Database(":memory:")
    db.record_session_account(
        "sess-uuid-abcd1234", account_id=1, email="a@b.com", repo_path="/r"
    )
    results = db.lookup_session_by_suffix("abcd1234")
    assert "last_activity_at" in results[0]
    assert results[0]["last_activity_at"] is not None
