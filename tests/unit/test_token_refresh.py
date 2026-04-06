"""Tests for circuit breaker DB columns and record_decision return ID.

Covers:
- Circuit breaker columns exist after migration
- Circuit breaker columns are writable via update_account
- Circuit breaker columns are clearable (set to None)
- record_decision returns integer ID
- record_decision returns incrementing IDs
"""

import sqlite3
import time
from pathlib import Path

import pytest

from jacked.web.database import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Database:
    """Create a Database with one account for testing."""
    db = Database(str(tmp_path / "test.db"))
    db.create_account(
        email="alice@test.com",
        access_token="alice_at",
        refresh_token="alice_rt",
        expires_at=int(time.time()) + 3600,
        scopes=None,
    )
    return db


# ---------------------------------------------------------------------------
# Circuit breaker columns exist after migration
# ---------------------------------------------------------------------------


class TestCircuitBreakerMigration:
    def test_columns_exist_in_schema(self, tmp_path):
        """refresh_last_failed_at and refresh_failure_type columns exist after DB init."""
        db = _make_db(tmp_path)
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        cursor = conn.execute("PRAGMA table_info(accounts)")
        col_names = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "refresh_last_failed_at" in col_names
        assert "refresh_failure_type" in col_names

    def test_columns_default_to_null(self, tmp_path):
        """New accounts have NULL circuit breaker columns by default."""
        db = _make_db(tmp_path)
        accounts = db.list_accounts()
        acct = accounts[0]
        assert acct["refresh_last_failed_at"] is None
        assert acct["refresh_failure_type"] is None


# ---------------------------------------------------------------------------
# Circuit breaker columns writable via update_account
# ---------------------------------------------------------------------------


class TestCircuitBreakerUpdate:
    def test_write_circuit_breaker_columns(self, tmp_path):
        """update_account can set refresh_last_failed_at and refresh_failure_type."""
        db = _make_db(tmp_path)
        now = int(time.time())
        db.update_account(
            1,
            refresh_last_failed_at=now,
            refresh_failure_type="invalid_grant",
        )
        accounts = db.list_accounts()
        acct = accounts[0]
        assert acct["refresh_last_failed_at"] == now
        assert acct["refresh_failure_type"] == "invalid_grant"

    def test_clear_circuit_breaker_columns(self, tmp_path):
        """Circuit breaker columns can be set back to None (cleared)."""
        db = _make_db(tmp_path)
        now = int(time.time())
        # Set them first
        db.update_account(
            1,
            refresh_last_failed_at=now,
            refresh_failure_type="invalid_grant",
        )
        # Clear them
        db.update_account(
            1,
            refresh_last_failed_at=None,
            refresh_failure_type=None,
        )
        accounts = db.list_accounts()
        acct = accounts[0]
        assert acct["refresh_last_failed_at"] is None
        assert acct["refresh_failure_type"] is None


# ---------------------------------------------------------------------------
# record_decision returns integer ID
# ---------------------------------------------------------------------------


class TestRecordDecisionReturnsId:
    def test_returns_integer(self, tmp_path):
        """record_decision returns an integer row ID."""
        db = _make_db(tmp_path)
        result = db.record_decision(
            account_id=1,
            action="stay",
            trigger="monitor",
            reason="usage low",
        )
        assert isinstance(result, int)

    def test_returns_incrementing_ids(self, tmp_path):
        """Successive record_decision calls return incrementing IDs."""
        db = _make_db(tmp_path)
        id1 = db.record_decision(
            account_id=1,
            action="stay",
            trigger="monitor",
            reason="first",
        )
        id2 = db.record_decision(
            account_id=1,
            action="swap",
            trigger="monitor",
            target_id=2,
            reason="second",
        )
        id3 = db.record_decision(
            account_id=1,
            action="manual_switch",
            trigger="user",
            reason="third",
        )
        assert id1 < id2 < id3
