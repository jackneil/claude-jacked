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
from dataclasses import dataclass
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


def _update_profile_metadata(account_id: int, data: dict, db) -> None:
    """Update account metadata from a profile API response."""
    org = data.get("organization", {})
    org_type = org.get("organization_type", "")
    subscription_type = ORG_TYPE_MAP.get(org_type)

    updates: dict = {}
    if subscription_type:
        updates["subscription_type"] = subscription_type
    if org.get("rate_limit_tier"):
        updates["rate_limit_tier"] = org["rate_limit_tier"]
    if "has_extra_usage_enabled" in org:
        updates["has_extra_usage"] = org["has_extra_usage_enabled"]
    if org.get("name"):
        updates["organization_name"] = org["name"]
    if updates:
        db.update_account(account_id, **updates)


@dataclass
class TokenExchangeResult:
    """Result of exchanging a refresh token for new tokens."""
    success: bool
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    error: str | None = None       # "invalid_grant", "http_429", "network_error", etc.
    status_code: int | None = None


async def _exchange_refresh_token(
    refresh_token: str,
    timeout: float = 15.0,
) -> TokenExchangeResult:
    """Exchange a refresh token for new tokens via Anthropic's OAuth endpoint.

    Single implementation of the token exchange POST. All refresh paths
    (refresh_cc_token, refresh_account_token, _try_refresh_on_429,
    _try_refresh_primary_token) should use this helper.
    """
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                },
                headers={
                    "Content-Type": "application/json",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )

        if resp.status_code == 200:
            data = resp.json()
            return TokenExchangeResult(
                success=True,
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", refresh_token),
                expires_in=data.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS),
            )

        # Parse error type from response
        error = f"http_{resp.status_code}"
        try:
            error_data = resp.json()
            error = error_data.get("error", error)
        except Exception:
            pass

        return TokenExchangeResult(
            success=False,
            error=error,
            status_code=resp.status_code,
        )

    except Exception as exc:
        return TokenExchangeResult(
            success=False,
            error="network_error",
        )


def _get_usage_state(account_id: int) -> dict:
    """Get or create per-account usage coordinator state."""
    if account_id not in _account_usage_state:
        _account_usage_state[account_id] = {
            "last_fetched_at": 0.0,
            "backoff_until": 0.0,
            "tier": "idle",
            "interval": _TIER_INTERVALS["idle"],
            "consecutive_429s": 0,
        }
    state = _account_usage_state[account_id]
    if "consecutive_429s" not in state:
        state["consecutive_429s"] = 0
    return state


_primary_refresh_state: dict[int, dict] = {}


def _get_primary_refresh_state(account_id: int) -> dict:
    if account_id not in _primary_refresh_state:
        _primary_refresh_state[account_id] = {
            "last_failed_at": 0.0,
            "dead": False,
        }
    return _primary_refresh_state[account_id]


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

    result = await _exchange_refresh_token(account["refresh_token"], timeout=30.0)

    if result.success:
        new_expires_at = int(time.time()) + result.expires_in

        # Update DB with new tokens (retry with backoff)
        db_updated = False
        for attempt in range(3):
            try:
                db.update_account(
                    account_id,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
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
        await fetch_profile(account_id, db, access_token=result.access_token)

        return True

    # --- Error handling for failed exchange ---

    if result.error == "invalid_grant":
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

    if result.status_code in (401, 403):
        db.update_account(
            account_id,
            validation_status="invalid",
            last_error=f"Token revoked (HTTP {result.status_code})",
            last_error_at=datetime.now(timezone.utc).isoformat(),
        )
        return False

    if result.status_code == 429:
        logger.warning(f"Account {account_id}: rate limited during refresh")
        db.record_account_error(account_id, "Rate limited during token refresh")
        return False

    if result.status_code and result.status_code >= 500:
        logger.warning(
            f"Account {account_id}: server error {result.status_code} during refresh"
        )
        db.record_account_error(
            account_id, f"Server error ({result.status_code}) during refresh"
        )
        return False

    if result.error == "network_error":
        logger.warning(f"Account {account_id}: network error during token refresh")
        db.record_account_error(
            account_id, "Network error during token refresh", increment_failures=False
        )
        return False

    # Unknown error
    db.record_account_error(
        account_id, f"Unexpected error during refresh: {result.error}"
    )
    return False


async def _try_refresh_on_429(
    account_id: int, db: Database, state: dict,
) -> str | None:
    """Attempt to get a fresh access token to clear a per-token rate limit.

    Acquires the Claude Code cross-process lock, exchanges the refresh
    token for a fresh access token, and saves all tokens to DB + credential
    stores. Returns the fresh access token on success, None on failure.
    """
    from jacked.api.credential_helpers import (
        acquire_claude_lock,
        read_platform_credentials,
        sync_credential_to_all_stores,
    )

    account = db.get_account(account_id)
    if not account:
        return None

    # Prefer CC refresh token, fall back to primary
    refresh_token = account.get("cc_refresh_token") or account.get("refresh_token")
    if not refresh_token:
        logger.debug("Account %d: no refresh token for 429 recovery", account_id)
        return None

    with acquire_claude_lock() as locked:
        if not locked:
            logger.warning("Account %d: could not acquire lock for 429 recovery", account_id)
            return None

        # Re-read from live store — another process may have refreshed
        live = read_platform_credentials()
        if not live:
            cred_path = Path.home() / ".claude" / ".credentials.json"
            if cred_path.exists() and not cred_path.is_symlink():
                try:
                    live = json.loads(cred_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    live = None

        if live and live.get("_jackedAccountId") == account_id:
            live_token = live.get("claudeAiOauth", {}).get("accessToken")
            db_token = account.get("cc_access_token") or account.get("access_token")
            if live_token and live_token != db_token:
                # Another process already refreshed — use their token
                logger.info("Account %d: 429 recovery — using token from live store", account_id)
                db.update_account(account_id, cc_access_token=live_token)
                return live_token

        # Exchange refresh token for fresh access token
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(
                    TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                        "client_id": CLIENT_ID,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "anthropic-beta": OAUTH_BETA_HEADER,
                    },
                )

            if resp.status_code != 200:
                logger.warning(
                    "Account %d: 429 recovery token exchange failed (HTTP %d)",
                    account_id, resp.status_code,
                )
                return None

            tokens = resp.json()
            fresh_access = tokens["access_token"]
            new_refresh = tokens.get("refresh_token", refresh_token)
            new_expires = int(time.time()) + tokens.get("expires_in", DEFAULT_TOKEN_TTL_SECONDS)

            # Determine which token set was used
            used_cc = bool(account.get("cc_refresh_token"))
            if used_cc:
                db.update_account(
                    account_id,
                    cc_access_token=fresh_access,
                    cc_refresh_token=new_refresh,
                    cc_expires_at=new_expires,
                )
            else:
                db.update_account(
                    account_id,
                    access_token=fresh_access,
                    refresh_token=new_refresh,
                    expires_at=new_expires,
                )

            # Write to credential stores ONLY if this account is currently
            # the active one. Writing credentials for a non-active account
            # would overwrite the active account's credentials in
            # .credentials.json, silently switching Claude Code to the wrong
            # account. The DB update above is enough for non-active accounts —
            # the credential file will be written when/if they become active.
            if live and live.get("_jackedAccountId") == account_id:
                updated_account = db.get_account(account_id)
                if updated_account:
                    sync_credential_to_all_stores(
                        account_id, updated_account,
                        email=updated_account.get("email"),
                    )

            logger.info(
                "Account %d: 429 recovery — fresh token obtained%s",
                account_id,
                " (refresh rotated)" if new_refresh != refresh_token else "",
            )
            return fresh_access

        except Exception as exc:
            logger.warning("Account %d: 429 recovery failed: %s", account_id, exc)
            return None


async def _try_refresh_primary_token(
    account_id: int,
    db,
    stale_token: str | None = None,
) -> str | None:
    """Refresh the primary access token on 401. Returns new token or None.

    Uses per-account asyncio lock to prevent concurrent refresh races.
    Checks circuit breaker (10-min cooldown after transient failure,
    permanent skip after invalid_grant). Compares stale_token against
    current DB to detect if another coroutine already refreshed.

    Does NOT write to credential stores — primary tokens are for
    jacked's own API calls, not for Claude Code.
    """
    _COOLDOWN_SECONDS = 600

    state = _get_primary_refresh_state(account_id)

    # Circuit breaker: skip if refresh token is known dead
    if state["dead"]:
        return None

    # Circuit breaker: skip if recently failed (transient cooldown)
    if time.time() - state["last_failed_at"] < _COOLDOWN_SECONDS:
        return None

    lock = _get_refresh_lock(account_id)
    async with lock:
        # Re-read account under lock — another coroutine may have refreshed
        account = db.get_account(account_id)
        if not account:
            return None

        # If the DB token changed since the caller's stale copy,
        # someone else already refreshed — use the new token
        current_token = account.get("access_token")
        if stale_token and current_token and current_token != stale_token:
            logger.info(
                "Account %d: primary token already refreshed by another path",
                account_id,
            )
            return current_token

        refresh_token = account.get("refresh_token")
        if not refresh_token:
            return None

        result = await _exchange_refresh_token(refresh_token)

        if result.success:
            db.update_account(
                account_id,
                access_token=result.access_token,
                refresh_token=result.refresh_token,
                expires_at=int(time.time()) + result.expires_in,
            )
            state["last_failed_at"] = 0.0
            state["dead"] = False
            logger.info(
                "Account %d: primary token refreshed%s",
                account_id,
                " (refresh rotated)" if result.refresh_token != refresh_token else "",
            )
            return result.access_token

        # Handle specific error types
        if result.error == "invalid_grant":
            state["dead"] = True
            logger.warning(
                "Account %d: primary refresh token is dead (invalid_grant)",
                account_id,
            )
        else:
            state["last_failed_at"] = time.time()
            logger.warning(
                "Account %d: primary token refresh failed (%s) — will retry in %ds",
                account_id, result.error, _COOLDOWN_SECONDS,
            )

        return None


async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
    manual: bool = False,
    _retry_depth: int = 0,
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
                state["consecutive_429s"] = 0

                # clear_account_errors marks valid, clears last_error, consecutive_failures
                db.clear_account_errors(account_id)
                logger.info(f"Usage fetched for account {account_id}")
                return data

            if resp.status_code in (401, 403):
                # Try refreshing the primary token before giving up.
                if _retry_depth < 1:
                    fresh = await _try_refresh_primary_token(
                        account_id, db, stale_token=token,
                    )
                    if fresh:
                        state["last_fetched_at"] = 0  # Reset ceiling for retry
                        return await fetch_usage(
                            account_id, db, access_token=fresh,
                            _retry_depth=_retry_depth + 1,
                        )

                # Refresh failed or already retried — mark invalid
                error_msg = f"Usage fetch failed (HTTP {resp.status_code}) — token refresh failed"
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_error=error_msg,
                    last_error_at=datetime.now(timezone.utc).isoformat(),
                )
                logger.warning(
                    "Usage fetch auth failure for account %d: %d (refresh failed)",
                    account_id, resp.status_code,
                )
                return None

            if resp.status_code == 429:
                # Try to clear the per-token rate limit by getting a fresh token.
                # Only attempt on first try (_retry_depth=0) to prevent recursion.
                if _retry_depth == 0:
                    fresh_token = await _try_refresh_on_429(account_id, db, state)
                    if fresh_token:
                        state["consecutive_429s"] = 0
                        state["last_fetched_at"] = 0  # Allow immediate retry
                        logger.info("Account %d: retrying usage fetch with fresh token", account_id)
                        return await fetch_usage(
                            account_id, db, access_token=fresh_token, _retry_depth=1,
                        )

                # No refresh available — escalating backoff
                state["consecutive_429s"] = state.get("consecutive_429s", 0) + 1
                n = state["consecutive_429s"]
                base_backoff = min(_USAGE_RATE_LIMIT_CEILING * (2 ** (n - 1)), 900)
                retry_after = resp.headers.get("retry-after", str(base_backoff))
                try:
                    backoff_seconds = min(max(int(retry_after), base_backoff), 900)
                except (ValueError, TypeError):
                    backoff_seconds = base_backoff
                state["backoff_until"] = time.time() + backoff_seconds
                state["last_fetched_at"] = time.time()
                db.record_account_error(
                    account_id,
                    f"Usage fetch rate limited (429) — backing off {backoff_seconds}s "
                    f"(consecutive: {n})",
                    increment_failures=False,
                )
                logger.warning(
                    f"Usage fetch rate limited for account {account_id}, "
                    f"backing off {backoff_seconds}s (consecutive: {n})"
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
    _refresh_depth: int = 0,
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
                _update_profile_metadata(account_id, data, db)
                logger.info(f"Profile fetched for account {account_id}")
                return data

            if resp.status_code in (401, 403) and _refresh_depth < 1:
                fresh = await _try_refresh_primary_token(
                    account_id, db, stale_token=token,
                )
                if fresh:
                    return await fetch_profile(
                        account_id, db, access_token=fresh,
                        _refresh_depth=_refresh_depth + 1,
                    )

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
                _update_profile_metadata(account_id, resp.json(), db)
                return {"valid": True, "error": None}

            if resp.status_code in (401, 403):
                fresh = await _try_refresh_primary_token(
                    account_id, db, stale_token=account['access_token'],
                )
                if fresh:
                    retry_resp = await client.get(
                        PROFILE_URL,
                        headers={
                            "Authorization": f"Bearer {fresh}",
                            "anthropic-beta": OAUTH_BETA_HEADER,
                        },
                    )
                    if retry_resp.status_code == 200:
                        db.update_account(
                            account_id,
                            validation_status="valid",
                            last_validated_at=int(time.time()),
                            consecutive_failures=0,
                        )
                        _update_profile_metadata(account_id, retry_resp.json(), db)
                        return {"valid": True, "error": None}

                # Refresh failed — truly invalid
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_validated_at=int(time.time()),
                    last_error=f"Token invalid (HTTP {resp.status_code}), refresh failed",
                    last_error_at=datetime.now(timezone.utc).isoformat(),
                )
                return {"valid": False, "error": f"Token invalid (HTTP {resp.status_code})"}

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
