"""Unit tests for paginated log list methods.

Covers list_gatekeeper_decisions, list_hook_executions, list_version_checks
with offset, total count, and server-side filters.
"""

import time
from jacked.web.database import Database


def _make_db():
    """Create an in-memory Database for testing.

    >>> db = _make_db()
    >>> db is not None
    True
    """
    return Database(":memory:")


# ------------------------------------------------------------------
# list_gatekeeper_decisions — pagination
# ------------------------------------------------------------------


def test_gatekeeper_empty():
    """Empty table returns zero rows and total.

    >>> db = _make_db()
    >>> db.list_gatekeeper_decisions()
    {'rows': [], 'total': 0}
    """
    db = _make_db()
    result = db.list_gatekeeper_decisions()
    assert result == {"rows": [], "total": 0}


def test_gatekeeper_basic_pagination():
    """Offset and limit slice correctly, total reflects full count.

    >>> # Verified via unit test
    """
    db = _make_db()
    for i in range(10):
        time.sleep(0.005)
        db.record_gatekeeper_decision(
            "ALLOW",
            command=f"cmd{i}",
            method="LOCAL",
            reason="ok",
            elapsed_ms=1.0,
            session_id="s1",
            repo_path="/repo",
        )

    # Full set
    r = db.list_gatekeeper_decisions(limit=100)
    assert r["total"] == 10
    assert len(r["rows"]) == 10

    # Page 1: first 3
    r = db.list_gatekeeper_decisions(limit=3, offset=0)
    assert r["total"] == 10
    assert len(r["rows"]) == 3

    # Page 2: next 3
    r2 = db.list_gatekeeper_decisions(limit=3, offset=3)
    assert r2["total"] == 10
    assert len(r2["rows"]) == 3
    # Pages shouldn't overlap
    ids_p1 = {row["id"] for row in r["rows"]}
    ids_p2 = {row["id"] for row in r2["rows"]}
    assert ids_p1.isdisjoint(ids_p2)


def test_gatekeeper_offset_past_end():
    """Offset past total returns empty rows but correct total.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_gatekeeper_decision(
        "ALLOW",
        command="hi",
        method="LOCAL",
        reason="ok",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/repo",
    )
    r = db.list_gatekeeper_decisions(limit=10, offset=999)
    assert r["total"] == 1
    assert len(r["rows"]) == 0


# ------------------------------------------------------------------
# list_gatekeeper_decisions — server-side filters
# ------------------------------------------------------------------


def test_gatekeeper_filter_decision():
    """Decision filter applied in SQL, total reflects filtered count.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_gatekeeper_decision(
        "ALLOW",
        command="a",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/r",
    )
    time.sleep(0.005)
    db.record_gatekeeper_decision(
        "ASK_USER",
        command="b",
        method="PATTERN",
        elapsed_ms=2.0,
        session_id="s1",
        repo_path="/r",
    )
    time.sleep(0.005)
    db.record_gatekeeper_decision(
        "ALLOW",
        command="c",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/r",
    )

    r = db.list_gatekeeper_decisions(filters={"decision": "ALLOW"})
    assert r["total"] == 2
    assert all(row["decision"] == "ALLOW" for row in r["rows"])

    r = db.list_gatekeeper_decisions(filters={"decision": "ASK_USER"})
    assert r["total"] == 1


def test_gatekeeper_filter_command_search():
    """Command search uses LIKE matching.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_gatekeeper_decision(
        "ALLOW",
        command="git push origin main",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/r",
    )
    time.sleep(0.005)
    db.record_gatekeeper_decision(
        "ALLOW",
        command="npm install",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/r",
    )

    r = db.list_gatekeeper_decisions(filters={"command_search": "git"})
    assert r["total"] == 1
    assert "git" in r["rows"][0]["command"]

    r = db.list_gatekeeper_decisions(filters={"command_search": "install"})
    assert r["total"] == 1


def test_gatekeeper_filter_repo_path():
    """Repo path filter is case-insensitive exact match.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_gatekeeper_decision(
        "ALLOW",
        command="a",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/Repo/A",
    )
    time.sleep(0.005)
    db.record_gatekeeper_decision(
        "ALLOW",
        command="b",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/repo/b",
    )

    r = db.list_gatekeeper_decisions(filters={"repo_path": "/repo/a"})
    assert r["total"] == 1

    r = db.list_gatekeeper_decisions(filters={"repo_path": "/repo/b"})
    assert r["total"] == 1


def test_gatekeeper_filter_session_id():
    """Session ID filter scopes results.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_gatekeeper_decision(
        "ALLOW",
        command="a",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s1",
        repo_path="/r",
    )
    time.sleep(0.005)
    db.record_gatekeeper_decision(
        "ALLOW",
        command="b",
        method="LOCAL",
        elapsed_ms=1.0,
        session_id="s2",
        repo_path="/r",
    )

    r = db.list_gatekeeper_decisions(filters={"session_id": "s1"})
    assert r["total"] == 1
    assert r["rows"][0]["session_id"] == "s1"


def test_gatekeeper_combined_filters_and_pagination():
    """Multiple filters + pagination work together.

    >>> # Verified via unit test
    """
    db = _make_db()
    for i in range(8):
        time.sleep(0.005)
        db.record_gatekeeper_decision(
            "ALLOW",
            command=f"git cmd{i}",
            method="LOCAL",
            elapsed_ms=1.0,
            session_id="s1",
            repo_path="/repo/a",
        )
    for i in range(4):
        time.sleep(0.005)
        db.record_gatekeeper_decision(
            "ASK_USER",
            command=f"rm cmd{i}",
            method="PATTERN",
            elapsed_ms=2.0,
            session_id="s1",
            repo_path="/repo/a",
        )

    # Filter to ALLOW + command_search "git" — should get 8
    r = db.list_gatekeeper_decisions(
        limit=3,
        offset=0,
        filters={"decision": "ALLOW", "command_search": "git"},
    )
    assert r["total"] == 8
    assert len(r["rows"]) == 3

    # Page 3 (offset 6) should get remaining 2
    r = db.list_gatekeeper_decisions(
        limit=3,
        offset=6,
        filters={"decision": "ALLOW", "command_search": "git"},
    )
    assert r["total"] == 8
    assert len(r["rows"]) == 2


# ------------------------------------------------------------------
# list_hook_executions — pagination
# ------------------------------------------------------------------


def test_hooks_empty():
    """Empty table returns zero rows and total.

    >>> db = _make_db()
    >>> db.list_hook_executions()
    {'rows': [], 'total': 0}
    """
    db = _make_db()
    assert db.list_hook_executions() == {"rows": [], "total": 0}


def test_hooks_pagination():
    """Offset and limit work correctly for hooks.

    >>> # Verified via unit test
    """
    db = _make_db()
    for i in range(6):
        time.sleep(0.005)
        db.record_hook_execution(
            hook_name="test_hook",
            hook_type="pre_tool_use",
            success=True,
            duration_ms=10.0,
            session_id=f"s{i}",
        )

    r = db.list_hook_executions(limit=2, offset=0)
    assert r["total"] == 6
    assert len(r["rows"]) == 2

    r = db.list_hook_executions(limit=2, offset=4)
    assert r["total"] == 6
    assert len(r["rows"]) == 2


def test_hooks_filter_hook_name():
    """Hook name filter scopes results and total.

    >>> # Verified via unit test
    """
    db = _make_db()
    db.record_hook_execution(
        hook_name="security_gatekeeper",
        hook_type="pre_tool_use",
        success=True,
        duration_ms=5.0,
    )
    time.sleep(0.005)
    db.record_hook_execution(
        hook_name="session_indexing",
        hook_type="post_tool_use",
        success=True,
        duration_ms=3.0,
    )

    r = db.list_hook_executions(hook_name="security_gatekeeper")
    assert r["total"] == 1
    assert r["rows"][0]["hook_name"] == "security_gatekeeper"


# ------------------------------------------------------------------
# list_version_checks — pagination
# ------------------------------------------------------------------


def test_version_checks_empty():
    """Empty table returns zero rows and total.

    >>> db = _make_db()
    >>> db.list_version_checks()
    {'rows': [], 'total': 0}
    """
    db = _make_db()
    assert db.list_version_checks() == {"rows": [], "total": 0}


def test_version_checks_pagination():
    """Offset and limit work correctly for version checks.

    >>> # Verified via unit test
    """
    db = _make_db()
    for i in range(5):
        time.sleep(0.005)
        db.record_version_check(
            current_version=f"0.{i}.0",
            latest_version="1.0.0",
            outdated=True,
            cache_hit=False,
        )

    r = db.list_version_checks(limit=2, offset=0)
    assert r["total"] == 5
    assert len(r["rows"]) == 2

    r = db.list_version_checks(limit=2, offset=4)
    assert r["total"] == 5
    assert len(r["rows"]) == 1
