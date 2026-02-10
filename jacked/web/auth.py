"""Token refresh, usage fetch, profile fetch, and account validation.

This module handles the ongoing lifecycle of authenticated accounts:
- Proactive token refresh (5-minute buffer before expiry)
- Background bulk refresh (every 30min via _token_refresh_loop)
- Usage cache updates (5h + 7d utilization)
- Profile metadata refresh (subscription type, rate limit tier)
- Account validation (verify token is still valid)

All API interactions follow design doc section 4 header matrix.
"""

import asyncio
import logging
import time
from datetime import datetime
from typing import Optional

import httpx

from jacked.web.database import Database
from jacked.web.oauth import (
    CLIENT_ID,
    OAUTH_BETA_HEADER,
    ORG_TYPE_MAP,
    PROFILE_URL,
    TOKEN_URL,
    USAGE_URL,
)

logger = logging.getLogger("jacked.auth")


def should_refresh(account: dict) -> bool:
    """Check if an account's token needs refreshing.

    Rules:
    - API key accounts (refresh_token is None) cannot be refreshed
    - Refresh when now > expires_at - 300 (5-minute buffer)

    >>> should_refresh({"refresh_token": None, "expires_at": 9999999999})
    False
    >>> should_refresh({"refresh_token": "rt-test", "expires_at": 0})
    True
    >>> should_refresh({"refresh_token": "rt-test", "expires_at": 9999999999})
    False
    """
    if not account.get("refresh_token"):
        return False
    return time.time() > account["expires_at"] - 300


async def refresh_account_token(account_id: int, db: Database) -> bool:
    """Refresh an account's token if needed.

    Implements design doc section 10 refresh logic:
    - Skip if no refresh_token (API key account)
    - Skip if token not near expiry
    - POST to TOKEN_URL with JSON body
    - Handle token rotation (new refresh_token in response)
    - Handle invalid_grant → mark account invalid
    - After successful refresh, also refresh profile metadata

    Returns True if token is valid (either still fresh or successfully refreshed).
    """
    account = db.get_account(account_id)
    if not account:
        return False

    if not should_refresh(account):
        return True  # Token still valid

    if not account.get("refresh_token"):
        return True  # API key account — no refresh needed, still valid

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": account["refresh_token"],
                    "client_id": CLIENT_ID,
                },
                headers={
                    "Content-Type": "application/json",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )

            if resp.status_code == 200:
                tokens = resp.json()

                # Token rotation: save new refresh_token if provided
                new_refresh = tokens.get("refresh_token", account["refresh_token"])
                new_expires_at = int(time.time()) + tokens.get("expires_in", 28800)

                # CRITICAL: If DB update fails after consuming the refresh token,
                # the old refresh token is gone and the new one wasn't saved.
                try:
                    db.update_account(
                        account_id,
                        access_token=tokens["access_token"],
                        refresh_token=new_refresh,
                        expires_at=new_expires_at,
                        consecutive_failures=0,
                    )
                except Exception as db_err:
                    logger.error(
                        f"Token refresh succeeded but DB update FAILED for "
                        f"account {account_id}. Token may be lost! Error: {db_err}"
                    )
                    return False

                logger.info(f"Token refreshed for account {account_id}")

                # Also refresh profile metadata after successful refresh
                await fetch_profile(account_id, db, access_token=tokens["access_token"])

                return True

            # Error handling per design doc section 4d
            if resp.status_code == 400:
                try:
                    error_data = resp.json()
                    if error_data.get("error") == "invalid_grant":
                        db.update_account(
                            account_id,
                            is_active=False,
                            validation_status="invalid",
                            last_error="Refresh token expired or revoked",
                            last_error_at=datetime.utcnow().isoformat(),
                        )
                        logger.warning(
                            f"Account {account_id}: invalid_grant — marked invalid"
                        )
                        return False
                except Exception:
                    pass

            if resp.status_code in (401, 403):
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_error=f"Token revoked (HTTP {resp.status_code})",
                    last_error_at=datetime.utcnow().isoformat(),
                )
                return False

            if resp.status_code == 429:
                logger.warning(f"Account {account_id}: rate limited during refresh")
                db.record_account_error(account_id, "Rate limited during token refresh")
                return False

            if resp.status_code >= 500:
                logger.warning(
                    f"Account {account_id}: server error {resp.status_code} during refresh"
                )
                db.record_account_error(
                    account_id, f"Server error ({resp.status_code}) during refresh"
                )
                return False

            # Unknown error
            db.record_account_error(
                account_id, f"Unexpected HTTP {resp.status_code} during refresh"
            )
            return False

    except httpx.TimeoutException:
        logger.warning(f"Account {account_id}: timeout during token refresh")
        db.record_account_error(account_id, "Timeout during token refresh", increment_failures=False)
        return False
    except Exception as e:
        logger.error(f"Account {account_id}: refresh error: {e}")
        db.record_account_error(account_id, str(e))
        return False


async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
) -> Optional[dict]:
    """Fetch usage data from the Anthropic Usage API (design doc section 4f).

    Updates the account's cached usage fields in the database.

    Returns the raw usage response dict, or None on failure.
    """
    account = db.get_account(account_id)
    if not account:
        return None

    token = access_token or account["access_token"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )

            if resp.status_code == 200:
                data = resp.json()
                five_hour = data.get("five_hour", {})
                seven_day = data.get("seven_day", {})

                db.update_account_usage_cache(
                    account_id,
                    five_hour=five_hour.get("utilization"),
                    seven_day=seven_day.get("utilization"),
                    five_hour_resets_at=five_hour.get("resets_at"),
                    seven_day_resets_at=seven_day.get("resets_at"),
                    raw=data,
                )

                db.clear_account_errors(account_id)
                logger.info(f"Usage fetched for account {account_id}")
                return data

            logger.warning(
                f"Usage fetch HTTP {resp.status_code} for account {account_id}"
            )
            return None

    except Exception as e:
        logger.warning(f"Usage fetch failed for account {account_id}: {e}")
        return None


async def fetch_profile(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
) -> Optional[dict]:
    """Fetch profile from the Anthropic Profile API (design doc section 4e).

    Updates account metadata: subscription_type, rate_limit_tier,
    has_extra_usage, display_name.

    Returns the raw profile response dict, or None on failure.
    """
    account = db.get_account(account_id)
    if not account:
        return None

    token = access_token or account["access_token"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )

            if resp.status_code == 200:
                data = resp.json()
                org = data.get("organization", {})
                acct_info = data.get("account", {})

                # Map organization_type to subscription_type
                org_type = org.get("organization_type", "")
                subscription_type = ORG_TYPE_MAP.get(org_type)

                updates: dict = {}
                if subscription_type:
                    updates["subscription_type"] = subscription_type
                if org.get("rate_limit_tier"):
                    updates["rate_limit_tier"] = org["rate_limit_tier"]
                if "has_extra_usage_enabled" in org:
                    updates["has_extra_usage"] = org["has_extra_usage_enabled"]
                if acct_info.get("display_name"):
                    updates["display_name"] = acct_info["display_name"]

                if updates:
                    db.update_account(account_id, **updates)

                logger.info(f"Profile fetched for account {account_id}")
                return data

            logger.warning(
                f"Profile fetch HTTP {resp.status_code} for account {account_id}"
            )
            return None

    except Exception as e:
        logger.warning(f"Profile fetch failed for account {account_id}: {e}")
        return None


async def validate_account(account_id: int, db: Database) -> dict:
    """Validate an account by attempting a profile fetch.

    If the profile fetch succeeds, the token is valid.
    If it fails with 401/403, the token is invalid.

    Returns dict with 'valid' (bool) and 'error' (str or None).

    This is simpler than ralphx's approach — we don't try to refresh
    as part of validation. The frontend calls refresh first if needed.
    """
    account = db.get_account(account_id)
    if not account:
        return {"valid": False, "error": "Account not found"}

    # Mark as checking
    db.update_account(account_id, validation_status="checking")

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {account['access_token']}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )

            if resp.status_code == 200:
                db.update_account(
                    account_id,
                    validation_status="valid",
                    last_validated_at=int(time.time()),
                    consecutive_failures=0,
                )

                # Also update profile metadata while we're at it
                data = resp.json()
                org = data.get("organization", {})
                acct_info = data.get("account", {})
                org_type = org.get("organization_type", "")
                subscription_type = ORG_TYPE_MAP.get(org_type)

                updates: dict = {}
                if subscription_type:
                    updates["subscription_type"] = subscription_type
                if org.get("rate_limit_tier"):
                    updates["rate_limit_tier"] = org["rate_limit_tier"]
                if "has_extra_usage_enabled" in org:
                    updates["has_extra_usage"] = org["has_extra_usage_enabled"]
                if acct_info.get("display_name"):
                    updates["display_name"] = acct_info["display_name"]
                if updates:
                    db.update_account(account_id, **updates)

                return {"valid": True, "error": None}

            if resp.status_code in (401, 403):
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_validated_at=int(time.time()),
                    last_error=f"Token invalid (HTTP {resp.status_code})",
                    last_error_at=datetime.utcnow().isoformat(),
                )
                return {
                    "valid": False,
                    "error": f"Token invalid (HTTP {resp.status_code})",
                }

            if resp.status_code == 429:
                # Rate limited — don't mark invalid, just note the error
                db.update_account(
                    account_id,
                    validation_status=account.get("validation_status", "unknown"),
                    last_error="Rate limited during validation",
                    last_error_at=datetime.utcnow().isoformat(),
                )
                return {"valid": False, "error": "Rate limited — try again later"}

            db.update_account(
                account_id,
                validation_status="unknown",
                last_error=f"Validation HTTP {resp.status_code}",
                last_error_at=datetime.utcnow().isoformat(),
            )
            return {"valid": False, "error": f"Unexpected HTTP {resp.status_code}"}

    except httpx.TimeoutException:
        db.update_account(
            account_id,
            validation_status=account.get("validation_status", "unknown"),
        )
        return {"valid": False, "error": "Network timeout during validation"}
    except Exception as e:
        logger.error(f"Validation error for account {account_id}: {e}")
        db.update_account(
            account_id,
            validation_status="unknown",
            last_error=str(e),
            last_error_at=datetime.utcnow().isoformat(),
        )
        return {"valid": False, "error": str(e)}


# Per-account refresh locks to prevent concurrent refresh collisions
# between the background loop and manual API calls.
_refresh_locks: dict[int, asyncio.Lock] = {}


def _get_refresh_lock(account_id: int) -> asyncio.Lock:
    """Get or create a per-account refresh lock.

    >>> lock = _get_refresh_lock(1)
    >>> isinstance(lock, asyncio.Lock)
    True
    >>> _get_refresh_lock(1) is lock
    True
    """
    if account_id not in _refresh_locks:
        _refresh_locks[account_id] = asyncio.Lock()
    return _refresh_locks[account_id]


async def refresh_all_expiring_tokens(buffer_seconds: int = 14400) -> dict:
    """Refresh all account tokens expiring within buffer_seconds.

    Called by background task to proactively keep tokens fresh.
    Skips API key accounts (no refresh_token) and inactive accounts.
    Uses per-account locks to avoid collisions with manual refresh calls.

    Args:
        buffer_seconds: Refresh tokens expiring within this many seconds (default 4 hours)

    Returns:
        dict with counts: {"checked": N, "refreshed": N, "skipped": N, "failed": N}

    >>> import asyncio
    >>> result = asyncio.get_event_loop().run_until_complete(refresh_all_expiring_tokens())
    >>> sorted(result.keys()) == ['checked', 'failed', 'refreshed', 'skipped']
    True
    """
    db = Database()
    now = int(time.time())
    result = {"checked": 0, "refreshed": 0, "skipped": 0, "failed": 0}

    accounts = db.list_accounts(include_inactive=False)
    for account in accounts:
        result["checked"] += 1

        # Skip API key accounts (no refresh_token)
        if not account.get("refresh_token"):
            result["skipped"] += 1
            continue

        # Skip if not expiring within buffer
        if now < account["expires_at"] - buffer_seconds:
            result["skipped"] += 1
            continue

        # Non-blocking lock: skip if another refresh is in progress
        lock = _get_refresh_lock(account["id"])
        if lock.locked():
            result["skipped"] += 1
            continue

        async with lock:
            success = await refresh_account_token(account["id"], db)
            if success:
                result["refreshed"] += 1
            else:
                result["failed"] += 1

    return result
