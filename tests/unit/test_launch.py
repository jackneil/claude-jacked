"""Unit tests for jacked claude launch — per-account credential isolation.

Tests prepare_account_dir(), resolve_account(), launch_claude(),
hook CLAUDE_CONFIG_DIR support, and account deletion cleanup.
"""

import json
import os
import stat
import subprocess
import time
from pathlib import Path
from unittest import mock

import click
import pytest

from jacked.web.database import Database
from tests._platform import requires_symlinks

# Windows holds SQLite file locks


def _make_db(tmp_path: Path) -> Database:
    """Create a test DB with sample accounts.

    Accounts have both primary and CC tokens (dual-token architecture).
    CC tokens are what credential files receive; primary tokens are jacked-only.
    """
    db = Database(str(tmp_path / "test.db"))
    future = int(time.time()) + 3600
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token, expires_at,
                cc_access_token, cc_refresh_token, cc_expires_at,
                is_active, is_deleted, validation_status,
                consecutive_failures, subscription_type, rate_limit_tier)
               VALUES (1, 'alice@test.com', 'org-alice', 'alice_access', 'alice_refresh',
                       ?, 'alice_cc_access', 'alice_cc_refresh', ?,
                       1, 0, 'valid', 0, 'max', 't1')""",
            (future, future),
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token, expires_at,
                cc_access_token, cc_refresh_token, cc_expires_at,
                is_active, is_deleted, validation_status,
                consecutive_failures, subscription_type, rate_limit_tier)
               VALUES (2, 'bob@test.com', 'org-bob', 'bob_access', 'bob_refresh',
                       ?, 'bob_cc_access', 'bob_cc_refresh', ?,
                       1, 0, 'valid', 0, 'pro', 't2')""",
            (future, future),
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

    @requires_symlinks
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

    @requires_symlinks
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
# _seed_oauth_account
# ---------------------------------------------------------------------------


class TestSeedOauthAccount:
    def test_seeds_when_absent(self, tmp_path):
        """Seeds oauthAccount with email when absent from .claude.json."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text('{"hasCompletedOnboarding": true}')

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "test@example.com", "display_name": "Test User"})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result["oauthAccount"]["emailAddress"] == "test@example.com"
        assert result["oauthAccount"]["displayName"] == "Test User"
        # Preserves existing keys
        assert result["hasCompletedOnboarding"] is True

    def test_updates_email_but_preserves_other_fields(self, tmp_path):
        """Updates emailAddress but preserves Claude Code's other oauthAccount fields."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        existing = {
            "oauthAccount": {
                "emailAddress": "existing@example.com",
                "accountUuid": "uuid-123",
                "billingType": "pro",
            }
        }
        (config_dir / ".claude.json").write_text(json.dumps(existing))

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "different@example.com"})

        result = json.loads((config_dir / ".claude.json").read_text())
        # Email updated to match DB account
        assert result["oauthAccount"]["emailAddress"] == "different@example.com"
        # Other Claude Code fields preserved
        assert result["oauthAccount"]["accountUuid"] == "uuid-123"
        assert result["oauthAccount"]["billingType"] == "pro"

    def test_handles_non_dict_oauth_account(self, tmp_path):
        """Replaces non-dict oauthAccount with correct seed data."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": "garbage",
            "hasCompletedOnboarding": True,
        }))

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "test@example.com"})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result["oauthAccount"]["emailAddress"] == "test@example.com"
        assert result["hasCompletedOnboarding"] is True

    def test_skips_when_email_already_matches(self, tmp_path):
        """No-ops when oauthAccount email already matches — avoids unnecessary writes."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        existing = {
            "oauthAccount": {
                "emailAddress": "same@example.com",
                "accountUuid": "uuid-123",
            }
        }
        (config_dir / ".claude.json").write_text(json.dumps(existing))
        mtime_before = (config_dir / ".claude.json").stat().st_mtime

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "same@example.com"})

        # File should not be rewritten
        mtime_after = (config_dir / ".claude.json").stat().st_mtime
        assert mtime_before == mtime_after

    def test_skips_when_email_matches_case_insensitively(self, tmp_path):
        """No-ops when oauthAccount email differs only in case."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        existing = {
            "oauthAccount": {
                "emailAddress": "Same@Example.COM",
                "accountUuid": "uuid-123",
            }
        }
        (config_dir / ".claude.json").write_text(json.dumps(existing))
        mtime_before = (config_dir / ".claude.json").stat().st_mtime

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "same@example.com"})

        # File should not be rewritten — case-insensitive match
        mtime_after = (config_dir / ".claude.json").stat().st_mtime
        assert mtime_before == mtime_after

    def test_handles_null_email_in_oauth_account(self, tmp_path):
        """Handles emailAddress: null without crashing."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        existing = {"oauthAccount": {"emailAddress": None, "accountUuid": "uuid-123"}}
        (config_dir / ".claude.json").write_text(json.dumps(existing))

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "correct@example.com"})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result["oauthAccount"]["emailAddress"] == "correct@example.com"
        # Should preserve other fields
        assert result["oauthAccount"]["accountUuid"] == "uuid-123"

    def test_skips_when_no_email(self, tmp_path):
        """Returns immediately when account has no email."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text("{}")

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"display_name": "No Email"})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert "oauthAccount" not in result

    def test_skips_when_empty_email(self, tmp_path):
        """Returns immediately when email is empty string."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text("{}")

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": ""})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert "oauthAccount" not in result

    @requires_symlinks
    def test_skips_when_symlink(self, tmp_path):
        """Refuses to write when .claude.json is a symlink."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        real_file = tmp_path / "real.json"
        real_file.write_text("{}")
        (config_dir / ".claude.json").symlink_to(real_file)

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "test@example.com"})

        result = json.loads(real_file.read_text())
        assert "oauthAccount" not in result

    def test_skips_malformed_file(self, tmp_path):
        """Returns without clobbering a malformed .claude.json."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        garbage = "not json {{{"
        (config_dir / ".claude.json").write_text(garbage)

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "test@example.com"})

        assert (config_dir / ".claude.json").read_text() == garbage

    def test_creates_file_when_missing(self, tmp_path):
        """Creates .claude.json with oauthAccount when file doesn't exist."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "test@example.com"})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result["oauthAccount"]["emailAddress"] == "test@example.com"

    def test_omits_display_name_when_absent(self, tmp_path):
        """Only sets emailAddress when display_name is not provided."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        (config_dir / ".claude.json").write_text("{}")

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "test@example.com"})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result["oauthAccount"] == {"emailAddress": "test@example.com"}
        assert "displayName" not in result["oauthAccount"]


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
        # build_oauth_data prefers CC tokens for credential files
        assert oauth["accessToken"] == "alice_cc_access"
        assert oauth["refreshToken"] == "alice_cc_refresh"
        assert oauth["subscriptionType"] == "max"
        assert oauth["rateLimitTier"] == "t1"

        snapshot = json.loads(
            (result / "jacked-resolver-snapshot.json").read_text()
        )
        assert snapshot["scope"] == "global"
        assert snapshot["state"] == "resolved"
        assert snapshot["observed"]["account_id"] == 1
        assert snapshot["credential_revision"] == "switch:launch-test-operation"
        assert "launch:global-authority:observed_target_unfenced" in snapshot["evidence"]
        assert "alice_cc_access" not in json.dumps(snapshot)

        # Check permissions (skip on Windows)
        if os.name != "nt":
            dir_mode = stat.S_IMODE(result.stat().st_mode)
            assert dir_mode == 0o700
            file_mode = stat.S_IMODE(cred_file.stat().st_mode)
            assert file_mode == 0o600

    def test_scoped_launch_skips_global_activation_on_file_authority_platforms(self, tmp_path):
        """Where the credential file is the authority, the launch dir is what Claude
        reads; the global file must stay untouched and the snapshot must say so."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        with (
            mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"),
            mock.patch("jacked.launch.should_refresh", return_value=False),
            mock.patch("jacked.launch.scoped_launch_needs_global_activation", return_value=False),
            mock.patch("jacked.launch._activate_launch_credentials") as activate,
        ):
            from jacked.launch import prepare_account_dir

            result = prepare_account_dir(account, db)

        activate.assert_not_called()
        snapshot = json.loads((result / "jacked-resolver-snapshot.json").read_text())
        assert snapshot["state"] == "resolved"
        assert "launch:global-authority:skipped" in snapshot["evidence"]
        assert "launch:scoped-file-readback" in snapshot["evidence"]
        assert snapshot["observed"]["account_id"] == 1
        assert snapshot["observed"]["organization_id"] == snapshot["desired"]["organization_id"]
        assert snapshot["credential_revision"].startswith("launch:")
        assert "alice_cc_access" not in json.dumps(snapshot)

    def test_scoped_launch_fails_when_the_written_file_does_not_read_back(self, tmp_path):
        db = _make_db(tmp_path)
        account = db.get_account(1)

        def corrupt_replace(src, dst, **kwargs):
            Path(dst).write_text("{not json", encoding="utf-8")

        with (
            mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"),
            mock.patch("jacked.launch.should_refresh", return_value=False),
            mock.patch("jacked.launch.scoped_launch_needs_global_activation", return_value=False),
            mock.patch("jacked.launch._safe_replace", corrupt_replace),
            pytest.raises(click.ClickException, match="read back"),
        ):
            from jacked.launch import prepare_account_dir

            prepare_account_dir(account, db)

    def test_refreshes_if_near_expiry(self, tmp_path):
        """Pre-launch token refresh fires when should_refresh returns True."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=True):
                # Plain MagicMock, NOT the auto-detected AsyncMock: an AsyncMock
                # call creates a real coroutine that the mocked asyncio.run
                # never awaits, which leaks a RuntimeWarning at gc.
                with mock.patch(
                    "jacked.web.auth.refresh_account_token", new=mock.MagicMock()
                ):
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

    @requires_symlinks
    def test_rejects_symlink_dir(self, tmp_path):
        """Refuses to use a symlinked account directory."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        accounts_dir = tmp_path / "accounts"
        accounts_dir.mkdir(parents=True)
        # Use a guaranteed-missing target so this covers dangling links too.
        symlink_dir = accounts_dir / "1"
        symlink_dir.symlink_to(
            tmp_path / "missing-account-target", target_is_directory=True
        )

        with mock.patch("jacked.launch.ACCOUNTS_DIR", accounts_dir):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                from jacked.launch import prepare_account_dir

                with pytest.raises(click.ClickException, match="symlink"):
                    prepare_account_dir(account, db)

    @pytest.mark.skipif(os.name != "nt", reason="Windows junction regression")
    def test_rejects_junction_dir(self, tmp_path):
        """Refuses to write account credentials through a Windows junction."""
        db = _make_db(tmp_path)
        account = db.get_account(1)
        accounts_dir = tmp_path / "accounts"
        accounts_dir.mkdir(parents=True)
        target = tmp_path / "junction-target"
        target.mkdir()
        junction = accounts_dir / "1"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if created.returncode != 0:
            pytest.skip(created.stderr or created.stdout)

        with mock.patch("jacked.launch.ACCOUNTS_DIR", accounts_dir):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                from jacked.launch import prepare_account_dir

                with pytest.raises(click.ClickException, match="reparse point"):
                    prepare_account_dir(account, db)

        assert list(target.iterdir()) == []

    @requires_symlinks
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

    def test_fails_closed_when_global_authority_cannot_be_observed(self, tmp_path):
        """Never launch or publish resolved state from a failed native write."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        from jacked.credentials.models import (
            CredentialIdentity,
            IdentityAxis,
            ProviderVerificationState,
            SessionActivationState,
            SwitchOutcome,
            SwitchResult,
        )

        failed = SwitchResult(
            operation_id="launch-failed-operation",
            outcome=SwitchOutcome.UNSUPPORTED,
            desired_default=IdentityAxis(1, "desired"),
            storage=IdentityAxis(None, "unchanged"),
            committed_authority=IdentityAxis(None, "unchanged"),
            existing_session_activation=SessionActivationState.UNCHANGED,
            provider_verification=ProviderVerificationState.UNVERIFIED,
            observed_identity=CredentialIdentity(),
            message="exact Claude build is not certified",
        )

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                with mock.patch(
                    "jacked.launch._activate_launch_credentials", return_value=failed
                ):
                    from jacked.launch import prepare_account_dir

                    with pytest.raises(click.ClickException, match="not certified"):
                        prepare_account_dir(account, db)

        snapshot = (
            tmp_path / "accounts" / "1" / "jacked-resolver-snapshot.json"
        )
        assert not snapshot.exists()

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

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            result = resolve_account(1, db)
        assert result["email"] == "alice@test.com"

    def test_with_string_id(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            result = resolve_account("2", db)
        assert result["email"] == "bob@test.com"

    def test_with_email(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            result = resolve_account("bob@test.com", db)
        assert result["email"] == "bob@test.com"

    def test_rejects_codex_account_by_id(self, tmp_path):
        """A Codex account must NEVER resolve for `jacked claude` — launching it
        as Claude Code would overwrite the global keychain with its sentinel."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        codex = db.create_account("cx@test.com", "codex-managed", 4102444800,
                                  provider="codex", organization_uuid="acct-CX")
        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="Codex account"):
                resolve_account(codex["id"], db)

    def test_prepare_account_dir_rejects_codex(self, tmp_path):
        """Defense-in-depth: prepare_account_dir refuses a Codex account so no
        caller can drive it into the Claude credential write."""
        db = _make_db(tmp_path)
        from jacked.launch import prepare_account_dir

        codex = db.create_account("cx2@test.com", "codex-managed", 4102444800,
                                  provider="codex", organization_uuid="acct-CX2")
        with pytest.raises(click.ClickException, match="Codex account"):
            prepare_account_dir(codex, db)

    def test_without_id_uses_active(self, tmp_path):
        """resolve_account(None) accepts an evidence-qualified active ID."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with mock.patch(
                "jacked.api.credential_helpers.read_active_account_id", return_value=1
            ):
                result = resolve_account(None, db)
        assert result["email"] == "alice@test.com"

    def test_missing_raises(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="not found"):
                resolve_account(999, db)

    def test_deleted_raises(self, tmp_path):
        """Soft-deleted account is filtered by get_account — shows 'not found'."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="not found"):
                resolve_account(3, db)

    def test_id_not_found_lists_accounts(self, tmp_path):
        """Unknown ID error names the launchable accounts — IDs are database
        keys with holes after deletions, not row positions, so the user needs
        the real roster to self-correct."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException) as exc:
                resolve_account(999, db)
        msg = str(exc.value)
        assert "Available accounts" in msg
        assert "alice@test.com" in msg
        assert "bob@test.com" in msg
        # Soft-deleted accounts never appear in the hint
        assert "deleted@test.com" not in msg

    def test_email_not_found_lists_accounts(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(
                click.ClickException, match="No account found for email"
            ) as exc:
                resolve_account("nobody@nowhere.com", db)
        assert "Available accounts" in str(exc.value)

    def test_substring_unique_match(self, tmp_path):
        """A unique part of an email resolves the account."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            result = resolve_account("ali", db)
        assert result["email"] == "alice@test.com"

    def test_substring_case_insensitive(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            result = resolve_account("ALICE", db)
        assert result["email"] == "alice@test.com"

    def test_substring_ambiguous_raises(self, tmp_path):
        """A part that matches several Claude accounts lists them all."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="more than one") as exc:
                resolve_account("test.com", db)
        msg = str(exc.value)
        assert "alice@test.com" in msg
        assert "bob@test.com" in msg

    def test_substring_no_match_lists_accounts(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(
                click.ClickException, match="No account matches"
            ) as exc:
                resolve_account("zzz", db)
        assert "Available accounts" in str(exc.value)

    def test_substring_ignores_codex_for_winner(self, tmp_path):
        """A same-email Codex account never makes a Claude match ambiguous —
        same provider-scoping rule as the exact-email branch."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        db.create_account("alice@test.com", "codex-managed", 4102444800,
                          provider="codex", organization_uuid="acct-AX")
        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            result = resolve_account("ali", db)
        assert result["email"] == "alice@test.com"
        assert (result.get("provider") or "claude") == "claude"

    def test_substring_codex_only_match_points_at_codex_launcher(self, tmp_path):
        """A part that matches only a Codex account explains the Codex launcher
        instead of claiming no account exists."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        codex = db.create_account("onlycodex@test.com", "codex-managed", 4102444800,
                                  provider="codex", organization_uuid="acct-OC")
        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(
                click.ClickException, match="jacked codex launch"
            ) as exc:
                resolve_account("onlycodex", db)
        assert str(codex["id"]) in str(exc.value)

    def test_no_token_raises(self, tmp_path):
        db = _make_db(tmp_path)
        # Set access token to empty string (NOT NULL constraint)
        db.update_account(1, access_token="")
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with pytest.raises(click.ClickException, match="no access token"):
                resolve_account(1, db)

    def test_no_claude_raises(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value=None):
            with pytest.raises(click.ClickException, match="claude not found"):
                resolve_account(1, db)

    def test_without_id_uses_canonical_resolver(self, tmp_path):
        """resolve_account(None) uses resolver consensus, never DB precedence."""
        db = _make_db(tmp_path)
        db.set_setting("active_account_id", "1")
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with mock.patch(
                "jacked.api.credential_helpers.read_active_account_id", return_value=1
            ):
                result = resolve_account(None, db)
        assert result["email"] == "alice@test.com"

    def test_without_id_canonical_resolver_can_select_second_account(self, tmp_path):
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with mock.patch(
                "jacked.api.credential_helpers.read_active_account_id", return_value=2
            ):
                result = resolve_account(None, db)
        assert result["email"] == "bob@test.com"

    def test_without_id_never_uses_db_pointer_without_consensus(self, tmp_path):
        db = _make_db(tmp_path)
        db.set_setting("active_account_id", "2")
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with mock.patch(
                "jacked.api.credential_helpers.read_active_account_id", return_value=None
            ):
                with pytest.raises(click.ClickException, match="No active account detected"):
                    resolve_account(None, db)

    def test_without_id_all_layers_fail(self, tmp_path):
        """resolve_account(None) raises when no file, no DB setting, no Keychain."""
        db = _make_db(tmp_path)
        from jacked.launch import resolve_account

        with mock.patch("jacked.launch.find_bin", return_value="/usr/local/bin/claude"):
            with mock.patch(
                "jacked.api.credential_helpers.read_active_account_id", return_value=None
            ):
                with pytest.raises(
                    click.ClickException, match="No active account detected"
                ):
                    resolve_account(None, db)


# ---------------------------------------------------------------------------
# launch_claude
# ---------------------------------------------------------------------------


class TestLaunchClaude:
    def test_sets_env_and_launches(self, tmp_path):
        """Verifies CLAUDE_CONFIG_DIR is set and subprocess.Popen is called."""
        from jacked.launch import launch_claude

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)

        mock_proc = mock.MagicMock()
        mock_proc.pid = 42
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0

        with mock.patch("jacked.launch.subprocess.Popen", return_value=mock_proc) as mock_popen:
            with pytest.raises(SystemExit):
                launch_claude(config_dir, ("--resume", "abc123"))

        mock_popen.assert_called_once()
        args = mock_popen.call_args
        assert args[0][0] == ["claude", "--resume", "abc123"]
        env = args[1]["env"]
        assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)
        assert env["JACKED_CREDENTIAL_SCOPE"] == "unknown"
        assert "JACKED_LAUNCH_NONCE" not in env
        assert "JACKED_SCOPED_CREDENTIAL_CERTIFIED" not in env

    def test_launch_passes_token_free_revision_without_claiming_certification(
        self, tmp_path
    ):
        from jacked.launch import launch_claude

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        now = time.time()
        (config_dir / "jacked-resolver-snapshot.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "published_at": now,
                    "fresh_until": now + 30,
                    "scope": "global",
                    "state": "resolved",
                    "credential_revision": "revision-123",
                    "observed": {"account_id": 1},
                }
            )
        )
        mock_proc = mock.MagicMock(pid=42, returncode=0)
        mock_proc.wait.return_value = 0

        with mock.patch(
            "jacked.launch.subprocess.Popen", return_value=mock_proc
        ) as mock_popen:
            with pytest.raises(SystemExit):
                launch_claude(config_dir, ())

        env = mock_popen.call_args.kwargs["env"]
        assert env["JACKED_CREDENTIAL_SCOPE"] == "global"
        assert env["JACKED_CREDENTIAL_REVISION"] == "revision-123"
        assert "JACKED_LAUNCH_NONCE" not in env
        assert "JACKED_SCOPED_CREDENTIAL_CERTIFIED" not in env

    def test_launch_does_not_inherit_false_scoped_certification(self, tmp_path):
        from jacked.launch import launch_claude

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        mock_proc = mock.MagicMock(pid=42, returncode=0)
        mock_proc.wait.return_value = 0
        inherited = {
            "JACKED_CREDENTIAL_SCOPE": "scoped",
            "JACKED_SCOPED_CREDENTIAL_CERTIFIED": "1",
            "JACKED_LAUNCH_NONCE": "stale-nonce",
            "JACKED_CREDENTIAL_REVISION": "stale-revision",
        }

        with (
            mock.patch.dict(os.environ, inherited, clear=False),
            mock.patch(
                "jacked.launch.subprocess.Popen", return_value=mock_proc
            ) as mock_popen,
            pytest.raises(SystemExit),
        ):
            launch_claude(config_dir, ())

        env = mock_popen.call_args.kwargs["env"]
        assert env["JACKED_CREDENTIAL_SCOPE"] == "unknown"
        assert "JACKED_SCOPED_CREDENTIAL_CERTIFIED" not in env
        assert "JACKED_LAUNCH_NONCE" not in env
        assert "JACKED_CREDENTIAL_REVISION" not in env

    def test_launches_with_db_path(self, tmp_path):
        """launch_claude with db_path starts token sync and closes sessions on exit."""
        from jacked.launch import launch_claude

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "tok", "refreshToken": "rt", "expiresAt": 9999999999000}
        }))

        mock_proc = mock.MagicMock()
        mock_proc.pid = 42
        mock_proc.wait.return_value = 0
        mock_proc.poll.return_value = 0  # exits immediately

        with mock.patch("jacked.launch.subprocess.Popen", return_value=mock_proc) as mock_popen:
            with pytest.raises(SystemExit):
                launch_claude(config_dir, ("--resume", "abc"), db_path=str(tmp_path / "test.db"))

        mock_popen.assert_called_once()
        call_kwargs = mock_popen.call_args
        assert call_kwargs[0][0] == ["claude", "--resume", "abc"]
        env = call_kwargs[1]["env"]
        assert env["CLAUDE_CONFIG_DIR"] == str(config_dir)
        mock_proc.wait.assert_called_once()


# ---------------------------------------------------------------------------
# Token sync and PID cleanup
# ---------------------------------------------------------------------------


class TestTokenSync:
    def test_sync_tokens_from_file_updates_db(self, tmp_path):
        """_sync_tokens_from_file writes changed tokens to cc_* columns."""
        db = _make_db(tmp_path)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "new_cc_access",
                "refreshToken": "new_cc_refresh",
                "expiresAt": 9999999999000,
            }
        }))

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        account = db.get_account(1)
        # Writes to cc_* columns, not primary
        assert account["cc_access_token"] == "new_cc_access"
        assert account["cc_refresh_token"] == "new_cc_refresh"
        assert account["validation_status"] == "valid"
        # Primary tokens unchanged
        assert account["access_token"] == "alice_access"

    def test_sync_tokens_noop_when_unchanged(self, tmp_path):
        """_sync_tokens_from_file skips write when CC token hasn't changed."""
        db = _make_db(tmp_path)
        original = db.get_account(1)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "alice_cc_access",  # same as DB cc_access_token
                "refreshToken": "alice_cc_refresh",
                "expiresAt": 9999999999000,
            }
        }))

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        # Should not have changed anything
        account = db.get_account(1)
        assert account["cc_access_token"] == original["cc_access_token"]

    def test_close_sessions_by_pid(self, tmp_path):
        """_close_sessions_by_pid marks matching open sessions as ended."""
        db = _make_db(tmp_path)
        db.record_session_account(
            "sess-pid1", account_id=1, email="a@b.com", pid=12345
        )
        db.record_session_account(
            "sess-pid2", account_id=1, email="a@b.com", pid=99999
        )

        from jacked.launch import _close_sessions_by_pid

        _close_sessions_by_pid(12345, str(tmp_path / "test.db"))

        rows1 = db.get_session_accounts("sess-pid1")
        assert rows1[0]["ended_at"] is not None

        rows2 = db.get_session_accounts("sess-pid2")
        assert rows2[0]["ended_at"] is None  # different PID, not closed

    def test_sync_rejects_mismatched_email(self, tmp_path):
        """_sync_tokens_from_file refuses to sync when email doesn't match DB."""
        db = _make_db(tmp_path)
        original = db.get_account(1)
        assert original["access_token"] == "alice_access"

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        # Claude Code re-authed as bob — wrong token in alice's dir
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "bobs_stolen_token",
                "refreshToken": "bobs_refresh",
                "expiresAt": 9999999999000,
            }
        }))
        # Claude Code also wrote bob's email to alice's .claude.json
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"emailAddress": "bob@different.com"}
        }))

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        # DB should NOT be contaminated — alice's original tokens preserved
        account = db.get_account(1)
        assert account["access_token"] == "alice_access"
        assert account["cc_access_token"] == "alice_cc_access"

    def test_sync_allows_matching_email(self, tmp_path):
        """_sync_tokens_from_file syncs CC tokens when email matches DB."""
        db = _make_db(tmp_path)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "refreshed_alice_token",
                "refreshToken": "new_refresh",
                "expiresAt": 9999999999000,
            }
        }))
        # Email matches DB — this is a legitimate token refresh
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "alice@test.com",
                "organizationUuid": "org-alice",
            }
        }))

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        account = db.get_account(1)
        # Writes to cc_* columns
        assert account["cc_access_token"] == "refreshed_alice_token"
        assert account["cc_refresh_token"] == "new_refresh"

    def test_sync_allows_case_insensitive_email_match(self, tmp_path):
        """Email guard compares case-insensitively — same email, different case syncs."""
        db = _make_db(tmp_path)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "refreshed_token",
                "refreshToken": "r",
                "expiresAt": 9999999999000,
            }
        }))
        # Same email, different case — should sync normally
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {
                "emailAddress": "Alice@Test.Com",
                "organizationUuid": "org-alice",
            }
        }))

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        account = db.get_account(1)
        assert account["cc_access_token"] == "refreshed_token"

    def test_sync_blocks_when_email_missing_from_oauth(self, tmp_path):
        """Blocks sync when oauthAccount exists but emailAddress is absent."""
        db = _make_db(tmp_path)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "suspicious_token",
                "refreshToken": "r",
                "expiresAt": 9999999999000,
            }
        }))
        # oauthAccount exists but no emailAddress — suspicious
        (config_dir / ".claude.json").write_text(json.dumps({
            "oauthAccount": {"accountUuid": "some-uuid"}
        }))

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        account = db.get_account(1)
        assert account["access_token"] == "alice_access"  # not contaminated
        assert account["cc_access_token"] == "alice_cc_access"  # CC not contaminated

    def test_sync_allows_when_no_claude_json(self, tmp_path):
        """_sync_tokens_from_file syncs CC tokens when .claude.json missing."""
        db = _make_db(tmp_path)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "new_cc_access",
                "refreshToken": "new_cc_refresh",
                "expiresAt": 9999999999000,
            }
        }))
        # No .claude.json at all — can't validate, allow sync

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        account = db.get_account(1)
        assert account["cc_access_token"] == "new_cc_access"
        assert account["cc_refresh_token"] == "new_cc_refresh"

    def test_sync_skips_when_claude_json_corrupted(self, tmp_path):
        """Corrupted .claude.json → fail closed, skip sync without identity check."""
        db = _make_db(tmp_path)

        config_dir = tmp_path / "accounts" / "1"
        config_dir.mkdir(parents=True)
        # Write valid credential file with new tokens
        (config_dir / ".credentials.json").write_text(json.dumps({
            "claudeAiOauth": {
                "accessToken": "new_cc_access",
                "refreshToken": "new_cc_refresh",
                "expiresAt": 9999999999000,
            }
        }))
        # Write CORRUPTED .claude.json
        (config_dir / ".claude.json").write_text("not valid json{{{")

        from jacked.launch import _sync_tokens_from_file

        _sync_tokens_from_file(config_dir, str(tmp_path / "test.db"))

        account = db.get_account(1)
        # Sync should NOT have happened — fail closed
        assert account["cc_access_token"] == "alice_cc_access"  # unchanged


# ---------------------------------------------------------------------------
# _seed_oauth_account — email always updated
# ---------------------------------------------------------------------------


class TestSeedOauthAccountEmailUpdate:
    def test_updates_email_even_when_oauth_account_exists(self, tmp_path):
        """Always updates emailAddress to match DB, even when oauthAccount exists."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        # Claude Code wrote a rich oauthAccount with a STALE email
        existing = {
            "oauthAccount": {
                "emailAddress": "stale@wrong.com",
                "accountUuid": "uuid-123",
                "billingType": "pro",
                "displayName": "Stale Name",
            }
        }
        (config_dir / ".claude.json").write_text(json.dumps(existing))

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "correct@test.com", "display_name": "Correct"})

        result = json.loads((config_dir / ".claude.json").read_text())
        # Email MUST be updated to match DB
        assert result["oauthAccount"]["emailAddress"] == "correct@test.com"
        # Other Claude Code fields preserved
        assert result["oauthAccount"]["accountUuid"] == "uuid-123"
        assert result["oauthAccount"]["billingType"] == "pro"

    def test_preserves_other_oauth_fields(self, tmp_path):
        """Updating email preserves all other oauthAccount fields from Claude Code."""
        config_dir = tmp_path / "acct"
        config_dir.mkdir()
        existing = {
            "oauthAccount": {
                "emailAddress": "old@test.com",
                "accountUuid": "uuid-456",
                "billingType": "max",
                "organizationType": "claude_max",
                "displayName": "Old Name",
            },
            "hasCompletedOnboarding": True,
        }
        (config_dir / ".claude.json").write_text(json.dumps(existing))

        from jacked.launch import _seed_oauth_account

        _seed_oauth_account(config_dir, {"email": "new@test.com"})

        result = json.loads((config_dir / ".claude.json").read_text())
        assert result["oauthAccount"]["emailAddress"] == "new@test.com"
        assert result["oauthAccount"]["accountUuid"] == "uuid-456"
        assert result["oauthAccount"]["billingType"] == "max"
        assert result["oauthAccount"]["organizationType"] == "claude_max"
        assert result["hasCompletedOnboarding"] is True


# ---------------------------------------------------------------------------
# Hook CLAUDE_CONFIG_DIR support
# ---------------------------------------------------------------------------


class TestHookConfigDir:
    def test_get_cred_data_reads_config_dir(self, tmp_path):
        """_get_cred_data reads the scoped resolver snapshot, never tokens."""
        snapshot_file = tmp_path / "jacked-resolver-snapshot.json"
        snapshot_file.write_text(json.dumps({
            "schema_version": 1,
            "published_at": time.time() - 1,
            "fresh_until": time.time() + 60,
            "scope": "scoped",
            "state": "resolved",
            "evidence": ["launch_binding"],
            "credential_revision": "rev-1",
            "desired": {
                "account_id": 1,
                "email": "alice@test.com",
                "organization_id": "org-alice",
            },
            "observed": {
                "account_id": 1,
                "email": "alice@test.com",
                "organization_id": "org-alice",
            },
        }))
        snapshot_file.chmod(0o600)

        from jacked.data.hooks.session_account_tracker import _get_cred_data

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(tmp_path)}):
            token, data = _get_cred_data()

        assert token is None
        assert data["observed"]["account_id"] == 1

    def test_match_uses_path_based_account_id(self, tmp_path):
        """_match_token_to_account resolves canonical snapshot identity."""
        _make_db(tmp_path)

        from jacked.data.hooks.session_account_tracker import (
            _match_token_to_account,
        )

        cred_data = {
            "state": "resolved",
            "observed": {
                "account_id": 1,
                "email": "alice@test.com",
                "organization_id": "org-alice",
            },
        }

        with mock.patch(
            "jacked.data.hooks.session_account_tracker.DB_PATH",
            Path(str(tmp_path / "test.db")),
        ):
            account_id, email = _match_token_to_account(
                "irrelevant_token", cred_data
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


class TestSessionHookEarlyReturn:
    def test_match_works_without_token(self, tmp_path):
        """_match_token_to_account needs no token for resolver evidence."""
        _make_db(tmp_path)
        from jacked.data.hooks.session_account_tracker import _match_token_to_account

        cred_data = {
            "state": "resolved",
            "observed": {
                "account_id": 1,
                "email": "alice@test.com",
                "organization_id": "org-alice",
            },
        }
        with mock.patch(
            "jacked.data.hooks.session_account_tracker.DB_PATH",
            Path(str(tmp_path / "test.db")),
        ):
            account_id, email = _match_token_to_account(None, cred_data)

        assert account_id == 1
        assert email == "alice@test.com"

    def test_handle_event_matches_without_token(self, tmp_path):
        """_handle_event matches a secret-free resolver observation."""
        db = _make_db(tmp_path)
        from jacked.data.hooks.session_account_tracker import _handle_event

        cred_data = {
            "state": "resolved",
            "scope": "global",
            "published_at": time.time(),
            "credential_revision": "rev-1",
            "evidence": "store_consensus",
            "observed": {
                "account_id": 1,
                "email": "alice@test.com",
                "organization_id": "org-alice",
            },
        }
        with mock.patch(
            "jacked.data.hooks.session_account_tracker.DB_PATH",
            Path(str(tmp_path / "test.db")),
        ):
            with mock.patch(
                "jacked.data.hooks.session_account_tracker._get_cred_data",
                return_value=(None, cred_data),
            ):
                _handle_event("SessionStart", "sess-notoken", "/tmp")

        rows = db.get_session_accounts("sess-notoken")
        assert len(rows) == 1
        assert rows[0]["account_id"] == 1
        assert rows[0]["email"] == "alice@test.com"


# ---------------------------------------------------------------------------
# Session PID tracking
# ---------------------------------------------------------------------------


class TestSessionPid:
    def test_record_session_with_pid(self, tmp_path):
        """record_session_account stores pid column."""
        db = _make_db(tmp_path)
        rid = db.record_session_account(
            "sess-pid", account_id=1, email="a@b.com", pid=12345
        )
        assert rid > 0
        rows = db.get_session_accounts("sess-pid")
        assert rows[0]["pid"] == 12345

    def test_record_session_without_pid(self, tmp_path):
        """pid defaults to None when not provided."""
        db = _make_db(tmp_path)
        db.record_session_account("sess-nopid", account_id=1, email="a@b.com")
        rows = db.get_session_accounts("sess-nopid")
        assert rows[0].get("pid") is None


# ---------------------------------------------------------------------------
# Shared symlinks
# ---------------------------------------------------------------------------


class TestSharedSymlinks:
    @requires_symlinks
    def test_creates_symlinks_for_shared_resources(self, tmp_path):
        """prepare_account_dir symlinks settings.json, plugins/, etc."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        # Create global resources
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text("{}")
        (claude_dir / "CLAUDE.md").write_text("# rules")
        (claude_dir / "plugins").mkdir()
        (claude_dir / "agents").mkdir()

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / ".claude" / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                with mock.patch.object(Path, "home", return_value=tmp_path):
                    from jacked.launch import prepare_account_dir

                    config_dir = prepare_account_dir(account, db)

        # Check symlinks
        assert (config_dir / "settings.json").is_symlink()
        assert (config_dir / "settings.json").resolve() == (claude_dir / "settings.json").resolve()
        assert (config_dir / "CLAUDE.md").is_symlink()
        assert (config_dir / "plugins").is_symlink()
        assert (config_dir / "agents").is_symlink()

    def test_skips_missing_global_resources(self, tmp_path):
        """Doesn't create symlinks for resources that don't exist globally."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        # Don't create any resources

        with mock.patch("jacked.launch.ACCOUNTS_DIR", tmp_path / ".claude" / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                with mock.patch.object(Path, "home", return_value=tmp_path):
                    from jacked.launch import prepare_account_dir

                    config_dir = prepare_account_dir(account, db)

        assert not (config_dir / "settings.json").exists()
        assert not (config_dir / "plugins").exists()

    @requires_symlinks
    def test_replaces_real_file_with_symlink(self, tmp_path):
        """Replaces a real settings.json (created by Claude Code) with a symlink."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text('{"hooks": {}}')

        # Pre-create account dir with a REAL settings.json (as Claude Code would)
        acct_dir = claude_dir / "accounts" / "1"
        acct_dir.mkdir(parents=True)
        (acct_dir / "settings.json").write_text('{"old": true}')
        assert not (acct_dir / "settings.json").is_symlink()

        with mock.patch("jacked.launch.ACCOUNTS_DIR", claude_dir / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                with mock.patch.object(Path, "home", return_value=tmp_path):
                    from jacked.launch import prepare_account_dir

                    config_dir = prepare_account_dir(account, db)

        # Should now be a symlink to global
        assert (config_dir / "settings.json").is_symlink()
        assert (config_dir / "settings.json").resolve() == (claude_dir / "settings.json").resolve()
        # Backup should exist
        assert (config_dir / "settings.json.bak").exists()
        assert json.loads((config_dir / "settings.json.bak").read_text()) == {"old": True}

    @requires_symlinks
    def test_replaces_real_dir_with_symlink(self, tmp_path):
        """Replaces a real plugins/ dir (created by Claude Code) with a symlink."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "plugins").mkdir()
        (claude_dir / "plugins" / "installed_plugins.json").write_text("[]")

        # Pre-create account dir with a REAL plugins dir
        acct_dir = claude_dir / "accounts" / "1"
        acct_dir.mkdir(parents=True)
        (acct_dir / "plugins").mkdir()
        (acct_dir / "plugins" / "blocklist.json").write_text("{}")
        assert not (acct_dir / "plugins").is_symlink()

        with mock.patch("jacked.launch.ACCOUNTS_DIR", claude_dir / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                with mock.patch.object(Path, "home", return_value=tmp_path):
                    from jacked.launch import prepare_account_dir

                    config_dir = prepare_account_dir(account, db)

        # Should now be a symlink to global
        assert (config_dir / "plugins").is_symlink()
        assert (config_dir / "plugins").resolve() == (claude_dir / "plugins").resolve()
        # Backup should exist with original content
        assert (config_dir / "plugins.bak").is_dir()
        assert (config_dir / "plugins.bak" / "blocklist.json").exists()

    @requires_symlinks
    def test_backup_already_exists_removes_and_recreates(self, tmp_path):
        """If .bak already exists, removes the real file and recreates symlink."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text('{"hooks": {}}')

        acct_dir = claude_dir / "accounts" / "1"
        acct_dir.mkdir(parents=True)
        # Real file AND existing backup
        (acct_dir / "settings.json").write_text('{"old": true}')
        (acct_dir / "settings.json.bak").write_text('{"older": true}')

        with mock.patch("jacked.launch.ACCOUNTS_DIR", claude_dir / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                with mock.patch.object(Path, "home", return_value=tmp_path):
                    from jacked.launch import prepare_account_dir

                    config_dir = prepare_account_dir(account, db)

        assert (config_dir / "settings.json").is_symlink()
        # Original backup preserved (not overwritten)
        assert json.loads((config_dir / "settings.json.bak").read_text()) == {"older": True}

    @requires_symlinks
    def test_skips_existing_correct_symlink(self, tmp_path):
        """Doesn't recreate symlink if it already points to correct target."""
        db = _make_db(tmp_path)
        account = db.get_account(1)

        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(exist_ok=True)
        (claude_dir / "settings.json").write_text("{}")

        acct_dir = claude_dir / "accounts" / "1"
        acct_dir.mkdir(parents=True)
        (acct_dir / "settings.json").symlink_to(claude_dir / "settings.json")

        with mock.patch("jacked.launch.ACCOUNTS_DIR", claude_dir / "accounts"):
            with mock.patch("jacked.launch.should_refresh", return_value=False):
                with mock.patch.object(Path, "home", return_value=tmp_path):
                    from jacked.launch import prepare_account_dir

                    config_dir = prepare_account_dir(account, db)

        # Should still be a valid symlink
        assert (config_dir / "settings.json").is_symlink()


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


# ---------------------------------------------------------------------------
# claude_cmd — CLI flag passthrough (jacked claude --resume, -p, etc.)
# ---------------------------------------------------------------------------


class TestClaudeCmdFlagPassthrough:
    """Verify that Claude CLI flags aren't eaten as the account argument."""

    @staticmethod
    def _make_claude_db(tmp_path, active_account_id="1"):
        """Create DB at the path claude_cmd expects (~/.claude/jacked.db)."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        db = Database(str(claude_dir / "jacked.db"))
        with db._writer() as conn:
            conn.execute(
                """INSERT INTO accounts
                   (id, email, access_token, refresh_token, expires_at,
                    is_active, is_deleted, validation_status,
                    consecutive_failures, subscription_type, rate_limit_tier)
                   VALUES (1, 'alice@test.com', 'tok', 'ref', ?, 1, 0, 'valid', 0, 'max', 't1')""",
                (int(time.time()) + 3600,),
            )
            conn.execute(
                """INSERT INTO accounts
                   (id, email, access_token, refresh_token, expires_at,
                    is_active, is_deleted, validation_status,
                    consecutive_failures, subscription_type, rate_limit_tier)
                   VALUES (2, 'bob@test.com', 'tok2', 'ref2', ?, 1, 0, 'valid', 0, 'pro', 't2')""",
                (int(time.time()) + 3600,),
            )
        if active_account_id:
            db.set_setting("active_account_id", active_account_id)
        db.close()

    def test_resume_flag_passed_through(self, tmp_path):
        """jacked claude --resume abc123 → resolves active account, passes --resume abc123."""
        from click.testing import CliRunner

        from jacked.cli import main

        self._make_claude_db(tmp_path)

        with mock.patch.object(Path, "home", return_value=tmp_path), \
             mock.patch("jacked.launch.resolve_account") as mock_resolve, \
             mock.patch("jacked.launch.prepare_account_dir") as mock_prepare, \
             mock.patch("jacked.launch.launch_claude") as mock_launch:
            mock_resolve.return_value = {"id": 1, "email": "alice@test.com"}
            mock_prepare.return_value = tmp_path / "accounts" / "1"

            runner = CliRunner()
            result = runner.invoke(main, ["claude", "--resume", "abc123"])

            assert result.exit_code == 0, result.output
            # account_ref should be None (active account), not "--resume"
            mock_resolve.assert_called_once()
            assert mock_resolve.call_args[0][0] is None
            # --resume abc123 should be in claude_args passed to launch_claude
            launch_args = mock_launch.call_args[0][1]
            assert "--resume" in launch_args
            assert "abc123" in launch_args

    def test_short_flag_passed_through(self, tmp_path):
        """jacked claude -p editor → resolves active account, passes -p editor."""
        from click.testing import CliRunner

        from jacked.cli import main

        self._make_claude_db(tmp_path)

        with mock.patch.object(Path, "home", return_value=tmp_path), \
             mock.patch("jacked.launch.resolve_account") as mock_resolve, \
             mock.patch("jacked.launch.prepare_account_dir") as mock_prepare, \
             mock.patch("jacked.launch.launch_claude") as mock_launch:
            mock_resolve.return_value = {"id": 1, "email": "alice@test.com"}
            mock_prepare.return_value = tmp_path / "accounts" / "1"

            runner = CliRunner()
            result = runner.invoke(main, ["claude", "-p", "editor"])

            assert result.exit_code == 0, result.output
            assert mock_resolve.call_args[0][0] is None
            launch_args = mock_launch.call_args[0][1]
            assert "-p" in launch_args
            assert "editor" in launch_args

    def test_account_with_flags_still_works(self, tmp_path):
        """jacked claude 2 --resume abc123 → account=2, passes --resume abc123."""
        from click.testing import CliRunner

        from jacked.cli import main

        self._make_claude_db(tmp_path)

        with mock.patch.object(Path, "home", return_value=tmp_path), \
             mock.patch("jacked.launch.resolve_account") as mock_resolve, \
             mock.patch("jacked.launch.prepare_account_dir") as mock_prepare, \
             mock.patch("jacked.launch.launch_claude") as mock_launch:
            mock_resolve.return_value = {"id": 2, "email": "bob@test.com"}
            mock_prepare.return_value = tmp_path / "accounts" / "2"

            runner = CliRunner()
            result = runner.invoke(main, ["claude", "2", "--resume", "abc123"])

            assert result.exit_code == 0, result.output
            # account_ref should be 2 (integer)
            assert mock_resolve.call_args[0][0] == 2
            launch_args = mock_launch.call_args[0][1]
            assert "--resume" in launch_args
            assert "abc123" in launch_args

    def test_bare_claude_no_args(self, tmp_path):
        """jacked claude (no args) → resolves active account."""
        from click.testing import CliRunner

        from jacked.cli import main

        self._make_claude_db(tmp_path)

        with mock.patch.object(Path, "home", return_value=tmp_path), \
             mock.patch("jacked.launch.resolve_account") as mock_resolve, \
             mock.patch("jacked.launch.prepare_account_dir") as mock_prepare, \
             mock.patch("jacked.launch.launch_claude") as mock_launch:
            mock_resolve.return_value = {"id": 1, "email": "alice@test.com"}
            mock_prepare.return_value = tmp_path / "accounts" / "1"

            runner = CliRunner()
            result = runner.invoke(main, ["claude"])

            assert result.exit_code == 0, result.output
            assert mock_resolve.call_args[0][0] is None
            launch_args = mock_launch.call_args[0][1]
            assert len(launch_args) == 0
