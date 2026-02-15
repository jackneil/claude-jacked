"""Token sync between Claude Code's credential file and jacked's DB.

When Claude Code independently refreshes OAuth tokens, the new tokens
are written to ~/.claude/.credentials.json but jacked's database retains
the old (now-dead) tokens.  This module syncs the file back to the DB.

Also provides self-healing helpers:
- re_stamp_jacked_account_id: re-adds the stamp when Claude Code removes it
- create_missing_credentials_file: recreates the file from the default account
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def read_platform_credentials() -> dict | None:
    """Read credentials from the platform's native credential store.

    macOS: Keychain ("Claude Code-credentials")
    Linux/Windows: not yet needed (still use .credentials.json)

    Returns parsed dict (same shape as .credentials.json) or None.
    """
    if sys.platform != "darwin":
        return None
    try:
        result = subprocess.run(
            ["security", "find-generic-password",
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        if result.returncode != 0:
            logger.debug("Keychain read failed: %s", result.stderr.strip())
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as exc:
        logger.debug("Keychain read error: %s", exc)
    return None


def write_platform_credentials(data: dict) -> bool:
    """Write credentials to the platform's native credential store.

    macOS: Keychain ("Claude Code-credentials")
    Linux/Windows: no-op (they use .credentials.json)

    Returns True if written successfully, False otherwise.

    >>> write_platform_credentials({}) if sys.platform != "darwin" else True
    True
    """
    if sys.platform != "darwin":
        return True  # no-op on non-macOS (file write is sufficient)
    try:
        json_data = json.dumps(data, separators=(",", ":"))
        # Delete existing entry (ignore failure if not found)
        subprocess.run(
            ["security", "delete-generic-password",
             "-s", "Claude Code-credentials"],
            capture_output=True, timeout=5,
        )
        # Add new entry
        result = subprocess.run(
            ["security", "add-generic-password",
             "-s", "Claude Code-credentials",
             "-a", "Claude Code",
             "-w", json_data],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("Keychain write failed: %s", result.stderr.strip())
            return False
        return True
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("Keychain write error: %s", exc)
        return False


def sync_credential_tokens(db, cred_data: dict) -> bool:
    """Sync tokens from credential file back to DB.

    Layer priority (must stay in sync with session_account_tracker.py,
    credentials.py:get_active_credential, and auth.py:refresh_all_expiring_tokens):

    Layer 1: _jackedAccountId stamp — strongest, explicitly set by jacked.
    Layer 2: Exact access_token match — cryptographically unique.
    Layer 3: Email from ~/.claude.json — weakest, Claude Code can change independently.

    Returns True if tokens were synced, False otherwise.

    >>> sync_credential_tokens(None, {})
    False
    >>> sync_credential_tokens(None, {"claudeAiOauth": {"accessToken": "x"}})
    False
    """
    if db is None:
        return False

    oauth = cred_data.get("claudeAiOauth", {})
    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if not access_token:
        return False

    # Layer 1: _jackedAccountId stamp (strongest — user's explicit choice)
    account = None
    jacked_id = cred_data.get("_jackedAccountId")
    if jacked_id is not None:
        account = db.get_account(jacked_id)

    # Layer 2: Exact access_token match (cryptographically unique)
    if not account or account.get("is_deleted"):
        accounts = db.list_accounts(include_inactive=True)
        for acct in accounts:
            if acct.get("access_token") == access_token and not acct.get("is_deleted"):
                account = acct
                break

    # Layer 3: Email from ~/.claude.json (weakest — can drift independently)
    if not account or account.get("is_deleted"):
        claude_config = Path.home() / ".claude.json"
        if claude_config.exists() and not claude_config.is_symlink():
            try:
                config = json.loads(claude_config.read_text(encoding="utf-8"))
                email = config.get("oauthAccount", {}).get("emailAddress")
                if email:
                    account = db.get_account_by_email(email)
            except (json.JSONDecodeError, OSError):
                pass

    if not account or account.get("is_deleted"):
        return False

    # Only sync if tokens actually changed
    if account.get("access_token") == access_token:
        # Tokens match — but if account is invalid, clear the error.
        # Note: this doesn't prove the token is valid against Anthropic's API,
        # only that the credential file has the same token. In practice Claude Code
        # validates on startup and would fail visibly if the token were dead.
        if account.get("validation_status") == "invalid":
            updates = {
                "validation_status": "valid",
                "consecutive_failures": 0,
                "last_error": None,
                "last_error_at": None,
                "last_validated_at": int(time.time()),
            }
            db.update_account(account["id"], **updates)
            logger.info(
                "Cleared stale error for account %d (tokens match)", account["id"]
            )
            return True
        return False

    expires_at_ms = oauth.get("expiresAt", 0)
    expires_at = int(expires_at_ms // 1000) if expires_at_ms else None

    # Fresh tokens from Claude Code — reset validation state
    updates = {
        "access_token": access_token,
        "validation_status": "valid",
        "consecutive_failures": 0,
        "last_error": None,
    }
    if refresh_token:
        updates["refresh_token"] = refresh_token
    if expires_at:
        updates["expires_at"] = expires_at

    db.update_account(account["id"], **updates)
    logger.info("Synced tokens from credential file for account %d", account["id"])
    return True


def re_stamp_jacked_account_id(db, cred_data: dict, cred_path: Path) -> float | None:
    """Re-add _jackedAccountId stamp if Claude Code removed it.

    Called from the credential watcher after sync_credential_tokens().
    Only writes if the stamp is actually missing (idempotent).

    Returns the mtime of the written file (for watcher loop suppression),
    or None if no write was needed.

    >>> re_stamp_jacked_account_id(None, {}, Path("/tmp/fake"))
    """
    if db is None:
        return None

    if "_jackedAccountId" in cred_data:
        logger.debug("Re-stamp skipped: _jackedAccountId already present")
        return None

    access_token = cred_data.get("claudeAiOauth", {}).get("accessToken")
    if not access_token:
        return None

    # Match account: token first, then email
    matched = None
    match_method = None

    accounts = db.list_accounts(include_inactive=True)
    for acct in accounts:
        if acct.get("access_token") == access_token and not acct.get("is_deleted"):
            matched = acct
            match_method = "token"
            break

    if not matched:
        claude_config = Path.home() / ".claude.json"
        if claude_config.exists() and not claude_config.is_symlink():
            try:
                config = json.loads(claude_config.read_text(encoding="utf-8"))
                email = config.get("oauthAccount", {}).get("emailAddress")
                if email:
                    matched = db.get_account_by_email(email)
                    if matched:
                        match_method = "email"
            except (json.JSONDecodeError, OSError):
                pass

    if not matched:
        logger.debug("Re-stamp skipped: no matching account found")
        return None

    # Write stamp back to file
    cred_data["_jackedAccountId"] = matched["id"]
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(cred_path.parent),
            prefix=".credentials_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cred_data, f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, str(cred_path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        mtime = cred_path.stat().st_mtime
        logger.info(
            "Re-stamped _jackedAccountId=%d via %s", matched["id"], match_method
        )
        return mtime
    except Exception as exc:
        logger.warning("Failed to re-stamp .credentials.json: %s", exc)
        return None


def _write_credential_file(cred_path: Path, data: dict) -> float:
    """Write credential data to file atomically with secure permissions.

    Returns mtime of written file. Raises on failure.
    """
    if cred_path.is_symlink():
        raise OSError("Refusing to write through symlink")
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(cred_path.parent),
        prefix=".credentials_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, str(cred_path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cred_path.stat().st_mtime


def _match_keychain_to_account(db, data: dict) -> dict | None:
    """Match keychain credential data to a DB account using Layer 2/3.

    Layer 2: Exact access_token match.
    Layer 3: Email from ~/.claude.json.

    Does NOT use Layer 1 (keychain data won't have _jackedAccountId stamp).
    Returns full account dict or None.
    """
    access_token = data.get("claudeAiOauth", {}).get("accessToken")

    # Layer 2: Exact access_token match
    if access_token:
        accounts = db.list_accounts(include_inactive=True)
        for acct in accounts:
            if acct.get("access_token") == access_token and not acct.get("is_deleted"):
                return acct

    # Layer 3: Email from ~/.claude.json
    claude_config = Path.home() / ".claude.json"
    if claude_config.exists() and not claude_config.is_symlink():
        try:
            config = json.loads(claude_config.read_text(encoding="utf-8"))
            email = config.get("oauthAccount", {}).get("emailAddress")
            if email:
                account = db.get_account_by_email(email)
                if account and not account.get("is_deleted"):
                    return account
        except (json.JSONDecodeError, OSError):
            pass

    return None


def create_missing_credentials_file(db) -> float | None:
    """Recreate .credentials.json from keychain or default account when missing.

    On macOS, tries the Keychain first (has the freshest token).
    Falls back to creating from the default DB account.
    Refuses to write through symlinks.

    Returns the mtime of the written file (for watcher loop suppression),
    or None if no write was needed/possible.

    >>> create_missing_credentials_file(None)
    """
    if db is None:
        return None

    cred_path = Path.home() / ".claude" / ".credentials.json"
    if cred_path.exists():
        return None

    # Security: refuse to write through symlinks
    if cred_path.is_symlink():
        logger.warning(
            "Refusing to create .credentials.json — path is a symlink"
        )
        return None

    # Try platform keychain first (has the freshest token)
    keychain_data = read_platform_credentials()
    if keychain_data:
        oauth = keychain_data.get("claudeAiOauth", {})
        access_token = oauth.get("accessToken")
        if access_token:
            account = _match_keychain_to_account(db, keychain_data)
            if account:
                keychain_data["_jackedAccountId"] = account["id"]
                # Sync fresh token to DB
                sync_credential_tokens(db, keychain_data)
            try:
                mtime = _write_credential_file(cred_path, keychain_data)
                logger.info(
                    "Created .credentials.json from keychain (account=%s)",
                    account["id"] if account else "unknown",
                )
                return mtime
            except Exception as exc:
                logger.warning("Failed to write keychain creds to file: %s", exc)

    # Fallback: create from DB (existing logic)
    accounts = db.list_accounts(include_inactive=False)
    default_account = None
    for acct in accounts:
        if not acct.get("is_deleted") and acct.get("access_token"):
            if default_account is None or acct.get("priority", 999) < default_account.get(
                "priority", 999
            ):
                default_account = acct

    if not default_account:
        logger.debug("Cannot recreate .credentials.json: no default account found")
        return None

    # Parse scopes
    scopes = []
    raw_scopes = default_account.get("scopes")
    if raw_scopes:
        try:
            parsed = json.loads(raw_scopes)
            if isinstance(parsed, list):
                scopes = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    cred_data = {
        "_jackedAccountId": default_account["id"],
        "claudeAiOauth": {
            "accessToken": default_account["access_token"],
            "refreshToken": default_account.get("refresh_token"),
            "expiresAt": (default_account.get("expires_at") or 0) * 1000,
            "scopes": scopes,
            "subscriptionType": default_account.get("subscription_type"),
            "rateLimitTier": default_account.get("rate_limit_tier"),
        },
    }

    try:
        mtime = _write_credential_file(cred_path, cred_data)
        logger.info(
            "Created missing .credentials.json for account %d (%s)",
            default_account["id"],
            default_account["email"],
        )
        return mtime
    except Exception as exc:
        logger.warning("Failed to create .credentials.json: %s", exc)
        return None
