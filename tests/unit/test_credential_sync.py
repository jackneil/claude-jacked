"""Unit tests for the credential write and read paths.

Covers build_oauth_data(), the engine activation that every account switch
runs (``jacked.credentials.runtime.activate_account``) through the files it
writes, the ``~/.claude.json`` identity across switches,
read_platform_credentials(), read_fresh_active_token(), and the
reassign_sessions DB method.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from jacked import statusline
from jacked.api.claude_identity import minimal_identity_block, write_identity
from jacked.api.credential_helpers import (
    build_oauth_data,
    read_fresh_active_token,
    read_platform_credentials,
)
from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.models import (
    StoreReadResult,
    StoreStatus,
    SwitchContext,
    SwitchOutcome,
)
from jacked.credentials.repository import InMemoryCredentialSwitchRepository
from jacked.credentials.runtime import activate_account
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


def test_build_oauth_data_carries_the_row_tier_on_both_branches():
    """Claude Code skips its refresh-time profile fetch only when the
    credential payload has non-null subscriptionType and rateLimitTier."""
    base = {
        "access_token": "at",
        "refresh_token": "rt",
        "expires_at": 100,
        "scopes": None,
        "subscription_type": "max",
        "rate_limit_tier": "default_claude_max_20x",
    }
    primary = build_oauth_data(base)
    cc = build_oauth_data(
        {**base, "cc_access_token": "cc", "cc_refresh_token": "ccr", "cc_expires_at": 200}
    )
    for payload in (primary, cc):
        assert payload["subscriptionType"] == "max"
        assert payload["rateLimitTier"] == "default_claude_max_20x"


# ------------------------------------------------------------------
# Engine activation: the files that one switch writes
# ------------------------------------------------------------------


def _activate_on_linux(home: Path, account: dict):
    """Activate ``account`` through the real engine, with ``home`` as HOME.

    The Claude build is a Linux build, so the global credential file is the
    authority. The identity publisher and the resolver snapshot are real.
    """
    from tests.unit.test_credential_runtime import _RowDb, _identity

    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            return_value=_identity(platform_system="linux", platform_machine="x86_64"),
        ),
        mock.patch("jacked.credentials.runtime.Path.home", return_value=home),
        mock.patch(
            "jacked.credentials.runtime.DatabaseCredentialSwitchRepository",
            lambda _db: InMemoryCredentialSwitchRepository(),
        ),
    ):
        return activate_account(
            _RowDb(account), account, SwitchContext.MANUAL, "op-switch"
        )


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


def _activate_statusline_target(
    tmp_path: Path,
    access_canary: str,
    refresh_canary: str,
):
    """Activate a profiled target through the real credential engine."""
    account = {
        "id": 20,
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
        "account_uuid": "b0b0b0b0-0000-4000-8000-000000000020",
        "profile_fetched_at": 1_760_000_000.0,
        "profile_json": json.dumps({
            "account": {
                "uuid": "b0b0b0b0-0000-4000-8000-000000000020",
                "email": "new@test.com",
                "display_name": "New User",
                "full_name": "New Q. User",
                "created_at": "2024-01-01T00:00:00Z",
            },
            "organization": {
                "uuid": "org-new",
                "name": "New Org",
                "billing_type": "stripe_subscription",
                "rate_limit_tier": "default_claude_max_20x",
                "seat_tier": None,
                "has_extra_usage_enabled": False,
                "subscription_created_at": "2024-02-01T00:00:00Z",
                "claude_code_trial_ends_at": None,
                "claude_code_trial_duration_days": None,
            },
        }),
    }
    return _activate_on_linux(tmp_path, account)


def test_switch_moves_the_cached_statusline_identity_and_tier_together(tmp_path):
    """A real engine switch cannot mix the target email with the old tier."""
    config_path = _prime_cached_statusline_account(tmp_path)
    access_canary = "statusline-access-canary-7b8c"
    refresh_canary = "statusline-refresh-canary-4d2a"

    result = _activate_statusline_target(tmp_path, access_canary, refresh_canary)

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    line = statusline.render({}, home=str(tmp_path))
    config_text = config_path.read_text(encoding="utf-8")
    config = json.loads(config_text)
    oauth = config["oauthAccount"]
    snapshot_text = (
        tmp_path / ".claude" / "jacked-resolver-snapshot.json"
    ).read_text(encoding="utf-8")
    credentials = json.loads(
        (tmp_path / ".claude" / ".credentials.json").read_text(encoding="utf-8")
    )

    assert "new@test.com" in line
    assert "old@test.com" not in line
    assert oauth["organizationRateLimitTier"] == "default_claude_max_20x"
    assert "userRateLimitTier" not in oauth
    assert oauth["accountUuid"] == "b0b0b0b0-0000-4000-8000-000000000020"
    assert oauth["billingType"] == "stripe_subscription"
    assert config["topLevelSetting"] == {"preserve": True}
    # The outgoing person's block is replaced, never merged.
    assert "unrelatedAccountSetting" not in oauth
    assert oauth["organizationName"] == "New Org"
    # The credential file holds the stamp, the Claude Code token pair and
    # the expiry in milliseconds.
    assert credentials["_jackedAccountId"] == 20
    assert credentials["claudeAiOauth"]["accessToken"] == access_canary
    assert credentials["claudeAiOauth"]["refreshToken"] == refresh_canary
    assert credentials["claudeAiOauth"]["expiresAt"] == 1_800_000_000_000
    for public_text in (config_text, snapshot_text, line):
        assert access_canary not in public_text
        assert refresh_canary not in public_text


@pytest.mark.parametrize("rate_limit_tier", [None, "default_claude_max_5x"])
def test_switch_to_an_unprofiled_target_clears_both_outgoing_tier_fields(
    tmp_path, rate_limit_tier
):
    """Without a stored profile, the published identity has no tier.

    This is true also when the row knows its tier: the tier comes only with
    the complete block. So both outgoing tier fields are removed.
    """
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
        "id": 21,
        "email": "unknown-tier@test.com",
        "access_token": "access",
        "refresh_token": "refresh",
        "expires_at": 1800000000,
        "scopes": None,
        "subscription_type": "max",
        "rate_limit_tier": rate_limit_tier,
        "organization_uuid": "org-unknown",
    }

    result = _activate_on_linux(tmp_path, account)

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    oauth = json.loads(config_path.read_text(encoding="utf-8"))["oauthAccount"]
    assert oauth["emailAddress"] == "unknown-tier@test.com"
    assert "userRateLimitTier" not in oauth
    assert "organizationRateLimitTier" not in oauth
    # Another person's leftovers never survive a switch.
    assert "preserved" not in oauth
    line = statusline.render({}, home=str(tmp_path))
    assert line == "unknown-tier@test.com"
    assert "Max" not in line


def test_switch_keeps_foreign_top_level_keys_in_the_credential_file(tmp_path):
    """Keys that jacked does not own stay in the credential file.

    The engine merges the account stamp and ``claudeAiOauth`` into the
    authority it read, so the other top-level keys survive the switch.
    """
    (tmp_path / ".claude").mkdir()
    cred_path = tmp_path / ".claude" / ".credentials.json"
    cred_path.write_text(json.dumps({"someOtherKey": "preserved"}), encoding="utf-8")
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

    result = _activate_on_linux(tmp_path, account)

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    data = json.loads(cred_path.read_text(encoding="utf-8"))
    assert data["someOtherKey"] == "preserved"
    assert data["_jackedAccountId"] == 1
    assert data["claudeAiOauth"]["accessToken"] == "tok"


def test_a_refused_keychain_write_never_publishes_the_file_mirror(tmp_path):
    """On macOS the Keychain is the authority and the file is its mirror.

    When the Keychain refuses the write, the switch stops before the mirror
    and before the identity. The two stores never name different accounts.
    """
    from jacked.credentials.store import MemoryCredentialStore
    from tests.unit.test_credential_runtime import _RowDb, _identity

    (tmp_path / ".claude").mkdir()
    previous = {"_jackedAccountId": 1, "claudeAiOauth": {"accessToken": "old"}}
    cred_path = tmp_path / ".claude" / ".credentials.json"
    cred_path.write_text(json.dumps(previous), encoding="utf-8")
    mirror_before = cred_path.read_bytes()
    keychain = MemoryCredentialStore(
        "keychain", CredentialPayload.from_mapping(previous)
    )
    keychain.fail_writes = True
    account = {
        "id": 2,
        "email": "new@test.com",
        "access_token": "new",
        "expires_at": 1800000000,
    }

    with (
        mock.patch(
            "jacked.credentials.runtime.detect_claude_identity",
            return_value=_identity(),
        ),
        mock.patch("jacked.credentials.runtime.Path.home", return_value=tmp_path),
        mock.patch(
            "jacked.credentials.runtime.MacOSCredentialStore", return_value=keychain
        ),
        mock.patch(
            "jacked.credentials.runtime.DatabaseCredentialSwitchRepository",
            lambda _db: InMemoryCredentialSwitchRepository(),
        ),
        # No outgoing account to save: this test is about the refused write.
        mock.patch(
            "jacked.api.usage_monitor._read_active_account_id", return_value=None
        ),
    ):
        result = activate_account(
            _RowDb(account), account, SwitchContext.MANUAL, "op-refused"
        )

    assert result.outcome is SwitchOutcome.FAILED_PRESERVED
    assert keychain.write_count == 1
    assert keychain.read().payload.identity.account_id == 1
    assert cred_path.read_bytes() == mirror_before
    assert not (tmp_path / ".claude.json").exists()


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
# ~/.claude.json identity across switches (claude_identity.write_identity)
# ------------------------------------------------------------------


def test_two_sequential_switches_replace_the_display_name(tmp_path):
    """The second account's name replaces the first one's, never merges."""
    config_path = tmp_path / ".claude.json"

    write_identity(
        tmp_path,
        minimal_identity_block(1, "first@test.com", display_name="First Person"),
    )
    first = json.loads(config_path.read_text(encoding="utf-8"))
    write_identity(
        tmp_path,
        minimal_identity_block(2, "second@test.com", display_name="Second Person"),
    )

    assert first["oauthAccount"]["displayName"] == "First Person"
    result = json.loads(config_path.read_text(encoding="utf-8"))
    assert result["oauthAccount"] == {
        "emailAddress": "second@test.com",
        "displayName": "Second Person",
    }


def test_a_same_account_write_keeps_the_existing_tier_fields(tmp_path):
    """The same account merges: the fields that the block omits stay.

    The stored email differs only in case, so it is the same identity. The
    write keeps the stored spelling and does not rewrite the file.
    """
    config_path = tmp_path / ".claude.json"
    config_path.write_text(
        json.dumps({
            "oauthAccount": {
                "emailAddress": "Identity-Only@test.com",
                "userRateLimitTier": "default_claude_max_5x",
                "organizationRateLimitTier": "default_claude_max_20x",
            },
        }),
        encoding="utf-8",
    )
    before = config_path.read_bytes()

    result = write_identity(
        tmp_path, minimal_identity_block(1, "identity-only@test.com")
    )

    assert (result.status, result.mode) == ("unchanged", "merge")
    assert config_path.read_bytes() == before
    oauth = json.loads(config_path.read_text(encoding="utf-8"))["oauthAccount"]
    assert oauth["emailAddress"] == "Identity-Only@test.com"
    assert oauth["userRateLimitTier"] == "default_claude_max_5x"
    assert oauth["organizationRateLimitTier"] == "default_claude_max_20x"


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
