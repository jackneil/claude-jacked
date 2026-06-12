"""Tests for multi-organization account support.

Covers:
- create_account with org uniqueness (email, organization_uuid)
- get_account_by_email ambiguity detection
- _sync_tokens_from_file org mismatch blocking
- CC flow org validation
- Migration idempotency + crash recovery
- Fresh DB with org data
- Re-auth un-deletes soft-deleted accounts
- build_oauth_data org field inclusion
- update_claude_config_email org field writes
- _seed_oauth_account org field seeding
"""

import json
import sqlite3
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from jacked.api.credential_helpers import (
    build_oauth_data,
    update_claude_config_email,
)
from jacked.web.database import Database


# Windows holds SQLite file locks
_WIN = __import__("os").name == "nt"


# ---------------------------------------------------------------------------
# create_account org uniqueness
# ---------------------------------------------------------------------------


class TestCreateAccountOrgUniqueness:
    """Tests 1-3 from plan: compound UNIQUE(email, organization_uuid)."""

    def test_same_email_different_org_creates_two_rows(self, tmp_path):
        """Test 1: Two accounts for same email, different orgs."""
        db = Database(str(tmp_path / "test.db"))
        try:
            a1 = db.create_account(
                email="jack@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="",
            )
            a2 = db.create_account(
                email="jack@x.com",
                access_token="at2",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
                organization_name="Acme",
            )
            assert a1["id"] != a2["id"]
            assert a1["organization_uuid"] == ""
            assert a2["organization_uuid"] == "org-123"
            assert a2["organization_name"] == "Acme"
        finally:
            db.close()

    def test_same_email_same_org_updates_existing(self, tmp_path):
        """Test 2: Re-auth same (email, org) updates, doesn't duplicate."""
        db = Database(str(tmp_path / "test.db"))
        try:
            a1 = db.create_account(
                email="jack@x.com",
                access_token="at_old",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
                organization_name="Acme",
            )
            a2 = db.create_account(
                email="jack@x.com",
                access_token="at_new",
                expires_at=int(time.time()) + 7200,
                organization_uuid="org-123",
                organization_name="Acme Updated",
            )
            assert a1["id"] == a2["id"]
            assert a2["access_token"] == "at_new"
        finally:
            db.close()

    def test_same_email_personal_sentinel_updates_existing(self, tmp_path):
        """Test 3: Re-auth same email with empty sentinel updates existing."""
        db = Database(str(tmp_path / "test.db"))
        try:
            a1 = db.create_account(
                email="jack@x.com",
                access_token="at_old",
                expires_at=int(time.time()) + 3600,
                organization_uuid="",
            )
            a2 = db.create_account(
                email="jack@x.com",
                access_token="at_new",
                expires_at=int(time.time()) + 7200,
                organization_uuid="",
            )
            assert a1["id"] == a2["id"]
            assert a2["access_token"] == "at_new"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# get_account_by_email ambiguity detection
# ---------------------------------------------------------------------------


class TestGetAccountByEmailAmbiguity:
    """Test 4: Multiple active orgs for same email raises ValueError."""

    def test_ambiguous_email_raises(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        try:
            db.create_account(
                email="jack@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="",
            )
            db.create_account(
                email="jack@x.com",
                access_token="at2",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
            )
            with pytest.raises(ValueError, match="Ambiguous"):
                db.get_account_by_email("jack@x.com")
        finally:
            db.close()

    def test_exact_org_match_not_ambiguous(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        try:
            db.create_account(
                email="jack@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="",
            )
            db.create_account(
                email="jack@x.com",
                access_token="at2",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
                organization_name="Acme",
            )
            result = db.get_account_by_email("jack@x.com", organization_uuid="org-123")
            assert result is not None
            assert result["organization_uuid"] == "org-123"
        finally:
            db.close()

    def test_single_account_not_ambiguous(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        try:
            db.create_account(
                email="solo@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
            )
            result = db.get_account_by_email("solo@x.com")
            assert result is not None
            assert result["email"] == "solo@x.com"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# _sync_tokens_from_file org mismatch
# ---------------------------------------------------------------------------


class TestSyncTokensOrgCheck:
    """Test 5: Token sync blocked on org UUID mismatch."""

    def test_org_mismatch_blocks_sync(self, tmp_path):
        """Sync blocked when file org doesn't match DB org."""
        from jacked.launch import _sync_tokens_from_file

        db = Database(str(tmp_path / "test.db"))
        try:
            acct = db.create_account(
                email="jack@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
            )
            acct_id = acct["id"]
            # Give it a CC token so sync has something to check
            db.update_account(acct_id, cc_access_token="old_cc_at")

            config_dir = tmp_path / "accounts" / str(acct_id)
            config_dir.mkdir(parents=True)

            # Write credential file with a token
            cred_path = config_dir / ".credentials.json"
            cred_path.write_text(json.dumps({
                "claudeAiOauth": {
                    "accessToken": "new_cc_at",
                    "refreshToken": "new_cc_rt",
                    "expiresAt": (int(time.time()) + 7200) * 1000,
                }
            }))

            # Write .claude.json with WRONG org
            claude_json = config_dir / ".claude.json"
            claude_json.write_text(json.dumps({
                "oauthAccount": {
                    "emailAddress": "jack@x.com",
                    "organizationUuid": "wrong-org-456",
                }
            }))

            _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

            # Token should NOT have been updated
            refreshed = db.get_account(acct_id)
            assert refreshed["cc_access_token"] == "old_cc_at"
        finally:
            db.close()

    def test_missing_org_on_org_account_blocks_sync(self, tmp_path):
        """Sync blocked when file has no org but DB account is org-scoped."""
        from jacked.launch import _sync_tokens_from_file

        db = Database(str(tmp_path / "test.db"))
        try:
            acct = db.create_account(
                email="jack@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
            )
            acct_id = acct["id"]
            db.update_account(acct_id, cc_access_token="old_cc_at")

            config_dir = tmp_path / "accounts" / str(acct_id)
            config_dir.mkdir(parents=True)

            cred_path = config_dir / ".credentials.json"
            cred_path.write_text(json.dumps({
                "claudeAiOauth": {
                    "accessToken": "new_cc_at",
                    "expiresAt": (int(time.time()) + 7200) * 1000,
                }
            }))

            # Write .claude.json WITHOUT organizationUuid
            claude_json = config_dir / ".claude.json"
            claude_json.write_text(json.dumps({
                "oauthAccount": {
                    "emailAddress": "jack@x.com",
                }
            }))

            _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

            refreshed = db.get_account(acct_id)
            assert refreshed["cc_access_token"] == "old_cc_at"
        finally:
            db.close()

    def test_personal_account_skips_org_check(self, tmp_path):
        """Sync proceeds for personal accounts (empty sentinel) without org in file."""
        from jacked.launch import _sync_tokens_from_file

        db = Database(str(tmp_path / "test.db"))
        try:
            acct = db.create_account(
                email="jack@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="",  # personal
            )
            acct_id = acct["id"]
            db.update_account(acct_id, cc_access_token="old_cc_at")

            config_dir = tmp_path / "accounts" / str(acct_id)
            config_dir.mkdir(parents=True)

            cred_path = config_dir / ".credentials.json"
            cred_path.write_text(json.dumps({
                "claudeAiOauth": {
                    "accessToken": "new_cc_at",
                    "refreshToken": "new_cc_rt",
                    "expiresAt": (int(time.time()) + 7200) * 1000,
                }
            }))

            claude_json = config_dir / ".claude.json"
            claude_json.write_text(json.dumps({
                "oauthAccount": {
                    "emailAddress": "jack@x.com",
                    # No organizationUuid — fine for personal accounts
                }
            }))

            _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

            refreshed = db.get_account(acct_id)
            assert refreshed["cc_access_token"] == "new_cc_at"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# CC flow org validation
# ---------------------------------------------------------------------------


class TestCCFlowOrgValidation:
    """Tests 6-7: CC OAuth org validation in _store_cc_tokens."""

    def _make_flow(self, tmp_path, target_account):
        """Create an OAuthFlow with a real DB and a target account."""
        from jacked.web.oauth import OAuthFlow

        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account(
            email=target_account["email"],
            access_token="at1",
            expires_at=int(time.time()) + 3600,
            organization_uuid=target_account.get("organization_uuid", ""),
        )
        flow = OAuthFlow.__new__(OAuthFlow)
        flow.db = db
        flow._target_account_id = acct["id"]
        return flow, db

    def test_cc_org_mismatch_raises(self, tmp_path):
        """Test 6: CC auth with different org than target raises ValueError."""
        flow, db = self._make_flow(tmp_path, {
            "email": "j@x.com",
            "organization_uuid": "org-123",
        })
        try:
            tokens = {
                "organization_uuid": "org-456",
                "email": "j@x.com",
                "access_token": "at",
                "expires_in": 3600,
            }
            with pytest.raises(ValueError, match="does not match"):
                flow._store_cc_tokens(tokens, {})
        finally:
            db.close()

    def test_cc_missing_org_on_org_account_raises(self, tmp_path):
        """Test 7: CC auth missing org for org-scoped target raises ValueError."""
        flow, db = self._make_flow(tmp_path, {
            "email": "j@x.com",
            "organization_uuid": "org-123",
        })
        try:
            tokens = {
                "organization_uuid": "",
                "email": "j@x.com",
                "access_token": "at",
                "expires_in": 3600,
            }
            with pytest.raises(ValueError, match="cannot verify"):
                flow._store_cc_tokens(tokens, {})
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Migration idempotency + crash recovery
# ---------------------------------------------------------------------------


class TestMigrationIdempotency:
    """Tests 8-9: Migration can run multiple times safely."""

    def test_init_schema_twice_preserves_data(self, tmp_path):
        """Test 8: Running _init_schema() twice doesn't lose data."""
        db = Database(str(tmp_path / "test.db"))
        try:
            acct = db.create_account(
                email="jack@x.com",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
                organization_name="Acme",
            )
            acct_id = acct["id"]

            # Re-run _init_schema (simulates restart)
            db._init_schema()

            recovered = db.get_account(acct_id)
            assert recovered is not None
            assert recovered["email"] == "jack@x.com"
            assert recovered["organization_uuid"] == "org-123"
        finally:
            db.close()

    def test_crash_recovery_accounts_new_orphan(self, tmp_path):
        """Test 9: If accounts_new exists from crashed migration, handles gracefully."""
        db_path = str(tmp_path / "test.db")
        db = Database(db_path)
        db.create_account(
            email="jack@x.com",
            access_token="at1",
            expires_at=int(time.time()) + 3600,
        )
        db.close()

        # Simulate a crash that left accounts_new behind
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE IF NOT EXISTS accounts_new (id INTEGER PRIMARY KEY)"
        )
        conn.commit()
        conn.close()

        # Re-open DB — should handle the orphaned table
        db2 = Database(db_path)
        try:
            acct = db2.get_account_by_email("jack@x.com")
            assert acct is not None
            assert acct["email"] == "jack@x.com"
        finally:
            db2.close()

    def test_crash_recovery_drop_before_rename(self, tmp_path):
        """Test 9b: Crash between DROP TABLE accounts and ALTER TABLE RENAME.

        accounts_new has the migrated data, accounts is gone.
        Recovery should rename accounts_new → accounts and preserve data.
        """
        db_path = str(tmp_path / "test.db")
        # Create the migrated-shape table as accounts_new with real data
        conn = sqlite3.connect(db_path)
        conn.execute("""CREATE TABLE accounts_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            organization_uuid TEXT DEFAULT '',
            organization_name TEXT,
            display_name TEXT,
            access_token TEXT NOT NULL,
            refresh_token TEXT,
            expires_at INTEGER NOT NULL,
            scopes TEXT,
            subscription_type TEXT,
            rate_limit_tier TEXT,
            has_extra_usage BOOLEAN DEFAULT FALSE,
            priority INTEGER DEFAULT 0,
            is_active BOOLEAN DEFAULT TRUE,
            is_deleted BOOLEAN DEFAULT FALSE,
            last_used_at TIMESTAMP,
            cached_usage_5h REAL,
            cached_usage_7d REAL,
            cached_5h_resets_at TEXT,
            cached_7d_resets_at TEXT,
            usage_cached_at INTEGER,
            cached_usage_raw TEXT,
            last_error TEXT,
            last_error_at TIMESTAMP,
            consecutive_failures INTEGER DEFAULT 0,
            last_validated_at INTEGER,
            validation_status TEXT DEFAULT 'unknown',
            cc_access_token TEXT,
            cc_refresh_token TEXT,
            cc_expires_at INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(email, organization_uuid)
        )""")
        conn.execute(
            "INSERT INTO accounts_new (email, organization_uuid, access_token, expires_at) "
            "VALUES (?, ?, ?, ?)",
            ("jack@x.com", "org-123", "at1", int(time.time()) + 3600),
        )
        conn.commit()
        conn.close()

        # Open DB — should recover by renaming accounts_new → accounts
        db = Database(db_path)
        try:
            acct = db.get_account_by_email("jack@x.com", organization_uuid="org-123")
            assert acct is not None
            assert acct["email"] == "jack@x.com"
            assert acct["organization_uuid"] == "org-123"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Fresh DB with org data
# ---------------------------------------------------------------------------


class TestFreshDBWithOrg:
    """Test 10: First auth with org data on empty DB creates account correctly."""

    def test_first_auth_with_org(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        try:
            acct = db.create_account(
                email="jack@hank.ai",
                access_token="at1",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-hank-uuid",
                organization_name="Acme",
            )
            assert acct["email"] == "jack@hank.ai"
            assert acct["organization_uuid"] == "org-hank-uuid"
            assert acct["organization_name"] == "Acme"
            assert acct["id"] > 0
        finally:
            db.close()


# ---------------------------------------------------------------------------
# Re-auth un-deletes soft-deleted accounts
# ---------------------------------------------------------------------------


class TestReauthUndeletes:
    """Test 11: Re-authing a deleted account's (email, org) un-deletes it."""

    def test_reauth_undeletes_preserving_id(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        try:
            acct = db.create_account(
                email="jack@x.com",
                access_token="at_old",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-123",
            )
            original_id = acct["id"]

            # Soft-delete
            db.delete_account(original_id)
            deleted = db.get_account(original_id)
            assert deleted is None  # get_account filters deleted

            # Re-auth same (email, org) — should un-delete
            reauthed = db.create_account(
                email="jack@x.com",
                access_token="at_new",
                expires_at=int(time.time()) + 7200,
                organization_uuid="org-123",
            )
            assert reauthed["id"] == original_id  # preserved
            assert reauthed["is_deleted"] == 0  # un-deleted
            assert reauthed["access_token"] == "at_new"
        finally:
            db.close()


# ---------------------------------------------------------------------------
# build_oauth_data org field
# ---------------------------------------------------------------------------


class TestBuildOauthDataOrg:
    """build_oauth_data includes organizationUuid from account."""

    def test_org_uuid_included(self):
        account = {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_at": 100,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
            "organization_uuid": "org-123",
        }
        result = build_oauth_data(account)
        assert result["organizationUuid"] == "org-123"

    def test_empty_sentinel_becomes_none(self):
        account = {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_at": 100,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
            "organization_uuid": "",
        }
        result = build_oauth_data(account)
        assert result["organizationUuid"] is None

    def test_cc_tokens_include_org(self):
        account = {
            "access_token": "at",
            "refresh_token": "rt",
            "expires_at": 100,
            "cc_access_token": "cc_at",
            "cc_refresh_token": "cc_rt",
            "cc_expires_at": 200,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
            "organization_uuid": "org-456",
        }
        result = build_oauth_data(account)
        assert result["accessToken"] == "cc_at"
        assert result["organizationUuid"] == "org-456"


# ---------------------------------------------------------------------------
# update_claude_config_email org fields
# ---------------------------------------------------------------------------


class TestUpdateClaudeConfigOrg:
    """update_claude_config_email writes/clears org fields."""

    def test_writes_org_fields(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
            tmp_path = Path(tmp)
            with mock.patch(
                "jacked.api.credential_helpers.Path.home", return_value=tmp_path
            ):
                update_claude_config_email(
                    "jack@x.com",
                    organization_uuid="org-123",
                    organization_name="Acme",
                )

            config = json.loads(
                (tmp_path / ".claude.json").read_text(encoding="utf-8")
            )
            assert config["oauthAccount"]["organizationUuid"] == "org-123"
            assert config["oauthAccount"]["organizationName"] == "Acme"

    def test_clears_org_for_personal(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
            tmp_path = Path(tmp)
            # Pre-seed with org data
            (tmp_path / ".claude.json").write_text(json.dumps({
                "oauthAccount": {
                    "emailAddress": "jack@x.com",
                    "organizationUuid": "old-org",
                    "organizationName": "Old Org",
                }
            }))

            with mock.patch(
                "jacked.api.credential_helpers.Path.home", return_value=tmp_path
            ):
                update_claude_config_email("jack@x.com")  # no org params

            config = json.loads(
                (tmp_path / ".claude.json").read_text(encoding="utf-8")
            )
            assert "organizationUuid" not in config["oauthAccount"]
            assert "organizationName" not in config["oauthAccount"]


# ---------------------------------------------------------------------------
# _seed_oauth_account org fields
# ---------------------------------------------------------------------------


class TestSeedOauthAccountOrg:
    """_seed_oauth_account writes org identity into per-account .claude.json."""

    def test_seeds_org_on_fresh_account(self, tmp_path):
        from jacked.launch import _seed_oauth_account

        config_dir = tmp_path / "acct"
        config_dir.mkdir()

        account = {
            "email": "jack@x.com",
            "organization_uuid": "org-123",
            "organization_name": "Acme",
        }
        _seed_oauth_account(config_dir, account)

        data = json.loads(
            (config_dir / ".claude.json").read_text(encoding="utf-8")
        )
        assert data["oauthAccount"]["emailAddress"] == "jack@x.com"
        assert data["oauthAccount"]["organizationUuid"] == "org-123"
        assert data["oauthAccount"]["organizationName"] == "Acme"

    def test_updates_org_on_existing_account(self, tmp_path):
        from jacked.launch import _seed_oauth_account

        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "jack@x.com",
                "organizationUuid": "old-org",
            }
        }))

        account = {
            "email": "jack@x.com",
            "organization_uuid": "new-org",
            "organization_name": "New Org",
        }
        _seed_oauth_account(config_dir, account)

        data = json.loads(
            (config_dir / ".claude.json").read_text(encoding="utf-8")
        )
        assert data["oauthAccount"]["organizationUuid"] == "new-org"
        assert data["oauthAccount"]["organizationName"] == "New Org"

    def test_clears_org_for_personal_account(self, tmp_path):
        from jacked.launch import _seed_oauth_account

        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "jack@x.com",
                "organizationUuid": "stale-org",
                "organizationName": "Stale",
            }
        }))

        account = {
            "email": "jack@x.com",
            "organization_uuid": "",  # personal
        }
        _seed_oauth_account(config_dir, account)

        data = json.loads(
            (config_dir / ".claude.json").read_text(encoding="utf-8")
        )
        assert "organizationUuid" not in data["oauthAccount"]
        assert "organizationName" not in data["oauthAccount"]


# ---------------------------------------------------------------------------
# Re-auth with wrong org redirects to correct account
# ---------------------------------------------------------------------------


class TestReauthWrongOrgRedirect:
    """When re-auth returns a different org that already has an active account,
    update THAT account's tokens instead of crashing with UNIQUE constraint."""

    def test_reauth_different_org_updates_matching_account(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        try:
            # Two accounts, same email, different orgs
            acct_personal = db.create_account(
                email="jack@test.com",
                access_token="at_personal",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-personal",
                organization_name="Personal",
            )
            acct_work = db.create_account(
                email="jack@test.com",
                access_token="at_work_old",
                expires_at=int(time.time()) + 3600,
                organization_uuid="org-work",
                organization_name="Work Inc",
            )

            personal_id = acct_personal["id"]
            work_id = acct_work["id"]

            # Simulate re-auth on personal account but user picks work org.
            # The _store_account flow should detect the mismatch and update
            # the work account instead of the personal one.
            from jacked.web.oauth import OAuthFlow
            flow = OAuthFlow(db, target_account_id=personal_id)

            # Mock the OAuth flow internals to simulate callback with work org
            tokens = {
                "access_token": "at_work_fresh",
                "refresh_token": "rt_work_fresh",
                "email": "jack@test.com",
                "organization_uuid": "org-work",
                "organization_name": "Work Inc",
            }
            profile = {
                "account": {"email_address": "jack@test.com", "display_name": "Jack"},
                "organization": {"organization_type": "max"},
            }
            usage = {}

            # This should NOT raise IntegrityError
            account = flow._store_account(tokens, profile, usage)

            # The WORK account should have been updated (not personal)
            assert account["id"] == work_id
            assert account["access_token"] == "at_work_fresh"

            # Personal account should be unchanged
            personal = db.get_account(personal_id)
            assert personal["access_token"] == "at_personal"
            assert personal["organization_uuid"] == "org-personal"
        finally:
            db.close()
