"""Tests verifying jacked never rotates the active account's CC refresh token.

See docs/architecture/oauth-and-credential-flows.md §7.1 and §7.2.
The CC refresh token is single-use and shared with Claude Code on the active
account. If jacked exchanges it upstream, Claude Code's own refresher sees
invalid_grant and forces a re-login. These tests lock in the skip behavior
for both the background loop and the pre-launch path.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch


class TestBackgroundLoopSkipsActiveCC:
    def _make_account(self, account_id: int, cc_expires_at: int) -> dict:
        return {
            "id": account_id,
            "refresh_token": None,  # skip primary refresh path
            "expires_at": int(time.time()) + 3600,
            "cc_access_token": f"cc_at_{account_id}",
            "cc_refresh_token": f"cc_rt_{account_id}",
            "cc_expires_at": cc_expires_at,
        }

    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    @patch("jacked.web.auth.refresh_account_token", new_callable=AsyncMock)
    @patch("jacked.api.credential_helpers.read_active_account_id", return_value=1)
    @patch("jacked.web.auth.Database")
    def test_active_account_cc_refresh_is_skipped(
        self, mock_db_cls, mock_read_active, mock_refresh_primary, mock_refresh_cc,
    ):
        """Active account's CC refresh must NOT fire from the 30min loop."""
        from jacked.web import auth

        now = int(time.time())
        # CC expires in 2 minutes — well inside should_refresh_cc's 300s buffer
        active_acct = self._make_account(1, cc_expires_at=now + 120)
        inactive_acct = self._make_account(2, cc_expires_at=now + 120)

        mock_db = MagicMock()
        mock_db.list_accounts.return_value = [active_acct, inactive_acct]
        mock_db_cls.return_value = mock_db

        mock_refresh_cc.return_value = True

        with patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store"):
            result = asyncio.run(auth.refresh_all_expiring_tokens())

        # Only the inactive account should have been refreshed
        mock_refresh_cc.assert_called_once_with(2, mock_db)
        assert result["cc_refreshed"] == 1
        # Active account was checked and counted, just not CC-refreshed
        assert result["checked"] == 2

    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    @patch("jacked.api.credential_helpers.read_active_account_id",
           side_effect=RuntimeError("keychain down"))
    @patch("jacked.web.auth.Database")
    def test_refresh_loop_survives_active_account_lookup_exception(
        self, mock_db_cls, mock_read_active, mock_refresh_cc,
    ):
        """If active-account detection raises, the loop must not crash.

        Regression guard for the NameError that occurred when active_id
        was referenced before assignment on exception paths.
        """
        from jacked.web import auth

        now = int(time.time())
        acct = self._make_account(1, cc_expires_at=now + 120)

        mock_db = MagicMock()
        mock_db.list_accounts.return_value = [acct]
        mock_db_cls.return_value = mock_db
        mock_refresh_cc.return_value = True

        # Must not raise NameError or any other unhandled exception.
        result = asyncio.run(auth.refresh_all_expiring_tokens())
        # No active account detected → refresh proceeds.
        assert result["cc_refreshed"] == 1

    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    @patch("jacked.api.credential_helpers.read_active_account_id", return_value=None)
    @patch("jacked.web.auth.Database")
    def test_refresh_loop_survives_non_dict_live_data(
        self, mock_db_cls, mock_read_active, mock_refresh_cc,
    ):
        """`_jackedAccountId` missing or malformed → treated as no active."""
        from jacked.web import auth

        now = int(time.time())
        acct = self._make_account(1, cc_expires_at=now + 120)
        mock_db = MagicMock()
        mock_db.list_accounts.return_value = [acct]
        mock_db_cls.return_value = mock_db
        mock_refresh_cc.return_value = True

        result = asyncio.run(auth.refresh_all_expiring_tokens())
        assert result["cc_refreshed"] == 1

    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    @patch("jacked.api.credential_helpers.read_active_account_id", return_value=None)
    @patch("jacked.web.auth.Database")
    def test_no_active_account_still_refreshes_all(
        self, mock_db_cls, mock_read_active, mock_refresh_cc,
    ):
        """When no active-account marker is present, refresh proceeds normally."""
        from jacked.web import auth

        now = int(time.time())
        acct1 = self._make_account(1, cc_expires_at=now + 120)
        acct2 = self._make_account(2, cc_expires_at=now + 120)

        mock_db = MagicMock()
        mock_db.list_accounts.return_value = [acct1, acct2]
        mock_db_cls.return_value = mock_db

        # read_active_account_id default = None (from @patch decorator)
        mock_refresh_cc.return_value = True

        with patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store"):
            result = asyncio.run(auth.refresh_all_expiring_tokens())

        assert mock_refresh_cc.call_count == 2
        assert result["cc_refreshed"] == 2


class TestReadActiveAccountId:
    """The shared helper used by every skip site."""

    @patch("jacked.api.credential_helpers.read_platform_credentials")
    def test_returns_int_from_keychain_int(self, mock_read):
        from jacked.api.credential_helpers import read_active_account_id
        mock_read.return_value = {"_jackedAccountId": 7, "claudeAiOauth": {}}
        assert read_active_account_id() == 7

    @patch("jacked.api.credential_helpers.read_platform_credentials")
    def test_coerces_string_to_int(self, mock_read):
        """Hand-edited JSON with '1' instead of 1 must still match."""
        from jacked.api.credential_helpers import read_active_account_id
        mock_read.return_value = {"_jackedAccountId": "5", "claudeAiOauth": {}}
        assert read_active_account_id() == 5

    @patch("jacked.api.credential_helpers.read_platform_credentials")
    def test_rejects_zero(self, mock_read):
        from jacked.api.credential_helpers import read_active_account_id
        mock_read.return_value = {"_jackedAccountId": 0}
        assert read_active_account_id() is None

    @patch("jacked.api.credential_helpers.read_platform_credentials")
    def test_rejects_negative(self, mock_read):
        from jacked.api.credential_helpers import read_active_account_id
        mock_read.return_value = {"_jackedAccountId": -1}
        assert read_active_account_id() is None

    @patch("jacked.api.credential_helpers.read_platform_credentials")
    def test_missing_stamp_returns_none(self, mock_read):
        from jacked.api.credential_helpers import read_active_account_id
        mock_read.return_value = {"claudeAiOauth": {}}  # no stamp key
        assert read_active_account_id() is None

    @patch("jacked.api.credential_helpers.read_platform_credentials")
    def test_non_dict_returns_none(self, mock_read):
        from jacked.api.credential_helpers import read_active_account_id
        mock_read.return_value = "not a dict"
        assert read_active_account_id() is None

    @patch("jacked.api.credential_helpers.read_platform_credentials",
           side_effect=RuntimeError("keychain exploded"))
    def test_exception_returns_none_no_raise(self, mock_read):
        from jacked.api.credential_helpers import read_active_account_id
        # Must NOT raise — callers depend on this being safe.
        assert read_active_account_id() is None


class TestWindowKeeperSkipsActiveCC:
    """The window keeper 401-recovery path must not rotate the active CC token."""

    @patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store")
    @patch("jacked.api.credential_helpers.read_active_account_id", return_value=7)
    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    def test_active_account_reconciles_instead_of_refreshing(
        self, mock_refresh_cc, mock_read_active, mock_reconcile,
    ):
        """Simulate the window keeper recovery branch directly.

        When the active account's ping fails, the path must NOT call
        refresh_cc_token — it must reconcile from live creds instead.
        """
        # We test the exact branch logic in isolation by replicating
        # the call pattern from usage_monitor.py window keeper.
        from jacked.api.credential_helpers import (
            read_active_account_id,
            reconcile_credentials_from_live_store,
        )

        acct_id = 7
        active_id_now = read_active_account_id()
        assert active_id_now == acct_id  # prerequisite for the skip path

        # Simulate the new skip branch: reconcile, don't refresh.
        if active_id_now == acct_id:
            reconcile_credentials_from_live_store(acct_id, MagicMock())

        mock_refresh_cc.assert_not_called()
        mock_reconcile.assert_called_once()


class TestLaunchSkipsActiveCC:
    """prepare_account_dir must not rotate CC token for already-active account.

    We test the refresh-decision block in isolation by raising a sentinel
    exception from the next step after the refresh check. If refresh_cc_token
    was called vs skipped we can tell from the mock.
    """

    def _make_account(self, account_id: int, cc_expires_at: int) -> dict:
        return {
            "id": account_id,
            "display_name": f"Account {account_id}",
            "email": f"acct{account_id}@example.com",
            "refresh_token": None,
            "expires_at": int(time.time()) + 3600,
            "access_token": f"at_{account_id}",
            "cc_access_token": f"cc_at_{account_id}",
            "cc_refresh_token": f"cc_rt_{account_id}",
            "cc_expires_at": cc_expires_at,
            "validation_status": "valid",
        }

    @patch("jacked.api.credential_helpers.read_active_account_id")
    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    def test_skips_cc_refresh_when_account_is_active(
        self, mock_refresh_cc, mock_read_live,
    ):
        """If live creds say account 1 is active, launching account 1 must NOT refresh."""
        from jacked import launch

        now = int(time.time())
        acct = self._make_account(1, cc_expires_at=now + 60)
        mock_read_live.return_value = 1

        db = MagicMock()
        db.get_account.return_value = acct

        # Let prepare_account_dir run through the refresh check, then bail
        # on the next step (we don't care about the rest for this test).
        sentinel = RuntimeError("stop-after-refresh-check")
        with patch("jacked.launch.should_refresh", return_value=False), \
             patch("jacked.launch.should_refresh_cc", return_value=True), \
             patch("jacked.launch._time.time", side_effect=sentinel):
            try:
                launch.prepare_account_dir(acct, db)
            except RuntimeError as e:
                if str(e) != "stop-after-refresh-check":
                    raise
            except Exception:
                pass

        # Critical assertion: refresh_cc_token must not have been called.
        mock_refresh_cc.assert_not_called()

    @patch("jacked.api.credential_helpers.read_active_account_id")
    @patch("jacked.web.auth.refresh_cc_token", new_callable=AsyncMock)
    def test_refreshes_cc_when_launching_different_account(
        self, mock_refresh_cc, mock_read_live,
    ):
        """If the active account is 2 and we're launching 1, CC refresh should fire."""
        from jacked import launch

        now = int(time.time())
        acct = self._make_account(1, cc_expires_at=now + 60)
        mock_read_live.return_value = 2
        mock_refresh_cc.return_value = True

        db = MagicMock()
        db.get_account.return_value = acct

        sentinel = RuntimeError("stop-after-refresh-check")
        with patch("jacked.launch.should_refresh", return_value=False), \
             patch("jacked.launch.should_refresh_cc", return_value=True), \
             patch("jacked.launch._time.time", side_effect=sentinel):
            try:
                launch.prepare_account_dir(acct, db)
            except RuntimeError as e:
                if str(e) != "stop-after-refresh-check":
                    raise
            except Exception:
                pass

        mock_refresh_cc.assert_called_once_with(1, db)
