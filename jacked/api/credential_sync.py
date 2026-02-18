"""Token sync between Claude Code's credential file and jacked's DB.

When Claude Code independently refreshes OAuth tokens, the new tokens
are written to ~/.claude/.credentials.json but jacked's database retains
the old (now-dead) tokens.  This module syncs the file back to the DB.

Also provides self-healing helpers:
- re_stamp_jacked_account_id: re-adds the stamp when Claude Code removes it

Matching layer priority (shared across all callers):
  Layer 1:    _jackedAccountId stamp (strongest — user's explicit choice)
  Layer 2:    Exact access_token match (cryptographically unique)
  Layer 2.5:  Exact refresh_token match (weeks-lived, survives AT rotation)
  Layer 2.75: known_refresh_tokens table (historical RT → account mapping)
  Layer 2.85: Single-account optimization (exactly 1 active OAuth account)
  Layer 3:    Email from ~/.claude.json (staleness-gated — only if mtime < 60s)
  Fallback:   Profile API to identify token owner by email (auto-creates if new)
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from jacked.api.credential_helpers import _safe_replace
from jacked.web.oauth import OAUTH_BETA_HEADER, ORG_TYPE_MAP, PROFILE_URL

logger = logging.getLogger(__name__)

# Layer 3 freshness gate: only trust ~/.claude.json email if file was
# modified within this many seconds.  Prevents stale email from a past
# "Set Active" from contaminating a different account's tokens.
# SYNC: keep in sync with session_account_tracker.py:73
LAYER3_FRESHNESS_SECONDS = 60


def read_platform_credentials() -> dict | None:
    """Read credentials from the platform's native credential store.

    macOS: Keychain ("Claude Code-credentials")
    Linux/Windows: not yet needed (still use .credentials.json)

    Returns parsed dict (same shape as .credentials.json) or None."""
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
    True"""
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


# -- Shared credential matching ------------------------------------------------


def match_credential_to_account(
    db,
    cred_data: dict,
    *,
    include_layer3: bool = True,
) -> tuple[dict | None, str]:
    """Match credential data to a DB account using layered priority.

    Implements all matching layers with the staleness-gated Layer 3.
    Design philosophy: better to not match than to match wrong.

    Args:
        db: Database instance
        cred_data: Parsed credential data (from file or keychain)
        include_layer3: If False, skip Layer 3 (email). Use for conservative
                       callers like create_missing_credentials_file().

    Returns (account_dict, match_method) or (None, "none").

    >>> match_credential_to_account(None, {})
    (None, 'none')
    >>> match_credential_to_account(None, {"claudeAiOauth": {"accessToken": "x"}})
    (None, 'none')"""
    if db is None:
        return None, "none"

    oauth = cred_data.get("claudeAiOauth", {})
    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if not access_token:
        return None, "none"

    # Layer 1: _jackedAccountId stamp (strongest — user's explicit choice)
    jacked_id = cred_data.get("_jackedAccountId")
    if jacked_id is not None:
        account = db.get_account(jacked_id)
        if account and not account.get("is_deleted"):
            return account, "stamp"

    # Layer 2: Exact access_token match (cryptographically unique)
    accounts = db.list_accounts(include_inactive=True)
    for acct in accounts:
        if acct.get("access_token") == access_token and not acct.get("is_deleted"):
            return acct, "access_token"

    # Layer 2.5: Exact refresh_token match (current DB RT)
    if refresh_token:
        for acct in accounts:
            if acct.get("refresh_token") == refresh_token and not acct.get(
                "is_deleted"
            ):
                return acct, "refresh_token"

    # Layer 2.75: known_refresh_tokens table (historical RT → account mapping)
    if refresh_token and hasattr(db, "lookup_refresh_token"):
        known_account_id = db.lookup_refresh_token(refresh_token)
        if known_account_id is not None:
            acct = db.get_account(known_account_id)
            if acct and not acct.get("is_deleted"):
                return acct, "known_refresh_token"

    # Layer 2.85: Single-account optimization (unambiguous if only 1 OAuth account)
    oauth_accounts = [
        a for a in accounts
        if a.get("refresh_token") and not a.get("is_deleted")
    ]
    if len(oauth_accounts) == 1:
        return oauth_accounts[0], "single_oauth_account"

    # Layer 3: Staleness-gated email from ~/.claude.json
    if include_layer3:
        claude_config = Path.home() / ".claude.json"
        if claude_config.exists() and not claude_config.is_symlink():
            try:
                config_mtime = claude_config.stat().st_mtime
                if time.time() - config_mtime <= LAYER3_FRESHNESS_SECONDS:
                    config = json.loads(claude_config.read_text(encoding="utf-8"))
                    email = config.get("oauthAccount", {}).get("emailAddress")
                    if email:
                        account = db.get_account_by_email(email)
                        if account and not account.get("is_deleted"):
                            return account, "fresh_email"
                else:
                    logger.debug(
                        "Layer 3 skipped: ~/.claude.json is stale (%.0fs old)",
                        time.time() - config_mtime,
                    )
            except (json.JSONDecodeError, OSError):
                pass

    return None, "none"


# -- Profile API fallback helpers ----------------------------------------------


def _fetch_profile(access_token: str) -> dict | None:
    """Call profile API to get full account+org data. ~200ms, sync.

    Only triggers on unmatched tokens (rare — after /login with stale DB)."""
    try:
        resp = httpx.get(
            PROFILE_URL,
            headers={
                "Authorization": f"Bearer {access_token}",
                "anthropic-beta": OAUTH_BETA_HEADER,
            },
            timeout=5.0,
        )
        if resp.status_code == 200:
            return resp.json()
        logger.debug("Profile API HTTP %d", resp.status_code)
    except Exception as exc:
        logger.debug("Profile API call failed: %s", exc)
    return None


def _create_account_from_profile(db, profile: dict, oauth: dict) -> dict | None:
    """Auto-create a jacked account from profile API data + credential tokens.

    Same data extraction as oauth.py:_store_account() but triggered by
    credential sync discovering an unknown account via profile API."""
    acct_info = profile.get("account", {})
    org = profile.get("organization", {})
    email = acct_info.get("email")
    if not email:
        return None

    # Map org type to subscription type (same as oauth.py:427)
    org_type = org.get("organization_type", "")
    subscription_type = ORG_TYPE_MAP.get(org_type)

    # Parse expiry (credential file stores ms, DB stores seconds)
    expires_at_ms = oauth.get("expiresAt", 0)
    expires_at = (
        int(expires_at_ms // 1000) if expires_at_ms else int(time.time()) + 28800
    )

    # Build scopes JSON from credential file
    scopes_json = None
    raw_scopes = oauth.get("scopes")
    if raw_scopes and isinstance(raw_scopes, list):
        scopes_json = json.dumps(raw_scopes)

    account = db.create_account(
        email=email,
        access_token=oauth.get("accessToken", ""),
        refresh_token=oauth.get("refreshToken"),
        expires_at=expires_at,
        display_name=acct_info.get("display_name"),
        scopes=scopes_json,
        subscription_type=subscription_type,
        rate_limit_tier=org.get("rate_limit_tier"),
        has_extra_usage=org.get("has_extra_usage_enabled", False),
    )

    # Mark valid + record RT for future matching
    db.update_account(
        account["id"],
        validation_status="valid",
        last_validated_at=int(time.time()),
    )
    rt = oauth.get("refreshToken")
    if rt and hasattr(db, "record_refresh_token"):
        db.record_refresh_token(rt, account["id"])

    return account


# ---------------------------------------------------------------------------
# Token sync
# ---------------------------------------------------------------------------


def sync_credential_tokens(db, cred_data: dict) -> bool:
    """Sync tokens from credential file back to DB.

    Uses match_credential_to_account() for layered matching.
    Falls back to profile API when all layers fail (identifies token
    owner by email, auto-creates account if new).
    Returns True if tokens were synced, False otherwise.

    >>> sync_credential_tokens(None, {})
    False
    >>> sync_credential_tokens(None, {"claudeAiOauth": {"accessToken": "x"}})
    False"""
    if db is None:
        return False

    oauth = cred_data.get("claudeAiOauth", {})
    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if not access_token:
        return False

    account, method = match_credential_to_account(db, cred_data)
    if not account:
        # All matching layers failed — try profile API to identify token owner
        profile = _fetch_profile(access_token)
        if profile:
            email = profile.get("account", {}).get("email")
            if email:
                account = db.get_account_by_email(email)
                if account and not account.get("is_deleted"):
                    method = "profile_api"
                else:
                    # New account — auto-create from profile + credential data
                    account = _create_account_from_profile(db, profile, oauth)
                    if account:
                        method = "profile_api_created"
                        logger.info(
                            "Auto-created account %d (%s) from credential sync",
                            account["id"],
                            email,
                        )
                        return True  # account already has correct tokens
                    logger.warning(
                        "Token sync: failed to create account for %s", email
                    )
                    return False
            else:
                logger.debug("Token sync: profile API returned no email")
                return False
        else:
            # Profile API failed (network/timeout) — retry next cycle
            logger.debug(
                "Token sync: no match and profile API failed, will retry"
            )
            return False

    # Record refresh tokens for Layer 2.75 future matching
    if hasattr(db, "record_refresh_token"):
        if refresh_token:
            db.record_refresh_token(refresh_token, account["id"])
        # Also record the account's current DB RT (may differ from incoming)
        current_rt = account.get("refresh_token")
        if current_rt and current_rt != refresh_token:
            db.record_refresh_token(current_rt, account["id"])

    # Only sync if tokens actually changed
    if account.get("access_token") == access_token:
        # Tokens match — if account is in error/unknown state, mark valid
        # (token is actively used by Claude Code, so it's valid)
        if account.get("validation_status") in ("invalid", "unknown"):
            updates = {
                "validation_status": "valid",
                "last_validated_at": int(time.time()),
                "consecutive_failures": 0,
                "last_error": None,
                "last_error_at": None,
            }
            db.update_account(account["id"], **updates)
            logger.info(
                "Cleared stale error for account %d (tokens match, via %s)",
                account["id"],
                method,
            )
            return True
        return False

    expires_at_ms = oauth.get("expiresAt", 0)
    expires_at = int(expires_at_ms // 1000) if expires_at_ms else None

    # Fresh tokens from Claude Code — mark valid (actively used by CC)
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

    db.update_account(account["id"], **updates)
    logger.info(
        "Synced tokens from credential file for account %d (via %s)",
        account["id"],
        method,
    )
    return True


# ---------------------------------------------------------------------------
# Re-stamping
# ---------------------------------------------------------------------------


def re_stamp_jacked_account_id(db, cred_data: dict, cred_path: Path) -> float | None:
    """Re-add _jackedAccountId stamp if Claude Code removed it.

    Called from the credential watcher after sync_credential_tokens().
    Returns mtime of written file, or None if no write was needed.

    >>> re_stamp_jacked_account_id(None, {}, Path("/tmp/fake"))"""
    if db is None:
        return None

    if "_jackedAccountId" in cred_data:
        logger.debug("Re-stamp skipped: _jackedAccountId already present")
        return None

    access_token = cred_data.get("claudeAiOauth", {}).get("accessToken")
    if not access_token:
        return None

    matched, method = match_credential_to_account(db, cred_data)
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
            _safe_replace(tmp, str(cred_path))  # from credential_helpers
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

        mtime = cred_path.stat().st_mtime
        logger.info(
            "Re-stamped _jackedAccountId=%d via %s", matched["id"], method
        )
        return mtime
    except Exception as exc:
        logger.warning("Failed to re-stamp .credentials.json: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Active account detection
# ---------------------------------------------------------------------------


def detect_active_account(db) -> tuple[int | None, str | None]:
    """Detect the active Claude Code account and its live access token.

    Returns (account_id, live_access_token) or (None, None).

    >>> detect_active_account(None)
    (None, None)"""
    if db is None:
        return None, None

    cred_data = None
    cred_access_token = None

    # Try credential file first
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            cred_data = json.loads(cred_path.read_text(encoding="utf-8"))
            cred_access_token = (
                cred_data.get("claudeAiOauth", {}).get("accessToken")
            )
        except (json.JSONDecodeError, OSError):
            pass

    # Fallback: macOS Keychain (file may not exist on Mac)
    if cred_access_token is None:
        platform_data = read_platform_credentials()
        if platform_data:
            cred_data = platform_data
            cred_access_token = (
                platform_data.get("claudeAiOauth", {}).get("accessToken")
            )

    if not cred_data or not cred_access_token:
        return None, None

    account, _method = match_credential_to_account(db, cred_data)
    if account:
        return account["id"], cred_access_token

    return None, cred_access_token
