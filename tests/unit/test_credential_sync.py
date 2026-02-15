"""Unit tests for credential file -> DB token sync.

Tests sync_credential_tokens() which runs inside the credential file
watcher loop when Claude Code independently refreshes OAuth tokens.
Also tests the skip-active-account logic in refresh_all_expiring_tokens(),
re-stamping, and missing-file recreation.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from jacked.api.credential_sync import (
    create_missing_credentials_file,
    read_platform_credentials,
    re_stamp_jacked_account_id,
    sync_credential_tokens,
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
# sync_credential_tokens: happy path
# ------------------------------------------------------------------


def test_sync_tokens_via_jacked_account_id():
    """Syncs tokens when _jackedAccountId matches and tokens changed.

    Verifies: access_token, refresh_token, expires_at updated,
    validation_status reset to 'valid', consecutive_failures cleared,
    last_error cleared.

    >>> test_sync_tokens_via_jacked_account_id()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        db = _make_db(Path(tmp))
        try:
            cred_data = {
                "_jackedAccountId": 1,
                "claudeAiOauth": {
                    "accessToken": "new_access_token",
                    "refreshToken": "new_refresh_token",
                    "expiresAt": 1800000000000,  # milliseconds
                },
            }
            result = sync_credential_tokens(db, cred_data)
            assert result is True

            acct = db.get_account(1)
            assert acct["access_token"] == "new_access_token"
            assert acct["refresh_token"] == "new_refresh_token"
            assert acct["expires_at"] == 1800000000  # converted to seconds
            assert acct["validation_status"] == "valid"
            assert acct["consecutive_failures"] == 0
            assert acct["last_error"] is None
        finally:
            db.close()


def test_sync_tokens_via_email_fallback():
    """Falls back to email matching when _jackedAccountId is absent.

    >>> test_sync_tokens_via_email_fallback()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            config_path = tmp_path / ".claude.json"
            config_path.write_text(
                json.dumps({"oauthAccount": {"emailAddress": "bob@test.com"}}),
                encoding="utf-8",
            )

            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "brand_new_bob_token",
                    "refreshToken": "brand_new_bob_refresh",
                    "expiresAt": 1900000000000,
                },
            }

            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                result = sync_credential_tokens(db, cred_data)

            assert result is True
            acct = db.get_account(2)
            assert acct["access_token"] == "brand_new_bob_token"
        finally:
            db.close()


def test_sync_tokens_email_case_insensitive():
    """Email fallback matches case-insensitively.

    >>> test_sync_tokens_email_case_insensitive()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            config_path = tmp_path / ".claude.json"
            config_path.write_text(
                json.dumps({"oauthAccount": {"emailAddress": "Alice@Test.COM"}}),
                encoding="utf-8",
            )

            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "case_insensitive_token",
                },
            }

            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                result = sync_credential_tokens(db, cred_data)

            assert result is True
            acct = db.get_account(1)
            assert acct["access_token"] == "case_insensitive_token"
        finally:
            db.close()


# ------------------------------------------------------------------
# sync_credential_tokens: no-op cases
# ------------------------------------------------------------------


def test_sync_tokens_unchanged_valid_returns_false():
    """Returns False when access_token hasn't changed and account is valid.

    >>> test_sync_tokens_unchanged_valid_returns_false()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        db = _make_db(Path(tmp))
        try:
            # Set account to valid first
            db.update_account(1, validation_status="valid", consecutive_failures=0)
            cred_data = {
                "_jackedAccountId": 1,
                "claudeAiOauth": {
                    "accessToken": "old_access",  # same as DB
                },
            }
            result = sync_credential_tokens(db, cred_data)
            assert result is False
        finally:
            db.close()


def test_sync_tokens_unchanged_invalid_clears_error():
    """Clears error when tokens match but account is invalid.

    If the credential file has the same token as the DB and the account
    is marked invalid, clear the error — the token is clearly in use.

    >>> test_sync_tokens_unchanged_invalid_clears_error()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        db = _make_db(Path(tmp))
        try:
            cred_data = {
                "_jackedAccountId": 1,
                "claudeAiOauth": {
                    "accessToken": "old_access",  # same as DB
                },
            }
            # Account starts invalid with errors (from _make_db)
            result = sync_credential_tokens(db, cred_data)
            assert result is True

            acct = db.get_account(1)
            assert acct["validation_status"] == "valid"
            assert acct["consecutive_failures"] == 0
            assert acct["last_error"] is None
            assert acct["last_validated_at"] is not None
        finally:
            db.close()


def test_sync_tokens_empty_cred_data():
    """Returns False for empty credential data.

    >>> test_sync_tokens_empty_cred_data()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        db = _make_db(Path(tmp))
        try:
            assert sync_credential_tokens(db, {}) is False
        finally:
            db.close()


def test_sync_tokens_no_access_token():
    """Returns False when claudeAiOauth has no accessToken.

    >>> test_sync_tokens_no_access_token()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        db = _make_db(Path(tmp))
        try:
            cred_data = {"claudeAiOauth": {"refreshToken": "only_refresh"}}
            assert sync_credential_tokens(db, cred_data) is False
        finally:
            db.close()


def test_sync_tokens_none_db():
    """Returns False when db is None (graceful no-op).

    >>> test_sync_tokens_none_db()
    """
    assert (
        sync_credential_tokens(None, {"claudeAiOauth": {"accessToken": "x"}}) is False
    )


def test_sync_tokens_deleted_account():
    """Returns False when matched account is deleted.

    >>> test_sync_tokens_deleted_account()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        db = _make_db(Path(tmp))
        try:
            cred_data = {
                "_jackedAccountId": 3,
                "claudeAiOauth": {
                    "accessToken": "new_token_for_deleted",
                },
            }
            result = sync_credential_tokens(db, cred_data)
            assert result is False
        finally:
            db.close()


def test_sync_tokens_no_matching_account():
    """Returns False when _jackedAccountId doesn't match any account.

    >>> test_sync_tokens_no_matching_account()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            cred_data = {
                "_jackedAccountId": 999,
                "claudeAiOauth": {
                    "accessToken": "orphan_token",
                },
            }

            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                result = sync_credential_tokens(db, cred_data)

            assert result is False
        finally:
            db.close()


def test_sync_tokens_only_access_no_refresh():
    """Syncs access_token even when refresh_token is absent in cred data.

    The old refresh_token in DB should be preserved.

    >>> test_sync_tokens_only_access_no_refresh()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        db = _make_db(Path(tmp))
        try:
            cred_data = {
                "_jackedAccountId": 1,
                "claudeAiOauth": {
                    "accessToken": "access_only_token",
                },
            }
            result = sync_credential_tokens(db, cred_data)
            assert result is True

            acct = db.get_account(1)
            assert acct["access_token"] == "access_only_token"
            assert acct["refresh_token"] == "old_refresh"  # preserved
        finally:
            db.close()


# ------------------------------------------------------------------
# refresh_all_expiring_tokens: skip active account
# ------------------------------------------------------------------


def test_refresh_skips_active_account_by_jacked_id():
    """Active account (via _jackedAccountId) with valid token is skipped.

    >>> test_refresh_skips_active_account_by_jacked_id()
    """
    import asyncio
    from jacked.web.auth import refresh_all_expiring_tokens

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            db.update_account(
                1, expires_at=9999999999, refresh_token="rt_alice", validation_status="valid"
            )

            cred_dir = tmp_path / ".claude"
            cred_dir.mkdir()
            cred_path = cred_dir / ".credentials.json"
            cred_path.write_text(
                json.dumps(
                    {
                        "_jackedAccountId": 1,
                        "claudeAiOauth": {"accessToken": "whatever"},
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch("jacked.web.auth.Database", return_value=db),
                mock.patch("jacked.web.auth.Path.home", return_value=tmp_path),
                mock.patch("jacked.web.auth.refresh_account_token") as mock_refresh,
            ):
                mock_refresh.return_value = True
                asyncio.get_event_loop().run_until_complete(
                    refresh_all_expiring_tokens(buffer_seconds=999999)
                )

            for call_args in mock_refresh.call_args_list:
                assert call_args[0][0] != 1, "Should NOT refresh active account 1"
        finally:
            db.close()


def test_refresh_skips_active_account_by_email_fallback():
    """Active account identified via email fallback with valid token is also skipped.

    >>> test_refresh_skips_active_account_by_email_fallback()
    """
    import asyncio
    from jacked.web.auth import refresh_all_expiring_tokens

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            db.update_account(
                2, expires_at=9999999999, refresh_token="rt_bob", validation_status="valid"
            )

            cred_dir = tmp_path / ".claude"
            cred_dir.mkdir()
            cred_path = cred_dir / ".credentials.json"
            cred_path.write_text(
                json.dumps(
                    {
                        "claudeAiOauth": {"accessToken": "whatever"},
                    }
                ),
                encoding="utf-8",
            )

            config_path = tmp_path / ".claude.json"
            config_path.write_text(
                json.dumps({"oauthAccount": {"emailAddress": "bob@test.com"}}),
                encoding="utf-8",
            )

            with (
                mock.patch("jacked.web.auth.Database", return_value=db),
                mock.patch("jacked.web.auth.Path.home", return_value=tmp_path),
                mock.patch("jacked.web.auth.refresh_account_token") as mock_refresh,
            ):
                mock_refresh.return_value = True
                asyncio.get_event_loop().run_until_complete(
                    refresh_all_expiring_tokens(buffer_seconds=999999)
                )

            for call_args in mock_refresh.call_args_list:
                assert call_args[0][0] != 2, "Should NOT refresh active account 2 (bob)"
        finally:
            db.close()


def test_refresh_does_not_skip_expired_active_account():
    """Active account with expired token IS refreshed (jacked steps in).

    >>> test_refresh_does_not_skip_expired_active_account()
    """
    import asyncio
    from jacked.web.auth import refresh_all_expiring_tokens

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            db.update_account(
                1, expires_at=0, refresh_token="rt_alice", validation_status="valid"
            )

            cred_dir = tmp_path / ".claude"
            cred_dir.mkdir()
            cred_path = cred_dir / ".credentials.json"
            cred_path.write_text(
                json.dumps(
                    {
                        "_jackedAccountId": 1,
                        "claudeAiOauth": {"accessToken": "whatever"},
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch("jacked.web.auth.Database", return_value=db),
                mock.patch("jacked.web.auth.Path.home", return_value=tmp_path),
                mock.patch("jacked.web.auth.refresh_account_token") as mock_refresh,
            ):
                mock_refresh.return_value = True
                asyncio.get_event_loop().run_until_complete(
                    refresh_all_expiring_tokens(buffer_seconds=999999)
                )

            # Active account with expired token SHOULD be refreshed
            refreshed_ids = [c[0][0] for c in mock_refresh.call_args_list]
            assert 1 in refreshed_ids, "Should refresh expired active account 1"
            # Verify is_active_account=True was passed
            for call_args in mock_refresh.call_args_list:
                if call_args[0][0] == 1:
                    assert call_args[1].get("is_active_account") is True, (
                        "Should pass is_active_account=True for active account"
                    )
        finally:
            db.close()


def test_active_account_invalid_grant_not_marked_invalid():
    """Active account getting invalid_grant is NOT marked invalid (race with Claude Code).

    >>> test_active_account_invalid_grant_not_marked_invalid()
    """
    import asyncio
    from jacked.web.auth import refresh_account_token

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            db.update_account(
                1, expires_at=0, refresh_token="rt_alice", validation_status="valid"
            )

            # httpx.Response uses sync .json(), so use MagicMock (not AsyncMock)
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 400
            mock_resp.json.return_value = {"error": "invalid_grant"}

            mock_client = mock.AsyncMock()
            mock_client.__aenter__ = mock.AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = mock.AsyncMock(return_value=False)
            mock_client.post.return_value = mock_resp

            with mock.patch("jacked.web.auth.httpx.AsyncClient", return_value=mock_client):
                result = asyncio.get_event_loop().run_until_complete(
                    refresh_account_token(1, db, is_active_account=True)
                )

            assert result is False
            account = db.get_account(1)
            assert account["validation_status"] == "valid", (
                "Active account should NOT be marked invalid on invalid_grant"
            )
        finally:
            db.close()


def test_non_active_account_invalid_grant_marked_invalid():
    """Non-active account getting invalid_grant IS marked invalid (normal behavior).

    >>> test_non_active_account_invalid_grant_marked_invalid()
    """
    import asyncio
    from jacked.web.auth import refresh_account_token

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            db.update_account(
                1, expires_at=0, refresh_token="rt_alice", validation_status="valid"
            )

            # httpx.Response uses sync .json(), so use MagicMock (not AsyncMock)
            mock_resp = mock.MagicMock()
            mock_resp.status_code = 400
            mock_resp.json.return_value = {"error": "invalid_grant"}

            mock_client = mock.AsyncMock()
            mock_client.__aenter__ = mock.AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = mock.AsyncMock(return_value=False)
            mock_client.post.return_value = mock_resp

            with mock.patch("jacked.web.auth.httpx.AsyncClient", return_value=mock_client):
                result = asyncio.get_event_loop().run_until_complete(
                    refresh_account_token(1, db, is_active_account=False)
                )

            assert result is False
            account = db.get_account(1)
            assert account["validation_status"] == "invalid", (
                "Non-active account SHOULD be marked invalid on invalid_grant"
            )
        finally:
            db.close()


# ------------------------------------------------------------------
# sync_credential_tokens: Layer 2 — token match
# ------------------------------------------------------------------


def test_sync_tokens_via_token_match():
    """Layer 2 (exact token match) works when stamp is absent.

    >>> test_sync_tokens_via_token_match()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            # No _jackedAccountId, but token matches bob's old_access→bob_access
            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "bob_access",  # matches account 2
                },
            }
            # No .claude.json — so email fallback won't fire either
            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                result = sync_credential_tokens(db, cred_data)

            # Token matches, but it's unchanged → returns False (no-op)
            assert result is False

            # Now with a NEW token that doesn't match — should fall through
            cred_data2 = {
                "claudeAiOauth": {
                    "accessToken": "brand_new_token",
                },
            }
            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                result2 = sync_credential_tokens(db, cred_data2)

            # No match at all → False
            assert result2 is False
        finally:
            db.close()


def test_sync_tokens_token_match_with_changed_token():
    """Token match syncs when stamp absent but old token matches.

    Scenario: account has old_access in DB, cred file has new_token.
    Stamp is absent. Token match for new_token fails.
    Email fallback should pick up the account.

    >>> test_sync_tokens_token_match_with_changed_token()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            # Create .claude.json for email fallback
            config_path = tmp_path / ".claude.json"
            config_path.write_text(
                json.dumps({"oauthAccount": {"emailAddress": "alice@test.com"}}),
                encoding="utf-8",
            )

            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "totally_new_token",
                    "refreshToken": "new_refresh",
                },
            }

            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                result = sync_credential_tokens(db, cred_data)

            assert result is True
            acct = db.get_account(1)
            assert acct["access_token"] == "totally_new_token"
        finally:
            db.close()


# ------------------------------------------------------------------
# re_stamp_jacked_account_id
# ------------------------------------------------------------------


def test_re_stamp_adds_missing_stamp():
    """Re-stamping writes _jackedAccountId when it's missing from cred file.

    >>> test_re_stamp_adds_missing_stamp()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            cred_path = tmp_path / ".credentials.json"
            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "bob_access",  # matches account 2
                },
            }
            cred_path.write_text(json.dumps(cred_data), encoding="utf-8")

            mtime = re_stamp_jacked_account_id(db, cred_data, cred_path)

            assert mtime is not None
            # Verify stamp was written to file
            updated = json.loads(cred_path.read_text(encoding="utf-8"))
            assert updated["_jackedAccountId"] == 2
        finally:
            db.close()


def test_re_stamp_skips_existing_stamp():
    """Re-stamping is a no-op when stamp already exists.

    >>> test_re_stamp_skips_existing_stamp()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            cred_path = tmp_path / ".credentials.json"
            cred_data = {
                "_jackedAccountId": 1,
                "claudeAiOauth": {
                    "accessToken": "bob_access",
                },
            }
            cred_path.write_text(json.dumps(cred_data), encoding="utf-8")

            mtime = re_stamp_jacked_account_id(db, cred_data, cred_path)

            assert mtime is None  # No write needed
        finally:
            db.close()


def test_re_stamp_falls_back_to_email():
    """Re-stamp uses email fallback when token doesn't match any account.

    >>> test_re_stamp_falls_back_to_email()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            # Create .claude.json for email fallback
            config_path = tmp_path / ".claude.json"
            config_path.write_text(
                json.dumps({"oauthAccount": {"emailAddress": "alice@test.com"}}),
                encoding="utf-8",
            )

            cred_path = tmp_path / ".credentials.json"
            cred_data = {
                "claudeAiOauth": {
                    "accessToken": "unknown_token",  # no match
                },
            }
            cred_path.write_text(json.dumps(cred_data), encoding="utf-8")

            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                mtime = re_stamp_jacked_account_id(db, cred_data, cred_path)

            assert mtime is not None
            updated = json.loads(cred_path.read_text(encoding="utf-8"))
            assert updated["_jackedAccountId"] == 1  # alice
        finally:
            db.close()


# ------------------------------------------------------------------
# create_missing_credentials_file
# ------------------------------------------------------------------


def test_create_missing_credentials_file():
    """Creates .credentials.json with default account when file is missing.

    >>> test_create_missing_credentials_file()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            # Set priority so account 2 (bob) is the default
            db.update_account(1, priority=1)
            db.update_account(2, priority=0)

            cred_dir = tmp_path / ".claude"
            cred_dir.mkdir()
            cred_path = cred_dir / ".credentials.json"
            # File does NOT exist

            with (
                mock.patch(
                    "jacked.api.credential_sync.Path.home", return_value=tmp_path
                ),
                mock.patch(
                    "jacked.api.credential_sync.read_platform_credentials",
                    return_value=None,
                ),
            ):
                mtime = create_missing_credentials_file(db)

            assert mtime is not None
            assert cred_path.exists()
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            assert data["_jackedAccountId"] == 2  # bob (priority=0)
            assert data["claudeAiOauth"]["accessToken"] == "bob_access"
        finally:
            db.close()


def test_create_missing_noop_when_exists():
    """Does nothing when .credentials.json already exists.

    >>> test_create_missing_noop_when_exists()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            cred_dir = tmp_path / ".claude"
            cred_dir.mkdir()
            cred_path = cred_dir / ".credentials.json"
            cred_path.write_text("{}", encoding="utf-8")

            with mock.patch(
                "jacked.api.credential_sync.Path.home", return_value=tmp_path
            ):
                mtime = create_missing_credentials_file(db)

            assert mtime is None
        finally:
            db.close()


# ------------------------------------------------------------------
# reassign_sessions
# ------------------------------------------------------------------


def test_reassign_sessions():
    """Batch-reassigns sessions from one account to another.

    >>> test_reassign_sessions()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            # Create some sessions under account 2 (bob)
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

            # Verify both account_id AND email were updated
            records = db.get_session_accounts("sess-1")
            assert records[0]["account_id"] == 1
            assert records[0]["email"] == "alice@test.com"
        finally:
            db.close()


def test_reassign_sessions_validates_target():
    """Raises ValueError if target account is deleted.

    >>> test_reassign_sessions_validates_target()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            import pytest
            with pytest.raises(ValueError, match="not found"):
                db.reassign_sessions(
                    from_account_id=1,
                    to_account_id=3,  # deleted account (excluded by get_account)
                    since_iso="2000-01-01T00:00:00Z",
                )
        finally:
            db.close()


# ------------------------------------------------------------------
# read_platform_credentials: macOS Keychain
# ------------------------------------------------------------------


def test_read_platform_credentials_macos():
    """Reads credentials from macOS Keychain when on darwin.

    >>> test_read_platform_credentials_macos()
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
        mock.patch("jacked.api.credential_sync.sys") as mock_sys,
        mock.patch("jacked.api.credential_sync.subprocess.run", return_value=mock_result),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is not None
    assert result["claudeAiOauth"]["accessToken"] == "keychain_token"


def test_read_platform_credentials_linux():
    """Returns None immediately on Linux (no keychain support yet).

    >>> test_read_platform_credentials_linux()
    """
    with mock.patch("jacked.api.credential_sync.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = read_platform_credentials()

    assert result is None


def test_read_platform_credentials_keychain_not_found():
    """Returns None when keychain entry doesn't exist.

    >>> test_read_platform_credentials_keychain_not_found()
    """
    mock_result = mock.MagicMock()
    mock_result.returncode = 44  # security command: item not found
    mock_result.stdout = ""
    mock_result.stderr = "The specified item could not be found in the keychain."

    with (
        mock.patch("jacked.api.credential_sync.sys") as mock_sys,
        mock.patch("jacked.api.credential_sync.subprocess.run", return_value=mock_result),
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
        mock.patch("jacked.api.credential_sync.sys") as mock_sys,
        mock.patch("jacked.api.credential_sync.subprocess.run", return_value=mock_result),
    ):
        mock_sys.platform = "darwin"
        result = read_platform_credentials()

    assert result is None


# ------------------------------------------------------------------
# create_missing_credentials_file: keychain path
# ------------------------------------------------------------------


def test_create_missing_cred_file_from_keychain():
    """Creates .credentials.json from keychain data when file is missing.

    >>> test_create_missing_cred_file_from_keychain()
    """
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            cred_dir = tmp_path / ".claude"
            cred_dir.mkdir()
            cred_path = cred_dir / ".credentials.json"
            # File does NOT exist

            keychain_data = {
                "claudeAiOauth": {
                    "accessToken": "bob_access",  # matches account 2
                    "refreshToken": "keychain_refresh",
                }
            }

            with (
                mock.patch(
                    "jacked.api.credential_sync.Path.home", return_value=tmp_path
                ),
                mock.patch(
                    "jacked.api.credential_sync.read_platform_credentials",
                    return_value=keychain_data,
                ),
            ):
                mtime = create_missing_credentials_file(db)

            assert mtime is not None
            assert cred_path.exists()
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            assert data["_jackedAccountId"] == 2  # matched via token
            assert data["claudeAiOauth"]["accessToken"] == "bob_access"

            # Verify token was synced to DB
            acct = db.get_account(2)
            assert acct["validation_status"] == "valid"
        finally:
            db.close()


# ------------------------------------------------------------------
# write_platform_credentials: macOS Keychain
# ------------------------------------------------------------------


def test_write_platform_credentials_macos():
    """Writes credentials to macOS Keychain via security commands.

    >>> test_write_platform_credentials_macos()
    """
    cred_data = {
        "_jackedAccountId": 1,
        "claudeAiOauth": {"accessToken": "test_token"},
    }
    mock_delete = mock.MagicMock()
    mock_delete.returncode = 0
    mock_add = mock.MagicMock()
    mock_add.returncode = 0

    with (
        mock.patch("jacked.api.credential_sync.sys") as mock_sys,
        mock.patch(
            "jacked.api.credential_sync.subprocess.run",
            side_effect=[mock_delete, mock_add],
        ) as mock_run,
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials(cred_data)

    assert result is True
    assert mock_run.call_count == 2
    # First call: delete existing
    delete_args = mock_run.call_args_list[0][0][0]
    assert "delete-generic-password" in delete_args
    # Second call: add new
    add_args = mock_run.call_args_list[1][0][0]
    assert "add-generic-password" in add_args
    assert "Claude Code-credentials" in add_args
    # Verify JSON data was passed via -w flag
    w_index = add_args.index("-w")
    json_str = add_args[w_index + 1]
    parsed = json.loads(json_str)
    assert parsed["_jackedAccountId"] == 1


def test_write_platform_credentials_linux_noop():
    """Returns True (no-op) on Linux — file write is sufficient.

    >>> test_write_platform_credentials_linux_noop()
    """
    with mock.patch("jacked.api.credential_sync.sys") as mock_sys:
        mock_sys.platform = "linux"
        result = write_platform_credentials({"claudeAiOauth": {"accessToken": "x"}})

    assert result is True


def test_write_platform_credentials_keychain_error():
    """Returns False when keychain add command fails.

    >>> test_write_platform_credentials_keychain_error()
    """
    mock_delete = mock.MagicMock()
    mock_delete.returncode = 0
    mock_add = mock.MagicMock()
    mock_add.returncode = 1
    mock_add.stderr = "errSecAuthFailed"

    with (
        mock.patch("jacked.api.credential_sync.sys") as mock_sys,
        mock.patch(
            "jacked.api.credential_sync.subprocess.run",
            side_effect=[mock_delete, mock_add],
        ),
    ):
        mock_sys.platform = "darwin"
        result = write_platform_credentials({"claudeAiOauth": {"accessToken": "x"}})

    assert result is False
