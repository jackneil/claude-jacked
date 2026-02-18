"""Launch Claude Code with per-account credential isolation.

Uses CLAUDE_CONFIG_DIR to give each account its own credential file,
preventing sessions on different accounts from overwriting each other.

Directory structure:
    ~/.claude/accounts/<account_id>/.credentials.json
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from pathlib import Path

import click

from jacked.api.credential_helpers import _safe_replace
from jacked.api.credential_sync import write_platform_credentials
from jacked.web.auth import should_refresh
from jacked.web.database import Database

logger = logging.getLogger(__name__)

ACCOUNTS_DIR = Path.home() / ".claude" / "accounts"

# Keys safe to copy from global .claude.json into per-account dirs.
# Excludes identity (userID, anonymousId, oauthAccount), project permissions,
# and all internal caches — those must never leak across accounts.
_SAFE_CONFIG_KEYS = frozenset({
    "autoUpdates", "autoUpdatesProtectedForNative", "showSpinnerTree",
    "claudeInChromeDefaultEnabled", "penguinModeOrgEnabled",
    "hasCompletedOnboarding", "lastOnboardingVersion", "hasSeenTasksHint",
    "hasCompletedClaudeInChromeOnboarding", "effortCalloutDismissed",
    "opusProMigrationComplete", "sonnet1m45MigrationComplete",
    "officialMarketplaceAutoInstallAttempted", "officialMarketplaceAutoInstalled",
    "lastReleaseNotesSeen", "installMethod",
})


def _seed_claude_config(config_dir: Path) -> None:
    """Seed per-account .claude.json with safe global settings.

    Only runs when .claude.json is missing or incomplete (no hasCompletedOnboarding).
    Copies only UX/onboarding keys — never identity, analytics, or project data.
    """
    claude_json = config_dir / ".claude.json"

    if claude_json.is_symlink():
        logger.warning("Per-account .claude.json is a symlink — skipping seed")
        return

    # Read existing per-account config (may not exist yet)
    local = {}
    try:
        local = json.loads(claude_json.read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            local = {}
    except FileNotFoundError:
        pass  # Expected on first launch
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read per-account .claude.json: %s", exc)

    if local.get("hasCompletedOnboarding"):
        return  # Already seeded — don't overwrite per-session changes

    # Read global config
    global_config = Path.home() / ".claude.json"
    if not global_config.is_file() or global_config.is_symlink():
        return
    try:
        source = json.loads(global_config.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            return
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read global .claude.json: %s", exc)
        return

    for key in _SAFE_CONFIG_KEYS:
        if key in source:
            local[key] = source[key]

    try:
        claude_json.write_text(json.dumps(local, indent=2), encoding="utf-8")
        os.chmod(str(claude_json), 0o600)
    except OSError as exc:
        logger.debug("Failed to write per-account .claude.json: %s", exc)


def _seed_workspace_trust(config_dir: Path) -> None:
    """Copy workspace trust records from global config into per-account config.

    Unlike _seed_claude_config (which runs once), this runs every launch so
    newly trusted workspaces propagate to per-account dirs.  Only copies
    hasTrustDialogAccepted and hasCompletedProjectOnboarding — not allowedTools,
    MCP servers, or cost data.  Skips project paths that already exist in the
    per-account config (non-destructive).
    """
    claude_json = config_dir / ".claude.json"
    if claude_json.is_symlink():
        return

    global_config = Path.home() / ".claude.json"
    if not global_config.is_file() or global_config.is_symlink():
        return

    try:
        source = json.loads(global_config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    global_projects = source.get("projects") if isinstance(source, dict) else None
    if not isinstance(global_projects, dict):
        return

    # Read per-account config
    local = {}
    try:
        local = json.loads(claude_json.read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            local = {}
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Cannot parse per-account .claude.json for trust seeding: %s", exc)
        return  # Don't clobber a file we can't parse

    local_projects = local.setdefault("projects", {})
    changed = False

    for path, entry in global_projects.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("hasTrustDialogAccepted"):
            continue
        # Don't overwrite existing per-account project entries
        if path in local_projects:
            continue
        minimal = {"hasTrustDialogAccepted": True}
        if entry.get("hasCompletedProjectOnboarding"):
            minimal["hasCompletedProjectOnboarding"] = True
        local_projects[path] = minimal
        changed = True

    if not changed:
        return

    # Atomic write
    fd, tmp = tempfile.mkstemp(
        dir=str(config_dir), prefix=".claude_json_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(local, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(tmp, str(claude_json))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.debug("Failed to write workspace trust to %s", claude_json)


def _build_oauth_data(account: dict) -> dict:
    """Build Claude Code credential format from account dict.

    Same structure as routes/credentials.py:204-211.

    >>> _build_oauth_data({"access_token": "at", "refresh_token": "rt", "expires_at": 100, "scopes": None, "subscription_type": "max", "rate_limit_tier": "t1"})["accessToken"]
    'at'
    """
    scopes = None
    raw_scopes = account.get("scopes")
    if raw_scopes:
        try:
            parsed = json.loads(raw_scopes)
            if isinstance(parsed, list):
                scopes = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "accessToken": account.get("access_token", ""),
        "refreshToken": account.get("refresh_token"),
        "expiresAt": account.get("expires_at", 0) * 1000,
        "scopes": scopes,
        "subscriptionType": account.get("subscription_type"),
        "rateLimitTier": account.get("rate_limit_tier"),
    }


def prepare_account_dir(account: dict, db: Database) -> Path:
    """Create per-account config dir and write credentials.

    Returns the directory path (for use as CLAUDE_CONFIG_DIR).

    >>> # Tested via test_launch.py
    """
    account_id = account["id"]
    if account_id <= 0:
        raise click.ClickException(f"Invalid account ID: {account_id}")

    # Refresh token if near-expiry (refresh_account_token is async)
    if should_refresh(account):
        from jacked.web.auth import refresh_account_token

        try:
            asyncio.run(refresh_account_token(account_id, db))
        except Exception as exc:
            logger.warning("Pre-launch token refresh failed: %s", exc)
        # Re-read account to get fresh tokens
        account = db.get_account(account_id)
        if not account:
            raise click.ClickException(f"Account {account_id} disappeared after refresh")

    config_dir = ACCOUNTS_DIR / str(account_id)

    # Refuse symlinks on the directory itself (defense-in-depth)
    if config_dir.exists() and config_dir.is_symlink():
        raise click.ClickException(
            f"Account dir is a symlink — refusing to use: {config_dir}"
        )

    # Create dir with user-only permissions
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(config_dir), 0o700)
    except OSError:
        pass

    # Seed .claude.json from global config to skip first-run setup screens
    _seed_claude_config(config_dir)
    # Propagate workspace trust (runs every launch, not gated by onboarding)
    _seed_workspace_trust(config_dir)

    cred_path = config_dir / ".credentials.json"

    # Refuse symlinks (is_symlink uses lstat — catches broken symlinks too)
    if cred_path.is_symlink():
        raise click.ClickException(
            f"Credential path is a symlink — refusing to write: {cred_path}"
        )

    # Read existing file to preserve other keys (Claude Code may have added data)
    existing = {}
    if cred_path.exists():
        try:
            existing = json.loads(cred_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["claudeAiOauth"] = _build_oauth_data(account)
    # No _jackedAccountId stamp — account_id is derived from directory path

    # Atomic write
    fd, tmp = tempfile.mkstemp(
        dir=str(config_dir), prefix=".credentials_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(tmp, str(cred_path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    # macOS: also write to Keychain so Claude Code finds creds on first run.
    # Claude Code reads Keychain before the config-dir file on macOS.
    write_platform_credentials(existing)

    return config_dir


def resolve_account(account_ref, db: Database) -> dict:
    """Resolve account reference to a full account dict.

    account_ref can be: int (ID), str with @ (email), str digits (ID),
    or None (use currently active account).

    >>> # Tested via test_launch.py
    """
    if not shutil.which("claude"):
        raise click.ClickException(
            "claude not found in PATH. Install with: npm install -g @anthropic-ai/claude-code"
        )

    account = None

    if account_ref is None:
        # No arg — use currently active account from global cred file
        from jacked.api.credential_sync import detect_active_account

        acct_id, _token = detect_active_account(db)
        if acct_id is not None:
            account = db.get_account(acct_id)
        if not account:
            raise click.ClickException(
                "No active account detected. Specify an account: jacked claude <id>"
            )
    elif isinstance(account_ref, str) and "@" in account_ref:
        account = db.get_account_by_email(account_ref)
        if not account:
            raise click.ClickException(f"No account found for email: {account_ref}")
    else:
        # Try as integer ID
        try:
            acct_id = int(account_ref)
        except (ValueError, TypeError):
            raise click.ClickException(
                f"Invalid account reference: {account_ref}. Use an ID or email."
            )
        account = db.get_account(acct_id)
        if not account:
            raise click.ClickException(f"Account {acct_id} not found")

    if account.get("is_deleted"):
        raise click.ClickException(
            f"Account {account.get('id')} ({account.get('email')}) has been deleted"
        )
    if not account.get("access_token"):
        raise click.ClickException(
            f"Account {account.get('id')} ({account.get('email')}) has no access token. "
            "Try /login in Claude Code first."
        )

    return account


def launch_claude(config_dir: Path, claude_args: tuple):
    """Replace current process with claude, using isolated config dir.

    >>> # launch_claude replaces the process — tested via mock in test_launch.py
    """
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    os.execvpe("claude", ["claude", *claude_args], env)
