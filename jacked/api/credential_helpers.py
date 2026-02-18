"""Credential file helpers for atomic writes and missing-file recreation.

Extracted from credential_sync.py to keep file sizes within guardrails.
"""

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _safe_replace(src: str, dst: str, *, retries: int = 3, delay: float = 0.1):
    """os.replace() with retry for Windows PermissionError.

    On Windows, os.replace() can fail if the target file is held open
    by another process.  Retries with exponential backoff.
    On macOS/Linux, this is equivalent to a single os.replace() call.
    """
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == retries - 1:
                raise
            time.sleep(delay * (2 ** attempt))


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
        _safe_replace(tmp, str(cred_path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cred_path.stat().st_mtime


def create_missing_credentials_file(db) -> float | None:
    """Recreate .credentials.json from keychain or default account when missing.

    On macOS, tries the Keychain first (has the freshest token).
    Falls back to creating from the default DB account.
    Refuses to write through symlinks.

    Uses include_layer3=False for matching since this is a mutating
    operation at startup — conservative matching only.

    Returns the mtime of the written file (for watcher loop suppression),
    or None if no write was needed/possible.

    >>> create_missing_credentials_file(None)
    """
    # Lazy imports to avoid circular dependency with credential_sync
    from jacked.api.credential_sync import (
        match_credential_to_account,
        read_platform_credentials,
        sync_credential_tokens,
    )

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
            # Conservative matching: no Layer 3 for mutating startup operation
            account, _method = match_credential_to_account(
                db, keychain_data, include_layer3=False
            )
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
            if default_account is None or acct.get(
                "priority", 999
            ) < default_account.get("priority", 999):
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
