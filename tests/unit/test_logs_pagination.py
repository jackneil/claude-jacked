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
