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
from unittest.mock import AsyncMock, MagicMock, patch

import json

import pytest

from jacked.web.database import Database


@pytest.fixture(autouse=True)
def _network_up():
    """The heal sweep probes the token host; no test reaches the network."""
    with patch("jacked.web.auth._network_reachable", AsyncMock(return_value=True)):
        yield


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

    def test_http_5xx_cooldown_matches_by_status_class(self):
        """An exchange records a 5xx as http_<status>; the http_5xx entry
        must still apply (it never matched by exact key before)."""
        from jacked.web.auth import (
            _DEFAULT_CB_COOLDOWN,
            CIRCUIT_BREAKER_COOLDOWNS,
            breaker_cooldown,
        )
        assert breaker_cooldown("http_502") == CIRCUIT_BREAKER_COOLDOWNS["http_5xx"]
        assert breaker_cooldown("http_503") == CIRCUIT_BREAKER_COOLDOWNS["http_5xx"]
        assert breaker_cooldown("http_429") == CIRCUIT_BREAKER_COOLDOWNS["http_429"]
        assert breaker_cooldown("http_418") == _DEFAULT_CB_COOLDOWN
        assert breaker_cooldown(None) == _DEFAULT_CB_COOLDOWN


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


def _owner_profile(email, org_uuid=None, account_uuid=None):
    """A completed profile lookup that names ``email`` as the token owner."""
    from jacked.api.authority_guard import ProfileLookup

    return ProfileLookup(
        200,
        {
            "account": {"email": email, "uuid": account_uuid},
            "organization": {"uuid": org_uuid},
        },
    )


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
                          cc_refresh_last_failed_at=int(time.time()),
                          cc_refresh_failure_type="invalid_grant",
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
                   return_value=live_creds), \
             patch("jacked.api.credential_helpers._ownership_lookup",
                   return_value=_owner_profile("recon@test.com")):
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
                   return_value=live_creds), \
             patch("jacked.api.credential_helpers._ownership_lookup",
                   return_value=_owner_profile("recon@test.com")):
            reconcile_credentials_from_live_store(acct["id"], db)

        row = db.get_account(acct["id"])
        assert row["cc_refresh_token"] == "new-refresh"  # Should be imported


class TestReconcileOwnershipProof:
    """A stamped payload is imported only when its owner is proven (#154)."""

    def setup_method(self):
        from jacked.api.credential_helpers import invalidate_live_cred_cache
        invalidate_live_cred_cache()

    def _account(self, tmp_path, org="org-a", account_uuid="uuid-a"):
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("owner@test.com", "tok", int(time.time()) + 3600)
        db.update_account(
            acct["id"],
            cc_access_token="old-access",
            cc_refresh_token="old-refresh",
            organization_uuid=org,
            account_uuid=account_uuid,
        )
        return db, acct["id"]

    def _live(self, account_id, access="new-access", refresh="new-refresh", expires_s=None):
        return {
            "_jackedAccountId": account_id,
            "claudeAiOauth": {
                "accessToken": access,
                "refreshToken": refresh,
                "expiresAt": (expires_s or int(time.time()) + 3600) * 1000,
            },
        }

    def _reconcile(self, db, account_id, live, lookup):
        from unittest.mock import patch

        from jacked.api.credential_helpers import reconcile_credentials_from_live_store

        with patch("jacked.api.credential_helpers.read_platform_credentials",
                   return_value=live), \
             patch("jacked.api.credential_helpers._ownership_lookup",
                   return_value=lookup) as spy:
            reconcile_credentials_from_live_store(account_id, db)
        return spy

    @staticmethod
    def _no_answer():
        from jacked.api.authority_guard import ProfileLookup

        return ProfileLookup(None)

    def _imported(self, db, aid):
        return db.get_account(aid)["cc_refresh_token"] == "new-refresh"

    def test_foreign_token_with_our_stamp_is_not_imported(self, tmp_path):
        """A /login to another account keeps our stamp; never import it."""
        db, aid = self._account(tmp_path)
        self._reconcile(db, aid, self._live(aid),
                        _owner_profile("other@test.com", "org-b", "uuid-b"))
        row = db.get_account(aid)
        assert row["cc_access_token"] == "old-access"
        assert row["cc_refresh_token"] == "old-refresh"

    def test_same_email_other_org_is_not_imported(self, tmp_path):
        db, aid = self._account(tmp_path)
        self._reconcile(db, aid, self._live(aid),
                        _owner_profile("owner@test.com", "org-b", "uuid-a"))
        assert not self._imported(db, aid)

    def test_only_email_differs_is_not_imported(self, tmp_path):
        db, aid = self._account(tmp_path)
        self._reconcile(db, aid, self._live(aid),
                        _owner_profile("other@test.com", "org-a", "uuid-a"))
        assert not self._imported(db, aid)

    def test_proven_rotation_is_imported(self, tmp_path):
        db, aid = self._account(tmp_path)
        self._reconcile(db, aid, self._live(aid),
                        _owner_profile("OWNER@test.com", " org-a ", "uuid-a"))
        row = db.get_account(aid)
        assert row["cc_access_token"] == "new-access"
        assert row["cc_refresh_token"] == "new-refresh"

    def test_personal_account_row_skips_the_org_check(self, tmp_path):
        db, aid = self._account(tmp_path, org="", account_uuid=None)
        self._reconcile(db, aid, self._live(aid),
                        _owner_profile("owner@test.com", "org-personal", "uuid-x"))
        assert self._imported(db, aid)

    def test_profile_without_account_uuid_still_proves_email_and_org(self, tmp_path):
        db, aid = self._account(tmp_path)
        self._reconcile(db, aid, self._live(aid),
                        _owner_profile("owner@test.com", "org-a", None))
        assert self._imported(db, aid)

    def test_lookup_failure_imports_nothing(self, tmp_path):
        db, aid = self._account(tmp_path)
        self._reconcile(db, aid, self._live(aid), self._no_answer())
        assert not self._imported(db, aid)

    def test_known_refresh_token_needs_no_lookup(self, tmp_path):
        """Lineage proves ownership: an access-token refresh is imported offline."""
        db, aid = self._account(tmp_path)
        live = self._live(aid, refresh="old-refresh")
        spy = self._reconcile(db, aid, live, _owner_profile("nobody@test.com"))
        spy.assert_not_called()
        assert db.get_account(aid)["cc_access_token"] == "new-access"

    def test_event_loop_caller_never_blocks_on_the_lookup(self, tmp_path):
        import asyncio

        db, aid = self._account(tmp_path)

        async def on_loop():
            return self._reconcile(db, aid, self._live(aid),
                                   _owner_profile("owner@test.com", "org-a", "uuid-a"))

        spy = asyncio.run(on_loop())
        spy.assert_not_called()
        assert not self._imported(db, aid)

    def test_worker_thread_caller_reaches_the_lookup(self, tmp_path):
        """The async callers use asyncio.to_thread; the lookup must run there."""
        import asyncio

        db, aid = self._account(tmp_path)

        async def via_thread():
            return await asyncio.to_thread(
                self._reconcile, db, aid, self._live(aid),
                _owner_profile("owner@test.com", "org-a", "uuid-a"),
            )

        spy = asyncio.run(via_thread())
        spy.assert_called_once()
        assert self._imported(db, aid)

    def test_expired_unknown_token_is_never_imported(self, tmp_path):
        """A stale session's refresh under our stamp, expired before reconcile."""
        db, aid = self._account(tmp_path)
        live = self._live(aid, expires_s=int(time.time()) - 60)
        spy = self._reconcile(db, aid, live, _owner_profile("owner@test.com", "org-a", "uuid-a"))
        spy.assert_not_called()
        assert not self._imported(db, aid)

    def test_settled_verdict_is_cached_per_token(self, tmp_path):
        """A rejected foreign token costs one profile request, not one per poll."""
        from jacked.api.credential_helpers import invalidate_live_cred_cache

        db, aid = self._account(tmp_path)
        foreign = _owner_profile("other@test.com", "org-b", "uuid-b")
        first = self._reconcile(db, aid, self._live(aid), foreign)
        invalidate_live_cred_cache()
        second = self._reconcile(db, aid, self._live(aid), foreign)
        first.assert_called_once()
        second.assert_not_called()
        assert not self._imported(db, aid)

    def test_failed_lookup_is_retried_later(self, tmp_path):
        from jacked.api import credential_helpers as ch

        db, aid = self._account(tmp_path)
        self._reconcile(db, aid, self._live(aid), self._no_answer())
        ch.invalidate_live_cred_cache()
        for key, (verdict, at) in list(ch._ownership_cache.items()):
            ch._ownership_cache[key] = (verdict, at - ch._OWNERSHIP_RETRY_SECONDS - 1)
        spy = self._reconcile(db, aid, self._live(aid),
                              _owner_profile("owner@test.com", "org-a", "uuid-a"))
        spy.assert_called_once()
        assert self._imported(db, aid)

    def test_older_live_payload_never_replaces_newer_tokens(self, tmp_path):
        db, aid = self._account(tmp_path)
        db.update_account(aid, cc_expires_at=int(time.time()) + 7200)
        live = self._live(aid, expires_s=int(time.time()) + 600)
        self._reconcile(db, aid, live, _owner_profile("owner@test.com", "org-a", "uuid-a"))
        assert not self._imported(db, aid)

    # #179i: expiry order is not evidence that a token works. When the row's
    # Claude Code token is KNOWN dead, an owned live token that merely expires
    # sooner replaces it.

    def _older_live_over_newer_row(self, tmp_path, **row_fields):
        db, aid = self._account(tmp_path)
        db.update_account(aid, cc_expires_at=int(time.time()) + 7200, **row_fields)
        live = self._live(aid, expires_s=int(time.time()) + 600)
        return db, aid, live

    def test_older_live_token_replaces_a_row_token_the_api_refused(self, tmp_path):
        """A 401 on the row's cc token (auth_rejected) makes it known dead."""
        db, aid, live = self._older_live_over_newer_row(
            tmp_path, cc_refresh_failure_type="auth_rejected",
            cc_refresh_last_failed_at=int(time.time()) - 60)
        self._reconcile(db, aid, live, _owner_profile("owner@test.com", "org-a", "uuid-a"))
        row = db.get_account(aid)
        assert row["cc_access_token"] == "new-access"
        assert row["cc_refresh_token"] == "new-refresh"
        assert row["cc_expires_at"] < int(time.time()) + 7200
        # The refusal belonged to the replaced token.
        assert row["cc_refresh_failure_type"] is None

    def test_older_live_token_replaces_a_row_token_the_keeper_gave_up_on(self, tmp_path):
        """The window keeper's needs_reauth verdict, recorded against the
        row's CURRENT tokens, makes the cc token known dead."""
        from jacked.api.usage_monitor import _candidate_fingerprint

        db, aid, live = self._older_live_over_newer_row(tmp_path)
        row = db.get_account(aid)
        db.set_keeper_state(aid, last_status="needs_reauth",
                            token_fp=_candidate_fingerprint(row["cc_access_token"], row["access_token"]))
        self._reconcile(db, aid, live, _owner_profile("owner@test.com", "org-a", "uuid-a"))
        assert db.get_account(aid)["cc_access_token"] == "new-access"

    def test_a_stale_keeper_verdict_does_not_count(self, tmp_path):
        """A needs_reauth recorded against OTHER tokens says nothing about the
        row's current ones: the backward rule holds."""
        db, aid, live = self._older_live_over_newer_row(tmp_path)
        db.set_keeper_state(aid, last_status="needs_reauth", token_fp="aaaa|bbbb")
        self._reconcile(db, aid, live, _owner_profile("owner@test.com", "org-a", "uuid-a"))
        assert db.get_account(aid)["cc_access_token"] == "old-access"

    def test_invalid_grant_alone_keeps_the_backward_rule(self, tmp_path):
        """invalid_grant marks the refresh LINEAGE dead; the access token beside
        it may be fresh (adopted by the authority guard, or recovered from the
        live store), so an older live payload must not replace it."""
        db, aid, live = self._older_live_over_newer_row(
            tmp_path, cc_refresh_failure_type="invalid_grant")
        self._reconcile(db, aid, live, _owner_profile("owner@test.com", "org-a", "uuid-a"))
        row = db.get_account(aid)
        assert row["cc_access_token"] == "old-access"
        assert row["cc_refresh_token"] == "old-refresh"

    def test_a_reauth_during_the_ownership_lookup_wins(self, tmp_path):
        """The ownership lookup can take seconds. A re-authentication that
        lands meanwhile (new tokens, verdict cleared) must not be overwritten
        by a decision made from the row as it was before."""
        from unittest.mock import patch

        from jacked.api.credential_helpers import reconcile_credentials_from_live_store

        db, aid, live = self._older_live_over_newer_row(
            tmp_path, cc_refresh_failure_type="auth_rejected")
        owner = _owner_profile("owner@test.com", "org-a", "uuid-a")

        def lookup_while_reauth_lands(*args, **kwargs):
            db.update_account(aid, cc_access_token="reauth-access", cc_refresh_token="reauth-refresh",
                              cc_expires_at=int(time.time()) + 28800, cc_refresh_failure_type=None)
            return owner

        with patch("jacked.api.credential_helpers.read_platform_credentials", return_value=live), \
             patch("jacked.api.credential_helpers._ownership_lookup", side_effect=lookup_while_reauth_lands):
            reconcile_credentials_from_live_store(aid, db)
        row = db.get_account(aid)
        assert (row["cc_access_token"], row["cc_refresh_token"]) == ("reauth-access", "reauth-refresh")

    def test_the_override_acts_on_a_fresh_read_never_the_cache(self, tmp_path):
        """A cached live payload (up to 30 s old) must not be what a dead-token
        verdict admits: the store is read again first."""
        from unittest.mock import patch

        from jacked.api import credential_helpers as ch

        db, aid, cached = self._older_live_over_newer_row(
            tmp_path, cc_refresh_failure_type="auth_rejected")
        ch._cache_live_credentials(aid, self._live(aid, access="stale-cached", refresh="stale-r",
                                                   expires_s=int(time.time()) + 300))
        fresh = self._live(aid, access="fresh-access", refresh="fresh-refresh",
                           expires_s=int(time.time()) + 900)
        with patch("jacked.api.credential_helpers.read_platform_credentials", return_value=fresh), \
             patch("jacked.api.credential_helpers._ownership_lookup",
                   return_value=_owner_profile("owner@test.com", "org-a", "uuid-a")):
            ch.reconcile_credentials_from_live_store(aid, db)
        assert db.get_account(aid)["cc_access_token"] == "fresh-access"

    def test_a_keeper_verdict_that_skipped_the_cc_token_says_nothing_about_it(self, tmp_path):
        """Under invalid_grant the keeper leaves the cc token out and records
        needs_reauth against the primary token alone: no verdict on cc."""
        from jacked.api.credential_helpers import cc_token_known_dead
        from jacked.api.usage_monitor import _candidate_fingerprint

        row = {"cc_access_token": "recovered", "access_token": "primary",
               "cc_refresh_failure_type": "invalid_grant",
               "keeper_state": json.dumps({"last_status": "needs_reauth",
                                           "token_fp": _candidate_fingerprint(None, "primary")})}
        assert cc_token_known_dead(row) is False

    def test_a_primary_reauth_during_the_lookup_also_stops_the_write(self, tmp_path):
        """The keeper fingerprint includes the primary token, so a primary-only
        re-auth that lands during the lookup changes the verdict's meaning."""
        from unittest.mock import patch

        from jacked.api.credential_helpers import reconcile_credentials_from_live_store
        from jacked.api.usage_monitor import _candidate_fingerprint

        db, aid, live = self._older_live_over_newer_row(tmp_path)
        row = db.get_account(aid)
        db.set_keeper_state(aid, last_status="needs_reauth",
                            token_fp=_candidate_fingerprint(row["cc_access_token"], row["access_token"]))
        owner = _owner_profile("owner@test.com", "org-a", "uuid-a")

        def lookup_while_primary_reauth_lands(*args, **kwargs):
            db.update_account(aid, access_token="new-primary")
            return owner

        with patch("jacked.api.credential_helpers.read_platform_credentials", return_value=live), \
             patch("jacked.api.credential_helpers._ownership_lookup",
                   side_effect=lookup_while_primary_reauth_lands):
            reconcile_credentials_from_live_store(aid, db)
        assert db.get_account(aid)["cc_access_token"] == "old-access"

    def test_a_keeper_bookkeeping_write_never_vetoes_a_rotation_import(self, tmp_path):
        """keeper_state is read only on the dead-token path. On a plain
        rotation a keeper write during the lookup must not stop the import, or
        the rotated refresh token is lost when the switch overwrites the store."""
        from unittest.mock import patch

        from jacked.api.credential_helpers import reconcile_credentials_from_live_store

        db, aid = self._account(tmp_path)
        live = self._live(aid)
        owner = _owner_profile("owner@test.com", "org-a", "uuid-a")

        def lookup_while_keeper_writes(*args, **kwargs):
            db.set_keeper_state(aid, last_status="ok", last_ping_at=1)
            db.update_account(aid, access_token="primary-refreshed")
            return owner

        with patch("jacked.api.credential_helpers.read_platform_credentials", return_value=live), \
             patch("jacked.api.credential_helpers._ownership_lookup",
                   side_effect=lookup_while_keeper_writes):
            reconcile_credentials_from_live_store(aid, db)
        assert self._imported(db, aid)

    @pytest.mark.parametrize("raw", ["[]", "not json", '"needs_reauth"', None])
    def test_a_malformed_keeper_state_is_no_verdict(self, raw):
        from jacked.api.credential_helpers import cc_token_known_dead

        assert cc_token_known_dead({"keeper_state": raw, "cc_access_token": "a"}) is False

    def test_a_dead_row_token_still_needs_the_ownership_proof(self, tmp_path):
        db, aid, live = self._older_live_over_newer_row(
            tmp_path, cc_refresh_failure_type="auth_rejected")
        self._reconcile(db, aid, live, _owner_profile("other@test.com", "org-b", "uuid-b"))
        assert db.get_account(aid)["cc_access_token"] == "old-access"


# ---------------------------------------------------------------------------
# B6: exchange outcome classification (not_sent vs reply_lost)
# ---------------------------------------------------------------------------


def _post_raising(exc):
    client = AsyncMock()
    client.post.side_effect = exc
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    return client


class TestExchangeClassification:
    @pytest.mark.parametrize(
        "exc_factory, expected",
        [
            (lambda h: h.ConnectError("dns failure"), "not_sent"),
            (lambda h: h.ConnectTimeout("connect"), "not_sent"),
            (lambda h: h.PoolTimeout("pool"), "not_sent"),
            (lambda h: h.ReadTimeout("read"), "reply_lost"),
            (lambda h: h.WriteTimeout("write"), "reply_lost"),
            (lambda h: h.RemoteProtocolError("eof"), "reply_lost"),
            (lambda h: RuntimeError("unknown"), "network_error"),
        ],
    )
    def test_exception_maps_to_a_breaker_type(self, exc_factory, expected):
        import asyncio

        import httpx

        from jacked.web.auth import _exchange_refresh_token

        with patch("jacked.web.auth.httpx.AsyncClient",
                   return_value=_post_raising(exc_factory(httpx))):
            result = asyncio.run(_exchange_refresh_token("rt"))
        assert result.success is False
        assert result.error == expected
        assert result.detail  # exception class name, never a token

    def test_unreadable_200_body_is_reply_lost(self):
        import asyncio

        from jacked.web.auth import _exchange_refresh_token

        resp = MagicMock()
        resp.status_code = 200
        resp.json.side_effect = ValueError("not json")
        client = AsyncMock()
        client.post.return_value = resp
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=False)
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(_exchange_refresh_token("rt"))
        assert result.error == "reply_lost"

    def test_reply_lost_has_the_longer_cooldown(self):
        from jacked.web.auth import CIRCUIT_BREAKER_COOLDOWNS

        assert CIRCUIT_BREAKER_COOLDOWNS["reply_lost"] > CIRCUIT_BREAKER_COOLDOWNS["not_sent"]
        assert CIRCUIT_BREAKER_COOLDOWNS["not_sent"] == 60

    def test_reply_lost_marks_the_primary_chain_suspect(self, tmp_path):
        import asyncio

        import httpx

        from jacked.web import auth
        from jacked.web.auth import RefreshMode, _refresh_token_flow

        auth.reset_locks()
        db = _make_db(tmp_path)
        with patch("jacked.web.auth.httpx.AsyncClient",
                   return_value=_post_raising(httpx.ReadTimeout("slow"))):
            result = asyncio.run(_refresh_token_flow(1, db, RefreshMode.PRIMARY))
        assert result.error == "reply_lost"
        row = db.get_account(1)
        assert row["refresh_failure_type"] == "reply_lost"
        assert row["refresh_last_failed_at"] is not None
        assert row["refresh_token"] == "alice_rt"


class TestHealRespectsNetworkBreaker:
    def _invalid_with_breaker(self, tmp_path, failure_type, failed_at):
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("heal@test.com", "tok", int(time.time()) + 3600,
                                 refresh_token="rt-test")
        db.update_account(acct["id"], validation_status="invalid",
                          refresh_failure_type=failure_type,
                          refresh_last_failed_at=failed_at)
        return db, acct["id"]

    @pytest.mark.parametrize("failure_type", ["not_sent", "reply_lost", "network_error"])
    def test_network_breaker_in_cooldown_is_not_cleared_or_forced(
        self, tmp_path, failure_type,
    ):
        import asyncio

        from jacked.web.auth import heal_invalid_accounts

        db, account_id = self._invalid_with_breaker(
            tmp_path, failure_type, int(time.time()) - 5)
        flow = AsyncMock()
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            result = asyncio.run(heal_invalid_accounts())

        flow.assert_not_awaited()
        assert result["deferred"] == 1
        row = db.get_account(account_id)
        assert row["refresh_failure_type"] == failure_type

    def test_unreachable_network_defers_the_forced_renewal(self, tmp_path):
        """2026-10-20: 76 forced renewals on account 1 while DNS was down."""
        import asyncio

        from jacked.web.auth import heal_invalid_accounts

        db, account_id = self._invalid_with_breaker(tmp_path, "invalid_grant",
                                                    int(time.time()) - 5)
        flow = AsyncMock()
        probe = AsyncMock(return_value=False)
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._refresh_token_flow", flow), \
             patch("jacked.web.auth._network_reachable", probe):
            result = asyncio.run(heal_invalid_accounts())

        flow.assert_not_awaited()
        probe.assert_awaited_once()
        assert result["deferred"] == 1
        assert db.get_account(account_id)["refresh_failure_type"] == "invalid_grant"

    def test_expired_network_breaker_heals_normally(self, tmp_path):
        import asyncio

        from jacked.web.auth import TokenExchangeResult, heal_invalid_accounts

        db, account_id = self._invalid_with_breaker(
            tmp_path, "not_sent", int(time.time()) - 3600)
        flow = AsyncMock(return_value=TokenExchangeResult(success=True, access_token="a"))
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            result = asyncio.run(heal_invalid_accounts())

        flow.assert_awaited_once()
        assert result["healed"] == 1

    def test_probe_runs_once_per_pass(self, tmp_path):
        import asyncio

        from jacked.web.auth import heal_invalid_accounts

        db = Database(str(tmp_path / "test.db"))
        for n in range(3):
            acct = db.create_account(f"h{n}@test.com", f"tok{n}",
                                     int(time.time()) + 3600, refresh_token=f"rt{n}")
            db.update_account(acct["id"], validation_status="invalid")
        probe = AsyncMock(return_value=False)
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._refresh_token_flow", AsyncMock()), \
             patch("jacked.web.auth._network_reachable", probe):
            result = asyncio.run(heal_invalid_accounts())
        assert probe.await_count == 1
        assert result["deferred"] == 3


# ---------------------------------------------------------------------------
# B7: honest refresh accounting
# ---------------------------------------------------------------------------


class TestHonestRefreshAccounting:
    def test_token_inside_loop_buffer_is_exchanged(self, tmp_path):
        """The loop passes its 4h buffer down: a token it selected is
        exchanged, not skipped by the 5-minute default window."""
        import asyncio

        from jacked.web import auth
        from jacked.web.auth import refresh_all_expiring_tokens

        auth.reset_locks()
        db = Database(str(tmp_path / "test.db"))
        db.create_account("two@test.com", "tok", int(time.time()) + 2 * 3600,
                          refresh_token="rt")
        from jacked.web.auth import TokenExchangeResult

        flow = AsyncMock(return_value=TokenExchangeResult(
            success=True, access_token="a", exchanged=True))
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.api.credential_helpers.read_active_account_id",
                   return_value=None), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            result = asyncio.run(refresh_all_expiring_tokens(buffer_seconds=14400))

        flow.assert_awaited_once()
        assert result["refreshed"] == 1
        assert result["skipped"] == 0
        assert result["failed"] == 0

    def test_token_outside_loop_buffer_is_skipped(self, tmp_path):
        """A token 6h out is outside the 4h buffer: no exchange, counted skipped."""
        import asyncio

        from jacked.web import auth
        from jacked.web.auth import refresh_all_expiring_tokens

        auth.reset_locks()
        db = Database(str(tmp_path / "test.db"))
        db.create_account("six@test.com", "tok", int(time.time()) + 6 * 3600,
                          refresh_token="rt")
        flow = AsyncMock()
        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.api.credential_helpers.read_active_account_id",
                   return_value=None), \
             patch("jacked.web.auth._refresh_token_flow", flow):
            result = asyncio.run(refresh_all_expiring_tokens(buffer_seconds=14400))

        flow.assert_not_awaited()
        assert result["refreshed"] == 0
        assert result["skipped"] == 1
        assert result["failed"] == 0

    def test_only_a_real_exchange_counts_as_refreshed(self, tmp_path):
        import asyncio

        from jacked.web import auth
        from jacked.web.auth import (
            RefreshOutcome,
            TokenExchangeResult,
            refresh_account_token_result,
        )

        auth.reset_locks()
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("due@test.com", "tok", 0, refresh_token="rt")
        exchanged = TokenExchangeResult(success=True, access_token="a", exchanged=True)
        already = TokenExchangeResult(success=True, access_token="a")
        with patch("jacked.web.auth._refresh_token_flow", AsyncMock(return_value=exchanged)):
            assert asyncio.run(refresh_account_token_result(acct["id"], db)) \
                is RefreshOutcome.EXCHANGED
        with patch("jacked.web.auth._refresh_token_flow", AsyncMock(return_value=already)):
            assert asyncio.run(refresh_account_token_result(acct["id"], db)) \
                is RefreshOutcome.SKIPPED_NOT_DUE
        failed = TokenExchangeResult(success=False, error="not_sent")
        with patch("jacked.web.auth._refresh_token_flow", AsyncMock(return_value=failed)):
            assert asyncio.run(refresh_account_token_result(acct["id"], db)) \
                is RefreshOutcome.FAILED

    def test_loop_log_reports_skipped_separately(self, caplog):
        import asyncio
        import logging

        from jacked.api import main

        results = iter([{"checked": 3, "refreshed": 1, "skipped": 2, "failed": 0,
                         "cc_refreshed": 0, "cc_failed": 0}])

        async def fake_refresh(buffer_seconds):
            return next(results)

        sleeps = {"n": 0}

        async def fake_sleep(_s):
            sleeps["n"] += 1
            if sleeps["n"] > 1:
                raise asyncio.CancelledError

        with patch("jacked.web.auth.refresh_all_expiring_tokens", fake_refresh), \
             patch.object(main.asyncio, "sleep", fake_sleep), \
             caplog.at_level(logging.INFO, logger=main.logger.name):
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(main._token_refresh_loop())

        assert any(
            "Token refresh: checked=3, refreshed=1, skipped=2, failed=0" in r.getMessage()
            for r in caplog.records
        )
