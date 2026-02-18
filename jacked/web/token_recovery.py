"""Token recovery: force re-sync and crash-safe token persistence.

When Claude Code refreshes tokens independently, jacked may end up with
stale tokens in the DB.  Two recovery strategies live here:

1. _force_resync_for_active_account: bypasses the matching layers and
   assigns credential-file/keychain tokens directly to a known account.
   Called when invalid_grant proves Claude Code consumed our old RT.

2. Token recovery file (~/.claude/.token_recovery.json): if the DB update
   fails after Anthropic consumes the old refresh token, we write the
   new tokens to a recovery file so they survive a crash.  On next
   startup, apply_token_recovery() reads it and patches the DB.
"""

import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger("jacked.token_recovery")


def force_resync_for_active_account(account_id: int, db) -> bool:
    """Force re-read from credential file/keychain and assign directly.

    Called when invalid_grant proves Claude Code already consumed our RT.
    We KNOW the credential source has this account's fresh tokens.
    Bypasses normal matching — assigns directly to the known account.

    Safety: if the credential stamp points to a *different* account,
    we skip (those tokens belong elsewhere).

    Returns True if tokens were successfully re-synced, False otherwise.
    """
    from jacked.api.credential_sync import read_platform_credentials

    sources: list[dict] = []

    # Try credential file
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            sources.append(data)
        except (json.JSONDecodeError, OSError):
            pass

    # Try keychain (macOS)
    kc_data = read_platform_credentials()
    if kc_data:
        sources.append(kc_data)

    for data in sources:
        # Safety: if stamp points to a different account, skip
        stamped_id = data.get("_jackedAccountId")
        if stamped_id is not None and stamped_id != account_id:
            logger.debug(
                "Force re-sync skipped: stamp points to account %d, not %d",
                stamped_id,
                account_id,
            )
            continue

        oauth = data.get("claudeAiOauth", {})
        at = oauth.get("accessToken")
        rt = oauth.get("refreshToken")
        if at:
            expires_at_ms = oauth.get("expiresAt", 0)
            expires_at = int(expires_at_ms // 1000) if expires_at_ms else None
            updates = {
                "access_token": at,
                "validation_status": "valid",
                "last_validated_at": int(time.time()),
                "consecutive_failures": 0,
                "last_error": None,
                "last_error_at": None,
            }
            if rt:
                updates["refresh_token"] = rt
            if expires_at:
                updates["expires_at"] = expires_at
            db.update_account(account_id, **updates)
            # Record RT for future matching (Layer 2.75)
            if rt and hasattr(db, "record_refresh_token"):
                db.record_refresh_token(rt, account_id)
            logger.info(
                "Force re-synced credential tokens to account %d", account_id
            )
            return True

    logger.warning(
        "Force re-sync failed for account %d: no usable credential source",
        account_id,
    )
    return False


# ---------------------------------------------------------------------------
# Token recovery file
# ---------------------------------------------------------------------------

_RECOVERY_PATH = Path.home() / ".claude" / ".token_recovery.json"


def _safe_replace(src: str, dst: str, *, retries: int = 3, delay: float = 0.1):
    """os.replace() with retry for Windows PermissionError."""
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == retries - 1:
                raise
            time.sleep(delay * (2 ** attempt))


def write_token_recovery(
    account_id: int,
    access_token: str,
    refresh_token: str,
    expires_at: int,
) -> bool:
    """Write tokens to recovery file after DB update failure.

    Uses atomic write with 0o600 permissions, refuses to write through
    symlinks.  Recovery file is consumed by apply_token_recovery().

    Returns True on success, False on failure.
    """
    recovery_path = _RECOVERY_PATH
    if recovery_path.is_symlink():
        logger.warning("Refusing to write token recovery: path is a symlink")
        return False

    recovery_data = {
        "account_id": account_id,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": expires_at,
        "written_at": int(time.time()),
    }

    try:
        recovery_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(
            dir=str(recovery_path.parent),
            prefix=".token_recovery_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(recovery_data, f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            _safe_replace(tmp, str(recovery_path))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        logger.warning(
            "Wrote token recovery file for account %d (DB update failed)",
            account_id,
        )
        return True
    except Exception as exc:
        logger.error("Failed to write token recovery file: %s", exc)
        return False


def apply_token_recovery(db) -> bool:
    """Apply token recovery file to DB if it exists, then delete it.

    Called at startup from main.py.  Reads the recovery file, patches
    the DB account, and removes the file.

    Returns True if recovery was applied, False otherwise.
    """
    recovery_path = _RECOVERY_PATH
    if not recovery_path.exists() or recovery_path.is_symlink():
        return False

    try:
        data = json.loads(recovery_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read token recovery file: %s", exc)
        return False

    account_id = data.get("account_id")
    access_token = data.get("access_token")
    refresh_token = data.get("refresh_token")
    expires_at = data.get("expires_at")

    if not account_id or not access_token:
        logger.warning("Token recovery file is incomplete, removing")
        _safe_remove(recovery_path)
        return False

    # Check staleness: ignore recovery files older than 1 hour
    written_at = data.get("written_at", 0)
    if time.time() - written_at > 3600:
        logger.warning(
            "Token recovery file is stale (%ds old), removing",
            int(time.time() - written_at),
        )
        _safe_remove(recovery_path)
        return False

    account = db.get_account(account_id)
    if not account:
        logger.warning(
            "Token recovery: account %d not found, removing recovery file",
            account_id,
        )
        _safe_remove(recovery_path)
        return False

    try:
        updates = {
            "access_token": access_token,
            "validation_status": "valid",
            "last_validated_at": int(time.time()),
            "consecutive_failures": 0,
            "last_error": None,
            "last_error_at": None,
        }
        if refresh_token:
            updates["refresh_token"] = refresh_token
        if expires_at:
            updates["expires_at"] = expires_at
        db.update_account(account_id, **updates)
        # Record RT for Layer 2.75
        if refresh_token and hasattr(db, "record_refresh_token"):
            db.record_refresh_token(refresh_token, account_id)
        logger.info(
            "Applied token recovery for account %d", account_id
        )
    except Exception as exc:
        logger.error(
            "Failed to apply token recovery for account %d: %s",
            account_id,
            exc,
        )
        return False

    _safe_remove(recovery_path)
    return True


def _safe_remove(path: Path):
    """Remove a file, ignoring errors."""
    try:
        path.unlink()
    except OSError:
        pass
