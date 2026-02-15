"""Unit tests for layered active-credential matching.

Tests the 3-layer matching logic used by both get_active_credential (API)
and _match_token_to_account (session tracker hook):
  Layer 1: _jackedAccountId stamp — strongest, explicitly set by jacked.
  Layer 2: Exact access_token match — cryptographically unique.
  Layer 3: Email from ~/.claude.json — weakest, Claude Code can change independently.

Also tests the _update_claude_config_email write helper.
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest import mock

from jacked.data.hooks.session_account_tracker import _match_token_to_account
from jacked.api.routes.credentials import _update_claude_config_email


def _create_test_db(tmp_dir: Path) -> Path:
    """Create a test DB with accounts table and sample data.

    >>> import tempfile; from pathlib import Path
    >>> d = Path(tempfile.mkdtemp())
    >>> p = _create_test_db(d)
    >>> p.exists()
    True
    """
    db_path = tmp_dir / "jacked.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("""CREATE TABLE accounts (
        id INTEGER PRIMARY KEY,
        email TEXT,
        access_token TEXT,
        is_deleted INTEGER DEFAULT 0,
        priority INTEGER DEFAULT 0
    )""")
    conn.execute(
        "INSERT INTO accounts (id, email, access_token, is_deleted, priority) "
        "VALUES (1, 'alice@test.com', 'tok_alice', 0, 0)"
    )
    conn.execute(
        "INSERT INTO accounts (id, email, access_token, is_deleted, priority) "
        "VALUES (2, 'bob@test.com', 'tok_bob', 0, 1)"
    )
    conn.execute(
        "INSERT INTO accounts (id, email, access_token, is_deleted, priority) "
        "VALUES (3, 'deleted@test.com', 'tok_del', 1, 2)"
    )
    conn.commit()
    conn.close()
    return db_path


# ------------------------------------------------------------------
# Layer 1: ~/.claude.json email matching
# ------------------------------------------------------------------


def test_layer1_email_match():
    """Layer 1 matches by email from ~/.claude.json.

    >>> test_layer1_email_match()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "bob@test.com"}}),
            encoding="utf-8",
        )

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("wrong_token")

        assert acct_id == 2
        assert email == "bob@test.com"


def test_layer1_case_insensitive_email():
    """Layer 1 matches case-insensitively (DB has lowercase, config has mixed).

    >>> test_layer1_case_insensitive_email()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "Alice@Test.COM"}}),
            encoding="utf-8",
        )

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("wrong_token")

        assert acct_id == 1
        assert email == "alice@test.com"


def test_layer1_email_no_match_falls_through():
    """Layer 1 falls through when email doesn't match any account.

    >>> test_layer1_email_no_match_falls_through()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "unknown@test.com"}}),
            encoding="utf-8",
        )

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("tok_alice")

        # Falls through to Layer 3 (token match)
        assert acct_id == 1
        assert email == "alice@test.com"


def test_layer1_missing_config_falls_through():
    """Layer 1 falls through when ~/.claude.json doesn't exist.

    >>> test_layer1_missing_config_falls_through()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / "nonexistent.json"

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("tok_bob")

        assert acct_id == 2
        assert email == "bob@test.com"


def test_layer1_corrupt_config_falls_through():
    """Layer 1 falls through when ~/.claude.json is corrupt.

    >>> test_layer1_corrupt_config_falls_through()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / ".claude.json"
        config_path.write_text("not valid json{{{", encoding="utf-8")

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("tok_alice")

        assert acct_id == 1
        assert email == "alice@test.com"


# ------------------------------------------------------------------
# Layer 2: _jackedAccountId via cred_data parameter
# ------------------------------------------------------------------


def test_layer2_jacked_account_id():
    """Layer 2 matches by _jackedAccountId passed via cred_data.

    >>> test_layer2_jacked_account_id()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / "nonexistent.json"
        cred_data = {
            "_jackedAccountId": 2,
            "claudeAiOauth": {"accessToken": "wrong_token"},
        }

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("wrong_token", cred_data=cred_data)

        assert acct_id == 2
        assert email == "bob@test.com"


def test_layer2_deleted_account_falls_through():
    """Layer 2 skips deleted accounts and falls through to Layer 3.

    >>> test_layer2_deleted_account_falls_through()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / "nonexistent.json"
        cred_data = {
            "_jackedAccountId": 3,
            "claudeAiOauth": {"accessToken": "tok_alice"},
        }

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("tok_alice", cred_data=cred_data)

        # Layer 2 skips deleted, Layer 3 matches token
        assert acct_id == 1
        assert email == "alice@test.com"


# ------------------------------------------------------------------
# Layer 3: Exact access_token match (fallback)
# ------------------------------------------------------------------


def test_layer3_token_match():
    """Layer 3 matches by exact access_token.

    >>> test_layer3_token_match()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / "nonexistent.json"

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("tok_alice")

        assert acct_id == 1
        assert email == "alice@test.com"


# ------------------------------------------------------------------
# All layers miss
# ------------------------------------------------------------------


def test_all_layers_miss():
    """Returns (None, None) when all layers fail to match.

    >>> test_all_layers_miss()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / "nonexistent.json"

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("totally_unknown")

        assert acct_id is None
        assert email is None


def test_no_db_returns_none():
    """Returns (None, None) when DB doesn't exist.

    >>> test_no_db_returns_none()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "nonexistent.db"

        with mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path):
            acct_id, email = _match_token_to_account("tok_alice")

        assert acct_id is None
        assert email is None


# ------------------------------------------------------------------
# Layer priority: Layer 1 (_jackedAccountId) wins over Layer 2 and 3
# ------------------------------------------------------------------


def test_layer1_stamp_takes_priority_over_token_and_email():
    """Layer 1 (_jackedAccountId stamp) wins even when token and email point elsewhere.

    >>> test_layer1_stamp_takes_priority_over_token_and_email()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "alice@test.com"}}),
            encoding="utf-8",
        )
        # _jackedAccountId=2 (bob), but token matches bob and email matches alice
        cred_data = {"_jackedAccountId": 2, "claudeAiOauth": {"accessToken": "tok_bob"}}

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("tok_bob", cred_data=cred_data)

        # Layer 1 (_jackedAccountId=2) wins: bob, not alice
        assert acct_id == 2
        assert email == "bob@test.com"


# ------------------------------------------------------------------
# _update_claude_config_email write-path tests
# ------------------------------------------------------------------


def test_write_helper_updates_existing_file():
    """When .claude.json exists, only emailAddress changes; other keys preserved.

    >>> test_write_helper_updates_existing_file()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps(
                {
                    "someOtherKey": "preserved",
                    "oauthAccount": {
                        "emailAddress": "old@test.com",
                        "displayName": "Old Name",
                    },
                }
            ),
            encoding="utf-8",
        )

        with mock.patch(
            "jacked.api.routes.credentials.Path.home", return_value=tmp_path
        ):
            _update_claude_config_email("new@test.com")

        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["oauthAccount"]["emailAddress"] == "new@test.com"
        assert result["oauthAccount"]["displayName"] == "Old Name"  # preserved
        assert result["someOtherKey"] == "preserved"


def test_write_helper_creates_missing_file():
    """When .claude.json doesn't exist, creates it with email.

    >>> test_write_helper_creates_missing_file()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)

        with mock.patch(
            "jacked.api.routes.credentials.Path.home", return_value=tmp_path
        ):
            _update_claude_config_email("new@test.com", display_name="New User")

        config_path = tmp_path / ".claude.json"
        assert config_path.exists()
        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["oauthAccount"]["emailAddress"] == "new@test.com"
        assert result["oauthAccount"]["displayName"] == "New User"


def test_write_helper_handles_corrupt_file():
    """When .claude.json is corrupt, creates fresh config.

    >>> test_write_helper_handles_corrupt_file()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        config_path = tmp_path / ".claude.json"
        config_path.write_text("not json{{{", encoding="utf-8")

        with mock.patch(
            "jacked.api.routes.credentials.Path.home", return_value=tmp_path
        ):
            _update_claude_config_email("fresh@test.com")

        result = json.loads(config_path.read_text(encoding="utf-8"))
        assert result["oauthAccount"]["emailAddress"] == "fresh@test.com"


def test_write_helper_refuses_symlink():
    """When .claude.json is a symlink, refuses to write.

    >>> test_write_helper_refuses_symlink()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        target = tmp_path / "target.json"
        target.write_text("{}", encoding="utf-8")
        link = tmp_path / ".claude.json"
        try:
            link.symlink_to(target)
        except OSError:
            # Symlinks may require elevated privileges on Windows
            return

        with mock.patch(
            "jacked.api.routes.credentials.Path.home", return_value=tmp_path
        ):
            _update_claude_config_email("evil@test.com")

        # Target should be unchanged
        result = json.loads(target.read_text(encoding="utf-8"))
        assert "oauthAccount" not in result


# ------------------------------------------------------------------
# Layer 1: deleted account email falls through (YELLOW-5)
# ------------------------------------------------------------------


def test_layer1_deleted_email_falls_through():
    """Layer 1 skips deleted accounts even when email matches.

    >>> test_layer1_deleted_email_falls_through()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "deleted@test.com"}}),
            encoding="utf-8",
        )

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("tok_alice")

        # Layer 1 skips deleted, falls through to Layer 3 token match
        assert acct_id == 1
        assert email == "alice@test.com"


def test_layer1_deleted_email_no_token_returns_none():
    """Layer 1 skips deleted email, no token match either -> (None, None).

    >>> test_layer1_deleted_email_no_token_returns_none()
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = _create_test_db(tmp_path)
        config_path = tmp_path / ".claude.json"
        config_path.write_text(
            json.dumps({"oauthAccount": {"emailAddress": "deleted@test.com"}}),
            encoding="utf-8",
        )

        with (
            mock.patch("jacked.data.hooks.session_account_tracker.DB_PATH", db_path),
            mock.patch(
                "jacked.data.hooks.session_account_tracker.CLAUDE_CONFIG", config_path
            ),
        ):
            acct_id, email = _match_token_to_account("no_such_token")

        assert acct_id is None
        assert email is None


# ------------------------------------------------------------------
# use_account integration test (YELLOW-3)
# ------------------------------------------------------------------


def test_use_account_writes_correct_credential_format():
    """Full endpoint path: DB account -> credential file with correct format.

    Verifies expiresAt * 1000 multiplication and _jackedAccountId embedding.

    >>> test_use_account_writes_correct_credential_format()
    """
    from fastapi.testclient import TestClient
    from jacked.web.database import Database

    tmp_path = Path(tempfile.mkdtemp())
    db_file = str(tmp_path / "test.db")
    db = Database(db_file)

    # Insert a test account directly
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status, scopes, subscription_type, rate_limit_tier)
               VALUES (42, 'test@example.com', 'tok_test_123', 'ref_test', 1800000000,
                       1, 0, 'valid', '["user:read"]', 'pro', 'tier_1')"""
        )

    # Create a minimal FastAPI app with the credentials router
    from fastapi import FastAPI
    from jacked.api.routes.credentials import router

    app = FastAPI()
    app.state.db = db
    app.state.cred_last_written_mtime = None
    app.include_router(router, prefix="/api/auth")
    client = TestClient(app)

    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()

    with mock.patch("jacked.api.routes.credentials.Path.home", return_value=tmp_path):
        resp = client.post("/api/auth/accounts/42/use")

    assert resp.status_code == 200
    assert resp.json()["email"] == "test@example.com"

    # Verify credential file
    cred_path = claude_dir / ".credentials.json"
    assert cred_path.exists()
    cred = json.loads(cred_path.read_text(encoding="utf-8"))

    assert cred["_jackedAccountId"] == 42
    assert cred["claudeAiOauth"]["accessToken"] == "tok_test_123"
    assert cred["claudeAiOauth"]["refreshToken"] == "ref_test"
    # expiresAt should be epoch seconds * 1000 (milliseconds for JS)
    assert cred["claudeAiOauth"]["expiresAt"] == 1800000000 * 1000
    assert cred["claudeAiOauth"]["scopes"] == ["user:read"]
    assert cred["claudeAiOauth"]["subscriptionType"] == "pro"
    assert cred["claudeAiOauth"]["rateLimitTier"] == "tier_1"

    # Verify .claude.json was also created/updated
    config_path = tmp_path / ".claude.json"
    assert config_path.exists()
    config = json.loads(config_path.read_text(encoding="utf-8"))
    assert config["oauthAccount"]["emailAddress"] == "test@example.com"
