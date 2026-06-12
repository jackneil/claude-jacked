"""Unit tests for the unified credential write path.

Tests sync_credential_to_all_stores(), build_oauth_data(),
read/write_platform_credentials(), update_claude_config_email(),
and the reassign_sessions DB method.
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

from jacked.api.credential_helpers import (
    build_oauth_data,
    read_fresh_active_token,
    read_platform_credentials,
    sync_credential_to_all_stores,
    update_claude_config_email,
    write_platform_credentials,
)
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


def test_sync_writes_claude_json_email():
    """Updates ~/.claude.json with the active account's email.

    >>> test_sync_writes_claude_json_email()
    """
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

        config_path = tmp_path / ".claude.json"
        assert config_path.exists()
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["oauthAccount"]["emailAddress"] == "alice@test.com"


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


def test_read_platform_credentials_uses_user_account():
    """Reads keychain with -a $USER to match Claude Code's entry.

    >>> test_read_platform_credentials_uses_user_account()
    """
    keychain_json = json.dumps({
        "claudeAiOauth": {
            "accessToken": "keychain_token",
            "refreshToken": "keychain_refresh",
        }
    })
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = keychain_json

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch("jacked.api.credential_helpers.subprocess.run", return_value=mock_result) as mock_run,
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is not None
    assert result["claudeAiOauth"]["accessToken"] == "keychain_token"
    # Verify the command uses -a with $USER
    call_args = mock_run.call_args[0][0]
    assert "-a" in call_args
    a_idx = call_args.index("-a")
    assert call_args[a_idx + 1] == "testuser"


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
    mock_result = mock.MagicMock()
    mock_result.returncode = 44
    mock_result.stdout = ""
    mock_result.stderr = "The specified item could not be found in the keychain."

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch("jacked.api.credential_helpers.subprocess.run", return_value=mock_result),
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is None


def test_read_platform_credentials_malformed_json():
    """Returns None when keychain returns invalid JSON.

    >>> test_read_platform_credentials_malformed_json()
    """
    mock_result = mock.MagicMock()
    mock_result.returncode = 0
    mock_result.stdout = "not valid json{"

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch("jacked.api.credential_helpers.subprocess.run", return_value=mock_result),
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is None


# ------------------------------------------------------------------
# write_platform_credentials: macOS Keychain
# ------------------------------------------------------------------


def test_write_platform_credentials_uses_hex_and_user():
    """Writes keychain with -X hex encoding and -a $USER to match Claude Code.

    >>> test_write_platform_credentials_uses_hex_and_user()
    """
    cred_data = {
        "_jackedAccountId": 1,
        "claudeAiOauth": {"accessToken": "test_token"},
    }
    mock_result = mock.MagicMock()
    mock_result.returncode = 0

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.api.credential_helpers.subprocess.run",
            return_value=mock_result,
        ) as mock_run,
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials(cred_data)

    assert result is True
    # 2 calls: add-generic-password THEN orphan cleanup (delete old -a "Claude Code")
    # Orphan cleanup runs AFTER successful write so user is never left with no entry.
    assert mock_run.call_count == 2
    # First call: the actual add
    call_args = mock_run.call_args_list[0][0][0]
    assert "add-generic-password" in call_args
    # Second call: cleanup of orphan entry
    cleanup_args = mock_run.call_args_list[1][0][0]
    assert "delete-generic-password" in cleanup_args
    assert "Claude Code" in cleanup_args
    assert "-U" in call_args
    a_idx = call_args.index("-a")
    assert call_args[a_idx + 1] == "testuser"
    assert "-X" in call_args
    assert "-w" not in call_args
    x_idx = call_args.index("-X")
    hex_value = call_args[x_idx + 1]
    decoded = bytes.fromhex(hex_value).decode("utf-8")
    decoded_data = json.loads(decoded)
    assert decoded_data["_jackedAccountId"] == 1


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
    # The add call fails — orphan cleanup never runs (it's after the add)
    mock_add_fail = mock.MagicMock()
    mock_add_fail.returncode = 1
    mock_add_fail.stderr = "errSecAuthFailed"

    with (
        mock.patch("jacked.api.credential_helpers.sys") as mock_sys,
        mock.patch(
            "jacked.api.credential_helpers.subprocess.run",
            return_value=mock_add_fail,
        ) as mock_run,
        mock.patch("jacked.api.credential_helpers._get_keychain_username", return_value="testuser"),
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials({"claudeAiOauth": {"accessToken": "x"}})

    assert result is False
    # Only 1 call (the add) — cleanup is skipped because add failed
    assert mock_run.call_count == 1


# ------------------------------------------------------------------
# _get_keychain_username
# ------------------------------------------------------------------


def test_get_keychain_username_from_user():
    """Returns $USER when set.

    >>> test_get_keychain_username_from_user()
    """
    with mock.patch.dict(os.environ, {"USER": "alice"}, clear=False):
        from jacked.api.credential_helpers import _get_keychain_username
        assert _get_keychain_username() == "alice"


def test_get_keychain_username_falls_back_to_username():
    """Returns $USERNAME when $USER is unset (Windows).

    >>> test_get_keychain_username_falls_back_to_username()
    """
    env = dict(os.environ)
    env.pop("USER", None)
    env["USERNAME"] = "bob"
    with mock.patch.dict(os.environ, env, clear=True):
        from jacked.api.credential_helpers import _get_keychain_username
        assert _get_keychain_username() == "bob"


def test_get_keychain_username_fallback_to_claude_code():
    """Returns 'Claude Code' when both vars are unset.

    >>> test_get_keychain_username_fallback_to_claude_code()
    """
    env = dict(os.environ)
    env.pop("USER", None)
    env.pop("USERNAME", None)
    with mock.patch.dict(os.environ, env, clear=True):
        from jacked.api.credential_helpers import _get_keychain_username
        assert _get_keychain_username() == "Claude Code"


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
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result == "fresh_token_from_file"


def test_read_fresh_active_token_from_keychain():
    """Prefers keychain over file when both have tokens."""
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
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result is None
