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
import subprocess
import tempfile
import threading
import time as _time
from datetime import datetime, timezone
from pathlib import Path

import click

from jacked.api.credential_helpers import (
    _safe_replace,
    build_oauth_data,
    write_platform_credentials,
)
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


# Shared resources to symlink from global ~/.claude/ into per-account dirs.
_SHARED_SYMLINKS_FILES = ("settings.json", "CLAUDE.md")
_SHARED_SYMLINKS_DIRS = ("plugins", "agents", "commands", "skills", "projects")


def _ensure_shared_symlinks(config_dir: Path) -> None:
    """Symlink shared resources from global ~/.claude/ into per-account dir.

    Skips resources that don't exist globally or already have correct symlinks.
    On Windows, falls back to directory junctions if symlink fails.
    """
    global_dir = Path.home() / ".claude"

    for name in _SHARED_SYMLINKS_FILES + _SHARED_SYMLINKS_DIRS:
        source = global_dir / name
        target = config_dir / name

        if not source.exists():
            continue

        # Already a correct symlink — skip
        if target.is_symlink():
            try:
                if target.resolve() == source.resolve():
                    continue
            except OSError:
                pass
            # Wrong target — remove and recreate
            target.unlink()

        # Non-symlink file/dir exists (Claude Code created it) — skip
        if target.exists():
            continue

        is_dir = source.is_dir()
        try:
            os.symlink(source, target, target_is_directory=is_dir)
        except OSError:
            if is_dir and os.name == "nt":
                try:
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                        capture_output=True, timeout=5,
                    )
                except Exception:
                    logger.warning("Failed to create junction for %s", name)
            else:
                logger.warning("Failed to symlink %s (non-fatal)", name)


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
    # Symlink shared resources (settings, plugins, etc.) from global dir
    _ensure_shared_symlinks(config_dir)

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

    existing["claudeAiOauth"] = build_oauth_data(account)
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
        # No arg — read stamp from global credential file
        cred_path = Path.home() / ".claude" / ".credentials.json"
        acct_id = None
        if cred_path.exists() and not cred_path.is_symlink():
            try:
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                acct_id = data.get("_jackedAccountId")
            except (json.JSONDecodeError, OSError):
                pass
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


def _sync_tokens_from_file(config_dir: Path, db_path: str) -> None:
    """Read per-account .credentials.json and sync tokens back to DB.

    Lightweight: uses raw sqlite3 to avoid importing Database class in threads.
    """
    cred_path = config_dir / ".credentials.json"
    try:
        if not cred_path.exists() or cred_path.is_symlink():
            return
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        access_token = oauth.get("accessToken")
        if not access_token:
            return

        try:
            account_id = int(config_dir.name)
        except (ValueError, TypeError):
            return

        import sqlite3

        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")

            row = conn.execute(
                "SELECT access_token FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
            if row and row[0] == access_token:
                return  # unchanged

            refresh_token = oauth.get("refreshToken")
            expires_at_ms = oauth.get("expiresAt", 0)
            expires_at = int(expires_at_ms // 1000) if expires_at_ms else None

            now = datetime.now(timezone.utc).isoformat()
            updates = [
                "access_token = ?",
                "validation_status = 'valid'",
                "consecutive_failures = 0",
                "last_error = NULL",
                "updated_at = ?",
            ]
            params: list = [access_token, now]
            if refresh_token:
                updates.append("refresh_token = ?")
                params.append(refresh_token)
            if expires_at is not None:
                updates.append("expires_at = ?")
                params.append(expires_at)
            params.append(account_id)

            conn.execute(
                f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?",
                params,
            )
            conn.commit()
            logger.debug("Synced refreshed tokens for account %d", account_id)
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Token sync failed (non-fatal): %s", exc)


def _token_sync_loop(proc, config_dir: Path, db_path: str) -> None:
    """Daemon thread: poll credential file and sync changes to DB."""
    cred_path = config_dir / ".credentials.json"
    last_mtime = None
    try:
        last_mtime = cred_path.stat().st_mtime if cred_path.exists() else None
    except OSError:
        pass

    while proc.poll() is None:
        _time.sleep(30)
        try:
            mtime = cred_path.stat().st_mtime if cred_path.exists() else None
        except OSError:
            continue
        if mtime is not None and mtime != last_mtime:
            last_mtime = mtime
            _sync_tokens_from_file(config_dir, db_path)


def _close_sessions_by_pid(pid: int, db_path: str) -> None:
    """Mark all open sessions with the given PID as ended."""
    import sqlite3

    try:
        ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                "UPDATE session_accounts SET ended_at = ? WHERE pid = ? AND ended_at IS NULL",
                (ts, pid),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Session close by PID failed: %s", exc)


def launch_claude(
    config_dir: Path, claude_args: tuple, db_path: str | None = None
) -> None:
    """Launch claude as a subprocess with credential sync.  Always raises SystemExit.

    Runs claude with CLAUDE_CONFIG_DIR set. A daemon thread syncs refreshed
    tokens back to DB every 30s. On exit, marks sessions ended by PID.

    >>> # Tested via test_launch.py with mock subprocess
    """
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)

    proc = subprocess.Popen(["claude", *claude_args], env=env)

    if db_path:
        sync_thread = threading.Thread(
            target=_token_sync_loop, args=(proc, config_dir, db_path), daemon=True
        )
        sync_thread.start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()

    if db_path:
        _sync_tokens_from_file(config_dir, db_path)
        _close_sessions_by_pid(proc.pid, db_path)

    raise SystemExit(proc.returncode or 0)
