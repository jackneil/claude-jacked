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
        _make_db(tmp_path)
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


# ---------------------------------------------------------------------------
# RefreshMode enum and cooldown constants
# ---------------------------------------------------------------------------


class TestRefreshMode:
    def test_refresh_mode_enum_values(self):
        from jacked.web.auth import RefreshMode
        assert RefreshMode.PRIMARY.value == "primary"
        assert RefreshMode.CC.value == "cc"
        assert RefreshMode.CC_OR_PRIMARY_429.value == "cc_429"
        assert RefreshMode.PRIMARY_CIRCUIT_BREAKER.value == "primary_cb"

    def test_cooldown_constants_exist(self):
        from jacked.web.auth import CIRCUIT_BREAKER_COOLDOWNS
        assert "invalid_grant" in CIRCUIT_BREAKER_COOLDOWNS
        assert CIRCUIT_BREAKER_COOLDOWNS["invalid_grant"] == 600
        assert CIRCUIT_BREAKER_COOLDOWNS["network_error"] == 60


# ---------------------------------------------------------------------------
# refresh_account_token: 2-strike invalid-marking policy
# ---------------------------------------------------------------------------


class TestRefreshAccountTokenPolicy:
    """refresh_account_token uses PRIMARY mode with 2-strike invalid policy."""

    def test_first_401_does_not_mark_invalid(self, tmp_path):
        """First 401 on token exchange sets circuit breaker but does NOT mark invalid."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        from jacked.web.auth import TokenExchangeResult, refresh_account_token

        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("test@t.com", "tok", 0, refresh_token="rt-test")

        mock_result = TokenExchangeResult(
            success=False, error="http_401", status_code=401)
        with patch("jacked.web.auth._refresh_token_flow",
                   new_callable=AsyncMock, return_value=mock_result):
            asyncio.run(refresh_account_token(acct["id"], db))

        row = db.get_account(acct["id"])
        assert row["validation_status"] != "invalid"

    def test_second_401_marks_invalid(self, tmp_path):
        """Second consecutive 401 (after first set failure_type) marks invalid."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        from jacked.web.auth import TokenExchangeResult, refresh_account_token

        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("test@t.com", "tok", 0, refresh_token="rt-test")
        # Simulate first failure already recorded
        db.update_account(acct["id"], refresh_failure_type="http_401")

        mock_result = TokenExchangeResult(
            success=False, error="http_401", status_code=401)
        with patch("jacked.web.auth._refresh_token_flow",
                   new_callable=AsyncMock, return_value=mock_result):
            asyncio.run(refresh_account_token(acct["id"], db))

        row = db.get_account(acct["id"])
        assert row["validation_status"] == "invalid"


# ---------------------------------------------------------------------------
# Heal loop fixes: circuit breaker clearing + no should_refresh gate
# ---------------------------------------------------------------------------


class TestHealLoop:
    def test_heal_clears_circuit_breaker(self, tmp_path):
        """heal_invalid_accounts clears CB state before attempting recovery."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        from jacked.web.auth import TokenExchangeResult, heal_invalid_accounts

        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("heal@test.com", "tok", int(time.time()) + 3600,
                                 refresh_token="rt-test")
        db.update_account(acct["id"],
                          validation_status="invalid",
                          refresh_last_failed_at=int(time.time()),
                          refresh_failure_type="invalid_grant")

        # Mock the token exchange to succeed; mock Database() to return our test db
        mock_result = TokenExchangeResult(success=True, access_token="new-at")
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._refresh_token_flow",
                   new_callable=AsyncMock, return_value=mock_result):
            result = asyncio.run(heal_invalid_accounts())

        assert result["healed"] == 1
        row = db.get_account(acct["id"])
        # Circuit breaker should be cleared
        assert row["refresh_last_failed_at"] is None
        assert row["refresh_failure_type"] is None

    def test_heal_attempts_refresh_even_if_not_expiring(self, tmp_path):
        """Heal loop should attempt refresh even if token hasn't expired (no should_refresh gate)."""
        import asyncio
        from unittest.mock import patch, AsyncMock
        from jacked.web.auth import TokenExchangeResult, heal_invalid_accounts

        db = Database(str(tmp_path / "test.db"))
        # Token expires in 1 hour - should_refresh would return False
        acct = db.create_account("heal@test.com", "tok", int(time.time()) + 3600,
                                 refresh_token="rt-test")
        db.update_account(acct["id"], validation_status="invalid")

        mock_flow = AsyncMock(
            return_value=TokenExchangeResult(success=True, access_token="new-at"))
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._refresh_token_flow", mock_flow):
            asyncio.run(heal_invalid_accounts())

        # Should have attempted a real exchange even though token isn't near expiry
        mock_flow.assert_called_once()


# ---------------------------------------------------------------------------
# Reconciliation safety guard: skip cc_refresh_token on invalid_grant
# ---------------------------------------------------------------------------


class TestReconciliationSafetyGuard:
    def setup_method(self):
        """Invalidate the live credential cache before each test."""
        from jacked.api.credential_helpers import invalidate_live_cred_cache
        invalidate_live_cred_cache()

    def test_skips_cc_refresh_token_when_invalid_grant(self, tmp_path):
        """reconcile_credentials_from_live_store must NOT import cc_refresh_token
        when circuit breaker failure type is invalid_grant."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("recon@test.com", "tok", int(time.time()) + 3600)
        db.update_account(acct["id"],
                          refresh_last_failed_at=int(time.time()),
                          refresh_failure_type="invalid_grant",
                          cc_access_token="old-access",
                          cc_refresh_token=None)

        from jacked.api.credential_helpers import reconcile_credentials_from_live_store
        from unittest.mock import patch

        live_creds = {
            "_jackedAccountId": acct["id"],
            "claudeAiOauth": {
                "accessToken": "new-access-from-cc",
                "refreshToken": "dangerous-refresh-token",
                "expiresAt": (int(time.time()) + 3600) * 1000,
            }
        }
        with patch("jacked.api.credential_helpers.read_platform_credentials",
                   return_value=live_creds):
            reconcile_credentials_from_live_store(acct["id"], db)

        row = db.get_account(acct["id"])
        assert row["cc_access_token"] == "new-access-from-cc"
        assert row["cc_refresh_token"] is None  # Must NOT be imported

    def test_imports_cc_refresh_token_when_no_invalid_grant(self, tmp_path):
        """When circuit breaker is not invalid_grant, cc_refresh_token IS imported."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("recon@test.com", "tok", int(time.time()) + 3600)
        db.update_account(acct["id"],
                          cc_access_token="old-access",
                          cc_refresh_token=None)  # No circuit breaker set

        from jacked.api.credential_helpers import reconcile_credentials_from_live_store
        from unittest.mock import patch

        live_creds = {
            "_jackedAccountId": acct["id"],
            "claudeAiOauth": {
                "accessToken": "new-access",
                "refreshToken": "new-refresh",
                "expiresAt": (int(time.time()) + 3600) * 1000,
            }
        }
        with patch("jacked.api.credential_helpers.read_platform_credentials",
                   return_value=live_creds):
            reconcile_credentials_from_live_store(acct["id"], db)

        row = db.get_account(acct["id"])
        assert row["cc_refresh_token"] == "new-refresh"  # Should be imported
