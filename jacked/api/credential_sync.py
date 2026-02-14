"""Token sync between Claude Code's credential file and jacked's DB.

When Claude Code independently refreshes OAuth tokens, the new tokens
are written to ~/.claude/.credentials.json but jacked's database retains
the old (now-dead) tokens.  This module syncs the file back to the DB.
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def sync_credential_tokens(db, cred_data: dict) -> bool:
    """Sync tokens from credential file back to DB.

    Prevents token desync when Claude Code refreshes tokens
    independently of jacked's background refresh loop.

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

    # Layer 1: _jackedAccountId (stamped by /use endpoint)
    # Note: inverted from get_active_credential's order — here the file's
    # own stamp is the strongest signal since we're reacting to a file change.
    account = None
    jacked_id = cred_data.get("_jackedAccountId")
    if jacked_id is not None:
        account = db.get_account(jacked_id)

    # Layer 2: email from ~/.claude.json (case-insensitive via get_account_by_email)
    if not account:
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
