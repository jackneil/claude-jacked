"""Unit tests for the unified credential write path.

Tests sync_credential_to_all_stores(), build_oauth_data(),
read/write_platform_credentials(), update_claude_config_email(),
and the reassign_sessions DB method.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from jacked import statusline
from jacked.api.credential_helpers import (
    _ClaudeConfigAccount,
    _update_claude_config_account,
    build_oauth_data,
    read_fresh_active_token,
    read_platform_credentials,
    sync_credential_to_all_stores,
    update_claude_config_email,
    write_platform_credentials,
)
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.models import StoreReadResult, StoreStatus, StoreWriteResult
from jacked.web.database import Database

# Windows holds SQLite file locks — use ignore_cleanup_errors
_WIN = os.name == "nt"


def _make_db(tmp_path: Path) -> Database:
    """Create a test DB with sample accounts.

    >>> import tempfile; from pathlib import Path
    >>> d = Path(tempfile.mkdtemp())
    >>> db = _make_db(d)
    >>> db.get_account(1)['email']
    'alice@test.com'
    """
    db = Database(str(tmp_path / "test.db"))
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                consecutive_failures, last_error)
               VALUES (1, 'alice@test.com', 'old_access', 'old_refresh', 1700000000,
                       1, 0, 'invalid', 3, 'Refresh token expired')"""
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                consecutive_failures, last_error)
               VALUES (2, 'bob@test.com', 'bob_access', 'bob_refresh', 1700000000,
                       1, 0, 'valid', 0, NULL)"""
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                consecutive_failures, last_error)
               VALUES (3, 'deleted@test.com', 'del_access', 'del_refresh', 1700000000,
                       1, 1, 'valid', 0, NULL)"""
        )
    return db


# ------------------------------------------------------------------
# build_oauth_data
# ------------------------------------------------------------------


def test_build_oauth_data_basic():
    """Builds correct OAuth credential format from account dict.

    >>> test_build_oauth_data_basic()
    """
    account = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": 100,
        "scopes": None,
        "subscription_type": "max",
        "rate_limit_tier": "t1",
    }
    result = build_oauth_data(account)
    assert result["accessToken"] == "at"
    # Primary fallback: refreshToken is always None to prevent Claude Code
    # from consuming the primary refresh token (dual-token safety invariant)
    assert result["refreshToken"] is None
    assert result["expiresAt"] == 100000  # * 1000
    assert result["scopes"] is None
    assert result["subscriptionType"] == "max"
    assert result["rateLimitTier"] == "t1"


def test_build_oauth_data_with_scopes():
    """Parses JSON scopes string into list.

    >>> test_build_oauth_data_with_scopes()
    """
    account = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": 100,
        "scopes": '["user:read", "user:write"]',
        "subscription_type": "pro",
        "rate_limit_tier": "t2",
    }
    result = build_oauth_data(account)
    assert result["scopes"] == ["user:read", "user:write"]


# ------------------------------------------------------------------
# sync_credential_to_all_stores
# ------------------------------------------------------------------


def test_sync_writes_global_credential_file():
    """Writes global .credentials.json with stamp and OAuth data.

    >>> test_sync_writes_global_credential_file()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()

        account = {
            "id": 1,
            "email": "alice@test.com",
            "access_token": "new_access",
            "refresh_token": "new_refresh",
            "expires_at": 1800000000,
            "cc_access_token": "cc_new_access",
            "cc_refresh_token": "cc_new_refresh",
            "cc_expires_at": 1800000000,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.write_platform_credentials",
                return_value=True,
            ),
        ):
            sync_credential_to_all_stores(1, account)

        cred_path = cred_dir / ".credentials.json"
        assert cred_path.exists()
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        assert data["_jackedAccountId"] == 1
        # build_oauth_data prefers CC tokens for credential files
        assert data["claudeAiOauth"]["accessToken"] == "cc_new_access"
        assert data["claudeAiOauth"]["refreshToken"] == "cc_new_refresh"
        assert data["claudeAiOauth"]["expiresAt"] == 1800000000000


def test_sync_authority_failure_does_not_publish_file_target():
    """The compatibility path cannot recreate the original split-store bug."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(
            json.dumps({"_jackedAccountId": 1, "claudeAiOauth": {"accessToken": "old"}}),
            encoding="utf-8",
        )
        account = {
            "email": "new@test.com",
            "access_token": "new",
            "expires_at": 1800000000,
        }

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.write_platform_credentials",
                return_value=False,
            ),
        ):
            result = sync_credential_to_all_stores(2, account)

        assert result is False
        assert json.loads(cred_path.read_text(encoding="utf-8"))["_jackedAccountId"] == 1


@pytest.mark.parametrize("rate_limit_tier", ["t1", None])
def test_sync_writes_initial_claude_json_metadata(rate_limit_tier):
    """First activation writes coherent metadata even without a known tier."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()

        account = {
            "id": 1,
            "email": "alice@test.com",
            "display_name": "Alice",
            "access_token": "tok",
            "refresh_token": "rt",
            "expires_at": 1800000000,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": rate_limit_tier,
        }

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.write_platform_credentials",
                return_value=True,
            ),
        ):
            sync_credential_to_all_stores(1, account)

        config_path = tmp_path / ".claude.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        oauth = config["oauthAccount"]
        assert oauth["emailAddress"] == "alice@test.com"
        if rate_limit_tier is None:
            assert "userRateLimitTier" not in oauth
            assert "organizationRateLimitTier" not in oauth
            assert statusline.render({}, home=str(tmp_path)) == "alice@test.com"
        else:
            assert oauth["userRateLimitTier"] == rate_limit_tier


def _prime_cached_statusline_account(tmp_path: Path) -> Path:
    """Create outgoing metadata plus a resolver snapshot."""
    (tmp_path / ".claude").mkdir()
    config_path = tmp_path / ".claude.json"
    config_path.write_text(
        json.dumps({
            "topLevelSetting": {"preserve": True},
            "oauthAccount": {
                "emailAddress": "old@test.com",
                "organizationName": "Old Org",
                "userRateLimitTier": "default_claude_max_5x",
                "unrelatedAccountSetting": ["keep", 7],
            },
        }),
        encoding="utf-8",
    )
    from jacked.credentials.models import CredentialIdentity
    from jacked.credentials.resolver import (
        FileResolverSnapshotSink,
        ResolverState,
        SnapshotUpdate,
    )

    old_identity = CredentialIdentity(1, "old@test.com", "org-old")
    FileResolverSnapshotSink(
        tmp_path / ".claude" / "jacked-resolver-snapshot.json"
    ).publish(
        SnapshotUpdate(
            scope="global",
            state=ResolverState.RESOLVED,
            evidence=("test",),
            credential_revision="old-revision",
            desired=old_identity,
            observed=old_identity,
        )
    )
    assert statusline.render({}, home=str(tmp_path)) == "old@test.com"
    return config_path


def _sync_statusline_target(
    tmp_path: Path,
    access_canary: str,
    refresh_canary: str,
) -> None:
    """Activate the target through the real shared credential boundary."""
    account = {
        "email": "new@test.com",
        "display_name": "New User",
        "access_token": "primary-access",
        "refresh_token": "primary-refresh",
        "expires_at": 1800000000,
        "cc_access_token": access_canary,
        "cc_refresh_token": refresh_canary,
        "cc_expires_at": 1800000000,
        "scopes": None,
        "subscription_type": "max",
        "rate_limit_tier": "default_claude_max_20x",
        "organization_uuid": "org-new",
        "organization_name": "New Org",
    }
    with (
        mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
        mock.patch(
            "jacked.api.credential_helpers.write_platform_credentials",
            return_value=True,
        ),
    ):
        sync_credential_to_all_stores(20, account)


def test_sync_switches_cached_statusline_identity_and_tier_coherently():
    """A real credential sync cannot mix the target email with the old tier."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        config_path = _prime_cached_statusline_account(tmp_path)
        access_canary = "statusline-access-canary-7b8c"
        refresh_canary = "statusline-refresh-canary-4d2a"
        _sync_statusline_target(tmp_path, access_canary, refresh_canary)

        line = statusline.render({}, home=str(tmp_path))
        config = json.loads(config_path.read_text(encoding="utf-8"))
        oauth = config["oauthAccount"]
        config_text = config_path.read_text(encoding="utf-8")
        snapshot_text = (
            tmp_path / ".claude" / "jacked-resolver-snapshot.json"
        ).read_text(encoding="utf-8")
        credential_text = (
            tmp_path / ".claude" / ".credentials.json"
        ).read_text(encoding="utf-8")

        assert "new@test.com" in line
        assert "old@test.com" not in line
        assert oauth["organizationRateLimitTier"] == "default_claude_max_20x"
        assert "userRateLimitTier" not in oauth
        assert config["topLevelSetting"] == {"preserve": True}
        assert oauth["unrelatedAccountSetting"] == ["keep", 7]
        assert access_canary in credential_text
        assert refresh_canary in credential_text
        for public_text in (config_text, snapshot_text, line):
            assert access_canary not in public_text
            assert refresh_canary not in public_text


def test_sync_with_unknown_tier_clears_both_outgoing_tier_fields():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        (tmp_path / ".claude").mkdir()
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({
                "oauthAccount": {
                    "emailAddress": "old@test.com",
                    "userRateLimitTier": "default_claude_max_5x",
                    "organizationRateLimitTier": "default_claude_max_20x",
                    "preserved": {"nested": "value"},
                },
            }),
            encoding="utf-8",
        )
        account = {
            "email": "unknown-tier@test.com",
            "access_token": "access",
            "refresh_token": "refresh",
            "expires_at": 1800000000,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": None,
            "organization_uuid": "org-unknown",
        }

        with (
            mock.patch(
                "jacked.api.credential_helpers.Path.home", return_value=tmp_path
            ),
            mock.patch(
                "jacked.api.credential_helpers.write_platform_credentials",
                return_value=True,
            ),
        ):
            sync_credential_to_all_stores(21, account)

        oauth = json.loads(config_path.read_text(encoding="utf-8"))["oauthAccount"]
        assert oauth["emailAddress"] == "unknown-tier@test.com"
        assert "userRateLimitTier" not in oauth
        assert "organizationRateLimitTier" not in oauth
        assert oauth["preserved"] == {"nested": "value"}
        line = statusline.render({}, home=str(tmp_path))
        assert line == "unknown-tier@test.com"
        assert "Max" not in line


def test_sync_writes_per_account_dir():
    """Writes to per-account dir when it exists (no stamp).

    >>> test_sync_writes_per_account_dir()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        acct_dir = cred_dir / "accounts" / "1"
        acct_dir.mkdir(parents=True)

        account = {
            "id": 1,
            "email": "alice@test.com",
            "access_token": "tok",
            "refresh_token": "rt",
            "expires_at": 1800000000,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.write_platform_credentials",
                return_value=True,
            ),
        ):
            sync_credential_to_all_stores(1, account)

        acct_cred = acct_dir / ".credentials.json"
        assert acct_cred.exists()
        data = json.loads(acct_cred.read_text(encoding="utf-8"))
        assert "claudeAiOauth" in data
        assert data["claudeAiOauth"]["accessToken"] == "tok"
        # Per-account dir should NOT have _jackedAccountId stamp
        assert "_jackedAccountId" not in data


def test_sync_preserves_existing_keys():
    """Preserves non-OAuth keys in existing credential file.

    >>> test_sync_preserves_existing_keys()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(json.dumps({"someOtherKey": "preserved"}))

        account = {
            "id": 1,
            "email": "alice@test.com",
            "access_token": "tok",
            "refresh_token": "rt",
            "expires_at": 1800000000,
            "scopes": None,
            "subscription_type": "max",
            "rate_limit_tier": "t1",
        }

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.write_platform_credentials",
                return_value=True,
            ),
        ):
            sync_credential_to_all_stores(1, account)

        data = json.loads(cred_path.read_text(encoding="utf-8"))
        assert data["someOtherKey"] == "preserved"
        assert data["_jackedAccountId"] == 1


# ------------------------------------------------------------------
# read_platform_credentials: macOS Keychain
# ------------------------------------------------------------------


def test_read_platform_credentials_uses_security_framework_store():
    """Reads the native adapter without putting secrets in process arguments."""
    payload = CredentialPayload.from_mapping({
        "claudeAiOauth": {
            "accessToken": "keychain_token",
            "refreshToken": "keychain_refresh",
        }
    })
    store = mock.MagicMock()
    store.read.return_value = StoreReadResult(StoreStatus.OK, payload)

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.credentials.macos_store.MacOSCredentialStore",
            return_value=store,
        ) as store_class,
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is not None
    assert result["claudeAiOauth"]["accessToken"] == "keychain_token"
    store_class.assert_called_once_with("testuser")


def test_read_platform_credentials_linux():
    """Returns None immediately on Linux (no keychain support yet).

    >>> test_read_platform_credentials_linux()
    """
    with mock.patch("jacked.api.credential_helpers.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = read_platform_credentials()

    assert result is None


def test_read_platform_credentials_keychain_not_found():
    """Returns None when keychain entry doesn't exist.

    >>> test_read_platform_credentials_keychain_not_found()
    """
    store = mock.MagicMock()
    store.read.return_value = StoreReadResult(StoreStatus.MISSING)

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.credentials.macos_store.MacOSCredentialStore",
            return_value=store,
        ),
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is None


def test_read_platform_credentials_malformed_json():
    """Returns None when keychain returns invalid JSON.

    >>> test_read_platform_credentials_malformed_json()
    """
    store = mock.MagicMock()
    store.read.return_value = StoreReadResult(
        StoreStatus.UNUSABLE, reason="invalid credential JSON"
    )

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.credentials.macos_store.MacOSCredentialStore",
            return_value=store,
        ),
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is None


# ------------------------------------------------------------------
# write_platform_credentials: macOS Keychain
# ------------------------------------------------------------------


def test_write_platform_credentials_uses_native_store():
    """Writes through Security.framework without a credential subprocess."""
    cred_data = {
        "_jackedAccountId": 1,
        "claudeAiOauth": {"accessToken": "test_token"},
    }
    store = mock.MagicMock()
    store.write.return_value = StoreWriteResult(StoreStatus.OK)

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.credentials.macos_store.MacOSCredentialStore",
            return_value=store,
        ) as store_class,
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials(cred_data)

    assert result is True
    store_class.assert_called_once_with("testuser")
    payload = store.write.call_args.args[0]
    assert payload.identity.account_id == 1


def test_write_platform_credentials_linux_noop():
    """Returns True (no-op) on Linux.

    >>> test_write_platform_credentials_linux_noop()
    """
    with mock.patch("jacked.api.credential_helpers.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = write_platform_credentials({"claudeAiOauth": {"accessToken": "x"}})

    assert result is True


def test_write_platform_credentials_keychain_error():
    """Returns False when keychain add command fails.

    >>> test_write_platform_credentials_keychain_error()
    """
    store = mock.MagicMock()
    store.write.return_value = StoreWriteResult(StoreStatus.DENIED, "authorization denied")

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.credentials.macos_store.MacOSCredentialStore",
            return_value=store,
        ),
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials({"claudeAiOauth": {"accessToken": "x"}})

    assert result is False


# ------------------------------------------------------------------
# _get_keychain_username
# ------------------------------------------------------------------


def test_get_keychain_username_uses_os_identity_not_environment():
    """Environment spoofing cannot change the Keychain locator account."""
    with (
        mock.patch.dict(os.environ, {"USER": "spoofed"}, clear=False),
        mock.patch(
            "jacked.credentials.macos_store.system_account_name",
            return_value="os-user",
        ),
    ):
        from jacked.api.credential_helpers import _get_keychain_username

        assert _get_keychain_username() == "os-user"


# ------------------------------------------------------------------
# update_claude_config_email
# ------------------------------------------------------------------


def test_update_claude_config_email_creates_file():
    """Creates ~/.claude.json when it doesn't exist.

    >>> test_update_claude_config_email_creates_file()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)

        with mock.patch(
            "jacked.api.credential_helpers.Path.home", return_value=tmp_path
        ):
            update_claude_config_email("new@test.com", "New User")

        config_path = tmp_path / ".claude.json"
        assert config_path.exists()
        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["oauthAccount"]["emailAddress"] == "new@test.com"
        assert result["oauthAccount"]["displayName"] == "New User"


def test_update_claude_config_email_preserves_keys():
    """Preserves other keys in existing .claude.json.

    >>> test_update_claude_config_email_preserves_keys()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({
                "someOtherKey": "preserved",
                "oauthAccount": {"emailAddress": "old@test.com", "displayName": "Old"},
            }),
            encoding="utf-8",
        )

        with mock.patch(
            "jacked.api.credential_helpers.Path.home", return_value=tmp_path
        ):
            update_claude_config_email("new@test.com")

        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["oauthAccount"]["emailAddress"] == "new@test.com"
        assert result["oauthAccount"]["displayName"] == "Old"  # preserved
        assert result["someOtherKey"] == "preserved"


def test_update_claude_config_account_replaces_tier_field_for_organization():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({
                "topLevel": ["unchanged"],
                "oauthAccount": {
                    "emailAddress": "old@test.com",
                    "userRateLimitTier": "default_claude_max_5x",
                    "organizationRateLimitTier": "default_claude_pro",
                    "unrelated": {"unchanged": True},
                },
            }),
            encoding="utf-8",
        )

        with mock.patch(
            "jacked.api.credential_helpers.Path.home", return_value=tmp_path
        ):
            _update_claude_config_account(
                _ClaudeConfigAccount(
                    email="org@test.com",
                    organization_uuid="org-20x",
                    organization_name="Twenty Org",
                    rate_limit_tier="default_claude_max_20x",
                )
            )

        result = json.loads(config_path.read_text(encoding="utf-8"))
        oauth = result["oauthAccount"]
        assert oauth["organizationRateLimitTier"] == "default_claude_max_20x"
        assert "userRateLimitTier" not in oauth
        assert oauth["organizationUuid"] == "org-20x"
        assert result["topLevel"] == ["unchanged"]
        assert oauth["unrelated"] == {"unchanged": True}


def test_update_claude_config_account_replaces_tier_field_for_personal_account():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({
                "oauthAccount": {
                    "emailAddress": "org@test.com",
                    "organizationUuid": "old-org",
                    "organizationName": "Old Org",
                    "organizationRateLimitTier": "default_claude_max_20x",
                    "unrelated": "keep",
                },
            }),
            encoding="utf-8",
        )

        with mock.patch(
            "jacked.api.credential_helpers.Path.home", return_value=tmp_path
        ):
            _update_claude_config_account(
                _ClaudeConfigAccount(
                    email="personal@test.com",
                    rate_limit_tier="default_claude_max_5x",
                )
            )

        oauth = json.loads(config_path.read_text(encoding="utf-8"))["oauthAccount"]
        assert oauth["userRateLimitTier"] == "default_claude_max_5x"
        assert "organizationRateLimitTier" not in oauth
        assert "organizationUuid" not in oauth
        assert "organizationName" not in oauth
        assert oauth["unrelated"] == "keep"


def test_update_claude_config_account_explicit_null_clears_tier_fields():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({
                "topLevel": "keep",
                "oauthAccount": {
                    "emailAddress": "old@test.com",
                    "userRateLimitTier": "default_claude_max_5x",
                    "organizationRateLimitTier": "default_claude_max_20x",
                    "unrelated": [1, 2, 3],
                },
            }),
            encoding="utf-8",
        )

        with mock.patch(
            "jacked.api.credential_helpers.Path.home", return_value=tmp_path
        ):
            _update_claude_config_account(
                _ClaudeConfigAccount(
                    email="unknown@test.com",
                    rate_limit_tier=None,
                )
            )

        result = json.loads(config_path.read_text(encoding="utf-8"))
        oauth = result["oauthAccount"]
        assert "userRateLimitTier" not in oauth
        assert "organizationRateLimitTier" not in oauth
        assert result["topLevel"] == "keep"
        assert oauth["unrelated"] == [1, 2, 3]


def test_update_claude_config_email_omitted_tier_preserves_existing_fields():
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({
                "oauthAccount": {
                    "emailAddress": "old@test.com",
                    "userRateLimitTier": "default_claude_max_5x",
                    "organizationRateLimitTier": "default_claude_max_20x",
                },
            }),
            encoding="utf-8",
        )

        with mock.patch(
            "jacked.api.credential_helpers.Path.home", return_value=tmp_path
        ):
            update_claude_config_email("identity-only@test.com")

        oauth = json.loads(config_path.read_text(encoding="utf-8"))["oauthAccount"]
        assert oauth["userRateLimitTier"] == "default_claude_max_5x"
        assert oauth["organizationRateLimitTier"] == "default_claude_max_20x"


def test_update_claude_config_email_refuses_symlink():
    """Refuses to write when .claude.json is a symlink.

    >>> test_update_claude_config_email_refuses_symlink()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = tmp_path / ".claude.json"
        try:
            link.symlink_to(target)
        except OSError:
            return  # Symlinks may require privileges on Windows

        with mock.patch(
            "jacked.api.credential_helpers.Path.home", return_value=tmp_path
        ):
            update_claude_config_email("evil@test.com")

        result = json.loads(target.read_text(encoding="utf-8"))
        assert "oauthAccount" not in result


# ------------------------------------------------------------------
# reassign_sessions (DB method, kept from original test file)
# ------------------------------------------------------------------


def test_reassign_sessions():
    """Batch-reassigns sessions from one account to another.

    >>> test_reassign_sessions()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            db.record_session_account(
                "sess-1", account_id=2, email="bob@test.com", repo_path="/repo"
            )
            db.record_session_account(
                "sess-2", account_id=2, email="bob@test.com", repo_path="/repo2"
            )

            count = db.reassign_sessions(
                from_account_id=2,
                to_account_id=1,
                since_iso="2000-01-01T00:00:00Z",
            )
            assert count == 2

            records = db.get_session_accounts("sess-1")
            assert records[0]["account_id"] == 1
            assert records[0]["email"] == "alice@test.com"
        finally:
            db.close()


def test_reassign_sessions_validates_target():
    """Raises ValueError if target account is deleted.

    >>> test_reassign_sessions_validates_target()
    """
    import pytest

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            with pytest.raises(ValueError, match="not found"):
                db.reassign_sessions(
                    from_account_id=1,
                    to_account_id=3,
                    since_iso="2000-01-01T00:00:00Z",
                )
        finally:
            db.close()


# ------------------------------------------------------------------
# read_fresh_active_token
# ------------------------------------------------------------------


def test_read_fresh_active_token_from_file():
    """Reads access token from .credentials.json for matching account."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(json.dumps({
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "fresh_token_from_file"},
        }))

        with (
            mock.patch(
                "jacked.api.credential_helpers.read_active_account_id",
                return_value=1,
            ),
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result == "fresh_token_from_file"


def test_read_fresh_active_token_from_keychain_after_consensus():
    """Reads raw Keychain token only after canonical identity consensus."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(json.dumps({
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "file_token"},
        }))

        with (
            mock.patch(
                "jacked.api.credential_helpers.read_active_account_id",
                return_value=1,
            ),
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value={
                    "_jackedAccountId": 1,
                    "claudeAiOauth": {"accessToken": "keychain_token"},
                },
            ),
        ):
            result = read_fresh_active_token(1)

    assert result == "keychain_token"


def test_read_fresh_active_token_wrong_account():
    """Returns None when credential stores belong to a different account."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(json.dumps({
            "_jackedAccountId": 2,
            "claudeAiOauth": {"accessToken": "other_account_token"},
        }))

        with (
            mock.patch(
                "jacked.api.credential_helpers.read_active_account_id",
                return_value=2,
            ),
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result is None


def test_read_fresh_active_token_no_credentials():
    """Returns None when no credential file or keychain entry exists."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()

        with (
            mock.patch(
                "jacked.api.credential_helpers.read_active_account_id",
                return_value=None,
            ),
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result is None


def test_read_fresh_active_token_rejects_matching_file_when_stores_conflict():
    """A matching fallback store cannot override canonical resolver conflict."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        (cred_dir / ".credentials.json").write_text(
            json.dumps(
                {
                    "_jackedAccountId": 1,
                    "claudeAiOauth": {"accessToken": "must-not-return"},
                }
            )
        )
        with (
            mock.patch(
                "jacked.api.credential_helpers.read_active_account_id",
                return_value=None,
            ),
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value={
                    "_jackedAccountId": 2,
                    "claudeAiOauth": {"accessToken": "other-account"},
                },
            ),
        ):
            result = read_fresh_active_token(1)

    assert result is None
