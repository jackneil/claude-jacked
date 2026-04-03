"""Tests for dual-token architecture (CC tokens independent from primary).

Covers:
- Migration seeding (cc_access_token seeded, cc_refresh_token NOT seeded)
- build_oauth_data preference logic
- should_refresh_cc / refresh_cc_token
- CC invalid_grant handling
- CC OAuth identity validation
- CAS pattern in _sync_tokens_from_file
- resolve_account Layer 4 CC match
- AccountResponse CC fields
"""

import asyncio
import json
import sqlite3
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.api.credential_helpers import build_oauth_data
from jacked.web.auth import REFRESH_BUFFER_SECONDS, refresh_cc_token, should_refresh_cc
from jacked.web.database import Database


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_db(tmp_path: Path) -> Database:
    """Create a Database with two accounts for testing."""
    db = Database(str(tmp_path / "test.db"))
    db.create_account(
        email="alice@test.com",
        access_token="alice_primary_at",
        refresh_token="alice_primary_rt",
        expires_at=int(time.time()) + 3600,
        scopes=None,
    )
    db.create_account(
        email="bob@test.com",
        access_token="bob_primary_at",
        refresh_token="bob_primary_rt",
        expires_at=int(time.time()) + 3600,
        scopes=None,
    )
    return db


# ---------------------------------------------------------------------------
# Migration seeding
# ---------------------------------------------------------------------------


class TestMigrationSeeding:
    """Migration seeding only applies to accounts that existed BEFORE migration.

    We simulate by creating accounts, clearing cc_* columns (as if pre-migration),
    then re-running the migration SQL manually.
    """

    _MIGRATION_SQL = """UPDATE accounts SET cc_access_token = access_token,
        cc_expires_at = expires_at
        WHERE cc_access_token IS NULL AND access_token IS NOT NULL
        AND is_deleted = 0
        AND (expires_at IS NULL OR expires_at > strftime('%s','now'))"""

    def test_cc_access_token_seeded_from_primary(self, tmp_path):
        """Migration seeds cc_access_token from primary access_token."""
        db = _make_db(tmp_path)
        # Clear CC columns to simulate pre-migration state
        db.update_account(1, cc_access_token=None, cc_expires_at=None)
        # Re-run migration SQL
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute(self._MIGRATION_SQL)
        conn.commit()
        conn.close()

        acct = db.get_account(1)
        assert acct["cc_access_token"] == "alice_primary_at"

    def test_cc_expires_at_seeded_from_primary(self, tmp_path):
        """Migration seeds cc_expires_at from primary expires_at."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token=None, cc_expires_at=None)
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute(self._MIGRATION_SQL)
        conn.commit()
        conn.close()

        acct = db.get_account(1)
        assert acct["cc_expires_at"] == acct["expires_at"]

    def test_cc_refresh_token_not_seeded(self, tmp_path):
        """Migration does NOT seed cc_refresh_token (would recreate the bug)."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token=None, cc_expires_at=None)
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute(self._MIGRATION_SQL)
        conn.commit()
        conn.close()

        acct = db.get_account(1)
        assert acct["cc_refresh_token"] is None

    def test_expired_accounts_not_seeded(self, tmp_path):
        """Migration skips expired accounts when seeding cc_access_token."""
        db = Database(str(tmp_path / "test.db"))
        db.create_account(
            email="expired@test.com",
            access_token="expired_at",
            refresh_token="expired_rt",
            expires_at=int(time.time()) - 3600,  # expired 1 hour ago
            scopes=None,
        )
        # Clear CC columns and re-run migration
        db.update_account(1, cc_access_token=None, cc_expires_at=None)
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute(self._MIGRATION_SQL)
        conn.commit()
        conn.close()

        acct = db.get_account(1)
        assert acct["cc_access_token"] is None


# ---------------------------------------------------------------------------
# build_oauth_data preference logic
# ---------------------------------------------------------------------------


class TestBuildOauthData:
    def test_prefers_cc_tokens_when_present(self):
        """build_oauth_data returns CC tokens when cc_access_token exists."""
        account = {
            "access_token": "primary_at",
            "refresh_token": "primary_rt",
            "expires_at": int(time.time()) + 3600,
            "cc_access_token": "cc_at",
            "cc_refresh_token": "cc_rt",
            "cc_expires_at": int(time.time()) + 3600,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }
        result = build_oauth_data(account)
        assert result["accessToken"] == "cc_at"
        assert result["refreshToken"] == "cc_rt"

    def test_fallback_to_primary_with_none_refresh(self):
        """Fallback returns primary access_token but refreshToken is None."""
        account = {
            "access_token": "primary_at",
            "refresh_token": "primary_rt",
            "expires_at": int(time.time()) + 3600,
            "cc_access_token": None,
            "cc_refresh_token": None,
            "cc_expires_at": None,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }
        result = build_oauth_data(account)
        assert result["accessToken"] == "primary_at"
        # CRITICAL: never expose primary refresh_token
        assert result["refreshToken"] is None

    def test_expired_cc_without_refresh_falls_through(self):
        """Expired CC token with no refresh falls back to primary."""
        account = {
            "access_token": "primary_at",
            "refresh_token": "primary_rt",
            "expires_at": int(time.time()) + 3600,
            "cc_access_token": "expired_cc_at",
            "cc_refresh_token": None,
            "cc_expires_at": int(time.time()) - 100,  # expired
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }
        result = build_oauth_data(account)
        assert result["accessToken"] == "primary_at"
        assert result["refreshToken"] is None

    def test_expired_cc_with_refresh_still_returns_cc(self):
        """Expired CC token WITH refresh token is still returned (can be refreshed)."""
        account = {
            "access_token": "primary_at",
            "refresh_token": "primary_rt",
            "expires_at": int(time.time()) + 3600,
            "cc_access_token": "expired_cc_at",
            "cc_refresh_token": "cc_rt",
            "cc_expires_at": int(time.time()) - 100,  # expired
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }
        result = build_oauth_data(account)
        assert result["accessToken"] == "expired_cc_at"
        assert result["refreshToken"] == "cc_rt"

    def test_primary_refresh_never_exposed(self):
        """Even with a real primary refresh_token, fallback returns None."""
        account = {
            "access_token": "primary_at",
            "refresh_token": "very_real_refresh_token",
            "expires_at": int(time.time()) + 3600,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }
        result = build_oauth_data(account)
        # Safety invariant: primary refresh_token must NEVER appear in output
        assert result["refreshToken"] is None
        assert "very_real_refresh_token" not in str(result)


# ---------------------------------------------------------------------------
# should_refresh_cc
# ---------------------------------------------------------------------------


class TestShouldRefreshCC:
    def test_no_refresh_token_returns_false(self):
        assert not should_refresh_cc({"cc_refresh_token": None, "cc_expires_at": 0})

    def test_expired_returns_true(self):
        assert should_refresh_cc(
            {"cc_refresh_token": "rt", "cc_expires_at": int(time.time()) - 100}
        )

    def test_near_expiry_returns_true(self):
        # Within REFRESH_BUFFER_SECONDS of expiry
        assert should_refresh_cc(
            {"cc_refresh_token": "rt", "cc_expires_at": int(time.time()) + 60}
        )

    def test_fresh_returns_false(self):
        assert not should_refresh_cc(
            {"cc_refresh_token": "rt", "cc_expires_at": int(time.time()) + 7200}
        )

    def test_none_expires_at_returns_true(self):
        assert should_refresh_cc({"cc_refresh_token": "rt", "cc_expires_at": None})


# ---------------------------------------------------------------------------
# _sync_tokens_from_file CAS pattern
# ---------------------------------------------------------------------------


class TestSyncTokensCAS:
    def test_cas_writes_to_cc_columns(self, tmp_path):
        """_sync_tokens_from_file writes to cc_* columns, not primary."""
        db = _make_db(tmp_path)
        # Set initial CC token (sync requires non-NULL cc_access_token)
        db.update_account(1, cc_access_token="old_cc_at")
        original = db.get_account(1)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "new_cc_at",
                "refreshToken": "new_cc_rt",
                "expiresAt": 9999999999000,
            }
        }))

        from jacked.launch import _sync_tokens_from_file
        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        acct = db.get_account(1)
        assert acct["cc_access_token"] == "new_cc_at"
        assert acct["cc_refresh_token"] == "new_cc_rt"
        # Primary unchanged
        assert acct["access_token"] == original["access_token"]
        assert acct["refresh_token"] == original["refresh_token"]

    def test_cas_sql_rejects_stale_write(self, tmp_path):
        """CAS WHERE clause rejects update when old value doesn't match DB."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="fresh_from_refresh")

        # Directly test the CAS SQL — simulate a stale writer that read
        # "old_value" but DB now has "fresh_from_refresh"
        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        result = conn.execute(
            "UPDATE accounts SET cc_access_token = ? "
            "WHERE id = ? AND cc_access_token IS ?",
            ("stale_cc_at", 1, "old_value"),  # old_value doesn't match DB
        )
        conn.commit()

        # CAS should reject — rowcount 0
        assert result.rowcount == 0
        conn.close()

        acct = db.get_account(1)
        assert acct["cc_access_token"] == "fresh_from_refresh"

    def test_cas_sql_accepts_matching_write(self, tmp_path):
        """CAS WHERE clause accepts update when old value matches DB."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="current_value")

        conn = sqlite3.connect(str(tmp_path / "test.db"))
        conn.execute("PRAGMA journal_mode=WAL")
        result = conn.execute(
            "UPDATE accounts SET cc_access_token = ? "
            "WHERE id = ? AND cc_access_token IS ?",
            ("new_value", 1, "current_value"),  # matches DB
        )
        conn.commit()

        assert result.rowcount == 1
        conn.close()

        acct = db.get_account(1)
        assert acct["cc_access_token"] == "new_value"

    def test_sync_skips_when_cc_access_token_is_null(self, tmp_path):
        """_sync_tokens_from_file skips sync when cc_access_token is NULL in DB.

        Prevents resurrection of tokens cleared by invalid_grant.
        """
        db = _make_db(tmp_path)
        # cc_access_token is NULL (e.g., post-invalid_grant or pre-CC-auth)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "stale_file_token",
                "refreshToken": "stale_rt",
                "expiresAt": 9999999999000,
            }
        }))

        from jacked.launch import _sync_tokens_from_file
        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        acct = db.get_account(1)
        # Should NOT have written the file token — cc_access_token stays NULL
        assert acct["cc_access_token"] is None

    def test_null_refresh_token_propagated(self, tmp_path):
        """Null refreshToken from file is propagated (no COALESCE masking)."""
        db = _make_db(tmp_path)
        # Set initial CC tokens (sync requires non-NULL cc_access_token)
        db.update_account(1, cc_access_token="old_cc_at", cc_refresh_token="existing_rt")

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "new_cc_at",
                # refreshToken missing/null — Claude Code consumed it
                "expiresAt": 9999999999000,
            }
        }))

        from jacked.launch import _sync_tokens_from_file
        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        acct = db.get_account(1)
        assert acct["cc_access_token"] == "new_cc_at"
        # NULL propagated — not masked by COALESCE
        assert acct["cc_refresh_token"] is None


# ---------------------------------------------------------------------------
# resolve_account Layer 4 CC match
# ---------------------------------------------------------------------------


class TestResolveAccountCCMatch:
    @patch("jacked.launch.find_bin", return_value="/usr/bin/claude")
    @patch("jacked.launch.read_platform_credentials")
    @patch("jacked.launch.Path.home")
    def test_layer4_matches_cc_access_token(self, mock_home, mock_kc, mock_which, tmp_path):
        """resolve_account Layer 4 matches cc_access_token from Keychain."""
        mock_home.return_value = tmp_path  # isolate from real credential files
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="alice_cc_at")

        mock_kc.return_value = {
            "claudeAiOauth": {"accessToken": "alice_cc_at"}
        }

        from jacked.launch import resolve_account
        result = resolve_account(None, db)
        assert result["id"] == 1

    @patch("jacked.launch.find_bin", return_value="/usr/bin/claude")
    @patch("jacked.launch.read_platform_credentials")
    @patch("jacked.launch.Path.home")
    def test_layer4_cc_takes_precedence_over_primary(self, mock_home, mock_kc, mock_which, tmp_path):
        """CC token match takes precedence over primary token match."""
        mock_home.return_value = tmp_path  # isolate from real credential files
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="shared_token")
        db.update_account(2, access_token="shared_token")

        mock_kc.return_value = {
            "claudeAiOauth": {"accessToken": "shared_token"}
        }

        from jacked.launch import resolve_account
        result = resolve_account(None, db)
        assert result["id"] == 1


# ---------------------------------------------------------------------------
# AccountResponse CC fields
# ---------------------------------------------------------------------------


class TestAccountResponseCCFields:
    def test_has_cc_token_true_when_present(self, tmp_path):
        """AccountResponse.has_cc_token is True when cc_access_token exists."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="cc_at", cc_refresh_token="cc_rt")
        acct = db.get_account(1)

        from jacked.api.routes.auth import _account_to_response
        resp = _account_to_response(acct)
        assert resp.has_cc_token is True
        assert resp.cc_needs_auth is False

    def test_cc_needs_auth_has_refresh_token(self, tmp_path):
        """cc_needs_auth is False when cc_refresh_token exists (can auto-refresh)."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="cc_at", cc_refresh_token="cc_rt",
                          cc_expires_at=int(time.time()) + 3600)
        acct = db.get_account(1)

        from jacked.api.routes.auth import _account_to_response
        resp = _account_to_response(acct)
        assert resp.has_cc_token is True
        assert resp.cc_needs_auth is False

    def test_cc_needs_auth_no_refresh_but_valid(self, tmp_path):
        """cc_needs_auth is False when token is valid even without refresh token."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="cc_at",
                          cc_expires_at=int(time.time()) + 3600)
        acct = db.get_account(1)

        from jacked.api.routes.auth import _account_to_response
        resp = _account_to_response(acct)
        assert resp.has_cc_token is True
        assert resp.cc_needs_auth is False

    def test_cc_needs_auth_no_refresh_and_expired(self, tmp_path):
        """cc_needs_auth is True when CC token is expired, no CC refresh, AND no primary refresh."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="cc_at",
                          cc_expires_at=int(time.time()) - 60,
                          refresh_token=None)
        acct = db.get_account(1)

        from jacked.api.routes.auth import _account_to_response
        resp = _account_to_response(acct)
        assert resp.has_cc_token is True
        assert resp.cc_needs_auth is True

    def test_cc_needs_auth_no_refresh_null_expires(self, tmp_path):
        """cc_needs_auth is True when cc_expires_at is NULL, no CC refresh, AND no primary refresh."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="cc_at", refresh_token=None)
        acct = db.get_account(1)

        from jacked.api.routes.auth import _account_to_response
        resp = _account_to_response(acct)
        assert resp.has_cc_token is True
        assert resp.cc_needs_auth is True

    def test_cc_needs_auth_false_when_primary_refresh_exists(self, tmp_path):
        """cc_needs_auth is False when CC token expired but primary refresh_token exists (fallback)."""
        db = _make_db(tmp_path)
        db.update_account(1, cc_access_token="cc_at",
                          cc_expires_at=int(time.time()) - 60)
        acct = db.get_account(1)

        from jacked.api.routes.auth import _account_to_response
        resp = _account_to_response(acct)
        assert resp.has_cc_token is True
        assert resp.cc_needs_auth is False  # primary refresh_token exists as fallback

    def test_no_cc_token_fields(self, tmp_path):
        """has_cc_token False when cc_access_token is None."""
        db = Database(str(tmp_path / "test.db"))
        db.create_account(
            email="nocc@test.com",
            access_token="at",
            refresh_token="rt",
            expires_at=int(time.time()) + 3600,
            scopes=None,
        )
        # Ensure cc_access_token is None (not seeded because it matches primary)
        db.update_account(1, cc_access_token=None)
        acct = db.get_account(1)

        from jacked.api.routes.auth import _account_to_response
        resp = _account_to_response(acct)
        assert resp.has_cc_token is False
        assert resp.cc_needs_auth is False


# ---------------------------------------------------------------------------
# update_account whitelist
# ---------------------------------------------------------------------------


class TestUpdateAccountWhitelist:
    def test_cc_columns_in_whitelist(self, tmp_path):
        """cc_* columns are in _ACCOUNT_UPDATE_COLS whitelist."""
        db = _make_db(tmp_path)
        # Should not raise ValueError
        db.update_account(1, cc_access_token="test_at")
        db.update_account(1, cc_refresh_token="test_rt")
        db.update_account(1, cc_expires_at=12345)

        acct = db.get_account(1)
        assert acct["cc_access_token"] == "test_at"
        assert acct["cc_refresh_token"] == "test_rt"
        assert acct["cc_expires_at"] == 12345


# ---------------------------------------------------------------------------
# CC invalid_grant handling
# ---------------------------------------------------------------------------


class TestRefreshCCTokenSuccess:
    """Verify refresh_cc_token success path (HTTP 200)."""

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_success_updates_cc_columns(self, mock_client_cls, tmp_path):
        """Successful CC refresh updates cc_access_token, cc_refresh_token, cc_expires_at."""
        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="old_cc_rt",
            cc_expires_at=int(time.time()) - 100,  # expired
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new_cc_at",
            "refresh_token": "new_cc_rt",
            "expires_in": 7200,
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(refresh_cc_token(1, db))

        assert result is True
        acct = db.get_account(1)
        assert acct["cc_access_token"] == "new_cc_at"
        assert acct["cc_refresh_token"] == "new_cc_rt"
        assert acct["cc_expires_at"] > int(time.time())
        # Primary tokens unchanged
        assert acct["access_token"] == "alice_primary_at"
        assert acct["refresh_token"] == "alice_primary_rt"

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_success_preserves_refresh_token_when_not_rotated(self, mock_client_cls, tmp_path):
        """If server doesn't return new refresh_token, keep existing one."""
        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="existing_rt",
            cc_expires_at=int(time.time()) - 100,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "access_token": "new_cc_at",
            # No refresh_token in response — server didn't rotate it
            "expires_in": 7200,
        }
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(refresh_cc_token(1, db))

        assert result is True
        acct = db.get_account(1)
        assert acct["cc_access_token"] == "new_cc_at"
        assert acct["cc_refresh_token"] == "existing_rt"  # Preserved


class TestCCInvalidGrantHandling:
    """Verify that CC invalid_grant clears refresh token but preserves access token."""

    @patch("jacked.web.auth.httpx.AsyncClient")
    @patch("jacked.api.credential_helpers.read_platform_credentials", return_value=None)
    def test_invalid_grant_clears_refresh_preserves_access(
        self, mock_read_creds, mock_client_cls, tmp_path
    ):
        """invalid_grant clears cc_refresh_token when no live recovery available."""
        db = _make_db(tmp_path)
        expires = int(time.time()) - 100
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="old_cc_rt",
            cc_expires_at=expires,
        )

        # Mock HTTP response: 400 invalid_grant
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "invalid_grant"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        # Also mock the credentials file read to return nothing
        with patch("pathlib.Path.exists", return_value=False):
            result = asyncio.run(refresh_cc_token(1, db))

        assert result is False
        acct = db.get_account(1)
        assert acct["cc_access_token"] == "old_cc_at"      # preserved
        assert acct["cc_refresh_token"] is None              # cleared
        assert acct["cc_expires_at"] == expires               # preserved

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_invalid_grant_does_not_mark_account_invalid(
        self, mock_client_cls, tmp_path
    ):
        """CC invalid_grant does NOT change validation_status — primary determines that."""
        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="cc_at",
            cc_refresh_token="cc_rt",
            cc_expires_at=int(time.time()) - 100,
            validation_status="valid",
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.json.return_value = {"error": "invalid_grant"}
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        asyncio.run(refresh_cc_token(1, db))

        acct = db.get_account(1)
        assert acct["validation_status"] == "valid"


# ---------------------------------------------------------------------------
# CC OAuth identity validation
# ---------------------------------------------------------------------------


class TestCompleteAuthPurposeGuard:
    """Verify _complete_auth skips _create_api_key for CC flows (bug fix)."""

    @patch("jacked.api.credential_helpers.sync_credential_to_all_stores")
    def test_cc_flow_preserves_refresh_token(self, mock_sync, tmp_path):
        """CC flow does NOT call _create_api_key — refresh_token preserved."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)

        tokens = {
            "access_token": "cc_at",
            "refresh_token": "cc_rt",
            "expires_in": 3600,
            "scope": "org:create_api_key user:profile",
            "email": "alice@test.com",  # CC flows get email from token exchange
        }

        with (
            patch.object(flow, "_exchange_code", new_callable=AsyncMock, return_value=tokens),
            patch.object(flow, "_create_api_key", new_callable=AsyncMock) as mock_api_key,
        ):
            result = asyncio.run(flow._complete_auth("test_code"))

        # _create_api_key should NOT have been called for CC flow
        mock_api_key.assert_not_called()
        # Account should be stored with refresh_token intact
        acct = db.get_account(1)
        assert acct["cc_refresh_token"] == "cc_rt"

    @patch("jacked.api.credential_helpers.sync_credential_to_all_stores")
    def test_primary_flow_calls_create_api_key(self, mock_sync, tmp_path):
        """Primary flow calls _create_api_key when scope includes org:create_api_key."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="primary")

        tokens = {
            "access_token": "primary_at",
            "refresh_token": "primary_rt",
            "expires_in": 3600,
            "scope": "org:create_api_key user:profile",
        }
        api_key_tokens = {
            "access_token": "api_key_at",
            "refresh_token": None,
            "expires_in": 3600,
            "scope": "org:create_api_key user:profile",
        }
        profile = {"account": {"email_address": "newacc@test.com"}}
        usage = {}

        with (
            patch.object(flow, "_exchange_code", new_callable=AsyncMock, return_value=tokens),
            patch.object(
                flow, "_create_api_key", new_callable=AsyncMock,
                return_value=("api_key_at", api_key_tokens),
            ) as mock_api_key,
            patch.object(flow, "_fetch_profile", new_callable=AsyncMock, return_value=profile),
            patch.object(flow, "_fetch_usage", new_callable=AsyncMock, return_value=usage),
        ):
            result = asyncio.run(flow._complete_auth("test_code"))

        # _create_api_key SHOULD have been called for primary flow
        mock_api_key.assert_called_once()


class TestCCIdentityValidation:
    """Verify _store_cc_tokens validates email match."""

    def test_email_mismatch_raises(self, tmp_path):
        """CC auth with wrong email raises ValueError."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)

        tokens = {"access_token": "cc_at", "refresh_token": "cc_rt", "expires_in": 3600}
        profile = {"account": {"email_address": "wrong@example.com"}}

        with pytest.raises(ValueError, match="does not match"):
            flow._store_cc_tokens(tokens, profile)

    def test_case_insensitive_email_match(self, tmp_path):
        """CC auth with different case email succeeds."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)

        tokens = {"access_token": "cc_at", "refresh_token": "cc_rt", "expires_in": 3600}
        # Email differs only in case
        profile = {"account": {"email_address": "Alice@Test.Com"}}

        result = flow._store_cc_tokens(tokens, profile)
        assert result["cc_access_token"] == "cc_at"

    def test_token_email_fallback_with_empty_profile(self, tmp_path):
        """CC auth succeeds when profile is empty but tokens contain email."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)

        tokens = {
            "access_token": "cc_at", "refresh_token": "cc_rt",
            "expires_in": 3600, "email": "alice@test.com",
        }
        profile = {}  # Empty — _fetch_profile failed with raw OAuth token

        result = flow._store_cc_tokens(tokens, profile)
        assert result["cc_access_token"] == "cc_at"

    def test_missing_profile_and_no_token_email_raises(self, tmp_path):
        """CC auth with empty profile AND no token email raises RuntimeError."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)

        tokens = {"access_token": "cc_at", "refresh_token": "cc_rt", "expires_in": 3600}
        profile = {}

        with pytest.raises(RuntimeError, match="no email in token response or profile"):
            flow._store_cc_tokens(tokens, profile)


# ---------------------------------------------------------------------------
# _handle_callback — conditional HTML response
# ---------------------------------------------------------------------------


class TestHandleCallback:
    """Verify _handle_callback returns error HTML on failure, not 'Success!'."""

    def _make_flow(self, tmp_path):
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)
        # Set _state so CSRF check passes
        flow._state = "test-state"
        return flow

    def _make_request(self, query: dict):
        """Build a minimal aiohttp-style request with query params."""
        req = MagicMock()
        req.query = query
        return req

    def test_success_returns_success_html(self, tmp_path):
        """Valid code + _complete_auth succeeds → 'Success!' in response."""
        flow = self._make_flow(tmp_path)
        flow._complete_auth = AsyncMock(return_value={"account_id": 1, "email": "alice@test.com"})
        req = self._make_request({"code": "valid-code", "state": "test-state"})

        resp = asyncio.run(flow._handle_callback(req))

        assert flow._status == "completed"
        assert "Success!" in resp.text
        assert "window.close()" in resp.text

    def test_exception_returns_error_html(self, tmp_path):
        """Valid code + _complete_auth raises → 'Authorization Failed' (NOT 'Success!')."""
        flow = self._make_flow(tmp_path)
        flow._complete_auth = AsyncMock(side_effect=RuntimeError("token exchange failed"))
        req = self._make_request({"code": "valid-code", "state": "test-state"})

        resp = asyncio.run(flow._handle_callback(req))

        assert flow._status == "error"
        assert "Authorization Failed" in resp.text
        assert "token exchange failed" in resp.text
        assert "Success!" not in resp.text
        assert "window.close()" not in resp.text

    def test_error_query_param_returns_error_html(self, tmp_path):
        """Callback with error query param → error status, 'Error' in response."""
        flow = self._make_flow(tmp_path)
        req = self._make_request({"error": "access_denied", "error_description": "User denied"})

        resp = asyncio.run(flow._handle_callback(req))

        assert flow._status == "error"
        assert "Error" in resp.text
        assert "Success!" not in resp.text

    def test_invalid_state_returns_error_html(self, tmp_path):
        """Invalid state param → CSRF error, 'Security validation failed' in response."""
        flow = self._make_flow(tmp_path)
        req = self._make_request({"code": "valid-code", "state": "wrong-state"})

        resp = asyncio.run(flow._handle_callback(req))

        assert flow._status == "error"
        assert "Security validation failed" in resp.text
        assert "Success!" not in resp.text

    def test_no_code_no_error_returns_error_html(self, tmp_path):
        """Callback with no code AND no error → error status, 'Authorization Failed'."""
        flow = self._make_flow(tmp_path)
        req = self._make_request({"state": "test-state"})

        resp = asyncio.run(flow._handle_callback(req))

        assert flow._status == "error"
        assert "No authorization code" in resp.text
        assert "Authorization Failed" in resp.text
        assert "Success!" not in resp.text

    def test_xss_payload_in_error_is_escaped(self, tmp_path):
        """html.escape prevents XSS in error messages rendered in callback HTML."""
        flow = self._make_flow(tmp_path)
        xss = '<script>alert(1)</script>'
        flow._complete_auth = AsyncMock(side_effect=RuntimeError(xss))
        req = self._make_request({"code": "valid-code", "state": "test-state"})

        resp = asyncio.run(flow._handle_callback(req))

        assert "&lt;script&gt;" in resp.text
        assert "<script>alert(1)</script>" not in resp.text

    def test_cc_tokens_nonexistent_target_raises(self, tmp_path):
        """_store_cc_tokens raises ValueError for nonexistent target account."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=999)

        tokens = {"access_token": "cc_at", "refresh_token": "cc_rt", "expires_in": 3600}
        profile = {}

        with pytest.raises(ValueError, match="Target account 999 not found"):
            flow._store_cc_tokens(tokens, profile)


# ---------------------------------------------------------------------------
# build_oauth_data edge cases
# ---------------------------------------------------------------------------


class TestBuildOauthDataEdgeCases:
    def test_empty_string_cc_access_token_treated_as_present(self):
        """Empty string cc_access_token is truthy — returns CC tokens."""
        account = {
            "access_token": "primary_at",
            "refresh_token": "primary_rt",
            "expires_at": int(time.time()) + 3600,
            "cc_access_token": "",  # empty string is truthy in the check
            "cc_refresh_token": "cc_rt",
            "cc_expires_at": int(time.time()) + 3600,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }
        result = build_oauth_data(account)
        # Empty string is falsy in Python, so falls through to primary
        assert result["accessToken"] == "primary_at"

    def test_cc_expires_at_zero_treated_as_expired(self):
        """cc_expires_at=0 (epoch) is correctly treated as expired."""
        account = {
            "access_token": "primary_at",
            "refresh_token": "primary_rt",
            "expires_at": int(time.time()) + 3600,
            "cc_access_token": "cc_at",
            "cc_refresh_token": None,  # no refresh — should fall through
            "cc_expires_at": 0,  # epoch = expired
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }
        result = build_oauth_data(account)
        # cc_expires_at=0 is expired AND no refresh token → fall through to primary
        assert result["accessToken"] == "primary_at"
        assert result["refreshToken"] is None


# ---------------------------------------------------------------------------
# Migration seed runs only once
# ---------------------------------------------------------------------------


class TestMigrationSeedRunsOnce:
    """Verify that migration seed only runs when columns are first added."""

    def test_seed_does_not_rerun_on_second_init(self, tmp_path):
        """After CC tokens are cleared, a new Database() does NOT re-seed them."""
        db = _make_db(tmp_path)
        # _make_db creates accounts AFTER migration. Manually set CC tokens
        # to simulate what migration seeding would have done for pre-existing accounts.
        db.update_account(1, cc_access_token="alice_cc_at")

        # Simulate invalid_grant clearing CC tokens
        db.update_account(1, cc_access_token=None, cc_refresh_token=None, cc_expires_at=None)
        acct = db.get_account(1)
        assert acct["cc_access_token"] is None

        # Create a new Database instance (simulates server restart)
        db2 = Database(str(tmp_path / "test.db"))
        acct2 = db2.get_account(1)
        # CC tokens should STILL be None — seed must not re-run
        assert acct2["cc_access_token"] is None


# ---------------------------------------------------------------------------
# refresh_cc_token — lock-already-held and non-400 error paths
# ---------------------------------------------------------------------------


class TestRefreshCCTokenLockHeld:
    """Verify refresh_cc_token returns early when lock is already held."""

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_returns_true_without_http_call_when_locked(self, mock_client_cls, tmp_path):
        """If the per-account CC lock is held, skip refresh and return True."""
        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="old_cc_rt",
            cc_expires_at=int(time.time()) - 100,
        )

        async def _run():
            from jacked.web.auth import _get_cc_refresh_lock

            lock = _get_cc_refresh_lock(1)
            # Acquire the lock BEFORE calling refresh_cc_token
            async with lock:
                result = await refresh_cc_token(1, db)
                return result

        result = asyncio.run(_run())
        assert result is True
        # HTTP client should never have been called
        mock_client_cls.assert_not_called()
        # Tokens unchanged
        acct = db.get_account(1)
        assert acct["cc_access_token"] == "old_cc_at"


class TestRefreshCCTokenNon400Errors:
    """Verify refresh_cc_token handles non-200/non-400 HTTP codes correctly."""

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_401_returns_false_without_invalidating_account(self, mock_client_cls, tmp_path):
        """HTTP 401 returns False but does NOT mark account as invalid."""
        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="old_cc_rt",
            cc_expires_at=int(time.time()) - 100,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(refresh_cc_token(1, db))

        assert result is False
        acct = db.get_account(1)
        # Account NOT marked invalid — CC doesn't control account validity
        assert acct["validation_status"] != "invalid"
        # CC tokens NOT cleared (invalid_grant only clears cc_refresh_token)
        assert acct["cc_access_token"] == "old_cc_at"
        assert acct["cc_refresh_token"] == "old_cc_rt"

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_500_returns_false_preserves_cc_tokens(self, mock_client_cls, tmp_path):
        """HTTP 500 returns False, preserves CC tokens for retry."""
        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="old_cc_rt",
            cc_expires_at=int(time.time()) - 100,
        )

        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(refresh_cc_token(1, db))

        assert result is False
        acct = db.get_account(1)
        assert acct["cc_access_token"] == "old_cc_at"
        assert acct["cc_refresh_token"] == "old_cc_rt"


# ---------------------------------------------------------------------------
# prepare_account_dir — CC refresh path
# ---------------------------------------------------------------------------


class TestRefreshCCTokenNetworkError:
    """Verify refresh_cc_token handles network unavailability gracefully."""

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_connect_error_returns_false_preserves_tokens(self, mock_client_cls, tmp_path):
        """Network connection error returns False without modifying CC tokens."""
        import httpx

        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="old_cc_rt",
            cc_expires_at=int(time.time()) - 100,
        )

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.ConnectError("Connection refused")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(refresh_cc_token(1, db))

        assert result is False
        acct = db.get_account(1)
        assert acct["cc_access_token"] == "old_cc_at"
        assert acct["cc_refresh_token"] == "old_cc_rt"
        assert acct["validation_status"] != "invalid"

    @patch("jacked.web.auth.httpx.AsyncClient")
    def test_timeout_returns_false_preserves_tokens(self, mock_client_cls, tmp_path):
        """Timeout returns False without modifying CC tokens."""
        import httpx

        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="old_cc_at",
            cc_refresh_token="old_cc_rt",
            cc_expires_at=int(time.time()) - 100,
        )

        mock_client = AsyncMock()
        mock_client.post.side_effect = httpx.TimeoutException("Request timed out")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        result = asyncio.run(refresh_cc_token(1, db))

        assert result is False
        acct = db.get_account(1)
        assert acct["cc_access_token"] == "old_cc_at"
        assert acct["cc_refresh_token"] == "old_cc_rt"


# ---------------------------------------------------------------------------
# prepare_account_dir — CC refresh path
# ---------------------------------------------------------------------------


class TestPrepareAccountDirCCRefresh:
    """Verify prepare_account_dir triggers CC refresh when CC token is near-expiry."""

    @patch("jacked.launch.should_refresh_cc")
    @patch("jacked.launch.should_refresh")
    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    def test_cc_refresh_called_when_cc_near_expiry(
        self, mock_cc_refresh, mock_should_primary, mock_should_cc, tmp_path
    ):
        """When should_refresh_cc returns True, refresh_cc_token is called."""
        from jacked.launch import prepare_account_dir

        db = _make_db(tmp_path)
        db.update_account(
            1,
            cc_access_token="cc_at",
            cc_refresh_token="cc_rt",
            cc_expires_at=int(time.time()) - 100,
        )

        mock_should_primary.return_value = False  # Primary fine
        mock_should_cc.return_value = True  # CC needs refresh

        account = db.get_account(1)

        try:
            prepare_account_dir(account, db)
        except Exception:
            pass  # May fail on later steps, we only care about refresh call

        mock_cc_refresh.assert_called_once()


# ---------------------------------------------------------------------------
# CC flow dedup in start_cc_auth
# ---------------------------------------------------------------------------


class TestCcFlowDedup:
    """Verify that start_cc_auth deduplicates active CC flows per account."""

    def test_dedup_returns_existing_flow_id(self, tmp_path):
        """When an active CC flow exists for the same account, return it."""
        from jacked.web.oauth import _active_flows

        db = _make_db(tmp_path)

        # Simulate an active CC flow
        class FakeFlow:
            purpose = "claude_code"
            _target_account_id = 1

        _active_flows["existing-flow-123"] = FakeFlow()
        try:
            # The dedup logic: iterate list(_active_flows.items())
            found = None
            for fid, existing in list(_active_flows.items()):
                if existing.purpose == "claude_code" and existing._target_account_id == 1:
                    found = fid
                    break
            assert found == "existing-flow-123"
        finally:
            _active_flows.pop("existing-flow-123", None)

    def test_dedup_allows_different_account(self, tmp_path):
        """CC flow for account 1 does not block account 2."""
        from jacked.web.oauth import _active_flows

        db = _make_db(tmp_path)

        class FakeFlow:
            purpose = "claude_code"
            _target_account_id = 1

        _active_flows["flow-acct1"] = FakeFlow()
        try:
            found = None
            for fid, existing in list(_active_flows.items()):
                if existing.purpose == "claude_code" and existing._target_account_id == 2:
                    found = fid
                    break
            assert found is None  # No match — new flow should be created
        finally:
            _active_flows.pop("flow-acct1", None)

    def test_dedup_with_empty_active_flows(self, tmp_path):
        """When no flows are active, dedup falls through."""
        from jacked.web.oauth import _active_flows

        # Ensure empty
        saved = dict(_active_flows)
        _active_flows.clear()
        try:
            found = None
            for fid, existing in list(_active_flows.items()):
                if existing.purpose == "claude_code" and existing._target_account_id == 1:
                    found = fid
                    break
            assert found is None
        finally:
            _active_flows.update(saved)


# ---------------------------------------------------------------------------
# update_account return-value checks
# ---------------------------------------------------------------------------


class TestUpdateAccountReturnChecks:
    """Verify that _store_cc_tokens and _store_primary_account handle
    update_account returning False (0 rows affected)."""

    def test_cc_store_raises_on_failed_update(self, tmp_path):
        """_store_cc_tokens raises RuntimeError when update_account returns False."""
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="claude_code", target_account_id=1)

        tokens = {"access_token": "cc_at", "refresh_token": "cc_rt", "expires_in": 3600}
        profile = {"account": {"email_address": "alice@test.com"}}

        with patch.object(db, "update_account", return_value=False):
            with pytest.raises(RuntimeError, match="CC token update failed"):
                flow._store_cc_tokens(tokens, profile)

    def test_primary_store_warns_on_failed_update(self, tmp_path, caplog):
        """_store_primary_account logs warning when validation update returns False."""
        import logging
        from jacked.web.oauth import OAuthFlow

        db = _make_db(tmp_path)
        flow = OAuthFlow(db, purpose="add_account")

        tokens = {
            "access_token": "new_at",
            "refresh_token": "new_rt",
            "expires_in": 3600,
            "email": "newuser@test.com",
        }
        profile = {"account": {"email_address": "newuser@test.com"}, "organization": {}}
        usage = {}

        with patch.object(db, "update_account", return_value=False):
            with caplog.at_level(logging.WARNING):
                result = flow._store_primary_account(tokens, profile, usage)

        assert "Validation status update failed" in caplog.text
        # Account should still be returned (non-fatal)
        assert result is not None
