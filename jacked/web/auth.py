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
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import httpx

from jacked.web.database import Database
from jacked.web.oauth import (
    CLIENT_ID,
    DEFAULT_TOKEN_TTL_SECONDS,
    OAUTH_BETA_HEADER,
    ORG_TYPE_MAP,
    PROFILE_URL,
    TOKEN_URL,
    USAGE_URL,
)

logger = logging.getLogger("jacked.auth")


# Shared constant for refresh buffer (used by should_refresh and should_refresh_cc)
REFRESH_BUFFER_SECONDS = 300
USAGE_CACHE_FRESHNESS_SECONDS = 30

# Hard ceiling: never fetch usage more than once per this many seconds per account.
# The Anthropic usage API rate limits at ~1 req/60s/account.
_USAGE_RATE_LIMIT_CEILING = 65

# Adaptive polling intervals by urgency tier
_TIER_INTERVALS = {
    "idle": 300,
    "normal": 150,
    "warning": 90,
    "critical": 65,
}
_TIER_ORDER = ["idle", "normal", "warning", "critical"]

# Per-account usage coordinator state
_account_usage_state: dict[int, dict] = {}


def _get_usage_state(account_id: int) -> dict:
    """Get or create per-account usage coordinator state."""
    if account_id not in _account_usage_state:
        _account_usage_state[account_id] = {
            "last_fetched_at": 0.0,
            "backoff_until": 0.0,
            "tier": "idle",
            "interval": _TIER_INTERVALS["idle"],
        }
    return _account_usage_state[account_id]


def compute_urgency_tier(
    usage_5h: float | None,
    usage_7d: float | None,
    burn_rate_5h: float,
    critical_5h: float = 90.0,
) -> tuple[str, int]:
    """Compute the adaptive polling urgency tier and interval.

    Returns (tier_name, interval_seconds).
    """
    u5 = usage_5h if usage_5h is not None else 0.0
    u7 = usage_7d if usage_7d is not None else 0.0

    if u5 > 85:
        tier = "critical"
    elif u5 > 70:
        tier = "warning"
    elif u5 > 50:
        tier = "normal"
    else:
        tier = "idle"

    if burn_rate_5h > 0.01:
        mins_to_critical = (critical_5h - u5) / burn_rate_5h if u5 < critical_5h else 0
        if mins_to_critical <= 5:
            tier = "critical"
        elif mins_to_critical <= 15 and _TIER_ORDER.index(tier) < _TIER_ORDER.index("warning"):
            tier = "warning"

    if u7 > 80:
        idx = _TIER_ORDER.index(tier)
        if idx < len(_TIER_ORDER) - 1:
            tier = _TIER_ORDER[idx + 1]

    return tier, _TIER_INTERVALS[tier]




def should_refresh(account: dict) -> bool:
    """Check if an account's token needs refreshing.

    Rules:
    - API key accounts (refresh_token is None) cannot be refreshed
    - Refresh when now > expires_at - REFRESH_BUFFER_SECONDS (5-minute buffer)

    >>> should_refresh({"refresh_token": None, "expires_at": 9999999999})
    False
    >>> should_refresh({"refresh_token": "rt-test", "expires_at": 0})
    True
    >>> should_refresh({"refresh_token": "rt-test", "expires_at": 9999999999})
    False
    """
    if not account.get("refresh_token"):
        return False
    return account["expires_at"] < time.time() + REFRESH_BUFFER_SECONDS


def should_refresh_cc(account: dict) -> bool:
    """Check if CC (Claude Code) tokens need refresh.

    Returns False if cc_refresh_token is NULL — can't refresh without one.

    >>> should_refresh_cc({"cc_refresh_token": None, "cc_expires_at": 9999999999})
    False
    >>> should_refresh_cc({"cc_refresh_token": "rt", "cc_expires_at": 0})
    True
    >>> should_refresh_cc({"cc_refresh_token": "rt", "cc_expires_at": 9999999999})
    False
    >>> should_refresh_cc({"cc_refresh_token": "rt", "cc_expires_at": None})
    True
    """
    if not account.get("cc_refresh_token"):
        return False
    cc_expires_at = account.get("cc_expires_at")
    if not cc_expires_at:
        return True
    return cc_expires_at < time.time() + REFRESH_BUFFER_SECONDS


async def refresh_cc_token(account_id: int, db: Database) -> bool:
    """Refresh CC token pair independently. Updates cc_* columns only.

    On invalid_grant: clear only cc_refresh_token (consumed by Claude Code).
    Keep cc_access_token — it may still be valid until cc_expires_at.
    Does NOT mark account as invalid — primary token health determines
    account validity.

    Uses a per-account lock to prevent concurrent CC refresh collisions
    between the background refresh loop and pre-launch refresh.
    """
    account = db.get_account(account_id)
    if not account:
        return False

    if not should_refresh_cc(account):
        return True

    if not account.get("cc_refresh_token"):
        return True

    lock = _get_cc_refresh_lock(account_id)
    if lock.locked():
        return True  # Another refresh in progress — skip

    async with lock:
        # Re-read account under lock — another refresh may have completed
        account = db.get_account(account_id)
        if not account or not should_refresh_cc(account):
            return True

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": account["cc_refresh_token"],
                        "client_id": CLIENT_ID,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "anthropic-beta": OAUTH_BETA_HEADER,
                    },
                )

                if resp.status_code == 200:
                    tokens = resp.json()
                    new_refresh = tokens.get(
                        "refresh_token", account["cc_refresh_token"]
                    )
                    new_expires_at = int(time.time()) + tokens.get(
                        "expires_in", DEFAULT_TOKEN_TTL_SECONDS
                    )

                    db.update_account(
                        account_id,
                        cc_access_token=tokens["access_token"],
                        cc_refresh_token=new_refresh,
                        cc_expires_at=new_expires_at,
                    )
                    logger.info(f"CC token refreshed for account {account_id}")
                    return True

                if resp.status_code == 400:
                    try:
                        error_data = resp.json()
                        if error_data.get("error") == "invalid_grant":
                            # Attempt recovery from live credentials before clearing
                            from jacked.api.credential_helpers import read_platform_credentials
                            live = read_platform_credentials()
                            if not live:
                                cred_path = Path.home() / ".claude" / ".credentials.json"
                                if cred_path.exists() and not cred_path.is_symlink():
                                    try:
                                        live = json.loads(cred_path.read_text(encoding="utf-8"))
                                    except (json.JSONDecodeError, OSError):
                                        live = None

                            live_refresh = None
                            live_access = None
                            live_expires = None
                            if live and live.get("_jackedAccountId") == account_id:
                                oauth = live.get("claudeAiOauth", {})
                                live_refresh = oauth.get("refreshToken")
                                live_access = oauth.get("accessToken")
                                live_expires = oauth.get("expiresAt")

                            if live_refresh and live_refresh != account.get("cc_refresh_token"):
                                live_exp_s = (
                                    int(live_expires / 1000) if live_expires and live_expires > 1e12
                                    else int(live_expires) if live_expires else None
                                )
                                updates = {"cc_refresh_token": live_refresh}
                                if live_access:
                                    updates["cc_access_token"] = live_access
                                if live_exp_s:
                                    updates["cc_expires_at"] = live_exp_s
                                db.update_account(account_id, **updates)
                                logger.info(
                                    "Account %d: CC invalid_grant recovered — "
                                    "imported fresh token from live credentials",
                                    account_id,
                                )
                                return True
                            else:
                                logger.warning(
                                    "Account %d: CC invalid_grant — clearing refresh token "
                                    "(no live recovery available)",
                                    account_id,
                                )
                                db.update_account(account_id, cc_refresh_token=None)
                                return False
                    except (ValueError, AttributeError) as exc:
                        logger.warning(
                            "Account %d: CC refresh error parsing response: %s",
                            account_id, exc,
                        )

                logger.warning(
                    "Account %d: CC refresh HTTP %d", account_id, resp.status_code
                )
                return False

        except Exception as e:
            logger.warning(f"Account {account_id}: CC refresh error: {e}")
            return False


async def refresh_account_token(
    account_id: int, db: Database
) -> bool:
    """Refresh an account's token if needed.

    - Skip if no refresh_token (API key account)
    - Skip if token not near expiry
    - POST to TOKEN_URL with JSON body
    - Handle token rotation (new refresh_token in response)
    - Handle invalid_grant → record error, retry next cycle
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
                new_expires_at = int(time.time()) + tokens.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)

                # Update DB with new tokens (retry with backoff)
                db_updated = False
                for attempt in range(3):
                    try:
                        db.update_account(
                            account_id,
                            access_token=tokens["access_token"],
                            refresh_token=new_refresh,
                            expires_at=new_expires_at,
                            consecutive_failures=0,
                        )
                        db_updated = True
                        break
                    except Exception as db_err:
                        logger.warning(
                            "DB update attempt %d/3 failed for account %d: %s",
                            attempt + 1, account_id, db_err,
                        )
                        if attempt < 2:
                            await asyncio.sleep(0.1 * (2 ** attempt))

                if not db_updated:
                    logger.error(
                        "Token refresh succeeded but DB update FAILED for "
                        "account %d after 3 attempts.",
                        account_id,
                    )
                    return False

                logger.info(f"Token refreshed for account {account_id}")

                # NOTE: Refreshed tokens are stored in the DB only.
                # We do NOT write to Claude Code's credential files
                # (~/.claude/.credentials.json, Keychain) here — doing so
                # causes race conditions that log out active sessions.
                # Account switching is handled by `jacked claude <id>`.

                # Also refresh profile metadata after successful refresh
                await fetch_profile(account_id, db, access_token=tokens["access_token"])

                return True

            if resp.status_code == 400:
                try:
                    error_data = resp.json()
                    if error_data.get("error") == "invalid_grant":
                        # Refresh token was consumed (likely by Claude Code).
                        # Don't mark invalid — will retry next cycle.
                        logger.warning(
                            "Account %d: invalid_grant — will retry next cycle",
                            account_id,
                        )
                        db.record_account_error(
                            account_id, "Refresh token consumed (will retry)"
                        )
                        return False
                except Exception:
                    pass

            if resp.status_code in (401, 403):
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_error=f"Token revoked (HTTP {resp.status_code})",
                    last_error_at=datetime.now(timezone.utc).isoformat(),
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
        db.record_account_error(
            account_id, "Timeout during token refresh", increment_failures=False
        )
        return False
    except Exception as e:
        logger.error(f"Account {account_id}: refresh error: {e}")
        db.record_account_error(account_id, str(e))
        return False


async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
    manual: bool = False,
) -> Optional[dict]:
    """Fetch usage data from the Anthropic Usage API (design doc section 4f).

    Uses per-account coordinator state for rate limiting.  The hard ceiling
    (_USAGE_RATE_LIMIT_CEILING) is always enforced to prevent 429s from the
    upstream API.

    Updates the account's cached usage fields in the database.

    Returns the raw usage response dict, or None on failure.
    """
    account = db.get_account(account_id)
    if not account:
        return None

    now = time.time()
    state = _get_usage_state(account_id)

    # 429 backoff check
    if now < state["backoff_until"]:
        logger.debug(
            f"Usage fetch backed off for account {account_id} "
            f"({int(state['backoff_until'] - now)}s remaining)"
        )
        return {"_backed_off": True}

    # Hard ceiling: never exceed 1 req per _USAGE_RATE_LIMIT_CEILING seconds.
    # manual=True bypasses this (user explicitly asked for fresh data).
    if not manual:
        elapsed = now - state["last_fetched_at"]
        if elapsed < _USAGE_RATE_LIMIT_CEILING:
            logger.debug(
                f"Usage ceiling for account {account_id}: {int(elapsed)}s < "
                f"{_USAGE_RATE_LIMIT_CEILING}s, returning cached"
            )
            return {"_cached": True}

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

                state["last_fetched_at"] = time.time()

                # clear_account_errors marks valid, clears last_error, consecutive_failures
                db.clear_account_errors(account_id)
                logger.info(f"Usage fetched for account {account_id}")
                return data

            if resp.status_code in (401, 403):
                error_msg = f"Usage fetch failed (HTTP {resp.status_code}) — token may be revoked"
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_error=error_msg,
                    last_error_at=datetime.now(timezone.utc).isoformat(),
                )
                logger.warning(
                    f"Usage fetch auth failure for account {account_id}: {resp.status_code}"
                )
                return None

            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after", str(_USAGE_RATE_LIMIT_CEILING))
                try:
                    backoff_seconds = min(max(int(retry_after), _USAGE_RATE_LIMIT_CEILING), 900)
                except (ValueError, TypeError):
                    backoff_seconds = _USAGE_RATE_LIMIT_CEILING
                state["backoff_until"] = time.time() + backoff_seconds
                state["last_fetched_at"] = time.time()
                db.record_account_error(
                    account_id,
                    f"Usage fetch rate limited (429) — backing off {backoff_seconds}s",
                    increment_failures=False,
                )
                logger.warning(
                    f"Usage fetch rate limited for account {account_id}, "
                    f"backing off {backoff_seconds}s"
                )
                return None

            db.record_account_error(
                account_id, f"Usage fetch failed (HTTP {resp.status_code})"
            )
            logger.warning(
                f"Usage fetch HTTP {resp.status_code} for account {account_id}"
            )
            return None

    except Exception as e:
        db.record_account_error(account_id, f"Usage fetch error: {e}")
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
                # NOTE: display_name intentionally NOT updated here — user sets
                # custom labels via PATCH; profile refresh must not overwrite them.
                # Keep org name fresh (names can change), but NEVER change
                # organization_uuid via profile refresh — that's set at auth time
                if org.get("name"):
                    updates["organization_name"] = org["name"]

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
                # NOTE: display_name intentionally NOT updated — user sets
                # custom labels via PATCH; validation must not overwrite them.
                if org.get("name"):
                    updates["organization_name"] = org["name"]
                if updates:
                    db.update_account(account_id, **updates)

                return {"valid": True, "error": None}

            if resp.status_code in (401, 403):
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_validated_at=int(time.time()),
                    last_error=f"Token invalid (HTTP {resp.status_code})",
                    last_error_at=datetime.now(timezone.utc).isoformat(),
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
                    last_error_at=datetime.now(timezone.utc).isoformat(),
                )
                return {"valid": False, "error": "Rate limited — try again later"}

            db.update_account(
                account_id,
                validation_status="unknown",
                last_error=f"Validation HTTP {resp.status_code}",
                last_error_at=datetime.now(timezone.utc).isoformat(),
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
            last_error_at=datetime.now(timezone.utc).isoformat(),
        )
        return {"valid": False, "error": str(e)}


# Per-account refresh locks to prevent concurrent refresh collisions
# between the background loop and manual API calls.
_refresh_locks: dict[int, asyncio.Lock] = {}
_cc_refresh_locks: dict[int, asyncio.Lock] = {}


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


def _get_cc_refresh_lock(account_id: int) -> asyncio.Lock:
    """Get or create a per-account CC refresh lock."""
    if account_id not in _cc_refresh_locks:
        _cc_refresh_locks[account_id] = asyncio.Lock()
    return _cc_refresh_locks[account_id]


async def refresh_all_expiring_tokens(buffer_seconds: int = 14400) -> dict:
    """Refresh all account tokens expiring within buffer_seconds.

    Called by background task to proactively keep tokens fresh.
    Skips API key accounts (no refresh_token) and inactive accounts.
    Uses per-account locks to avoid collisions with manual refresh calls.

    Args:
        buffer_seconds: Refresh tokens expiring within this many seconds (default 4 hours)

    Returns:
        dict with counts: {"checked": N, "refreshed": N, "skipped": N, "failed": N,
                           "cc_refreshed": N, "cc_failed": N}

    >>> import asyncio
    >>> result = asyncio.get_event_loop().run_until_complete(refresh_all_expiring_tokens())
    >>> all(k in result for k in ['checked', 'failed', 'refreshed', 'skipped', 'cc_refreshed', 'cc_failed'])
    True
    """
    db = Database()
    now = int(time.time())
    result = {
        "checked": 0, "refreshed": 0, "skipped": 0, "failed": 0,
        "cc_refreshed": 0, "cc_failed": 0,
    }

    accounts = db.list_accounts(include_inactive=False)
    for account in accounts:
        result["checked"] += 1
        account_id = account["id"]

        # --- Primary token refresh ---
        primary_needs_refresh = (
            account.get("refresh_token")
            and now >= (account.get("expires_at") or 0) - buffer_seconds
        )

        if primary_needs_refresh:
            lock = _get_refresh_lock(account_id)
            if lock.locked():
                result["skipped"] += 1
            else:
                async with lock:
                    success = await refresh_account_token(account_id, db)
                    if success:
                        result["refreshed"] += 1
                    else:
                        result["failed"] += 1
        else:
            result["skipped"] += 1

        # --- CC token refresh (independent from primary) ---
        if should_refresh_cc(account):
            cc_success = await refresh_cc_token(account_id, db)
            if cc_success:
                result["cc_refreshed"] += 1
            else:
                result["cc_failed"] += 1

    return result


async def heal_invalid_accounts() -> dict:
    """Attempt recovery for accounts with validation_status 'invalid' or 'unknown'.

    Called every 5 minutes by the background heal loop.
    For each stuck account:
    - If has refresh_token and token near/past expiry → attempt refresh
    - Otherwise → validate via profile fetch (works for API key accounts too)
    - Mark healed (valid) or confirmed-invalid

    Returns dict with counts: {"checked": N, "healed": N, "confirmed_invalid": N}
    """
    db = Database()
    result = {"checked": 0, "healed": 0, "confirmed_invalid": 0}

    accounts = db.list_accounts(include_inactive=True)
    for account in accounts:
        if account.get("is_deleted"):
            continue
        status = account.get("validation_status", "valid")
        if status not in ("invalid", "unknown"):
            continue

        result["checked"] += 1
        account_id = account["id"]

        # Try refresh first if token is near/past expiry and has RT
        healed = False
        if account.get("refresh_token") and should_refresh(account):
            lock = _get_refresh_lock(account_id)
            if not lock.locked():
                async with lock:
                    success = await refresh_account_token(account_id, db)
                    if success:
                        healed = True

        if not healed:
            # Validate via profile fetch
            validation = await validate_account(account_id, db)
            healed = validation.get("valid", False)

        if healed:
            result["healed"] += 1
            # Check if CC tokens need re-auth after primary heals
            if not account.get("cc_refresh_token") and account.get("cc_access_token"):
                logger.info(
                    "Account %d healed but CC token needs re-authorization",
                    account_id,
                )
        else:
            result["confirmed_invalid"] += 1

    if result["checked"] > 0:
        logger.info(
            "Heal sweep: checked=%d, healed=%d, confirmed_invalid=%d",
            result["checked"],
            result["healed"],
            result["confirmed_invalid"],
        )
    return result
