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
    read_platform_credentials,
    write_platform_credentials,
)
from jacked.web.auth import should_refresh, should_refresh_cc
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

    # Atomic write (matches _seed_workspace_trust / _seed_oauth_account pattern)
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
        logger.debug("Failed to write per-account .claude.json")


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


def _seed_oauth_account(config_dir: Path, account: dict) -> None:
    """Seed oauthAccount identity into per-account .claude.json.

    Seeds emailAddress when oauthAccount is absent.  When oauthAccount already
    exists (Claude Code wrote the richer ~12-field version), preserves all
    fields but ALWAYS corrects emailAddress to match the DB account.  This
    prevents token contamination: if Claude Code re-authenticates as a
    different Google account inside this per-account dir, the stale email
    is overwritten on next launch so _sync_tokens_from_file() can detect
    the mismatch and block the wrong token from polluting the DB.
    """
    email = account.get("email")
    if not email:
        return

    claude_json = config_dir / ".claude.json"
    if claude_json.is_symlink():
        return

    local = {}
    try:
        local = json.loads(claude_json.read_text(encoding="utf-8"))
        if not isinstance(local, dict):
            local = {}
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        return  # Don't clobber a file we can't parse

    org_uuid = account.get("organization_uuid") or None
    org_name = account.get("organization_name")

    if "oauthAccount" in local and isinstance(local["oauthAccount"], dict):
        # Claude Code wrote the full version — preserve all fields but
        # ALWAYS update emailAddress + org identity to match the DB account.
        # This prevents stale emails/orgs from persisting after Claude Code
        # re-authenticates inside a per-account directory.
        changed = False
        if (local["oauthAccount"].get("emailAddress") or "").lower() != email.lower():
            local["oauthAccount"]["emailAddress"] = email
            changed = True
        # Always sync org identity
        if org_uuid:
            if local["oauthAccount"].get("organizationUuid") != org_uuid:
                local["oauthAccount"]["organizationUuid"] = org_uuid
                changed = True
            if org_name and local["oauthAccount"].get("organizationName") != org_name:
                local["oauthAccount"]["organizationName"] = org_name
                changed = True
        else:
            # Personal account — clear any stale org fields
            if "organizationUuid" in local["oauthAccount"]:
                del local["oauthAccount"]["organizationUuid"]
                changed = True
            if "organizationName" in local["oauthAccount"]:
                del local["oauthAccount"]["organizationName"]
                changed = True
        if not changed:
            return  # Already correct — nothing to do
    else:
        oauth = {"emailAddress": email}
        display_name = account.get("display_name")
        if display_name:
            oauth["displayName"] = display_name
        if org_uuid:
            oauth["organizationUuid"] = org_uuid
        if org_name:
            oauth["organizationName"] = org_name
        local["oauthAccount"] = oauth

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
        logger.debug("Failed to write oauthAccount to %s", claude_json)


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

        # Non-symlink file/dir exists (Claude Code created it) — back up
        # and replace with symlink so global resources are always used.
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak")
            try:
                if backup.exists():
                    # Already backed up before — just remove the stale copy
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                else:
                    target.rename(backup)
            except OSError:
                logger.warning("Failed to replace %s with symlink (non-fatal)", name)
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

    # Refresh primary token if near-expiry (refresh_account_token is async)
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

    # Refresh CC token if near-expiry (independent from primary)
    if should_refresh_cc(account):
        from jacked.web.auth import refresh_cc_token

        try:
            asyncio.run(refresh_cc_token(account_id, db))
        except Exception as exc:
            logger.warning("Pre-launch CC token refresh failed: %s", exc)
        account = db.get_account(account_id)
        if not account:
            raise click.ClickException(f"Account {account_id} disappeared after CC refresh")

    # Pre-launch CC token warnings
    cc_at = account.get("cc_access_token")
    cc_rt = account.get("cc_refresh_token")
    cc_exp = account.get("cc_expires_at")
    if not cc_at:
        logger.warning(
            "Account %d: No CC token — using primary token (non-refreshable). "
            "Authorize CC via dashboard for stable sessions.",
            account_id,
        )
    elif cc_exp and cc_exp < _time.time() and not cc_rt:
        logger.warning(
            "Account %d: CC token expired and no refresh token — Claude Code "
            "session may fail. Re-authorize CC via dashboard.",
            account_id,
        )

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
    # Seed oauthAccount identity so Claude Code doesn't open browser to verify
    _seed_oauth_account(config_dir, account)
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
    if not write_platform_credentials(existing):
        logger.warning(
            "Keychain write failed for account %d — Claude Code may use "
            "stale credentials from a previous session",
            account_id,
        )

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
        acct_id = None

        # Layer 1: File stamp (fast, but Claude Code may delete the file)
        cred_path = Path.home() / ".claude" / ".credentials.json"
        if cred_path.exists() and not cred_path.is_symlink():
            try:
                data = json.loads(cred_path.read_text(encoding="utf-8"))
                acct_id = data.get("_jackedAccountId")
            except (json.JSONDecodeError, OSError):
                pass

        # Layer 2: DB settings (immune to Claude Code credential management)
        if acct_id is None:
            try:
                stored = db.get_setting("active_account_id")
                if stored:
                    acct_id = int(stored)
            except (ValueError, TypeError, Exception):
                pass

        # Layer 3: Keychain stamp
        if acct_id is None:
            try:
                kc_data = read_platform_credentials()
                if kc_data:
                    acct_id = kc_data.get("_jackedAccountId")
            except Exception:
                pass

        if acct_id is not None:
            account = db.get_account(acct_id)

        # Layer 4: Keychain token → DB match (last resort)
        # Credential files/Keychain contain CC tokens, so check cc_access_token
        # first (preferred), then fall back to primary access_token.
        if not account:
            try:
                kc_data = read_platform_credentials()
                if kc_data:
                    token = kc_data.get("claudeAiOauth", {}).get("accessToken")
                    if token:
                        accounts_list = db.list_accounts(include_inactive=False)
                        for acct in accounts_list:
                            if acct.get("cc_access_token") == token:
                                account = acct
                                break
                        if not account:
                            for acct in accounts_list:
                                if acct.get("access_token") == token:
                                    account = acct
                                    break
            except Exception:
                pass

        if not account:
            raise click.ClickException(
                "No active account detected. Specify an account: jacked claude <id>"
            )
    elif isinstance(account_ref, str) and "@" in account_ref:
        try:
            account = db.get_account_by_email(account_ref)
        except ValueError as exc:
            # Ambiguous: multiple orgs for the same email
            raise click.ClickException(str(exc))
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

        # Guard: verify the token still belongs to the expected account.
        # If Claude Code re-authenticated as a different Google account
        # inside this per-account dir, the .claude.json oauthAccount email
        # will no longer match the DB account's email.  Refuse to sync
        # the wrong token — it would contaminate the DB permanently.
        claude_json = config_dir / ".claude.json"
        if claude_json.exists() and not claude_json.is_symlink():
            try:
                cj = json.loads(claude_json.read_text(encoding="utf-8"))
                oauth_block = cj.get("oauthAccount")
                if not isinstance(oauth_block, dict):
                    # oauthAccount key is missing or non-dict — suspicious since
                    # jacked always seeds this block via _seed_oauth_account().
                    # Fail closed to prevent unverified token sync.
                    logger.warning(
                        "Token sync BLOCKED for account %d: "
                        "oauthAccount block missing or invalid in .claude.json",
                        account_id,
                    )
                    return
                file_email = oauth_block.get("emailAddress")
                if file_email:
                    import sqlite3 as _sq

                    _conn = _sq.connect(db_path, timeout=2.0)
                    try:
                        _conn.execute("PRAGMA busy_timeout = 5000")
                        _conn.row_factory = _sq.Row
                        _row = _conn.execute(
                            "SELECT email, organization_uuid FROM accounts WHERE id = ?",
                            (account_id,),
                        ).fetchone()
                        if _row and _row["email"] and _row["email"].lower() != file_email.lower():
                            logger.warning(
                                "Token sync BLOCKED for account %d: "
                                "credential file has email %s but DB has %s "
                                "— Claude Code may have re-authenticated as "
                                "a different account",
                                account_id, file_email, _row["email"],
                            )
                            return

                        # Org identity check — fail closed for org-scoped accounts
                        db_org = _row["organization_uuid"] if _row else ""
                        if db_org:  # non-empty sentinel = real org
                            file_org = oauth_block.get("organizationUuid")
                            if not file_org:
                                logger.warning(
                                    "Token sync BLOCKED for account %d: "
                                    "file missing org for org-scoped account",
                                    account_id,
                                )
                                return
                            if file_org != db_org:
                                logger.warning(
                                    "Token sync BLOCKED for account %d: "
                                    "org mismatch %s != %s",
                                    account_id, file_org, db_org,
                                )
                                return
                    except _sq.OperationalError as exc:
                        logger.debug(
                            "Token sync identity check failed for account %d: %s",
                            account_id, exc,
                        )
                        return
                    finally:
                        _conn.close()
                else:
                    # oauthAccount exists but emailAddress is missing —
                    # suspicious (jacked always seeds emailAddress).
                    # Block sync to prevent unverified token contamination.
                    logger.warning(
                        "Token sync BLOCKED for account %d: "
                        "oauthAccount present but emailAddress missing",
                        account_id,
                    )
                    return
            except (json.JSONDecodeError, OSError, AttributeError):
                # Corrupted/unreadable file — fail closed to avoid
                # syncing tokens without identity verification.
                logger.debug(
                    "Token sync skipped for account %d: "
                    "cannot parse credential file for identity check",
                    account_id,
                )
                return

        import sqlite3

        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")

            # Check if cc_* columns exist (handles pre-migration edge case)
            cursor = conn.execute("PRAGMA table_info(accounts)")
            cols = {row[1] for row in cursor.fetchall()}
            has_cc_cols = "cc_access_token" in cols

            refresh_token = oauth.get("refreshToken")
            expires_at_ms = oauth.get("expiresAt", 0)
            expires_at = int(expires_at_ms // 1000) if expires_at_ms else None

            if has_cc_cols:
                # Credential files contain CC tokens — sync to cc_* columns.
                # CAS: only write if DB value hasn't changed under us.
                row = conn.execute(
                    "SELECT cc_access_token FROM accounts WHERE id = ?",
                    (account_id,),
                ).fetchone()
                old_cc_access_token = row[0] if row else None
                if old_cc_access_token == access_token:
                    return  # Token unchanged

                # If cc_access_token is NULL in DB, skip sync. NULL means
                # CC auth was never done — file contains primary fallback
                # tokens that shouldn't be stored in cc_* columns.
                # In both cases, only the OAuth flow or async refresh should
                # populate cc_* columns from NULL.
                if old_cc_access_token is None:
                    logger.debug(
                        "Skipping CC sync for account %d — cc_access_token "
                        "is NULL (awaiting CC auth or post-invalidation)",
                        account_id,
                    )
                    return

                now = int(_time.time())
                # Column names are hardcoded below, not from user input
                set_clauses = ["cc_access_token = ?"]
                params: list = [access_token]

                # Only overwrite cc_expires_at if we have a real value
                if expires_at:
                    set_clauses.append("cc_expires_at = ?")
                    params.append(expires_at)

                # Write cc_refresh_token as-is (including NULL).
                # Do NOT use COALESCE — if Claude Code consumed the refresh
                # token and wrote null to the file, we must propagate that
                # null so should_refresh_cc() knows the token is gone.
                set_clauses.append("cc_refresh_token = ?")
                params.append(refresh_token)

                set_clauses.extend([
                    "validation_status = 'valid'",
                    "last_validated_at = ?",
                ])
                params.append(now)
                params.extend([account_id, old_cc_access_token])

                # CAS WHERE clause: only update if cc_access_token hasn't
                # changed since we read it (prevents stale overwrites from
                # concurrent refresh_cc_token in the async event loop).
                # Uses IS instead of = for correct NULL comparison in SQL.
                result = conn.execute(
                    f"UPDATE accounts SET {', '.join(set_clauses)} "
                    f"WHERE id = ? AND cc_access_token IS ?",
                    params,
                )
                if result.rowcount > 0:
                    conn.commit()
                    logger.debug(
                        "Synced CC tokens for account %d", account_id
                    )
                else:
                    logger.debug(
                        "CAS skip for account %d — another writer updated first",
                        account_id,
                    )
            else:
                # Pre-migration fallback: write to primary columns
                row = conn.execute(
                    "SELECT access_token FROM accounts WHERE id = ?",
                    (account_id,),
                ).fetchone()
                if row and row[0] == access_token:
                    return  # unchanged

                now_iso = datetime.now(timezone.utc).isoformat()
                updates = [
                    "access_token = ?",
                    "validation_status = 'valid'",
                    "consecutive_failures = 0",
                    "last_error = NULL",
                    "updated_at = ?",
                ]
                params_legacy: list = [access_token, now_iso]
                if refresh_token:
                    updates.append("refresh_token = ?")
                    params_legacy.append(refresh_token)
                if expires_at is not None:
                    updates.append("expires_at = ?")
                    params_legacy.append(expires_at)
                params_legacy.append(account_id)

                conn.execute(
                    f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?",
                    params_legacy,
                )
                conn.commit()
                logger.debug(
                    "Synced refreshed tokens for account %d (pre-migration)",
                    account_id,
                )
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
