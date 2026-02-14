"""Unit tests for credential file -> DB token sync.

Tests sync_credential_tokens() which runs inside the credential file
watcher loop when Claude Code independently refreshes OAuth tokens.
Also tests the skip-active-account logic in refresh_all_expiring_tokens().
"""

import json
import os
import tempfile
from pathlib import Path
from unittest import mock

from jacked.api.credential_sync import sync_credential_tokens
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
    """Active account (via _jackedAccountId) is skipped in background refresh.

    >>> test_refresh_skips_active_account_by_jacked_id()
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

            for call_args in mock_refresh.call_args_list:
                assert call_args[0][0] != 1, "Should NOT refresh active account 1"
        finally:
            db.close()


def test_refresh_skips_active_account_by_email_fallback():
    """Active account identified via email fallback is also skipped.

    >>> test_refresh_skips_active_account_by_email_fallback()
    """
    import asyncio
    from jacked.web.auth import refresh_all_expiring_tokens

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        db = _make_db(tmp_path)
        try:
            db.update_account(
                2, expires_at=0, refresh_token="rt_bob", validation_status="valid"
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
