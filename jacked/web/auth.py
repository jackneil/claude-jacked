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
from enum import Enum
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

# Bound on acquiring a per-account refresh lock in _refresh_token_flow.
# An unbounded wait behind a wedged holder would suspend every later
# refresh/401-recovery path for that account until server restart.
_REFRESH_LOCK_ACQUIRE_TIMEOUT = 60.0

# Hard ceiling: never fetch usage more than once per this many seconds per account.
# The Anthropic usage API rate limits at ~1 req/60s/account, but jacked runs on
# SEVERAL machines against the same accounts and each process paces independently
# (state below is in-memory). Per-machine automatic checks are therefore budgeted
# at once per 3 minutes so the aggregate across machines stays near the limit.
_USAGE_RATE_LIMIT_CEILING = 180

# Floor for manual=True fetches. Manual used to bypass the ceiling entirely,
# which let a runaway dashboard bulk-refresh loop drive ~25 fetches/min
# (2026-06 incident). No human workflow needs fresher-than-20s usage, and
# this caps ANY client loop at 3 fetches/min/account worst case.
_USAGE_MANUAL_FLOOR_SECONDS = 20

# Minimum seconds between 429-triggered token rotations per account.
# Rotating refresh tokens to dodge per-token rate limits at high frequency
# churns the shared CC token store and masks real rate-limiting — the same
# incident rotated CC tokens 243 times in 4.5h. Outside this window a 429
# goes straight to the escalating-backoff branch.
_USAGE_429_ROTATION_INTERVAL = 600

# Adaptive polling intervals by urgency tier. The floor is the 180s rate-limit
# ceiling above — polling faster than the ceiling just returns cached data while
# churning logs, so no tier goes below it.
_TIER_INTERVALS = {
    # The ACTIVE account is polled this often. Floor is 5 min even when idle so a
    # glanceable pill never drifts more than ~5 min behind on the account you're
    # using; tightens to 4 min (warning) / 3 min (critical) as it nears a cap.
    # (Non-active accounts are covered separately by all_accounts_refresh_loop.)
    "idle": 300,
    "normal": 300,
    "warning": 240,
    "critical": 180,
}
_TIER_ORDER = ["idle", "normal", "warning", "critical"]

# Per-account usage coordinator state
_account_usage_state: dict[int, dict] = {}

# Per-account throttle for piggybacked profile refreshes (see fetch_usage).
# Profile metadata (subscription_type, rate_limit_tier) otherwise only updates
# at account-add and after a token refresh — a parked account whose token never
# rotates would show a stale plan forever after an upgrade/downgrade.
_PROFILE_REFRESH_INTERVAL = 6 * 3600  # seconds
_profile_refreshed_at: dict[int, float] = {}


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


class RefreshMode(str, Enum):
    """Token refresh modes — one per caller, behavior hardcoded per mode."""
    PRIMARY = "primary"
    CC = "cc"
    CC_OR_PRIMARY_429 = "cc_429"
    PRIMARY_CIRCUIT_BREAKER = "primary_cb"


# Circuit breaker cooldown by error type (seconds)
CIRCUIT_BREAKER_COOLDOWNS: dict[str, int] = {
    "invalid_grant": 600,
    "network_error": 60,
    "http_429": 120,
    "http_5xx": 120,
}
_DEFAULT_CB_COOLDOWN = 300


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

    except Exception:
        return TokenExchangeResult(
            success=False,
            error="network_error",
        )


async def _refresh_token_flow(
    account_id: int,
    db: Database,
    mode: RefreshMode,
) -> TokenExchangeResult:
    """Unified token refresh orchestrator — replaces per-caller refresh logic.

    Handles token resolution, locking, circuit breaker, exchange, DB write
    with retry, credential store sync, and error recovery. Each RefreshMode
    encodes caller-specific behavior (which token, which lock, timeouts,
    error handling).
    """
    # ------------------------------------------------------------------
    # 1. Read account from DB
    # ------------------------------------------------------------------
    account = db.get_account(account_id)
    if not account:
        return TokenExchangeResult(success=False, error="account_not_found")

    # ------------------------------------------------------------------
    # 2. Resolve which refresh token to use
    # ------------------------------------------------------------------
    if mode in (RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER):
        refresh_token = account.get("refresh_token")
    elif mode == RefreshMode.CC:
        refresh_token = account.get("cc_refresh_token")
    elif mode == RefreshMode.CC_OR_PRIMARY_429:
        refresh_token = account.get("cc_refresh_token") or account.get("refresh_token")
    else:
        refresh_token = None

    if not refresh_token:
        return TokenExchangeResult(success=False, error="no_refresh_token")

    # Track whether CC_OR_PRIMARY_429 is using CC or primary token
    # (determines which DB columns to update on success)
    used_cc = mode == RefreshMode.CC_OR_PRIMARY_429 and bool(account.get("cc_refresh_token"))

    # ------------------------------------------------------------------
    # 3. Determine which lock to acquire
    # Lock rule: all callers sharing a token set share the same lock.
    # CC_OR_PRIMARY_429 falls back to primary when cc_refresh_token is
    # absent — in that case, use the primary lock to prevent races with
    # PRIMARY/PRIMARY_CIRCUIT_BREAKER callers.
    # ------------------------------------------------------------------
    if mode in (RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER):
        lock = _get_refresh_lock(account_id)
    elif mode == RefreshMode.CC_OR_PRIMARY_429 and not used_cc:
        # Falling back to primary token — use primary lock
        lock = _get_refresh_lock(account_id)
    else:
        # CC and CC_OR_PRIMARY_429 (with CC token) use the CC lock
        lock = _get_cc_refresh_lock(account_id)

    # ------------------------------------------------------------------
    # 4. Acquire lock (bounded) and execute under it
    # The lock is non-reentrant: an unbounded acquire behind a wedged
    # holder would suspend this caller forever, so fail fast instead.
    # ------------------------------------------------------------------
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_REFRESH_LOCK_ACQUIRE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(
            "Account %d: could not acquire %s refresh lock within %ds — "
            "possible wedged holder; aborting this refresh attempt",
            account_id, mode.value, int(_REFRESH_LOCK_ACQUIRE_TIMEOUT),
        )
        return TokenExchangeResult(success=False, error="lock_timeout")

    exchanged = False

    async def _locked_section() -> TokenExchangeResult:
        # Critical section under the per-account lock — runs as a SHIELDED
        # task (created in step 5 below) and therefore owns the lock
        # release: the awaiting caller may be cancelled out from under it
        # by any of the asyncio.wait_for wrappers on the refresh paths.
        nonlocal exchanged, used_cc
        try:
            # 4a. Re-read account under lock
            account = db.get_account(account_id)
            if not account:
                return TokenExchangeResult(success=False, error="account_not_found")

            # Recompute used_cc under lock — another coroutine may have cleared
            # cc_refresh_token between our pre-lock read and lock acquisition.
            if mode == RefreshMode.CC_OR_PRIMARY_429:
                used_cc = bool(account.get("cc_refresh_token"))

            # 4c. Check circuit breaker (PRIMARY_CIRCUIT_BREAKER only)
            if mode == RefreshMode.PRIMARY_CIRCUIT_BREAKER:
                last_failed = account.get("refresh_last_failed_at")
                failure_type = account.get("refresh_failure_type")
                if last_failed is not None:
                    cooldown = CIRCUIT_BREAKER_COOLDOWNS.get(failure_type, _DEFAULT_CB_COOLDOWN)
                    remaining = cooldown - (time.time() - last_failed)
                    if remaining > 0:
                        logger.info(
                            "Account %d: circuit breaker active (%s, %ds remaining)",
                            account_id, failure_type, int(remaining),
                        )
                        return TokenExchangeResult(success=False, error="circuit_breaker")
                    else:
                        logger.info(
                            "Account %d: circuit breaker cooldown expired, re-attempting refresh",
                            account_id,
                        )

            # 4d. Re-read refresh token under lock — detect if another coroutine refreshed
            if mode in (RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER):
                current_refresh = account.get("refresh_token")
            elif mode == RefreshMode.CC:
                current_refresh = account.get("cc_refresh_token")
            else:
                current_refresh = account.get("cc_refresh_token") or account.get("refresh_token")

            if current_refresh and current_refresh != refresh_token:
                logger.info("Account %d: token already refreshed by another path", account_id)
                # Return the new access token
                if mode in (RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER):
                    return TokenExchangeResult(
                        success=True, access_token=account.get("access_token"),
                    )
                else:
                    return TokenExchangeResult(
                        success=True, access_token=account.get("cc_access_token"),
                    )

            # 4e. Determine timeout
            if mode in (RefreshMode.PRIMARY, RefreshMode.CC):
                timeout = 30.0
            else:
                timeout = 15.0

            # 4f. Exchange refresh token
            result = await _exchange_refresh_token(refresh_token, timeout=timeout)

            # ------------------------------------------------------------------
            # 4g. On success — single atomic DB write
            # ------------------------------------------------------------------
            if result.success:
                new_expires_at = int(time.time()) + result.expires_in

                if mode in (RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER):
                    updates = {
                        "access_token": result.access_token,
                        "refresh_token": result.refresh_token,
                        "expires_at": new_expires_at,
                        "refresh_last_failed_at": None,
                        "refresh_failure_type": None,
                    }
                elif mode == RefreshMode.CC:
                    updates = {
                        "cc_access_token": result.access_token,
                        "cc_refresh_token": result.refresh_token,
                        "cc_expires_at": new_expires_at,
                        "refresh_last_failed_at": None,
                        "refresh_failure_type": None,
                    }
                elif mode == RefreshMode.CC_OR_PRIMARY_429:
                    if used_cc:
                        updates = {
                            "cc_access_token": result.access_token,
                            "cc_refresh_token": result.refresh_token,
                            "cc_expires_at": new_expires_at,
                            "refresh_last_failed_at": None,
                            "refresh_failure_type": None,
                        }
                    else:
                        updates = {
                            "access_token": result.access_token,
                            "refresh_token": result.refresh_token,
                            "expires_at": new_expires_at,
                            "refresh_last_failed_at": None,
                            "refresh_failure_type": None,
                        }

                # DB retry 3x with exponential backoff
                db_updated = False
                for attempt in range(3):
                    try:
                        db.update_account(account_id, **updates)
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
                        "Token refresh succeeded but DB update FAILED for account %d after 3 attempts.",
                        account_id,
                    )
                    return TokenExchangeResult(success=False, error="db_write_failed")

                logger.info("Account %d: %s token refreshed", account_id, mode.value)

                # 4h. Post-DB-write actions (still under lock)
                if mode == RefreshMode.CC_OR_PRIMARY_429:
                    try:
                        from jacked.api.credential_helpers import (
                            acquire_claude_lock,
                            read_platform_credentials,
                            sync_credential_to_all_stores,
                        )
                        live = read_platform_credentials()
                        if live and live.get("_jackedAccountId") == account_id:
                            with acquire_claude_lock() as locked:
                                if locked:
                                    updated_account = db.get_account(account_id)
                                    if updated_account:
                                        sync_credential_to_all_stores(
                                            account_id, updated_account,
                                            email=updated_account.get("email"),
                                        )
                    except Exception as cred_err:
                        logger.warning(
                            "Account %d: credential store sync failed (non-fatal): %s",
                            account_id, cred_err,
                        )

                # 4i. Success — returning releases the lock via the finally;
                # the PRIMARY post-refresh profile fetch happens in step 6,
                # in the caller, after the shielded task completes.
                exchanged = True
                return TokenExchangeResult(
                    success=True,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    expires_in=result.expires_in,
                )

            # ------------------------------------------------------------------
            # 4j. On invalid_grant
            # ------------------------------------------------------------------
            elif result.error == "invalid_grant":
                if mode in (RefreshMode.CC, RefreshMode.CC_OR_PRIMARY_429):
                    # Attempt live credential recovery (access token ONLY, NOT refresh token)
                    try:
                        from jacked.api.credential_helpers import read_platform_credentials
                        live = read_platform_credentials()
                        if not live:
                            cred_path = Path.home() / ".claude" / ".credentials.json"
                            if cred_path.exists() and not cred_path.is_symlink():
                                try:
                                    live = json.loads(cred_path.read_text(encoding="utf-8"))
                                except (json.JSONDecodeError, OSError):
                                    live = None

                        if live and live.get("_jackedAccountId") == account_id:
                            oauth = live.get("claudeAiOauth", {})
                            live_access = oauth.get("accessToken")
                            live_expires = oauth.get("expiresAt")
                            if live_access:
                                live_exp_s = (
                                    int(live_expires / 1000) if live_expires and live_expires > 1e12
                                    else int(live_expires) if live_expires else None
                                )
                                recovery_updates: dict = {"cc_access_token": live_access}
                                if live_exp_s:
                                    recovery_updates["cc_expires_at"] = live_exp_s
                                # SAFETY: Never import cc_refresh_token during invalid_grant
                                recovery_updates["refresh_last_failed_at"] = int(time.time())
                                recovery_updates["refresh_failure_type"] = "invalid_grant"
                                db.update_account(account_id, **recovery_updates)
                                logger.info(
                                    "Account %d: invalid_grant — recovered cc_access_token from live credentials",
                                    account_id,
                                )
                                return TokenExchangeResult(success=True, access_token=live_access)
                    except Exception:
                        pass  # Fall through to no-recovery path

                    # No recovery — clear cc_refresh_token + set circuit breaker
                    db.update_account(
                        account_id,
                        cc_refresh_token=None,
                        refresh_last_failed_at=int(time.time()),
                        refresh_failure_type="invalid_grant",
                    )
                    logger.warning(
                        "Account %d: invalid_grant — clearing cc_refresh_token (no recovery)",
                        account_id,
                    )

                elif mode == RefreshMode.PRIMARY_CIRCUIT_BREAKER:
                    db.update_account(
                        account_id,
                        refresh_last_failed_at=int(time.time()),
                        refresh_failure_type="invalid_grant",
                    )
                    logger.warning(
                        "Account %d: primary refresh token is dead (invalid_grant) — cooldown %ds",
                        account_id, CIRCUIT_BREAKER_COOLDOWNS["invalid_grant"],
                    )

                elif mode == RefreshMode.PRIMARY:
                    db.update_account(
                        account_id,
                        refresh_last_failed_at=int(time.time()),
                        refresh_failure_type="invalid_grant",
                    )
                    db.record_account_error(account_id, "Refresh token consumed (will retry)")

                return TokenExchangeResult(success=False, error="invalid_grant")

            # ------------------------------------------------------------------
            # 4k. On other errors — set circuit breaker cooldown
            # ------------------------------------------------------------------
            else:
                cooldown = CIRCUIT_BREAKER_COOLDOWNS.get(result.error, _DEFAULT_CB_COOLDOWN)
                db.update_account(
                    account_id,
                    refresh_last_failed_at=int(time.time()),
                    refresh_failure_type=result.error,
                )
                logger.warning(
                    "Account %d: refresh failed (%s) — cooldown %ds",
                    account_id, result.error, cooldown,
                )
                return result
        finally:
            lock.release()

    # ------------------------------------------------------------------
    # 5. Run the locked section as a SHIELDED task. Anthropic ROTATES
    # refresh tokens: a cancellation (route/sweep/heal/poll-loop
    # asyncio.wait_for timeouts) landing after the exchange POST has been
    # consumed upstream but before the rotated pair is persisted would
    # orphan the account behind a permanent invalid_grant. shield() lets
    # the task finish the exchange, DB write, and lock release in the
    # background while the caller observes its timeout.
    # ------------------------------------------------------------------
    flow_task = asyncio.create_task(_locked_section())
    # Strong reference: the event loop holds only weak refs to tasks, and
    # a cancelled caller drops its own reference at the shield.
    _inflight_refresh_tasks.add(flow_task)
    flow_task.add_done_callback(_retire_refresh_task)
    try:
        flow_result = await asyncio.shield(flow_task)
    except asyncio.CancelledError:
        if not flow_task.done():
            logger.warning(
                "Account %d: %s refresh caller cancelled mid-flow — exchange "
                "continues in background to persist any rotated token",
                account_id, mode.value,
            )
        raise

    # ------------------------------------------------------------------
    # 6. Post-refresh profile fetch — deliberately OUTSIDE the lock.
    # fetch_profile's 401 path re-enters _refresh_token_flow on the same
    # primary lock; running it while holding the lock self-deadlocks.
    # Only a real successful exchange gets here (the 4d already-refreshed
    # fast path and every failure path leave `exchanged` False).
    # ------------------------------------------------------------------
    if mode == RefreshMode.PRIMARY and exchanged:
        try:
            await fetch_profile(account_id, db, access_token=flow_result.access_token)
        except Exception as prof_err:
            logger.warning(
                "Account %d: post-refresh profile fetch failed (non-fatal): %s",
                account_id, prof_err,
            )
    return flow_result



def _get_usage_state(account_id: int) -> dict:
    """Get or create per-account usage coordinator state."""
    if account_id not in _account_usage_state:
        _account_usage_state[account_id] = {
            "last_fetched_at": 0.0,
            "backoff_until": 0.0,
            "tier": "idle",
            "interval": _TIER_INTERVALS["idle"],
            "consecutive_429s": 0,
            "last_429_rotation_at": 0.0,
        }
    state = _account_usage_state[account_id]
    if "consecutive_429s" not in state:
        state["consecutive_429s"] = 0
    if "last_429_rotation_at" not in state:
        state["last_429_rotation_at"] = 0.0
    return state



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
    >>> should_refresh({"provider": "codex", "refresh_token": "x", "expires_at": 0})
    False
    """
    # Codex tokens are never Anthropic-refreshed (Codex manages its own). This is
    # already implied by refresh_token being NULL, but gate explicitly so a future
    # change can't accidentally drive a Codex row into an Anthropic token exchange.
    if (account.get("provider") or "claude") == "codex":
        return False
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
    >>> should_refresh_cc({"provider": "codex", "cc_refresh_token": "rt", "cc_expires_at": 0})
    False
    """
    if (account.get("provider") or "claude") == "codex":
        return False  # Codex has no Claude Code tokens to refresh
    if not account.get("cc_refresh_token"):
        return False
    cc_expires_at = account.get("cc_expires_at")
    if not cc_expires_at:
        return True
    return cc_expires_at < time.time() + REFRESH_BUFFER_SECONDS


async def refresh_cc_token(account_id: int, db: Database) -> bool:
    """Refresh CC token pair independently. Updates cc_* columns only."""
    account = db.get_account(account_id)
    if not account:
        return False
    if not should_refresh_cc(account):
        return True
    if not account.get("cc_refresh_token"):
        return True

    lock = _get_cc_refresh_lock(account_id)
    if lock.locked():
        return True  # Another refresh in progress

    result = await _refresh_token_flow(account_id, db, RefreshMode.CC)
    return result.success


async def refresh_account_token(account_id: int, db: Database) -> bool:
    """Refresh an account's primary token if needed."""
    account = db.get_account(account_id)
    if not account:
        return False
    if not should_refresh(account):
        return True
    if not account.get("refresh_token"):
        return True

    result = await _refresh_token_flow(account_id, db, RefreshMode.PRIMARY)

    if result.success:
        return True

    # Caller-specific error policy: 401/403 marks invalid only after 2 consecutive failures
    if result.status_code in (401, 403):
        prev_failure = account.get("refresh_failure_type")
        if prev_failure in ("http_401", "http_403"):
            # Second consecutive auth failure — mark invalid
            db.update_account(
                account_id,
                validation_status="invalid",
                last_error=f"Token revoked (HTTP {result.status_code})",
                last_error_at=datetime.now(timezone.utc).isoformat(),
            )
        return False

    if result.error == "network_error":
        db.record_account_error(
            account_id, "Network error during token refresh",
            increment_failures=False)

    return False


async def _try_refresh_on_429(
    account_id: int, db: Database, state: dict,
) -> str | None:
    """Attempt to get a fresh access token to clear a per-token rate limit."""
    result = await _refresh_token_flow(account_id, db, RefreshMode.CC_OR_PRIMARY_429)
    return result.access_token if result.success else None


async def _try_refresh_primary_token(
    account_id: int,
    db,
    stale_token: str | None = None,
) -> str | None:
    """Refresh the primary access token on 401. Returns new token or None."""
    result = await _refresh_token_flow(account_id, db, RefreshMode.PRIMARY_CIRCUIT_BREAKER)
    return result.access_token if result.success else None


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
    upstream API; manual=True gets the shorter _USAGE_MANUAL_FLOOR_SECONDS
    floor instead of a full bypass.  Only the single bounded 401/429 retry
    (_retry_depth > 0) skips pacing, and it never resets pacing state.

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

    # Pacing: hard ceiling for background fetches, shorter floor for manual.
    # _retry_depth > 0 is the single bounded 401/429 retry with a fresh
    # token — let it through WITHOUT touching last_fetched_at (the old
    # "reset to 0 before retry" un-paced the account and turned every
    # 429 into a license for the next client request to fetch again).
    if _retry_depth == 0:
        # Pace against BOTH the in-memory stamp and the DB's usage_cached_at.
        # The in-memory dict dies with the process, so without the DB check
        # every restart un-paced all accounts and the startup prime fetch
        # re-hit the upstream API for the whole fleet (2026-06 incident).
        try:
            db_cached_at = float(account.get("usage_cached_at") or 0)
        except (TypeError, ValueError):
            db_cached_at = 0.0
        elapsed = now - max(state["last_fetched_at"], db_cached_at)
        if manual:
            if elapsed < _USAGE_MANUAL_FLOOR_SECONDS:
                logger.debug(
                    f"Manual usage floor for account {account_id}: "
                    f"{int(elapsed)}s < {_USAGE_MANUAL_FLOOR_SECONDS}s, "
                    f"returning cached"
                )
                return {"_cached": True}
        elif elapsed < _USAGE_RATE_LIMIT_CEILING:
            logger.debug(
                f"Usage ceiling for account {account_id}: {int(elapsed)}s < "
                f"{_USAGE_RATE_LIMIT_CEILING}s, returning cached"
            )
            return {"_cached": True}

    token = access_token or account["access_token"]

    # Stamp pacing at ATTEMPT time, not only on success/429: the failure
    # exits below (401 after refresh + live import both fail, generic HTTP
    # error, transport exception) never stamped, so a permanently failing
    # account (dead refresh token, upstream 5xx) was never paced and EVERY
    # client request made a fresh upstream GET — the unbounded half of the
    # 2026-06 fetch storm. Success/429 paths re-stamp afterwards, which is
    # harmless. Depth>0 retries skip this (the depth-0 frame re-stamps
    # after they return).
    if _retry_depth == 0:
        state["last_fetched_at"] = time.time()

    # Provider dispatch: Codex usage comes from the `codex app-server` JSON-RPC,
    # not the Anthropic HTTP API. Pacing/backoff/stamping above are
    # provider-agnostic and already applied; everything below is Anthropic-only.
    if (account.get("provider") or "claude") == "codex":
        from jacked.codex.usage import fetch_codex_usage, live_codex_account_id

        # app-server reports usage for whichever Codex account is live in the
        # shared ~/.codex root. Polling a NON-active account there would cache
        # the live account's numbers under the wrong id (and risk rotating its
        # tokens), so only poll the account currently in the root; the others
        # keep their last-known cached usage until switched to.
        if live_codex_account_id(db) != account_id:
            return {"_cached": True}
        return await fetch_codex_usage(account_id, db, state=state)

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

                # Piggyback a throttled profile refresh so subscription changes
                # (Pro <-> Max, 5x <-> 20x) reach the dashboard within hours even
                # if this account's token never rotates. Non-fatal by design.
                now = time.time()
                if now - _profile_refreshed_at.get(account_id, 0) > _PROFILE_REFRESH_INTERVAL:
                    _profile_refreshed_at[account_id] = now  # stamp first — a failing profile API must not retry every poll
                    try:
                        await fetch_profile(account_id, db, access_token=token)
                    except Exception:
                        logger.debug(
                            "piggybacked profile refresh failed for account %d",
                            account_id, exc_info=True,
                        )

                return data

            if resp.status_code in (401, 403):
                # Try refreshing the primary token before giving up. The
                # retry rides _retry_depth through the pacing check; we
                # re-stamp last_fetched_at afterwards (success or not) so
                # a 401 retry can never un-pace the account.
                if _retry_depth < 1:
                    fresh = await _try_refresh_primary_token(
                        account_id, db, stale_token=token,
                    )
                    if fresh:
                        result = await fetch_usage(
                            account_id, db, access_token=fresh,
                            _retry_depth=_retry_depth + 1,
                        )
                        state["last_fetched_at"] = time.time()
                        return result

                    # Refresh failed — try live credential import for active account
                    try:
                        from jacked.api.credential_helpers import reconcile_credentials_from_live_store
                        reconcile_credentials_from_live_store(account_id, db)
                        refreshed_acct = db.get_account(account_id)
                        if refreshed_acct:
                            live_token = refreshed_acct.get("access_token")
                            if live_token and live_token != token:
                                result = await fetch_usage(
                                    account_id, db, access_token=live_token,
                                    _retry_depth=_retry_depth + 1,
                                )
                                state["last_fetched_at"] = time.time()
                                return result
                    except Exception:
                        logger.debug("Live credential import failed during 401 recovery",
                                     exc_info=True)

                # Refresh and live import both failed or already retried — mark invalid
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
                # Try to clear the per-token rate limit by getting a fresh
                # token. Only attempt on first try (_retry_depth=0) to
                # prevent recursion, and at most once per
                # _USAGE_429_ROTATION_INTERVAL per account — rotating
                # refresh tokens to dodge per-token rate limits at high
                # frequency churns the shared CC token store and masks
                # real rate-limiting (243 rotations in one 4.5h incident).
                # Otherwise skip straight to the backoff branch below.
                rotation_due = (
                    time.time() - state.get("last_429_rotation_at", 0.0)
                    > _USAGE_429_ROTATION_INTERVAL
                )
                if _retry_depth == 0 and rotation_due:
                    # Stamp at attempt time (success or not): a failed
                    # rotation still hit the token endpoint.
                    state["last_429_rotation_at"] = time.time()
                    fresh_token = await _try_refresh_on_429(account_id, db, state)
                    if fresh_token:
                        state["consecutive_429s"] = 0
                        logger.info("Account %d: retrying usage fetch with fresh token", account_id)
                        # The retry rides _retry_depth=1 through the pacing
                        # check; re-stamp last_fetched_at afterwards
                        # (success or not) so a 429 retry can never
                        # un-pace the account.
                        result = await fetch_usage(
                            account_id, db, access_token=fresh_token, _retry_depth=1,
                        )
                        state["last_fetched_at"] = time.time()
                        return result

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


def _validate_codex_account(account_id: int, db: Database) -> dict:
    """Validate a Codex account by its signed-in state (its slot holds a real
    credential), NOT the Anthropic profile API — Codex tokens aren't Anthropic
    tokens, so the profile call would always 401 and falsely mark it invalid."""
    try:
        from jacked.codex.credentials import read_auth_json
        from jacked.codex.switching import codex_account_home

        auth = read_auth_json(codex_account_home(account_id))
        signed_in = bool(auth and (auth.get("tokens") or auth.get("OPENAI_API_KEY")))
    except Exception:
        logger.debug("codex validation read failed for %s", account_id, exc_info=True)
        signed_in = True  # never false-invalidate on a transient read error
    if signed_in:
        db.update_account(
            account_id,
            validation_status="valid",
            last_validated_at=int(time.time()),
            consecutive_failures=0,
            last_error=None,
            last_error_at=None,
        )
        return {"valid": True, "error": None}
    db.update_account(
        account_id,
        validation_status="invalid",
        last_validated_at=int(time.time()),
        last_error="Codex account signed out — run `codex login`",
        last_error_at=datetime.now(timezone.utc).isoformat(),
    )
    return {"valid": False, "error": "Codex signed out"}


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

    # Codex accounts are NOT Anthropic-token-validated (their stored token is a
    # sentinel). Validity = signed in to Codex (the account's slot holds a real
    # credential); hitting the Anthropic profile API would always 401 and
    # falsely mark them invalid.
    if (account.get("provider") or "claude") == "codex":
        return _validate_codex_account(account_id, db)

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
                    last_error=None,
                    last_error_at=None,
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
                            last_error=None,
                            last_error_at=None,
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


async def reset_stale_checking_accounts(
    db: "Database",
    threshold_seconds: int = 120,
) -> int:
    """Reset accounts stuck in validation_status='checking' past threshold.

    Iterates the scan result, delegating to db.reset_stuck_checking's
    WHERE-guarded atomic UPDATE so a concurrent validator can't be
    clobbered.  Returns total rows actually reset.
    """
    reset_count = 0
    for acct in db.list_stuck_checking_accounts(threshold_seconds=threshold_seconds):
        now = int(time.time())
        updated_at = acct.get("updated_at")
        if updated_at:
            try:
                updated_at_dt = datetime.fromisoformat(
                    updated_at.replace("Z", "+00:00"),
                )
                age = int(now - updated_at_dt.timestamp())
            except (ValueError, TypeError):
                age = threshold_seconds
        else:
            age = threshold_seconds  # NULL updated_at

        reason = f"validation timed out after {age}s — reset by watchdog"
        rows_reset = db.reset_stuck_checking(
            acct["id"], threshold_seconds=threshold_seconds, reason=reason,
        )
        if rows_reset > 0:
            logger.warning(
                "Stuck-checking watchdog: account %d was 'checking' for %ds — "
                "reset to 'unknown'",
                acct["id"], age,
            )
            reset_count += rows_reset
    return reset_count


# Per-account refresh locks to prevent concurrent refresh collisions
# between the background loop and manual API calls.
_refresh_locks: dict[int, asyncio.Lock] = {}
_cc_refresh_locks: dict[int, asyncio.Lock] = {}

# Strong references to in-flight shielded refresh tasks. The event loop
# holds only weak refs to tasks; when a caller is cancelled at the shield
# in _refresh_token_flow, this set is what keeps the still-running
# exchange task alive until it persists the rotated token.
_inflight_refresh_tasks: set[asyncio.Task] = set()


def _retire_refresh_task(task: asyncio.Task) -> None:
    """Done-callback for shielded refresh tasks: drop the strong ref and
    retrieve any exception so abandoned (caller-cancelled) tasks don't
    emit 'Task exception was never retrieved' warnings."""
    _inflight_refresh_tasks.discard(task)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Shielded token refresh task failed: %r", exc)


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


def reset_locks() -> None:
    """Clear per-account locks so the next event loop can bind fresh ones.

    See jacked.api.routes.auth.reset_locks for the full story — in-process
    uvicorn restarts (tray) create a new event loop while this module stays
    imported, leaving every cached Lock bound to the dead loop. Log the
    stranded-lock count if non-zero so on-call can spot "tray restart
    while refreshes were in flight" from the log stream.
    """
    stranded = len(_refresh_locks) + len(_cc_refresh_locks)
    if stranded:
        logger.warning(
            "reset_locks: dropping %d per-account refresh lock(s) from previous loop "
            "(primary=%d, cc=%d)",
            stranded, len(_refresh_locks), len(_cc_refresh_locks),
        )
    _refresh_locks.clear()
    _cc_refresh_locks.clear()
    # In-flight shielded refresh tasks are bound to the dead loop too —
    # they can never complete or run their done-callbacks; drop the refs.
    _inflight_refresh_tasks.clear()


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

    # Identify the active Claude Code session up front so the CC-refresh
    # skip below can't be defeated by an exception anywhere in reconcile.
    # Must be initialized BEFORE the try block so it's always bound (a
    # NameError here would crash the entire refresh pipeline — see
    # architecture doc §7.1).
    active_id: int | None = None
    try:
        from jacked.api.credential_helpers import (
            read_active_account_id,
            reconcile_credentials_from_live_store,
        )
        active_id = read_active_account_id()
        if active_id is not None:
            reconcile_credentials_from_live_store(active_id, db)
    except Exception:
        logger.debug("Periodic credential reconciliation failed", exc_info=True)

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
                # Fast-skip only — must NOT hold the lock across the call:
                # _refresh_token_flow acquires the same non-reentrant lock
                # internally and would self-deadlock the loop.
                try:
                    success = await asyncio.wait_for(
                        refresh_account_token(account_id, db), timeout=60,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Account %d: refresh timed out after 60s — counting as failed",
                        account_id,
                    )
                    success = False
                except Exception:
                    logger.exception(
                        "Account %d: refresh raised unexpectedly", account_id,
                    )
                    success = False
                if success:
                    result["refreshed"] += 1
                else:
                    result["failed"] += 1
        else:
            result["skipped"] += 1

        # --- CC token refresh (independent from primary) ---
        # Never rotate the active account's CC refresh token from a background
        # loop. The CC refresh token is single-use and SHARED with Claude Code
        # on the active account; if jacked exchanges it upstream, Claude Code's
        # own refresher sees invalid_grant on its next attempt and forces a
        # re-login across every session using those credentials. Claude Code
        # (v2.1.81+) manages rotation for its active account on its own
        # schedule; jacked imports rotated tokens via
        # reconcile_credentials_from_live_store at line ~1044 above.
        # See docs/architecture/oauth-and-credential-flows.md §7.1.
        if active_id == account_id:
            continue

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
        if status not in ("invalid", "unknown", "checking"):
            continue

        result["checked"] += 1
        account_id = account["id"]

        # Try refresh first if has refresh token. Call _refresh_token_flow
        # directly — refresh_account_token's should_refresh gate would
        # report success without exchanging when the token isn't near
        # expiry, marking accounts healed without proving the credentials
        # work (phantom heal). The flow owns locking, so no lock is held
        # here (it's non-reentrant — holding it would self-deadlock).
        healed = False
        if account.get("refresh_token"):
            lock = _get_refresh_lock(account_id)
            if not lock.locked():
                # Clear circuit breaker state before recovery attempt
                logger.info(
                    "Account %d: clearing circuit breaker for heal attempt",
                    account_id,
                )
                db.update_account(
                    account_id,
                    refresh_last_failed_at=None,
                    refresh_failure_type=None,
                )
                refresh_result = await _refresh_token_flow(
                    account_id, db, RefreshMode.PRIMARY,
                )
                healed = refresh_result.success

        if not healed:
            # Try reconciling from live credentials before validate
            try:
                from jacked.api.credential_helpers import reconcile_credentials_from_live_store
                reconcile_credentials_from_live_store(account_id, db)
            except ImportError:
                try:
                    from jacked.api.credential_helpers import reconcile_outgoing_credentials
                    reconcile_outgoing_credentials(account_id, db)
                except (ImportError, Exception):
                    pass
            except Exception:
                logger.debug("Credential reconciliation failed for account %d", account_id)

            try:
                validation = await asyncio.wait_for(
                    validate_account(account_id, db), timeout=60,
                )
                healed = validation.get("valid", False)
            except asyncio.TimeoutError:
                logger.error(
                    "Account %d: heal validation timed out after 60s — "
                    "treating as not healed",
                    account_id,
                )
                healed = False

        if healed:
            result["healed"] += 1
            db.update_account(account_id,
                              validation_status="valid",
                              last_validated_at=int(time.time()))
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
