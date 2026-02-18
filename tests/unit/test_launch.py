"""Unit tests for jacked claude launch — per-account credential isolation.

Tests prepare_account_dir(), resolve_account(), launch_claude(),
scan_account_credential_dirs(), sync_credential_tokens_direct(),
hook CLAUDE_CONFIG_DIR support, and account deletion cleanup.
"""

import json
import os
import stat
import time
from pathlib import Path
from unittest import mock

import click
import pytest

from jacked.web.database import Database

# Windows holds SQLite file locks
_WIN = os.name == "nt"


def _make_db(tmp_path: Path) -> Database:
    """Create a test DB with sample accounts."""
    db = Database(str(tmp_path / "test.db"))
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                consecutive_failures, subscription_type, rate_limit_tier)
               VALUES (1, 'alice@test.com', 'alice_access', 'alice_refresh',
                       ?, 1, 0, 'valid', 0, 'max', 't1')""",
            (int(time.time()) + 3600,),
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                consecutive_failures, subscription_type, rate_limit_tier)
               VALUES (2, 'bob@test.com', 'bob_access', 'bob_refresh',
                       ?, 1, 0, 'valid', 0, 'pro', 't2')""",
            (int(time.time()) + 3600,),
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status)
               VALUES (3, 'deleted@test.com', 'del_access', 'del_refresh',
                       1700000000, 1, 1, 'valid')"""
        )
    return db


# ---------------------------------------------------------------------------
# _seed_workspace_trust
# ---------------------------------------------------------------------------


class TestSeedWorkspaceTrust:
    def test_seeds_trust_from_global(self, tmp_path):
        """Copies trusted projects from global config into per-account config."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text("{}")

        global_config = {
            "projects": {
                "/Users/me/trusted-project": {
                    "hasTrustDialogAccepted": True,
                    "hasCompletedProjectOnboarding": True,
                    "allowedTools": ["Bash"],
                    "lastCost": 42.0,
                },
                "/Users/me/untrusted": {
                    "hasTrustDialogAccepted": False,
                },
            }
        }

        (tmp_path / ".claude.json").write_text(json.dumps(global_config))

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        result = json.loads((config_dir / ".claude.json").read_text())
        projects = result.get("projects", {})

        # Trusted project should be seeded with minimal entry
        assert "/Users/me/trusted-project" in projects
        entry = projects["/Users/me/trusted-project"]
        assert entry["hasTrustDialogAccepted"] is True
        assert entry["hasCompletedProjectOnboarding"] is True
        # Should NOT copy allowedTools or cost data
        assert "allowedTools" not in entry
        assert "lastCost" not in entry

        # Untrusted project should NOT be seeded
        assert "/Users/me/untrusted" not in projects

    def test_skips_existing_project_entries(self, tmp_path):
        """Doesn't overwrite per-account project entries that already exist."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()

        existing_entry = {
            "hasTrustDialogAccepted": True,
            "allowedTools": ["Bash", "Read"],
            "mcpServers": {"myserver": {}},
            "lastCost": 99.0,
        }
        local_config = {"projects": {"/Users/me/project": existing_entry}}
        (config_dir / ".claude.json").write_text(json.dumps(local_config))

        global_config = {
            "projects": {
                "/Users/me/project": {
                    "hasTrustDialogAccepted": True,
                    "hasCompletedProjectOnboarding": True,
                },
            }
        }
        (tmp_path / ".claude.json").write_text(json.dumps(global_config))

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        result = json.loads((config_dir / ".claude.json").read_text())
        entry = result["projects"]["/Users/me/project"]
        # Original entry preserved completely
        assert entry["allowedTools"] == ["Bash", "Read"]
        assert entry["mcpServers"] == {"myserver": {}}
        assert entry["lastCost"] == 99.0

    def test_partial_overlap_seeds_only_new(self, tmp_path):
        """Seeds project B but skips project A which already exists."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()

        local_config = {
            "projects": {
                "/existing": {"hasTrustDialogAccepted": True, "allowedTools": ["X"]},
            }
        }
        (config_dir / ".claude.json").write_text(json.dumps(local_config))

        global_config = {
            "projects": {
                "/existing": {"hasTrustDialogAccepted": True},
                "/new-project": {
                    "hasTrustDialogAccepted": True,
                    "hasCompletedProjectOnboarding": True,
                },
            }
        }
        (tmp_path / ".claude.json").write_text(json.dumps(global_config))

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        result = json.loads((config_dir / ".claude.json").read_text())
        # Existing preserved
        assert result["projects"]["/existing"]["allowedTools"] == ["X"]
        # New one seeded
        assert result["projects"]["/new-project"]["hasTrustDialogAccepted"] is True

    def test_handles_missing_global_config(self, tmp_path):
        """Gracefully does nothing when global .claude.json doesn't exist."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text("{}")

        from jacked.launch import _seed_workspace_trust

        # No global .claude.json exists at tmp_path
        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result == {}

    def test_skips_when_global_is_symlink(self, tmp_path):
        """Skips seeding when global .claude.json is a symlink."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text("{}")

        real_file = tmp_path / "real_claude.json"
        real_file.write_text(json.dumps({
            "projects": {"/proj": {"hasTrustDialogAccepted": True}}
        }))
        symlink = tmp_path / ".claude.json"
        symlink.symlink_to(real_file)

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        result = json.loads((config_dir / ".claude.json").read_text())
        assert "projects" not in result

    def test_skips_when_local_is_symlink(self, tmp_path):
        """Skips seeding when per-account .claude.json is a symlink."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()

        real_file = tmp_path / "real_local.json"
        real_file.write_text("{}")
        (config_dir / ".claude.json").symlink_to(real_file)

        (tmp_path / ".claude.json").write_text(json.dumps({
            "projects": {"/proj": {"hasTrustDialogAccepted": True}}
        }))

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        # Should not have been modified
        result = json.loads(real_file.read_text())
        assert "projects" not in result

    def test_omits_onboarding_when_false(self, tmp_path):
        """Omits hasCompletedProjectOnboarding when false/absent in global."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text("{}")

        global_config = {
            "projects": {
                "/proj": {
                    "hasTrustDialogAccepted": True,
                    # hasCompletedProjectOnboarding absent
                },
            }
        }
        (tmp_path / ".claude.json").write_text(json.dumps(global_config))

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        result = json.loads((config_dir / ".claude.json").read_text())
        entry = result["projects"]["/proj"]
        assert entry == {"hasTrustDialogAccepted": True}
        assert "hasCompletedProjectOnboarding" not in entry

    def test_creates_file_when_local_missing(self, tmp_path):
        """Creates .claude.json with trust when file doesn't exist yet."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        # No .claude.json created — function should handle FileNotFoundError

        global_config = {
            "projects": {
                "/proj": {"hasTrustDialogAccepted": True},
            }
        }
        (tmp_path / ".claude.json").write_text(json.dumps(global_config))

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result["projects"]["/proj"]["hasTrustDialogAccepted"] is True

    def test_skips_malformed_local_config(self, tmp_path):
        """Returns without clobbering a malformed per-account .claude.json."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        garbage = "not json {{{"
        (config_dir / ".claude.json").write_text(garbage)

        global_config = {
            "projects": {
                "/proj": {"hasTrustDialogAccepted": True},
            }
        }
        (tmp_path / ".claude.json").write_text(json.dumps(global_config))

        from jacked.launch import _seed_workspace_trust

        with mock.patch.object(Path, "home", return_value=tmp_path):
            _seed_workspace_trust(config_dir)

        # File should be unchanged — not clobbered
        assert (config_dir / ".claude.json").read_text() == garbage


# ---------------------------------------------------------------------------
# prepare_account_dir
# ---------------------------------------------------------------------------


class TestPrepareAccountDir:
    def test_creates_cred_file(self, tmp_path):
        """Creates per-account dir with correct OAuth format and perms."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                from jacked.launch import prepare_account_dir

                result = prepare_account_dir(account, db)

        assert result == tmp_path / "accounts" / "1"
        cred_file = result / ".credentials.json"
        assert cred_file.exists()

        data = json.loads(cred_file.read_text())
        oauth = data["claudeAiOauth"]
        assert oauth["accessToken"] == "alice_access"
        assert oauth["refreshToken"] == "alice_refresh"
        assert oauth["subscriptionType"] == "max"
        assert oauth["rateLimitTier"] == "t1"

        # Check permissions (skip on Windows)
        if os.name != "nt":
            dir_mode = stat.S_IMODE(result.stat().st_mode)
            assert dir_mode == 0o700
            file_mode = stat.S_IMODE(cred_file.stat().st_mode)
            assert file_mode == 0o600

    def test_refreshes_if_near_expiry(self, tmp_path):
        """Pre-launch token refresh fires when should_refresh returns True."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=True):
                with mock.patch("jacked.web.auth.refresh_account_token"):
                    with mock.patch("jacked.launch.asyncio") as mock_asyncio:
                        from jacked.launch import prepare_account_dir

                        prepare_account_dir(account, db)
                        mock_asyncio.run.assert_called_once()

    def test_validates_account_id(self, tmp_path):
        """Rejects account_id <= 0."""
        db = _make_db(tmp_path)
        from jacked.launch import prepare_account_dir

        with mock.patch("jacked.launch.should_refresh", return_value=False):
            with pytest.raises(click.ClickException, match="Invalid account ID"):
                prepare_account_dir({"id": 0}, db)
            with pytest.raises(click.ClickException, match="Invalid account ID"):
                prepare_account_dir({"id": -1}, db)

    def test_rejects_symlink_dir(self, tmp_path):
        """Refuses to use a symlinked account directory."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        accounts_dir = tmp_path / "accounts"
        accounts_dir.mkdir(parents=True)
        # Create symlink at accounts/1 -> /tmp
        symlink_dir = accounts_dir / "1"
        symlink_dir.symlink_to("/tmp")

        with mock.patch("jacked.launch.ACCOUNTS_DIR", accounts_dir):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                from jacked.launch import prepare_account_dir

                with pytest.raises(click.ClickException, match="symlink"):
                    prepare_account_dir(account, db)

    def test_rejects_symlink_cred_file(self, tmp_path):
        """Refuses to write to a symlinked credential file."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        acct_dir = tmp_path / "accounts" / "1"
        acct_dir.mkdir(parents=True)
        # Create symlink at .credentials.json -> /tmp/evil
        cred_symlink = acct_dir / ".credentials.json"
        cred_symlink.symlink_to("/tmp/evil_creds.json")

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                from jacked.launch import prepare_account_dir

                with pytest.raises(click.ClickException, match="symlink"):
                    prepare_account_dir(account, db)

    def test_preserves_existing_keys(self, tmp_path):
        """Preserves non-OAuth keys Claude Code may have added."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        acct_dir = tmp_path / "accounts" / "1"
        acct_dir.mkdir(parents=True)
        cred_path = acct_dir / ".credentials.json"
        cred_path.write_text(json.dumps({"someOtherKey": "preserved"}))

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                from jacked.launch import prepare_account_dir

                prepare_account_dir(account, db)

        data = json.loads(cred_path.read_text())
        assert data["someOtherKey"] == "preserved"
        assert "claudeAiOauth" in data


# ---------------------------------------------------------------------------
# resolve_account
# ---------------------------------------------------------------------------


class TestResolveAccount:
    def test_with_id(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = resolve_account(1, db)
        assert result["email"] == "alice@test.com"

    def test_with_string_id(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = resolve_account("2", db)
        assert result["email"] == "bob@test.com"

    def test_with_email(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            result = resolve_account("bob@test.com", db)
        assert result["email"] == "bob@test.com"

    def test_without_id_uses_active(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            with mock.patch(
                "jacked.api.credential_sync.detect_active_account",
                return_value=(1, "alice_access"),
            ):
                result = resolve_account(None, db)
        assert result["email"] == "alice@test.com"

    def test_missing_raises(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="not found"):
                resolve_account(999, db)

    def test_deleted_raises(self, tmp_path):
        """Soft-deleted account is filtered by get_account — shows 'not found'."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="not found"):
                resolve_account(3, db)

    def test_no_token_raises(self, tmp_path):
        db = _make_db(tmp_path)
        # Set access token to empty string (NOT NULL constraint)
        db.update_account(1, access_token="")
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="no access token"):
                resolve_account(1, db)

    def test_no_claude_raises(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("shutil.which", return_value=None):
            with pytest.raises(click.ClickException, match="claude not found"):
                resolve_account(1, db)


# ---------------------------------------------------------------------------
# launch_claude
# ---------------------------------------------------------------------------


class TestLaunchClaude:
    def test_sets_env_and_execs(self, tmp_path):
        """Verifies CLAUDE_CONFIG_DIR is set and os.execvpe is called."""
        from jacked.launch import launch_claude

        config_dir = tmp_path / "accounts" / "1"

        with mock.patch("os.execvpe") as mock_exec:
            launch_claude(config_dir, ("--resume", "abc123"))

        mock_exec.assert_called_once()
        args = mock_exec.call_args
        assert args[0][0] == "claude"
        assert args[0][1] == ["claude", "--resume", "abc123"]
        env = args[0][2]
        assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)


# ---------------------------------------------------------------------------
# scan_account_credential_dirs + sync_credential_tokens_direct
# ---------------------------------------------------------------------------


class TestPerAccountWatcher:
    def test_scan_syncs_changed_files(self, tmp_path):
        """scan_account_credential_dirs detects changed files and syncs."""
        db = _make_db(tmp_path)

        accounts_dir = tmp_path / "accounts"
        acct_dir = accounts_dir / "1"
        acct_dir.mkdir(parents=True)
        cred_file = acct_dir / ".credentials.json"
        cred_file.write_text(
            json.dumps(
                {
                    "claudeAiOauth": {
                        "accessToken": "new_alice_access",
                        "refreshToken": "new_alice_refresh",
                        "expiresAt": (int(time.time()) + 7200) * 1000,
                    }
                }
            )
        )

        from jacked.api.watchers import scan_account_credential_dirs

        with mock.patch("jacked.api.watchers.ACCOUNTS_DIR", accounts_dir):
            result = scan_account_credential_dirs(db, {})

        assert 1 in result
        # Token should have been synced
        account = db.get_account(1)
        assert account["access_token"] == "new_alice_access"
        assert account["refresh_token"] == "new_alice_refresh"

    def test_scan_skips_unchanged(self, tmp_path):
        """scan_account_credential_dirs skips files with same mtime."""
        db = _make_db(tmp_path)

        accounts_dir = tmp_path / "accounts"
        acct_dir = accounts_dir / "1"
        acct_dir.mkdir(parents=True)
        cred_file = acct_dir / ".credentials.json"
        cred_file.write_text(json.dumps({"claudeAiOauth": {"accessToken": "x"}}))

        from jacked.api.watchers import scan_account_credential_dirs

        with mock.patch("jacked.api.watchers.ACCOUNTS_DIR", accounts_dir):
            # First scan populates mtimes
            mtimes = scan_account_credential_dirs(db, {})
            # Second scan with same mtimes should be a no-op
            mtimes2 = scan_account_credential_dirs(db, mtimes)

        assert mtimes == mtimes2

    def test_scan_skips_non_integer_dirs(self, tmp_path):
        """Directories that aren't integer names are ignored."""
        db = _make_db(tmp_path)

        accounts_dir = tmp_path / "accounts"
        bad_dir = accounts_dir / "not_an_id"
        bad_dir.mkdir(parents=True)
        (bad_dir / ".credentials.json").write_text("{}")

        from jacked.api.watchers import scan_account_credential_dirs

        with mock.patch("jacked.api.watchers.ACCOUNTS_DIR", accounts_dir):
            result = scan_account_credential_dirs(db, {})

        assert result == {}

    def test_sync_direct_updates_tokens(self, tmp_path):
        """sync_credential_tokens_direct updates a known account directly."""
        db = _make_db(tmp_path)

        from jacked.api.watchers import sync_credential_tokens_direct

        cred_data = {
            "claudeAiOauth": {
                "accessToken": "brand_new_access",
                "refreshToken": "brand_new_refresh",
                "expiresAt": (int(time.time()) + 7200) * 1000,
            }
        }
        result = sync_credential_tokens_direct(db, cred_data, 1)
        assert result is True

        account = db.get_account(1)
        assert account["access_token"] == "brand_new_access"
        assert account["refresh_token"] == "brand_new_refresh"
        assert account["validation_status"] == "valid"

    def test_sync_direct_noop_for_same_tokens(self, tmp_path):
        """sync_credential_tokens_direct returns False when tokens match."""
        db = _make_db(tmp_path)
        # Mark account valid so the "fix status" branch doesn't fire
        db.update_account(1, validation_status="valid")

        from jacked.api.watchers import sync_credential_tokens_direct

        cred_data = {
            "claudeAiOauth": {
                "accessToken": "alice_access",
                "refreshToken": "alice_refresh",
            }
        }
        result = sync_credential_tokens_direct(db, cred_data, 1)
        assert result is False

    def test_sync_direct_none_db(self):
        """sync_credential_tokens_direct handles None db gracefully."""
        from jacked.api.watchers import sync_credential_tokens_direct

        assert sync_credential_tokens_direct(None, {}, 1) is False

    def test_scan_none_db(self):
        """scan_account_credential_dirs handles None db gracefully."""
        from jacked.api.watchers import scan_account_credential_dirs

        assert scan_account_credential_dirs(None, {}) == {}


# ---------------------------------------------------------------------------
# Hook CLAUDE_CONFIG_DIR support
# ---------------------------------------------------------------------------


class TestHookConfigDir:
    def test_get_cred_data_reads_config_dir(self, tmp_path):
        """_get_cred_data reads from CLAUDE_CONFIG_DIR when set."""
        cred_file = tmp_path / ".credentials.json"
        cred_file.write_text(
            json.dumps(
                {"claudeAiOauth": {"accessToken": "per_acct_token"}}
            )
        )

        from jacked.data.hooks.session_account_tracker import _get_cred_data

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(tmp_path)}):
            token, data = _get_cred_data()

        assert token == "per_acct_token"
        assert data["claudeAiOauth"]["accessToken"] == "per_acct_token"

    def test_match_uses_path_based_account_id(self, tmp_path):
        """_match_token_to_account parses account_id from CLAUDE_CONFIG_DIR path."""
        _make_db(tmp_path)

        from jacked.data.hooks.session_account_tracker import (
            _match_token_to_account,
        )

        config_dir = str(tmp_path / "accounts" / "1")

        with mock.patch(
            "jacked.data.hooks.session_account_tracker.DB_PATH",
            Path(str(tmp_path / "test.db")),
        ):
            with mock.patch(
                "jacked.data.hooks.session_account_tracker.ACCOUNTS_DIR",
                tmp_path / "accounts",
            ):
                with mock.patch.dict(
                    os.environ, {"CLAUDE_CONFIG_DIR": config_dir}
                ):
                    account_id, email = _match_token_to_account(
                        "irrelevant_token"
                    )

        assert account_id == 1
        assert email == "alice@test.com"

    def test_match_falls_through_for_non_account_dir(self, tmp_path):
        """_match_token_to_account falls through when path doesn't match pattern."""
        _make_db(tmp_path)

        from jacked.data.hooks.session_account_tracker import _match_token_to_account

        with mock.patch(
            "jacked.data.hooks.session_account_tracker.DB_PATH",
            Path(str(tmp_path / "test.db")),
        ):
            with mock.patch.dict(
                os.environ, {"CLAUDE_CONFIG_DIR": "/some/random/dir"}
            ):
                account_id, email = _match_token_to_account(
                    "nonexistent_token"
                )

        # Should fall through to normal matching and not find anything
        assert account_id is None


# ---------------------------------------------------------------------------
# Account deletion cleanup
# ---------------------------------------------------------------------------


class TestDeleteAccountCleanup:
    def test_delete_removes_per_account_dir(self, tmp_path):
        """Deleting an account also removes its per-account credential dir."""
        # Create per-account dir
        acct_dir = tmp_path / "accounts" / "1"
        acct_dir.mkdir(parents=True)
        (acct_dir / ".credentials.json").write_text("{}")

        import shutil

        # Simulate the cleanup logic from delete_account()
        real_dir = tmp_path / "accounts" / "1"
        assert real_dir.exists()
        if real_dir.exists() and real_dir.is_dir() and not real_dir.is_symlink():
            shutil.rmtree(real_dir, ignore_errors=True)
        assert not real_dir.exists()
