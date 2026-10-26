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
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Literal, Optional

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

# A 429 backs off; it does not rotate a token. Rotating refresh tokens to
# dodge per-token rate limits churns the shared CC token store and masks real
# rate-limiting (one incident rotated CC tokens 243 times in 4.5h; another
# renewed the primary chain ~140 times a day, once per poll cycle, because the
# old 600s guard equaled the 600s poll period and never throttled). A
# rotation is allowed only after _USAGE_429_ROTATION_MIN_CONSECUTIVE
# consecutive 429s on the SAME token, and at most once per guard interval:
# _USAGE_429_ROTATION_POLL_PERIODS poll periods, read at call time from the
# usage_check_interval setting (floored at the 600s all-accounts poll).
_USAGE_POLL_PERIOD_FLOOR = 600
_USAGE_429_ROTATION_POLL_PERIODS = 3
_USAGE_429_ROTATION_MIN_CONSECUTIVE = 3
# The guard interval at the default settings (3 x 600s).
_USAGE_429_ROTATION_INTERVAL = _USAGE_429_ROTATION_POLL_PERIODS * _USAGE_POLL_PERIOD_FLOOR
# Upper bound on an honored Retry-After header (a guard against absurd values).
_USAGE_RETRY_AFTER_MAX_SECONDS = 6 * 3600

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


def _profile_owner_mismatch(row: dict, data: dict) -> Optional[str]:
    """Return the NAME of the first profile field that does not match the row.

    The profile must hold an account object whose ``email`` is equal to the
    row email (casefold, stripped). When the row has an organization uuid,
    ``organization.uuid`` must be equal to it. When the row and the profile
    both carry an account uuid, they must be equal. Return None when the
    profile belongs to the row. The return value is a field name, never a
    value. The rule lives in ``claude_identity.profile_owner_mismatch``.

    >>> row = {"email": "a@x.com", "organization_uuid": "o1"}
    >>> _profile_owner_mismatch(row, {"account": {"email": " A@X.com"},
    ...                               "organization": {"uuid": "o1"}}) is None
    True
    >>> _profile_owner_mismatch(row, {"account": {"email": "b@x.com"}})
    'account.email'
    >>> _profile_owner_mismatch(row, {"account": {"email": "a@x.com"},
    ...                               "organization": {"uuid": "o2"}})
    'organization.uuid'
    """
    from jacked.api.claude_identity import profile_owner_mismatch

    return profile_owner_mismatch(
        row.get("email"),
        row.get("organization_uuid"),
        row.get("account_uuid"),
        data,
    )


def _update_profile_metadata(account_id: int, data: dict, db) -> bool:
    """Update the account row from a profile API response.

    This function applies NOTHING when the profile does not belong to the
    row (see ``_profile_owner_mismatch``). It then logs a WARNING that names
    the row id and the field name, never a value. On a match, it stores the
    plan metadata, the Claude account uuid, the non-secret profile subset
    (the ``account`` and ``organization`` objects only, exactly as the
    endpoint returned them; a missing organization is stored as null) and
    the fetch time.

    Returns True when the profile was applied, False when the row is
    unknown or the profile does not belong to it.
    """
    row = db.get_account(account_id)
    if not row:
        return False
    mismatch = _profile_owner_mismatch(row, data)
    if mismatch is not None:
        logger.warning(
            "Profile for account %d not applied: %s does not match the row",
            account_id, mismatch,
        )
        return False

    acct = data["account"]
    raw_org = data.get("organization")
    # The empty-dict fallback is only for reading the plan metadata below;
    # profile_json keeps the organization value the endpoint returned.
    org = raw_org if isinstance(raw_org, dict) else {}
    org_type = org.get("organization_type", "")
    subscription_type = ORG_TYPE_MAP.get(org_type)

    updates: dict = {
        "profile_json": json.dumps({"account": acct, "organization": raw_org}),
        "profile_fetched_at": time.time(),
    }
    account_uuid = acct.get("uuid")
    if isinstance(account_uuid, str) and account_uuid:
        updates["account_uuid"] = account_uuid
    if subscription_type:
        updates["subscription_type"] = subscription_type
    if org.get("rate_limit_tier"):
        updates["rate_limit_tier"] = org["rate_limit_tier"]
    if "has_extra_usage_enabled" in org:
        updates["has_extra_usage"] = org["has_extra_usage_enabled"]
    if org.get("name"):
        updates["organization_name"] = org["name"]
    db.update_account(account_id, **updates)
    return True


@dataclass
class TokenExchangeResult:
    """Result of exchanging a refresh token for new tokens."""
    success: bool
    access_token: str | None = None
    refresh_token: str | None = None
    expires_in: int | None = None
    error: str | None = None       # "invalid_grant", "http_429", "not_sent", "reply_lost", ...
    status_code: int | None = None
    # True only when THIS call completed a token exchange and stored it. The
    # "already refreshed by another path" return and a live recovery leave it
    # False, so callers can count real exchanges.
    exchanged: bool = False
    # The refresh lineage the flow used: "cc" or "primary".
    chain: str | None = None
    # Values-free detail for logs (an exception class name), never a token.
    detail: str | None = None


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
    # The request never left this machine (DNS, connect, pool timeout).
    "not_sent": 60,
    # The request was sent but the reply was lost (read/write timeout, a
    # dropped connection). The server may have rotated the refresh token, so
    # the chain is suspect and waits longer before the next attempt.
    "reply_lost": 900,
    "http_429": 120,
    "http_5xx": 120,
}
_DEFAULT_CB_COOLDOWN = 300


def breaker_cooldown(failure_type: str | None) -> int:
    """Cooldown seconds for a breaker failure type.

    An exchange records a 5xx as ``http_<status>`` (``http_502``), so the
    ``http_5xx`` entry is matched by status class here, not by exact key.

    >>> breaker_cooldown("invalid_grant")
    600
    >>> breaker_cooldown("http_503") == CIRCUIT_BREAKER_COOLDOWNS["http_5xx"]
    True
    >>> breaker_cooldown("http_418") == _DEFAULT_CB_COOLDOWN
    True
    """
    if isinstance(failure_type, str) and re.fullmatch(r"http_5\d\d", failure_type):
        return CIRCUIT_BREAKER_COOLDOWNS["http_5xx"]
    return CIRCUIT_BREAKER_COOLDOWNS.get(failure_type, _DEFAULT_CB_COOLDOWN)

# Breaker failure types that describe the network, not the credential.
NETWORK_FAILURE_TYPES = frozenset({"network_error", "not_sent", "reply_lost"})

# httpx errors raised before the request reached the server.
_NOT_SENT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.ProxyError,
    httpx.UnsupportedProtocol,
    httpx.LocalProtocolError,
)
# httpx errors raised after the request was (possibly) written.
_REPLY_LOST_ERRORS: tuple[type[BaseException], ...] = (
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.ReadError,
    httpx.WriteError,
    httpx.RemoteProtocolError,
    httpx.CloseError,
    httpx.TimeoutException,
)


def classify_exchange_exception(exc: BaseException) -> str:
    """Map an exchange exception to a breaker failure type.

    ``not_sent`` means the server never saw the request, so the refresh
    token is intact. ``reply_lost`` means the server may have consumed it.
    Anything else is ``network_error``.

    >>> classify_exchange_exception(httpx.ConnectError("dns"))
    'not_sent'
    >>> classify_exchange_exception(httpx.ReadTimeout("slow"))
    'reply_lost'
    >>> classify_exchange_exception(httpx.RemoteProtocolError("eof"))
    'reply_lost'
    >>> classify_exchange_exception(RuntimeError("other"))
    'network_error'
    """
    if isinstance(exc, _NOT_SENT_ERRORS):
        return "not_sent"
    if isinstance(exc, _REPLY_LOST_ERRORS):
        return "reply_lost"
    return "network_error"


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
    except Exception as exc:
        return TokenExchangeResult(
            success=False,
            error=classify_exchange_exception(exc),
            detail=type(exc).__name__,
        )

    try:
        if resp.status_code == 200:
            try:
                data = resp.json()
                access_token = data["access_token"]
            except Exception as exc:
                # The server answered 200, so it consumed the refresh token,
                # but the new pair is unreadable: the reply is lost.
                return TokenExchangeResult(
                    success=False,
                    error="reply_lost",
                    status_code=200,
                    detail=type(exc).__name__,
                )
            return TokenExchangeResult(
                success=True,
                access_token=access_token,
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
            detail=type(exc).__name__,
        )


Chain = Literal["cc", "primary"]

# Breaker columns per chain. The primary pair predates the split and keeps
# its name; the Claude Code (cc) chain has its own pair (database migration
# "workstream B, token chains").
_CHAIN_BREAKER_COLUMNS: dict[str, tuple[str, str]] = {
    "primary": ("refresh_failure_type", "refresh_last_failed_at"),
    "cc": ("cc_refresh_failure_type", "cc_refresh_last_failed_at"),
}


def _chain_token_columns(chain: Chain) -> tuple[str, str, str]:
    """Return the (access, refresh, expires) column names of ``chain``.

    >>> _chain_token_columns("cc")
    ('cc_access_token', 'cc_refresh_token', 'cc_expires_at')
    >>> _chain_token_columns("primary")
    ('access_token', 'refresh_token', 'expires_at')
    """
    if chain == "cc":
        return ("cc_access_token", "cc_refresh_token", "cc_expires_at")
    return ("access_token", "refresh_token", "expires_at")


def _breaker_updates(chain: Chain, failure_type: str | None) -> dict:
    """Return the update that records (or clears) one chain's breaker.

    >>> _breaker_updates("cc", None)
    {'cc_refresh_failure_type': None, 'cc_refresh_last_failed_at': None}
    >>> sorted(_breaker_updates("primary", "invalid_grant"))
    ['refresh_failure_type', 'refresh_last_failed_at']
    """
    type_col, at_col = _CHAIN_BREAKER_COLUMNS[chain]
    return {
        type_col: failure_type,
        at_col: int(time.time()) if failure_type is not None else None,
    }


def _resolve_chain(mode: RefreshMode, account: dict) -> Chain:
    """Decide which refresh lineage ``mode`` exchanges for ``account``.

    CC_OR_PRIMARY_429 uses the Claude Code chain while it has a refresh
    token and no recorded invalid_grant, else the primary chain.

    >>> _resolve_chain(RefreshMode.CC_OR_PRIMARY_429, {"cc_refresh_token": "r"})
    'cc'
    >>> _resolve_chain(RefreshMode.CC_OR_PRIMARY_429, {"cc_refresh_token": None})
    'primary'
    >>> _resolve_chain(RefreshMode.CC_OR_PRIMARY_429,
    ...                {"cc_refresh_token": "r", "cc_refresh_failure_type": "invalid_grant"})
    'primary'
    >>> _resolve_chain(RefreshMode.PRIMARY_CIRCUIT_BREAKER, {"cc_refresh_token": "r"})
    'primary'
    """
    if mode in (RefreshMode.PRIMARY, RefreshMode.PRIMARY_CIRCUIT_BREAKER):
        return "primary"
    if mode == RefreshMode.CC:
        return "cc"
    if account.get("cc_refresh_token") and (
        account.get("cc_refresh_failure_type") != "invalid_grant"
    ):
        return "cc"
    return "primary"


def _live_recovery_payload(account_id: int, account: dict) -> Optional[dict]:
    """Read the live Claude Code tokens for an invalid_grant recovery.

    Runs in a worker thread: it reads the credential stores and may send
    one profile request. It returns the ``claudeAiOauth`` section only when
    the shared stamp-and-ownership rule proves the tokens belong to
    ``account`` and the access token has not expired. Otherwise it returns
    None and logs a values-free reason.
    """
    from jacked.api.credential_helpers import (
        is_primary_publication,
        live_payload_belongs_to,
        read_live_credentials,
    )

    live = read_live_credentials()
    if not isinstance(live, dict):
        return None
    oauth = live.get("claudeAiOauth")
    if not isinstance(oauth, dict) or not oauth.get("accessToken"):
        return None
    if is_primary_publication(account, oauth.get("accessToken")):
        return None
    live_expires = oauth.get("expiresAt")
    if isinstance(live_expires, (int, float)) and live_expires:
        expires_s = live_expires / 1000 if live_expires > 1e12 else live_expires
        if expires_s <= time.time():
            return None
    if not live_payload_belongs_to(account, live):
        logger.warning(
            "Account %d: invalid_grant live recovery refused; the live "
            "credentials are not proven to belong to this account",
            account_id,
        )
        return None
    return oauth


async def _refresh_token_flow(
    account_id: int,
    db: Database,
    mode: RefreshMode,
) -> TokenExchangeResult:
    """Unified token refresh orchestrator — replaces per-caller refresh logic.

    Handles token resolution, locking, circuit breaker, exchange, DB write
    with retry, and error recovery. Each RefreshMode encodes caller-specific
    behavior (which token, timeouts, error handling).

    The flow resolves ONE chain (``cc`` or ``primary``) under the lock and
    uses it for every column it reads or writes, for the invalid_grant
    branch, for the log text, and for the token it returns. A failure on
    one chain never touches the other chain's tokens or breaker.

    Credential refresh is DB-only. It never writes Claude's live stores.
    """
    account = db.get_account(account_id)
    if not account:
        return TokenExchangeResult(success=False, error="account_not_found")

    # At most two passes: the first chain is resolved before the lock to
    # choose the lock. When the locked re-read resolves a different chain (a
    # concurrent cc invalid_grant cleared the cc refresh token), the flow
    # releases that lock and runs once more under the correct one.
    for _attempt in range(2):
        tentative = _resolve_chain(mode, account)
        result = await _run_chain_flow(account_id, db, mode, tentative, account)
        if result.error != "chain_changed":
            return result
        account = db.get_account(account_id)
        if not account:
            return TokenExchangeResult(success=False, error="account_not_found")
    logger.warning(
        "Account %d: %s refresh chain kept changing under the lock; aborting",
        account_id, mode.value,
    )
    return TokenExchangeResult(success=False, error="chain_changed")


async def _run_chain_flow(
    account_id: int,
    db: Database,
    mode: RefreshMode,
    chain: Chain,
    account: dict,
) -> TokenExchangeResult:
    """Run one refresh of ``chain`` under that chain's lock (see above)."""
    access_col, refresh_col, expires_col = _chain_token_columns(chain)
    refresh_token = account.get(refresh_col)
    if not refresh_token:
        return TokenExchangeResult(success=False, error="no_refresh_token", chain=chain)

    # Lock rule: all callers sharing a token set share the same lock.
    lock = _get_cc_refresh_lock(account_id) if chain == "cc" else _get_refresh_lock(account_id)

    # Acquire lock (bounded). The lock is non-reentrant: an unbounded
    # acquire behind a wedged holder would suspend this caller forever.
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_REFRESH_LOCK_ACQUIRE_TIMEOUT)
    except asyncio.TimeoutError:
        logger.error(
            "Account %d: could not acquire %s refresh lock within %ds — "
            "possible wedged holder; aborting this refresh attempt",
            account_id, mode.value, int(_REFRESH_LOCK_ACQUIRE_TIMEOUT),
        )
        return TokenExchangeResult(success=False, error="lock_timeout", chain=chain)

    exchanged = False

    async def _locked_section() -> TokenExchangeResult:
        # Critical section under the per-account lock — runs as a SHIELDED
        # task (created below) and therefore owns the lock release: the
        # awaiting caller may be cancelled out from under it by any of the
        # asyncio.wait_for wrappers on the refresh paths.
        nonlocal exchanged
        try:
            current = db.get_account(account_id)
            if not current:
                return TokenExchangeResult(success=False, error="account_not_found")

            # The chain is resolved ONCE, here, under the lock.
            if _resolve_chain(mode, current) != chain:
                return TokenExchangeResult(success=False, error="chain_changed")

            # Circuit breaker (PRIMARY_CIRCUIT_BREAKER only, primary chain).
            if mode == RefreshMode.PRIMARY_CIRCUIT_BREAKER:
                last_failed = current.get("refresh_last_failed_at")
                failure_type = current.get("refresh_failure_type")
                if last_failed is not None:
                    cooldown = breaker_cooldown(failure_type)
                    remaining = cooldown - (time.time() - last_failed)
                    if remaining > 0:
                        logger.info(
                            "Account %d: circuit breaker active (%s, %ds remaining)",
                            account_id, failure_type, int(remaining),
                        )
                        return TokenExchangeResult(
                            success=False, error="circuit_breaker", chain=chain,
                        )
                    logger.info(
                        "Account %d: circuit breaker cooldown expired, re-attempting refresh",
                        account_id,
                    )

            # Another path rotated this chain while we waited for the lock:
            # hand back THIS chain's current access token.
            current_refresh = current.get(refresh_col)
            if current_refresh and current_refresh != refresh_token:
                logger.info(
                    "Account %d: %s chain token already refreshed by another path",
                    account_id, chain,
                )
                return TokenExchangeResult(
                    success=True, access_token=current.get(access_col), chain=chain,
                )

            timeout = 30.0 if mode in (RefreshMode.PRIMARY, RefreshMode.CC) else 15.0
            result = await _exchange_refresh_token(refresh_token, timeout=timeout)

            # ----------------------------------------------------------
            # Success — single atomic DB write to this chain's columns.
            # ----------------------------------------------------------
            if result.success:
                new_expires_at = int(time.time()) + result.expires_in
                updates = {
                    access_col: result.access_token,
                    refresh_col: result.refresh_token,
                    expires_col: new_expires_at,
                    **_breaker_updates(chain, None),
                }

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
                    return TokenExchangeResult(
                        success=False, error="db_write_failed", chain=chain,
                    )

                logger.info(
                    "Account %d: %s chain token refreshed (caller %s)",
                    account_id, chain, mode.value,
                )
                # Credential refresh is DB-only. A 429 or maintenance path
                # must never mutate Claude's live stores: doing so races
                # Claude Code's own refresh-token lifecycle and can silently
                # rebind sessions.
                exchanged = True
                return TokenExchangeResult(
                    success=True,
                    access_token=result.access_token,
                    refresh_token=result.refresh_token,
                    expires_in=result.expires_in,
                    exchanged=True,
                    chain=chain,
                )

            # ----------------------------------------------------------
            # invalid_grant — the chain's refresh lineage is dead.
            # ----------------------------------------------------------
            if result.error == "invalid_grant":
                if chain == "cc":
                    return await _cc_chain_died(account_id, db, current)
                db.update_account(account_id, **_breaker_updates("primary", "invalid_grant"))
                if mode == RefreshMode.PRIMARY:
                    db.record_account_error(account_id, "Refresh token consumed (will retry)")
                else:
                    logger.warning(
                        "Account %d: primary refresh token is dead (invalid_grant) — "
                        "cooldown %ds (chain=primary, caller %s)",
                        account_id, CIRCUIT_BREAKER_COOLDOWNS["invalid_grant"], mode.value,
                    )
                return TokenExchangeResult(
                    success=False, error="invalid_grant", chain=chain,
                )

            # ----------------------------------------------------------
            # Other errors — this chain's breaker only.
            # ----------------------------------------------------------
            cooldown = breaker_cooldown(result.error)
            db.update_account(account_id, **_breaker_updates(chain, result.error))
            if result.error == "reply_lost":
                logger.warning(
                    "Account %d: %s chain refresh reply lost after the request "
                    "was sent (%s); the refresh token may be consumed, chain "
                    "marked suspect — cooldown %ds",
                    account_id, chain, result.detail or "unknown", cooldown,
                )
            else:
                logger.warning(
                    "Account %d: refresh failed (%s) — cooldown %ds (chain=%s)",
                    account_id, result.error, cooldown, chain,
                )
            result.chain = chain
            return result
        finally:
            lock.release()

    # Run the locked section as a SHIELDED task. Anthropic ROTATES refresh
    # tokens: a cancellation landing after the exchange POST has been
    # consumed upstream but before the rotated pair is persisted would
    # orphan the account behind a permanent invalid_grant. shield() lets
    # the task finish the exchange, DB write, and lock release in the
    # background while the caller observes its timeout.
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

    # Post-refresh profile fetch — deliberately OUTSIDE the lock.
    # fetch_profile's 401 path re-enters _refresh_token_flow on the same
    # primary lock; running it while holding the lock self-deadlocks.
    # Only a real successful exchange gets here.
    if mode == RefreshMode.PRIMARY and exchanged:
        try:
            await fetch_profile(account_id, db, access_token=flow_result.access_token)
        except Exception as prof_err:
            logger.warning(
                "Account %d: post-refresh profile fetch failed (non-fatal): %s",
                account_id, prof_err,
            )
    return flow_result


async def _cc_chain_died(account_id: int, db: Database, account: dict) -> TokenExchangeResult:
    """Handle invalid_grant on the Claude Code chain (lock held by caller).

    The chain is dead either way. Its refresh token is spent, so it leaves
    the row, and the cc breaker records invalid_grant. When the live store
    holds an unexpired access token that the shared ownership rule proves
    belongs to this account, that token is kept for the rest of its life.
    Otherwise the dead access token and its expiry leave the row too, so no
    consumer keeps sending a dead token. The live refresh token is never
    imported here: it is Claude Code's, and exchanging it would destroy the
    session that holds it.
    """
    try:
        oauth = await asyncio.to_thread(_live_recovery_payload, account_id, account)
    except Exception:
        logger.debug("Account %d: live recovery read failed", account_id, exc_info=True)
        oauth = None

    if oauth:
        live_access = oauth.get("accessToken")
        live_expires = oauth.get("expiresAt")
        live_exp_s = (
            int(live_expires / 1000) if live_expires and live_expires > 1e12
            else int(live_expires) if live_expires else None
        )
        db.update_account(
            account_id,
            cc_access_token=live_access,
            cc_expires_at=live_exp_s,
            cc_refresh_token=None,
            **_breaker_updates("cc", "invalid_grant"),
        )
        logger.info(
            "Account %d: invalid_grant on the cc chain — recovered cc_access_token "
            "from live credentials (ownership proven; refresh token not imported)",
            account_id,
        )
        return TokenExchangeResult(success=True, access_token=live_access, chain="cc")

    db.update_account(
        account_id,
        cc_access_token=None,
        cc_refresh_token=None,
        cc_expires_at=None,
        **_breaker_updates("cc", "invalid_grant"),
    )
    logger.warning(
        "Account %d: invalid_grant on the cc chain — clearing cc tokens "
        "(no recovery; the Claude Code chain needs re-authorization)",
        account_id,
    )
    return TokenExchangeResult(success=False, error="invalid_grant", chain="cc")


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
            "last_429_token_fp": None,
        }
    state = _account_usage_state[account_id]
    if "consecutive_429s" not in state:
        state["consecutive_429s"] = 0
    if "last_429_rotation_at" not in state:
        state["last_429_rotation_at"] = 0.0
    if "last_429_token_fp" not in state:
        state["last_429_token_fp"] = None
    return state


def _token_fingerprint(token: Optional[str]) -> Optional[str]:
    """Return a short, non-reversible fingerprint of a token for state keys.

    >>> _token_fingerprint(None) is None
    True
    >>> len(_token_fingerprint("abc"))
    16
    """
    if not token:
        return None
    import hashlib

    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def _usage_429_rotation_interval(db) -> float:
    """Return the 429 rotation guard interval, read from settings at call time.

    The guard spans _USAGE_429_ROTATION_POLL_PERIODS poll periods. The poll
    period is the ``usage_check_interval`` setting, floored at the 600s
    all-accounts poll, so the guard always spans several real poll cycles.

    >>> _usage_429_rotation_interval(None)
    1800.0
    """
    period = 300.0
    try:
        raw = db.get_setting("usage_check_interval") if db is not None else None
        if isinstance(raw, (str, int, float)) and not isinstance(raw, bool):
            period = float(raw)
    except Exception:
        logger.debug("usage_check_interval read failed", exc_info=True)
    return float(_USAGE_429_ROTATION_POLL_PERIODS * max(period, _USAGE_POLL_PERIOD_FLOOR))


def usage_cache_updates(data: dict) -> dict:
    """Map a usage API response to ``update_account_usage_cache`` arguments.

    A window key that is missing keeps the old cache. A window that is null
    (no active window) clears that window. A window object with a null
    ``resets_at`` keeps its utilization and clears the stale reset time.

    >>> usage_cache_updates({"five_hour": None, "seven_day": {"utilization": 3}})
    {'clear_five_hour': True, 'seven_day': 3, 'clear_seven_day_reset': True}
    >>> usage_cache_updates({"five_hour": {"utilization": 9, "resets_at": "R"}})
    {'five_hour': 9, 'five_hour_resets_at': 'R'}
    >>> usage_cache_updates({})
    {}
    """
    updates: dict = {}
    for key, short in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
        if key not in data:
            continue
        window = data.get(key)
        if not isinstance(window, dict):
            updates[f"clear_{short}"] = True
            continue
        utilization = window.get("utilization")
        resets_at = window.get("resets_at")
        if utilization is None and resets_at is None:
            updates[f"clear_{short}"] = True
            continue
        if utilization is not None:
            updates[short] = utilization
        if resets_at is None:
            updates[f"clear_{short}_reset"] = True
        else:
            updates[f"{short}_resets_at"] = resets_at
    return updates



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




def should_refresh(account: dict, buffer_seconds: int | None = None) -> bool:
    """Check if an account's token needs refreshing.

    ``buffer_seconds`` widens the window (the background loop passes its
    own buffer so a token it selected is exchanged, not skipped).

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
    buffer = REFRESH_BUFFER_SECONDS if buffer_seconds is None else buffer_seconds
    return account["expires_at"] < time.time() + buffer


def should_refresh_cc(account: dict, buffer_seconds: int | None = None) -> bool:
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
    buffer = REFRESH_BUFFER_SECONDS if buffer_seconds is None else buffer_seconds
    return cc_expires_at < time.time() + buffer


class CCRefreshOutcome(str, Enum):
    """What ``refresh_cc_token_result`` actually did."""

    ROTATED = "rotated"  # this call exchanged the CC refresh token
    PEER_ROTATED = "peer_rotated"  # another path already holds a newer token
    SKIPPED_NOT_DUE = "skipped_not_due"  # clock gate: token not near expiry
    NO_REFRESH_TOKEN = "no_refresh_token"  # nothing to exchange
    LOCK_HELD = "lock_held"  # another CC refresh is in flight
    FAILED = "failed"  # exchange failed, or the account is gone


@dataclass(frozen=True)
class CCRefreshResult:
    """Explicit result of a CC refresh attempt. ``access_token`` is the
    token now stored for the CC chain after ROTATED / PEER_ROTATED."""

    outcome: CCRefreshOutcome
    access_token: Optional[str] = None
    error: Optional[str] = None

    @property
    def new_token(self) -> bool:
        return self.outcome in (CCRefreshOutcome.ROTATED, CCRefreshOutcome.PEER_ROTATED)


async def refresh_cc_token_result(
    account_id: int, db: Database, *, force: bool = False,
    buffer_seconds: int | None = None,
) -> CCRefreshResult:
    """Refresh the CC token pair and say exactly what happened.

    ``force=True`` bypasses only the ``should_refresh_cc`` clock gate (a
    401 on an unexpired token must still rotate). The no-refresh-token and
    lock-held guards always apply.
    """
    account = db.get_account(account_id)
    if not account:
        return CCRefreshResult(CCRefreshOutcome.FAILED, error="account_not_found")
    if (account.get("provider") or "claude") == "codex" or not account.get("cc_refresh_token"):
        return CCRefreshResult(CCRefreshOutcome.NO_REFRESH_TOKEN)
    if not force and not should_refresh_cc(account, buffer_seconds):
        return CCRefreshResult(CCRefreshOutcome.SKIPPED_NOT_DUE)

    lock = _get_cc_refresh_lock(account_id)
    if lock.locked():
        return CCRefreshResult(CCRefreshOutcome.LOCK_HELD)

    result = await _refresh_token_flow(account_id, db, RefreshMode.CC)
    if not result.success:
        return CCRefreshResult(CCRefreshOutcome.FAILED, error=result.error)
    # Only a real exchange returns the rotated refresh token; the
    # already-refreshed and live-recovery paths return an access token only.
    outcome = (
        CCRefreshOutcome.ROTATED if result.refresh_token else CCRefreshOutcome.PEER_ROTATED
    )
    return CCRefreshResult(outcome, access_token=result.access_token)


async def refresh_cc_token(
    account_id: int, db: Database, *, buffer_seconds: int | None = None,
) -> bool:
    """Refresh CC token pair independently. Updates cc_* columns only.

    Bool wrapper over ``refresh_cc_token_result`` for existing callers:
    True unless the attempt FAILED (not-due, no-refresh-token and
    lock-held keep their historical True).
    """
    result = await refresh_cc_token_result(account_id, db, buffer_seconds=buffer_seconds)
    return result.outcome is not CCRefreshOutcome.FAILED


@dataclass(frozen=True)
class TokenChoice:
    """The access token a usage consumer should send, and its lineage."""

    token: str
    chain: Literal["cc", "primary"]


def cc_token_usable(account: dict, now: float | None = None) -> bool:
    """True when the row's Claude Code access token may be sent.

    The token must be present, its chain must have no recorded dead
    lineage or API rejection, and it must not have expired.

    >>> cc_token_usable({"cc_access_token": "c", "cc_expires_at": 9999999999})
    True
    >>> cc_token_usable({"cc_access_token": "c", "cc_expires_at": 1})
    False
    >>> cc_token_usable({"cc_access_token": "c", "cc_refresh_failure_type": "invalid_grant"})
    False
    >>> cc_token_usable({"cc_access_token": "c", "cc_refresh_failure_type": "auth_rejected"})
    False
    >>> cc_token_usable({"cc_access_token": "c", "cc_refresh_failure_type": "reply_lost"})
    True
    """
    from jacked.api.credential_helpers import CC_TOKEN_UNUSABLE_FAILURES

    if not account.get("cc_access_token"):
        return False
    if account.get("cc_refresh_failure_type") in CC_TOKEN_UNUSABLE_FAILURES:
        return False
    cc_expires_at = account.get("cc_expires_at")
    if cc_expires_at and cc_expires_at <= (time.time() if now is None else now):
        return False
    return True


def choose_usage_token(account: dict) -> Optional[TokenChoice]:
    """Pick the token a usage consumer sends for one Claude account.

    Every consumer that picks a token for a Claude account goes through
    this ONE helper. It prefers a usable Claude Code token
    (``cc_token_usable``), then the primary access token, else None. A dead
    Claude Code chain therefore never stays in service: the consumer falls
    back to the primary token instead of resending a token the API refuses.
    Codex rows have no Anthropic token and return None.

    >>> choose_usage_token({"cc_access_token": "c", "cc_expires_at": 9999999999,
    ...                     "access_token": "p"})
    TokenChoice(token='c', chain='cc')
    >>> choose_usage_token({"cc_access_token": "c", "access_token": "p",
    ...                     "cc_refresh_failure_type": "invalid_grant"})
    TokenChoice(token='p', chain='primary')
    >>> choose_usage_token({"access_token": ""}) is None
    True
    >>> choose_usage_token({"provider": "codex", "access_token": "x"}) is None
    True
    """
    if (account.get("provider") or "claude") == "codex":
        return None
    if cc_token_usable(account):
        return TokenChoice(account["cc_access_token"], "cc")
    if account.get("access_token"):
        return TokenChoice(account["access_token"], "primary")
    return None


def token_chain(account: dict, token: Optional[str]) -> Optional[str]:
    """Name the lineage that owns ``token`` in ``account``.

    Returns "cc" or "primary" for a token the row holds, "primary" for
    None (fetch_usage then sends the primary token), and None for a token
    the row does not hold (for example a live-store token).

    >>> row = {"access_token": "p", "cc_access_token": "c"}
    >>> token_chain(row, "c"), token_chain(row, "p"), token_chain(row, None)
    ('cc', 'primary', 'primary')
    >>> token_chain(row, "z") is None
    True
    """
    if token is None or token == account.get("access_token"):
        return "primary"
    if token == account.get("cc_access_token"):
        return "cc"
    return None


async def _active_account_id() -> Optional[int]:
    """Return the resolved active account id off the event loop, or None.

    The Claude Code refresh token of the active account is shared with the
    running Claude Code sessions; jacked never rotates it (invariant I2).
    """
    try:
        from jacked.api.credential_helpers import read_active_account_id

        return await asyncio.to_thread(read_active_account_id)
    except Exception:
        logger.debug("active account lookup failed", exc_info=True)
        return None


class RefreshOutcome(str, Enum):
    """What one primary refresh attempt actually did."""

    EXCHANGED = "exchanged"
    SKIPPED_NOT_DUE = "skipped_not_due"
    FAILED = "failed"


async def refresh_account_token_result(
    account_id: int, db: Database, *, buffer_seconds: int | None = None,
) -> RefreshOutcome:
    """Refresh an account's primary token if needed, and say what happened.

    EXCHANGED means a token exchange completed and was stored. SKIPPED_NOT_DUE
    means no exchange was needed: the token is outside the refresh window,
    the account has no primary refresh token, or another path had already
    rotated it. FAILED means an exchange was due and did not succeed.
    """
    account = db.get_account(account_id)
    if not account:
        return RefreshOutcome.FAILED
    if not should_refresh(account, buffer_seconds):
        return RefreshOutcome.SKIPPED_NOT_DUE
    if not account.get("refresh_token"):
        return RefreshOutcome.SKIPPED_NOT_DUE

    result = await _refresh_token_flow(account_id, db, RefreshMode.PRIMARY)

    if result.success:
        return RefreshOutcome.EXCHANGED if result.exchanged else RefreshOutcome.SKIPPED_NOT_DUE

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
        return RefreshOutcome.FAILED

    if result.error in NETWORK_FAILURE_TYPES:
        db.record_account_error(
            account_id, "Network error during token refresh",
            increment_failures=False)

    return RefreshOutcome.FAILED


async def refresh_account_token(account_id: int, db: Database) -> bool:
    """Refresh an account's primary token if needed.

    Returns False only when a due exchange failed. Use
    ``refresh_account_token_result`` to tell an exchange from a skip.
    """
    outcome = await refresh_account_token_result(account_id, db)
    return outcome is not RefreshOutcome.FAILED


async def _try_refresh_on_429(
    account_id: int, db: Database, state: dict, stale_token: Optional[str] = None,
) -> str | None:
    """Rotate the lineage that owns ``stale_token`` to clear a per-token limit.

    The Claude Code chain rotates only for a non-active account whose chain
    is alive; the active account's cc refresh token belongs to the running
    Claude Code sessions (invariant I2). Every other case rotates the
    primary chain through the circuit breaker.
    """
    account = db.get_account(account_id) or {}
    if token_chain(account, stale_token) == "cc" and await _active_account_id() != account_id:
        result = await _refresh_token_flow(account_id, db, RefreshMode.CC_OR_PRIMARY_429)
    else:
        result = await _refresh_token_flow(account_id, db, RefreshMode.PRIMARY_CIRCUIT_BREAKER)
    if not result.success or not result.access_token or result.access_token == stale_token:
        return None
    return result.access_token


async def _recover_rejected_cc_token(
    account_id: int, db, account: dict, stale_token: str,
) -> str | None:
    """Recover from a 401/403 on the row's Claude Code access token.

    The recovery stays on the lineage that owns the failing token, or falls
    through to the primary token. It never takes the primary circuit-breaker
    path, which used to renew the primary chain once per poll cycle.

    1. A non-active account with a live cc chain rotates that chain.
    2. Otherwise the cc chain records ``auth_rejected`` (unless it already
       records a dead lineage), so ``choose_usage_token`` stops picking the
       refused token, and the primary access token is returned. A primary
       token inside its refresh window is renewed first (a due renewal).
    """
    from jacked.api.credential_helpers import cc_chain_dead

    if (
        account.get("cc_refresh_token")
        and not cc_chain_dead(account)
        and await _active_account_id() != account_id
    ):
        result = await _refresh_token_flow(account_id, db, RefreshMode.CC)
        if result.success and result.access_token and result.access_token != stale_token:
            return result.access_token

    fresh = db.get_account(account_id) or account
    if not cc_chain_dead(fresh) and fresh.get("cc_access_token") == stale_token:
        db.update_account(account_id, **_breaker_updates("cc", "auth_rejected"))
        logger.warning(
            "Account %d: the API refused the cc access token; recorded on the cc "
            "chain, falling back to the primary token",
            account_id,
        )

    if should_refresh(fresh):
        await refresh_account_token_result(account_id, db)
        fresh = db.get_account(account_id) or fresh
    primary = fresh.get("access_token")
    if primary and primary != stale_token and not should_refresh(fresh):
        return primary
    return None


async def _try_refresh_primary_token(
    account_id: int,
    db,
    stale_token: str | None = None,
) -> str | None:
    """Recover from a 401/403 on ``stale_token``. Returns a new token or None.

    Only the lineage that owns the failing token is touched:

    - the row's cc access token: ``_recover_rejected_cc_token``;
    - a token the row does not hold (a live-store token): the primary
      access token when it is current, else a primary renewal;
    - the primary access token (or None): a primary renewal through the
      circuit breaker.
    """
    account = db.get_account(account_id)
    if not account:
        return None
    owner = token_chain(account, stale_token)
    if owner == "cc":
        return await _recover_rejected_cc_token(account_id, db, account, stale_token)
    if owner is None:
        primary = account.get("access_token")
        if primary and not should_refresh(account):
            return primary
    result = await _refresh_token_flow(account_id, db, RefreshMode.PRIMARY_CIRCUIT_BREAKER)
    return result.access_token if result.success else None


async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
    manual: bool = False,
    _retry_depth: int = 0,
    post_ping: bool = False,
) -> Optional[dict]:
    """Fetch usage data from the Anthropic Usage API (design doc section 4f).

    Uses per-account coordinator state for rate limiting.  The hard ceiling
    (_USAGE_RATE_LIMIT_CEILING) is always enforced to prevent 429s from the
    upstream API; manual=True gets the shorter _USAGE_MANUAL_FLOOR_SECONDS
    floor instead of a full bypass.  Only the single bounded 401/429 retry
    (_retry_depth > 0) skips pacing, and it never resets pacing state.

    post_ping=True is the window keeper's read-back right after a
    successful ping: it skips the ceiling but keeps the manual floor,
    measured against this process's own last attempt only (the keeper has
    just stamped usage_cached_at from the ping's reset header, so the DB
    stamp says nothing about the last upstream usage call). The 429
    backoff still applies, and pacing state is stamped exactly as for any
    other attempt.

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
    if _retry_depth == 0 and post_ping:
        elapsed = now - state["last_fetched_at"]
        if elapsed < _USAGE_MANUAL_FLOOR_SECONDS:
            logger.debug(
                f"Post-ping usage floor for account {account_id}: "
                f"{int(elapsed)}s < {_USAGE_MANUAL_FLOOR_SECONDS}s, "
                f"returning cached"
            )
            return {"_cached": True}
    elif _retry_depth == 0:
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
                db.update_account_usage_cache(
                    account_id, raw=data, **usage_cache_updates(data),
                )

                state["last_fetched_at"] = time.time()
                state["consecutive_429s"] = 0
                state["last_429_token_fp"] = None

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
                        await asyncio.to_thread(
                            reconcile_credentials_from_live_store, account_id, db
                        )
                        refreshed_acct = db.get_account(account_id)
                        if refreshed_acct:
                            # Reconcile writes the cc_* columns, so pick the
                            # token through the shared chooser.
                            choice = choose_usage_token(refreshed_acct)
                            live_token = choice.token if choice else None
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
                # A 429 backs off; it does not rotate. Count consecutive
                # 429s per token: a new token starts a new count.
                fp = _token_fingerprint(token)
                if state.get("last_429_token_fp") != fp:
                    state["consecutive_429s"] = 0
                    state["last_429_token_fp"] = fp
                state["consecutive_429s"] = state.get("consecutive_429s", 0) + 1
                n = state["consecutive_429s"]

                # One rotation of the lineage that owns this token is allowed
                # only after N consecutive 429s on the same token, at most
                # once per guard interval, and never from a retry frame.
                rotation_due = (
                    time.time() - state.get("last_429_rotation_at", 0.0)
                    > _usage_429_rotation_interval(db)
                )
                if (
                    _retry_depth == 0
                    and n >= _USAGE_429_ROTATION_MIN_CONSECUTIVE
                    and rotation_due
                ):
                    # Stamp at attempt time (success or not): a failed
                    # rotation still hit the token endpoint.
                    state["last_429_rotation_at"] = time.time()
                    fresh_token = await _try_refresh_on_429(
                        account_id, db, state, stale_token=token,
                    )
                    if fresh_token:
                        state["consecutive_429s"] = 0
                        state["last_429_token_fp"] = None
                        logger.info(
                            "Account %d: retrying usage fetch with fresh token "
                            "after %d consecutive 429s",
                            account_id, n,
                        )
                        # The retry rides _retry_depth=1 through the pacing
                        # check; re-stamp last_fetched_at afterwards
                        # (success or not) so a 429 retry can never
                        # un-pace the account.
                        result = await fetch_usage(
                            account_id, db, access_token=fresh_token, _retry_depth=1,
                        )
                        state["last_fetched_at"] = time.time()
                        return result

                # Escalating backoff. A Retry-After header is honored when it
                # asks for longer than the escalation step.
                base_backoff = min(_USAGE_RATE_LIMIT_CEILING * (2 ** (n - 1)), 900)
                retry_after = resp.headers.get("retry-after")
                try:
                    backoff_seconds = min(
                        max(int(retry_after), base_backoff),
                        _USAGE_RETRY_AFTER_MAX_SECONDS,
                    ) if retry_after is not None else base_backoff
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
    allow_refresh: bool = True,
) -> Optional[dict]:
    """Fetch profile from the Anthropic Profile API (design doc section 4e).

    Updates the account row through ``_update_profile_metadata``: plan
    metadata (subscription_type, rate_limit_tier, has_extra_usage,
    organization_name) and the stored identity (account_uuid, profile_json,
    profile_fetched_at). Nothing is applied when the profile does not
    belong to the row.

    When ``allow_refresh`` is False, a 401/403 returns None and no token
    refresh occurs.

    Returns the raw profile response dict, or None on failure. A profile
    that was fetched but not applied (owner mismatch) is still returned.
    """
    data, _applied = await _fetch_profile_applied(
        account_id, db, access_token=access_token,
        _refresh_depth=_refresh_depth, allow_refresh=allow_refresh,
    )
    return data


async def _fetch_profile_applied(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
    _refresh_depth: int = 0,
    allow_refresh: bool = True,
) -> tuple[Optional[dict], bool]:
    """Fetch the profile and apply it, as ``fetch_profile`` does.

    Returns ``(data, applied)``: ``data`` is the raw profile dict or None on
    failure, and ``applied`` is the verdict of ``_update_profile_metadata``
    (False when nothing was fetched or the profile was rejected).
    """
    account = db.get_account(account_id)
    if not account:
        return None, False

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
                applied = _update_profile_metadata(account_id, data, db)
                if applied:
                    logger.info("Profile applied for account %d", account_id)
                else:
                    logger.info("Profile rejected for account %d", account_id)
                return data, applied

            if (
                resp.status_code in (401, 403)
                and allow_refresh
                and _refresh_depth < 1
            ):
                fresh = await _try_refresh_primary_token(
                    account_id, db, stale_token=token,
                )
                if fresh:
                    return await _fetch_profile_applied(
                        account_id, db, access_token=fresh,
                        _refresh_depth=_refresh_depth + 1,
                    )

            logger.warning(
                "Profile fetch HTTP %d for account %d", resp.status_code, account_id
            )
            return None, False

    except Exception as e:
        logger.warning(
            "Profile fetch failed for account %d (%s)", account_id, type(e).__name__
        )
        return None, False


PROFILE_BACKFILL_PAUSE_SECONDS = 1.0


async def backfill_missing_profiles(
    db: Database,
    *,
    pause_seconds: float = PROFILE_BACKFILL_PAUSE_SECONDS,
    counts: Optional[dict] = None,
) -> Optional[dict]:
    """Fetch the profile for each Claude account that has no stored profile.

    This is a one-shot startup task. It examines the Claude rows that are not
    deleted (active and inactive) where profile_json is NULL. It calls
    ``fetch_profile(allow_refresh=False)`` for one row at a time and pauses
    ``pause_seconds`` between the calls. A 401/403 never refreshes a token.
    A row whose primary token has expired is skipped without a request.

    At the end, it logs ONE coverage line with counts only (no email, no
    token, no uuid). It never raises (cancellation is not caught). It
    returns the counts, or None when the pass could not run.

    When the caller gives a ``counts`` dict, the pass fills it in place as
    it runs. A caller that cancels the pass (for example on a timeout) can
    then log the partial counts with ``log_identity_coverage``.
    """
    try:
        rows = db.list_claude_accounts_missing_profile()
    except Exception as e:
        logger.warning(
            "Profile backfill skipped: account list failed (%s)", type(e).__name__
        )
        return None

    if counts is None:
        counts = {}
    counts.update(_empty_backfill_counts())
    counts["candidates"] = len(rows)
    now = time.time()
    attempted = 0
    for row in rows:
        account_id = row.get("id")
        expires_at = row.get("expires_at")
        if expires_at and expires_at <= now:
            counts["skipped_expired"] += 1
            continue
        if attempted and pause_seconds > 0:
            await asyncio.sleep(pause_seconds)
        attempted += 1
        try:
            data, applied = await _fetch_profile_applied(
                account_id, db, allow_refresh=False
            )
            if data is None:
                counts["failed"] += 1
                continue
            if applied:
                counts["fetched"] += 1
            else:
                # _update_profile_metadata refused it (owner mismatch) and
                # already logged the row id and the field name.
                counts["rejected"] += 1
        except Exception as e:
            counts["failed"] += 1
            logger.warning(
                "Profile backfill failed for account %s (%s)",
                account_id, type(e).__name__,
            )

    log_identity_coverage(db, counts)
    return counts


def _empty_backfill_counts() -> dict:
    """Return the zeroed backfill counters.

    >>> _empty_backfill_counts()["fetched"]
    0
    """
    return {
        "candidates": 0,
        "fetched": 0,
        "rejected": 0,
        "failed": 0,
        "skipped_expired": 0,
    }


def log_identity_coverage(
    db: Database, counts: Optional[dict] = None, *, timed_out: bool = False
) -> None:
    """Log ONE values-free Claude identity coverage line.

    The line holds counts only (no email, no token, no uuid). ``counts`` are
    the backfill counters (missing keys count as 0). ``timed_out`` marks a
    pass that was cancelled before it finished. This function never raises.
    """
    merged = _empty_backfill_counts()
    merged.update(counts or {})
    label = "backfill timed out" if timed_out else "backfill"
    try:
        cov = db.claude_identity_coverage()
        logger.info(
            "Claude identity coverage: total=%d with_uuid=%d without_uuid=%d "
            "with_profile=%d without_profile=%d (%s candidates=%d "
            "fetched=%d rejected=%d failed=%d skipped_expired=%d)",
            cov["total"], cov["with_uuid"], cov["without_uuid"],
            cov["with_profile"], cov["without_profile"], label,
            merged["candidates"], merged["fetched"], merged["rejected"],
            merged["failed"], merged["skipped_expired"],
        )
    except Exception as e:
        logger.warning(
            "Claude identity coverage unavailable (%s)", type(e).__name__
        )


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
        # A signed-in auth.json says nothing about whether usage is readable:
        # the usage fetch's error and failure counter stay until a successful
        # fetch clears them (fetch_codex_usage -> clear_account_errors).
        db.mark_account_validated(account_id)
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
                # A profile 200 proves the token, not that usage is readable:
                # the usage path's error and failure counter stay until a
                # successful usage fetch clears them (clear_account_errors).
                db.mark_account_validated(account_id)
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
                        # Same rule as the first-try 200 above.
                        db.mark_account_validated(account_id)
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


async def refresh_all_expiring_tokens(
    buffer_seconds: int = 14400, db: Optional[Database] = None,
) -> dict:
    """Refresh all account tokens expiring within buffer_seconds.

    Called by background task to proactively keep tokens fresh.
    Skips API key accounts (no refresh_token) and inactive accounts.
    Uses per-account locks to avoid collisions with manual refresh calls.

    Args:
        buffer_seconds: Refresh tokens expiring within this many seconds (default 4 hours)
        db: The database to use. None opens the default database; give an
            explicit one in tests and examples.

    Returns:
        dict with counts: {"checked": N, "refreshed": N, "skipped": N, "failed": N,
                           "cc_refreshed": N, "cc_failed": N}
        "refreshed" counts real exchanges only; a token inside the buffer that
        is not yet inside its refresh window counts as "skipped".

    The example uses an in-memory database and stubs the active-account
    read, so it touches no real database and no credential store even when
    it runs outside pytest.

    >>> import asyncio
    >>> from unittest.mock import patch
    >>> with patch("jacked.api.credential_helpers.read_active_account_id",
    ...            return_value=None):
    ...     counts = asyncio.run(
    ...         refresh_all_expiring_tokens(db=Database(":memory:")))
    >>> sorted(counts)
    ['cc_failed', 'cc_refreshed', 'checked', 'failed', 'refreshed', 'skipped']
    >>> counts["checked"]
    0
    """
    if db is None:
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
            await asyncio.to_thread(
                reconcile_credentials_from_live_store, active_id, db
            )
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
                    outcome = await asyncio.wait_for(
                        refresh_account_token_result(
                            account_id, db, buffer_seconds=buffer_seconds,
                        ),
                        timeout=60,
                    )
                except asyncio.TimeoutError:
                    logger.error(
                        "Account %d: refresh timed out after 60s — counting as failed",
                        account_id,
                    )
                    outcome = RefreshOutcome.FAILED
                except Exception:
                    logger.exception(
                        "Account %d: refresh raised unexpectedly", account_id,
                    )
                    outcome = RefreshOutcome.FAILED
                # Count only real exchanges as refreshed: a token inside the
                # buffer but outside the refresh window is a skip, not a
                # refresh.
                if outcome is RefreshOutcome.EXCHANGED:
                    result["refreshed"] += 1
                elif outcome is RefreshOutcome.SKIPPED_NOT_DUE:
                    result["skipped"] += 1
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

        if should_refresh_cc(account, buffer_seconds):
            cc_success = await refresh_cc_token(
                account_id, db, buffer_seconds=buffer_seconds,
            )
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

    A network breaker in its cooldown, or an unreachable token endpoint
    host, defers the account to a later pass (counted as ``deferred``).

    Returns dict with counts: {"checked": N, "healed": N, "confirmed_invalid": N,
    "deferred": N}
    """
    db = Database()
    result = {"checked": 0, "healed": 0, "confirmed_invalid": 0, "deferred": 0}
    # Probed once per pass, and only when a forced renewal is about to run.
    network_up: Optional[bool] = None

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
            # A network breaker describes the network, not the credential.
            # While its cooldown runs, or while the network is unreachable,
            # a forced renewal can only fail again, and a reply lost on the
            # way back can consume the refresh token. Leave the breaker and
            # the account for a later pass.
            failure_type = account.get("refresh_failure_type")
            last_failed = account.get("refresh_last_failed_at")
            if failure_type in NETWORK_FAILURE_TYPES and last_failed is not None:
                cooldown = breaker_cooldown(failure_type)
                remaining = cooldown - (time.time() - last_failed)
                if remaining > 0:
                    logger.info(
                        "Account %d: heal waits for the %s breaker (%ds remaining)",
                        account_id, failure_type, int(remaining),
                    )
                    result["deferred"] += 1
                    continue
            if network_up is None:
                network_up = await _network_reachable()
            if not network_up:
                logger.info(
                    "Account %d: heal deferred; the token endpoint host is unreachable",
                    account_id,
                )
                result["deferred"] += 1
                continue
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
                await asyncio.to_thread(
                    reconcile_credentials_from_live_store, account_id, db
                )
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
            from jacked.api.credential_helpers import cc_chain_dead

            if cc_chain_dead(account) or (
                not account.get("cc_refresh_token") and account.get("cc_access_token")
            ):
                logger.info(
                    "Account %d healed but CC token needs re-authorization",
                    account_id,
                )
        else:
            result["confirmed_invalid"] += 1

    if result["checked"] > 0:
        logger.info(
            "Heal sweep: checked=%d, healed=%d, confirmed_invalid=%d, deferred=%d",
            result["checked"],
            result["healed"],
            result["confirmed_invalid"],
            result["deferred"],
        )
    return result


_NETWORK_PROBE_TIMEOUT_SECONDS = 5.0


async def _network_reachable() -> bool:
    """Return True when the token endpoint host answers a cheap HEAD request.

    Any HTTP status counts as reachable: the probe tests the network path,
    not the endpoint. A DNS failure, a refused connection or a timeout
    counts as unreachable. The probe sends no credential.
    """
    url = httpx.URL(TOKEN_URL)
    try:
        async with httpx.AsyncClient(timeout=_NETWORK_PROBE_TIMEOUT_SECONDS) as client:
            await client.head(f"{url.scheme}://{url.host}/")
        return True
    except Exception as exc:
        logger.debug("Network probe failed (%s)", type(exc).__name__)
        return False
