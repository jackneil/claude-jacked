"""Auth routes -- account management, credential switching, session queries.

Handles OAuth flow initiation/polling, account CRUD, token refresh,
usage cache refresh, account validation, credential switching, and
session-account queries.
"""

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jacked.web.auth import (
    fetch_usage,
    refresh_account_token,
    validate_account,
)
from jacked.web.oauth import OAuthFlow, get_flow

logger = logging.getLogger(__name__)

router = APIRouter()

# Server-side guard: only one bulk usage refresh at a time. We track when the
# lock was acquired so a stuck holder doesn't wedge all future refreshes
# behind a 409. If the holder has been in there longer than the stale-watchdog
# threshold we treat the lock as orphaned and allow a fresh attempt.
_bulk_refresh_lock = asyncio.Lock()
_bulk_refresh_acquired_at: float = 0.0
# 0.41.23: track the task holding the lock so the stale-lock guard can
# cancel it before swapping in a fresh lock.
_bulk_refresh_task: "asyncio.Task | None" = None
_BULK_REFRESH_STALE_AFTER = (
    180.0  # generous: a concurrent pass worst-cases at ~_BULK_PER_ACCOUNT_TIMEOUT
)
# 0.41.25: max seconds per account in bulk refresh before declaring it hung.
# Happy path is 1-2s; 10s is plenty of slack for a slow Anthropic refresh.
# If refresh takes >10s something's wrong upstream — waiting longer doesn't help.
_BULK_PER_ACCOUNT_TIMEOUT = 10.0
# Accounts fetch concurrently — each uses its own OAuth token, and Anthropic's
# usage-API limit is per-account/per-token, so cross-account parallelism is
# safe. The cap exists only to bound simultaneous outbound sockets and
# OAuth-refresh fan-out on the rare all-accounts-401 path.
_BULK_MAX_CONCURRENCY = 8
# Route-level upper bounds for single-account endpoints so a wedged upstream
# call can't hold the HTTP request open indefinitely — the client gets a
# deterministic 504 instead of a spinner. Looser than the bulk per-account
# timeout because these are user-initiated one-offs that may ride out a
# lock-recovery inside web/auth.py.
_SINGLE_USAGE_TIMEOUT = 60.0
_TOKEN_REFRESH_TIMEOUT = 45.0
_VALIDATE_TIMEOUT = 60.0

# Pass-throttle for bulk refresh: if a new bulk request arrives within this
# many seconds of the previous pass COMPLETION, serve current DB values
# without touching the Anthropic API. A runaway client loop (2026-06
# incident: a dashboard tab drove back-to-back bulk passes at ~25
# fetches/min for hours) then only ever hits the DB.
_BULK_PASS_MIN_INTERVAL = 30.0
_last_bulk_completed_at: float = 0.0


def _gateway_timeout(message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_504_GATEWAY_TIMEOUT,
        content={"error": {"message": message, "code": code}},
    )


def reset_locks() -> None:
    """Rebind module-level locks to the current event loop.

    The tray can restart uvicorn in a new thread (see service/tray.py
    _on_restart). The new thread runs a new event loop, but this module
    stays imported, so the old asyncio.Lock is still bound to the dead
    loop. Any acquire on the new loop raises
    "bound to a different event loop" and callers pile up as waiters.
    Called from the FastAPI lifespan on startup so each loop gets a
    fresh lock.

    If the old lock appears held here, it means a previous-loop task
    was still in flight when the tray restart landed — the new lock
    won't serialize against it. We log loudly so post-mortem can spot
    the window. The old task's writes are on the old (dying) loop and
    will complete or cancel on their own.
    """
    global _bulk_refresh_lock, _bulk_refresh_acquired_at, _bulk_refresh_task
    global _last_bulk_completed_at
    if _bulk_refresh_lock.locked():
        logger.warning(
            "reset_locks: bulk_refresh_lock was held at rebind "
            "(previous-loop task still in flight); new lock will not "
            "serialize against it"
        )
    _bulk_refresh_lock = asyncio.Lock()
    _bulk_refresh_acquired_at = 0.0
    _bulk_refresh_task = None
    _last_bulk_completed_at = 0.0


# --- Pydantic v2 request/response models ---


class ModelUsage(BaseModel):
    """Per-model weekly usage cap.

    ``label`` is the provider's display name (e.g. "Fable", "GPT-5.3-Codex-Spark").
    ``severity`` / ``is_active`` come from Claude's ``limits`` array — ``is_active``
    flags the single binding constraint (surfaced as the inline binding bar).
    """

    utilization: float = 0
    resets_at: Optional[str] = None
    label: Optional[str] = None
    severity: Optional[str] = None
    is_active: bool = False


class ExtraUsage(BaseModel):
    """Extra usage credits information."""

    is_enabled: bool = False
    monthly_limit: Optional[float] = None
    used_credits: Optional[float] = None
    utilization: Optional[float] = None


class AccountUsage(BaseModel):
    """Usage statistics for an account with per-model breakdowns."""

    five_hour: float = 0
    seven_day: float = 0
    five_hour_resets_at: Optional[str] = None
    seven_day_resets_at: Optional[str] = None
    per_model: Optional[dict[str, ModelUsage]] = None
    # The per-model cap to surface inline (under the 5h/7d bars), shown for every
    # account that reports one at any level: the model the provider flags
    # is_active, else the highest-utilization model. None only when the account
    # reports no per-model cap. Selection lives in menubar_summary.binding_model.
    binding_model: Optional[ModelUsage] = None
    extra_usage: Optional[ExtraUsage] = None


class AccountResponse(BaseModel):
    """Account data with computed fields for API responses."""

    id: int
    provider: str = "claude"
    email: str
    organization_uuid: Optional[str] = None
    organization_name: Optional[str] = None
    display_name: Optional[str] = None
    expires_at: int
    scopes: Optional[str] = None
    subscription_type: Optional[str] = None
    rate_limit_tier: Optional[str] = None
    has_extra_usage: bool = False
    priority: int = 0
    is_active: bool = True
    is_deleted: bool = False
    last_used_at: Optional[str] = None
    cached_usage_5h: Optional[float] = None
    cached_usage_7d: Optional[float] = None
    cached_5h_resets_at: Optional[str] = None
    cached_7d_resets_at: Optional[str] = None
    usage_cached_at: Optional[int] = None
    last_error: Optional[str] = None
    last_error_at: Optional[str] = None
    consecutive_failures: int = 0
    last_validated_at: Optional[int] = None
    validation_status: str = "unknown"
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    # CC (Claude Code) token status (computed, not stored in DB)
    cc_expires_at: Optional[int] = None
    has_cc_token: bool = False
    cc_needs_auth: bool = False
    # Refresh capability — UI hides countdown when token auto-refreshes
    has_refresh_token: bool = False
    has_cc_refresh_token: bool = False
    # Computed / enriched fields
    is_default: bool = False
    is_expired: bool = False
    expires_in_seconds: int = 0
    usage: Optional[AccountUsage] = None


class AccountPatchRequest(BaseModel):
    display_name: Optional[str] = Field(None, max_length=50)
    is_active: Optional[bool] = None


class ReorderRequest(BaseModel):
    order: list[int]


class FlowStatusResponse(BaseModel):
    status: str
    flow_id: str
    account_id: Optional[int] = None
    email: Optional[str] = None
    organization_name: Optional[str] = None
    redirected_from_account_id: Optional[int] = None
    error: Optional[str] = None
    cc_flow_id: Optional[str] = None
    auth_url: Optional[str] = None
    mode: Optional[str] = None
    submit_error: Optional[str] = None
    purpose: Optional[str] = None
    target_account_id: Optional[int] = None
    target_email: Optional[str] = None
    target_org_name: Optional[str] = None
    browser_mode: Optional[str] = None
    browser_name: Optional[str] = None
    reopen_error: Optional[str] = None
    activation_status: Optional[str] = None
    activation_operation_id: Optional[str] = None
    activation_message: Optional[str] = None


def _flow_status_response(status_data: dict) -> FlowStatusResponse:
    """Shape an OAuthFlow.get_status() dict into the response model."""
    return FlowStatusResponse(
        status=status_data["status"],
        flow_id=status_data["flow_id"],
        account_id=status_data.get("account_id"),
        email=status_data.get("email"),
        organization_name=status_data.get("organization_name"),
        redirected_from_account_id=status_data.get("redirected_from_account_id"),
        error=status_data.get("error"),
        cc_flow_id=status_data.get("cc_flow_id"),
        auth_url=status_data.get("auth_url"),
        mode=status_data.get("mode"),
        submit_error=status_data.get("submit_error"),
        purpose=status_data.get("purpose"),
        target_account_id=status_data.get("target_account_id"),
        target_email=status_data.get("target_email"),
        target_org_name=status_data.get("target_org_name"),
        browser_mode=status_data.get("browser_mode"),
        browser_name=status_data.get("browser_name"),
        reopen_error=status_data.get("reopen_error"),
        activation_status=status_data.get("activation_status"),
        activation_operation_id=status_data.get("activation_operation_id"),
        activation_message=status_data.get("activation_message"),
    )


class SubmitCodeRequest(BaseModel):
    code: str


_LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost", "testclient")


def _manual_oauth(request: Request, remote: bool) -> bool:
    """Whether an OAuth flow must use manual code entry.

    Manual mode applies when the client asked for it (``remote=true``) or
    when the request comes from a non-loopback address: for a remote user,
    the server's own browser and localhost callback are useless.
    ("testclient" is Starlette's TestClient — local by definition.)
    """
    if remote:
        return True
    client = request.client
    host = client.host if client else ""
    return host not in _LOOPBACK_HOSTS


def _local_mutation_allowed(request: Request) -> bool:
    """Credential mutation is local-only until remote auth and TLS exist."""
    client = request.client
    host = client.host.lower() if client and client.host else ""
    return host in _LOOPBACK_HOSTS


class RefreshResponse(BaseModel):
    success: bool
    error: Optional[str] = None


class ValidateResponse(BaseModel):
    valid: bool
    error: Optional[str] = None


class UsageRefreshResponse(BaseModel):
    success: bool
    account_id: int
    cached_usage_5h: Optional[float] = None
    cached_usage_7d: Optional[float] = None


class BulkUsageRefreshResponse(BaseModel):
    refreshed: int
    failed: int
    # Number of accounts served from the DB by the pass-throttle (the
    # request arrived within _BULK_PASS_MIN_INTERVAL of the previous
    # pass's completion). 0 for a real API pass.
    throttled: int = 0
    results: list[dict] = []


class UseAccountResponse(BaseModel):
    status: str
    email: str
    operation_id: Optional[str] = None
    storage: Optional[dict] = None
    committed_authority: Optional[dict] = None
    existing_sessions: Optional[str] = None
    provider_verification: Optional[str] = None
    message: Optional[str] = None


class ActiveCredentialResponse(BaseModel):
    account_id: Optional[int] = None
    email: Optional[str] = None
    state: str = "unknown"
    evidence: list[str] = Field(default_factory=list)


_ACTION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")


def _switch_http_status(outcome) -> int:
    from jacked.credentials.models import SwitchOutcome

    if outcome in {SwitchOutcome.COMMITTED, SwitchOutcome.COMMITTED_DEGRADED}:
        return status.HTTP_200_OK
    if outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED:
        return status.HTTP_202_ACCEPTED
    if outcome is SwitchOutcome.INTERACTIVE_REQUIRED:
        return status.HTTP_428_PRECONDITION_REQUIRED
    if outcome in {
        SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS,
        SwitchOutcome.BUSY,
        SwitchOutcome.CONCURRENT_WRITE,
        SwitchOutcome.DIVERGED,
        SwitchOutcome.RESTART_REQUIRED,
    }:
        return status.HTTP_409_CONFLICT
    if outcome in {SwitchOutcome.UNUSABLE, SwitchOutcome.UNSUPPORTED}:
        return status.HTTP_422_UNPROCESSABLE_CONTENT
    return status.HTTP_503_SERVICE_UNAVAILABLE


def _switch_content(result, email: str) -> dict:
    return {
        "status": result.outcome.value,
        "email": email,
        "operation_id": result.operation_id,
        "storage": {
            "account_id": result.storage.account_id,
            "state": result.storage.state,
        },
        "committed_authority": {
            "account_id": result.committed_authority.account_id,
            "state": result.committed_authority.state,
        },
        "existing_sessions": result.existing_session_activation.value,
        "provider_verification": result.provider_verification.value,
        "message": result.message,
    }


def _claim_switch_action(db, request: Request, account_id: int):
    supplied = request.headers.get("X-Jacked-Action-Id")
    action_id = supplied or str(uuid.uuid4())
    operation_id = request.headers.get("X-Jacked-Operation-Id") or action_id
    if not _ACTION_ID_PATTERN.fullmatch(action_id) or not _ACTION_ID_PATTERN.fullmatch(
        operation_id
    ):
        return action_id, operation_id, "invalid", None
    session_key = request.headers.get("X-Jacked-Page-Session") or ""
    if not _ACTION_ID_PATTERN.fullmatch(session_key):
        return action_id, operation_id, "invalid_session", None
    request_digest = hashlib.sha256(
        f"use-account-v2:{account_id}".encode("utf-8")
    ).hexdigest()
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    state, stored = db.claim_auth_action(
        action_id,
        session_key=session_key,
        action="use_account",
        request_digest=request_digest,
        operation_id=operation_id,
        expires_at=expires_at,
    )
    return action_id, operation_id, state, stored


# --- Helpers ---


def _parse_usage_details(
    raw_json: Optional[str],
) -> tuple[Optional[dict[str, ModelUsage]], Optional[str], Optional[ExtraUsage]]:
    """Parse cached_usage_raw JSON into (per_model dict, binding key, ExtraUsage).

    The binding key names the per_model entry to surface as the inline bar (the
    active or notably-high model), or None. Selection lives in the shared
    ``menubar_summary.binding_model`` so every surface agrees.

    >>> _parse_usage_details(None)
    (None, None, None)
    >>> _parse_usage_details("not json")
    (None, None, None)
    >>> pm, bk, eu = _parse_usage_details('{"seven_day_sonnet": {"utilization": 42.5, "resets_at": "2025-02-08T00:00:00Z"}}')
    >>> pm["sonnet"].utilization, bk, eu is None
    (42.5, 'sonnet', True)
    >>> pm, bk, _ = _parse_usage_details('{"limits": [{"kind": "weekly_scoped", "percent": 92, "severity": "critical", "is_active": true, "scope": {"model": {"display_name": "Fable"}}}]}')
    >>> pm["fable"].utilization, pm["fable"].label, bk
    (92.0, 'Fable', 'fable')
    """
    if not raw_json:
        return None, None, None
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None, None, None

    # Per-model caps — shared parser handles Claude (limits array), Codex
    # (rateLimitsByLimitId), and legacy seven_day_* fallback. Ordered by
    # utilization descending; keyed by model slug. Binding selection uses the
    # same shared rule the WS/menu-bar surfaces use. Wrapped defensively: this
    # feeds GET /api/auth/accounts, so a single malformed cached payload must
    # degrade to "no per-model data", never 500 the whole account list.
    from jacked.service.menubar_summary import binding_model, parse_per_model

    try:
        parsed = parse_per_model(data)
        per_model: dict[str, ModelUsage] = {}
        for m in parsed:
            per_model[m["key"]] = ModelUsage(
                utilization=m.get("utilization") or 0,
                resets_at=m.get("resets_at"),
                label=m.get("label"),
                severity=m.get("severity"),
                is_active=bool(m.get("is_active")),
            )
        bm = binding_model(parsed)
        binding_key = bm["key"] if bm else None

        # Extra usage credits
        extra_raw = data.get("extra_usage")
        extra = None
        if isinstance(extra_raw, dict):
            raw_limit = extra_raw.get("monthly_limit")
            raw_used = extra_raw.get("used_credits")
            extra = ExtraUsage(
                is_enabled=extra_raw.get("is_enabled", False),
                monthly_limit=raw_limit / 100
                if isinstance(raw_limit, (int, float))
                else None,
                used_credits=raw_used / 100
                if isinstance(raw_used, (int, float))
                else None,
                utilization=extra_raw.get("utilization"),
            )
    except (TypeError, ValueError, AttributeError, KeyError):
        logger.warning(
            "Malformed cached_usage_raw — dropping per-model details", exc_info=True
        )
        return None, None, None

    return (per_model or None), binding_key, extra


def _build_account_usage(row: dict) -> Optional[AccountUsage]:
    """Build AccountUsage from a DB account row if usage data exists.

    >>> _build_account_usage({}) is None
    True
    >>> u = _build_account_usage({"cached_usage_5h": 25.0, "cached_usage_7d": 60.0})
    >>> u.five_hour
    25.0
    """
    if row.get("cached_usage_5h") is None and row.get("cached_usage_7d") is None:
        return None
    per_model, binding_key, extra_usage = _parse_usage_details(
        row.get("cached_usage_raw")
    )
    binding = per_model.get(binding_key) if (per_model and binding_key) else None
    return AccountUsage(
        five_hour=row.get("cached_usage_5h", 0) or 0,
        seven_day=row.get("cached_usage_7d", 0) or 0,
        five_hour_resets_at=row.get("cached_5h_resets_at"),
        seven_day_resets_at=row.get("cached_7d_resets_at"),
        per_model=per_model,
        binding_model=binding,
        extra_usage=extra_usage,
    )


def _get_db(request: Request):
    """Get database from app state."""
    return getattr(request.app.state, "db", None)


def _db_unavailable():
    return JSONResponse(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        content={
            "error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}
        },
    )


def _not_found(detail: str):
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error": {
                "message": "Account not found",
                "code": "NOT_FOUND",
                "detail": detail,
            }
        },
    )


_active_account_cache: dict = {"id": None, "expires_at": 0.0}


def _get_active_account_id_cached() -> int | None:
    """Get the canonical observed account ID, cached for 30 seconds."""
    if time.time() < _active_account_cache["expires_at"]:
        return _active_account_cache["id"]
    try:
        from jacked.credentials.resolver import ResolverState
        from jacked.credentials.runtime import resolve_active_identity

        observation = resolve_active_identity()
        account_id = (
            observation.identity.account_id
            if observation.state is ResolverState.RESOLVED
            else None
        )
    except (OSError, RuntimeError):
        account_id = None
    _active_account_cache["id"] = account_id
    _active_account_cache["expires_at"] = time.time() + 30.0
    return account_id


def _account_to_response(row: dict, db=None) -> AccountResponse:
    """Convert a DB account row to an API response with computed fields."""
    now = int(time.time())

    # On-demand credential reconciliation for active account
    # If CC tokens are missing/expired, try importing from live store
    if db is not None:
        _active_id = _get_active_account_id_cached()
        if row["id"] == _active_id and (
            row.get("cc_refresh_token") is None or (row.get("cc_expires_at") or 0) < now
        ):
            try:
                from jacked.api.credential_helpers import (
                    reconcile_credentials_from_live_store,
                )

                reconcile_credentials_from_live_store(row["id"], db)
                row = db.get_account(row["id"]) or row  # Re-read after reconciliation
            except Exception:
                pass

    # Build response without access_token or refresh_token (never expose)
    return AccountResponse(
        id=row["id"],
        provider=row.get("provider") or "claude",
        email=row["email"],
        organization_uuid=row.get("organization_uuid") or None,
        organization_name=row.get("organization_name"),
        display_name=row.get("display_name"),
        expires_at=row["expires_at"],
        scopes=row.get("scopes"),
        subscription_type=row.get("subscription_type"),
        rate_limit_tier=row.get("rate_limit_tier"),
        has_extra_usage=bool(row.get("has_extra_usage", False)),
        priority=row.get("priority", 0),
        is_active=bool(row.get("is_active", True)),
        is_deleted=bool(row.get("is_deleted", False)),
        last_used_at=row.get("last_used_at"),
        cached_usage_5h=row.get("cached_usage_5h"),
        cached_usage_7d=row.get("cached_usage_7d"),
        cached_5h_resets_at=row.get("cached_5h_resets_at"),
        cached_7d_resets_at=row.get("cached_7d_resets_at"),
        usage_cached_at=row.get("usage_cached_at"),
        last_error=row.get("last_error"),
        last_error_at=row.get("last_error_at"),
        consecutive_failures=row.get("consecutive_failures", 0),
        last_validated_at=row.get("last_validated_at"),
        validation_status=row.get("validation_status", "unknown"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        # CC token status (computed from DB columns, not stored)
        cc_expires_at=row.get("cc_expires_at"),
        has_cc_token=bool(row.get("cc_access_token")),
        cc_needs_auth=(
            row.get("cc_access_token") is not None
            and row.get("cc_refresh_token") is None
            and now >= (row.get("cc_expires_at") or 0)
            and not bool(row.get("refresh_token"))
        ),
        has_refresh_token=bool(row.get("refresh_token")),
        has_cc_refresh_token=bool(row.get("cc_refresh_token")),
        # Computed fields per design doc
        is_default=row.get("priority", 0) == 0,
        is_expired=now >= row["expires_at"],
        expires_in_seconds=max(0, row["expires_at"] - now),
        usage=_build_account_usage(row),
    )


# --- Routes ---


@router.post("/accounts/add")
async def start_add_account(
    request: Request, provider: str = "claude", remote: bool = False
):
    """Add an account.

    Claude (default): starts the Anthropic OAuth flow, returns a flow_id to poll.
    Codex (``?provider=codex``): forces file-based credential storage and imports
    the already-logged-in ``~/.codex/auth.json``. If no Codex account is logged
    in, returns 400 ``needs_login`` so the UI can prompt the user to run
    ``codex login`` (the browser OAuth can't be driven from the background API).
    """
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    if provider == "codex":
        from jacked.codex.accounts import CodexImportError, add_codex_account

        try:
            acct = add_codex_account(db, run_login=False)
        except CodexImportError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {"message": str(exc), "code": "CODEX_LOGIN_REQUIRED"},
                    "needs_login": True,
                    "command": "codex login",
                },
            )
        return {
            "provider": "codex",
            "imported": True,
            "account_id": acct["id"],
            "email": acct["email"],
            "plan": acct.get("subscription_type"),
        }

    flow = OAuthFlow(
        db,
        manual=_manual_oauth(request, remote),
        allow_credential_activation=_local_mutation_allowed(request),
    )
    result = await flow.start()

    if "error" in result:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {"message": result["error"], "code": "OAUTH_START_FAILED"}
            },
        )

    return result


@router.post("/accounts/{account_id}/reauth")
async def start_reauth(account_id: int, request: Request, remote: bool = False):
    """Start OAuth re-auth flow for an existing account.

    Unlike /accounts/add, this targets a specific account by ID so the
    OAuth callback updates the existing row instead of creating a duplicate
    (which can happen when organization_uuid changes between OAuth sessions).
    """
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={"error": {"message": "Account not found", "code": "NOT_FOUND"}},
        )

    # Codex accounts don't use the Anthropic OAuth flow — re-auth is `codex login`.
    if (account.get("provider") or "claude") == "codex":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Codex accounts re-authenticate with `codex login`, then Add Account → Codex.",
                    "code": "CODEX_NOT_OAUTH",
                }
            },
        )

    flow = OAuthFlow(
        db,
        purpose="primary",
        target_account_id=account_id,
        manual=_manual_oauth(request, remote),
        allow_credential_activation=_local_mutation_allowed(request),
    )
    result = await flow.start()

    if "error" in result:
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {"message": result["error"], "code": "OAUTH_START_FAILED"}
            },
        )

    return result


@router.get("/flow/{flow_id}", response_model=FlowStatusResponse)
async def get_flow_status(flow_id: str):
    """Poll OAuth flow status. Returns pending/completed/error/not_found."""
    flow = get_flow(flow_id)
    if flow is None:
        return FlowStatusResponse(status="not_found", flow_id=flow_id)

    return _flow_status_response(flow.get_status())


@router.post("/flow/{flow_id}/code", response_model=FlowStatusResponse)
async def submit_flow_code(flow_id: str, body: SubmitCodeRequest):
    """Complete an OAuth flow from a manually pasted authorization code.

    Used by manual (remote-dashboard) flows, and as a fallback when the
    local browser redirect fails. A recoverable problem (bad paste, wrong
    state) comes back as ``submit_error`` with the flow still pending.
    """
    flow = get_flow(flow_id)
    if flow is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "message": "Authorization flow not found or expired.",
                    "code": "NOT_FOUND",
                }
            },
        )

    return _flow_status_response(await flow.submit_code(body.code))


@router.post("/flow/{flow_id}/open", response_model=FlowStatusResponse)
async def reopen_flow_browser(flow_id: str):
    """Open the sign-in window again for a flow that is still pending.

    Windows will not let a background service raise the window it launched,
    so the user may never see it. This re-launches into the same per-account
    profile and pulls the window forward, which is the only safe way back:
    the dashboard's own browser is signed in to whatever account it is signed
    in to, and using it authorizes the wrong one.
    """
    flow = get_flow(flow_id)
    if flow is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "message": "Authorization flow not found or expired.",
                    "code": "NOT_FOUND",
                }
            },
        )

    return _flow_status_response(await flow.reopen_browser())


@router.get("/accounts", response_model=list[AccountResponse])
async def list_accounts(request: Request, include_inactive: bool = False):
    """List all accounts, ordered by priority. Active only by default."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    rows = db.list_accounts(include_inactive=include_inactive)
    return [_account_to_response(row, db=db) for row in rows]


@router.patch("/accounts/{account_id}")
async def update_account(account_id: int, body: AccountPatchRequest, request: Request):
    """Update display_name and/or is_active for an account."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    has_label = "display_name" in body.model_fields_set
    has_active = body.is_active is not None

    if not has_label and not has_active:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {"message": "No fields to update", "code": "VALIDATION_ERROR"}
            },
        )

    # Label changes go through dedicated set_account_label() — the ONLY
    # code path that can modify display_name (whitelist excludes it).
    if has_label:
        raw = body.display_name.strip() if body.display_name else None
        label = raw if raw else None
        logger.info(
            "PATCH label for account %d: %r (User-Agent: %s, Origin: %s)",
            account_id,
            label,
            request.headers.get("user-agent", "unknown"),
            request.headers.get("origin", "unknown"),
        )
        db.set_account_label(account_id, label)

    if has_active:
        if not db.update_account(account_id, is_active=body.is_active):
            return _not_found(f"Account {account_id} was deleted during update")

    updated = db.get_account(account_id)
    if not updated:
        return _not_found(f"Account {account_id} no longer exists")
    return _account_to_response(updated)


@router.delete("/accounts/{account_id}")
async def delete_account(account_id: int, request: Request):
    """Soft-delete an account. Cannot delete primary while others exist."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    # Cannot delete primary (priority=0) while other active accounts exist
    if account.get("priority", 0) == 0:
        other_active = db.list_accounts(include_inactive=False)
        if len(other_active) > 1:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={
                    "error": {
                        "message": "Cannot delete primary account while other active accounts exist",
                        "code": "CANNOT_DELETE_PRIMARY",
                        "detail": "Set a different account as primary first, or delete other accounts.",
                    }
                },
            )

    db.delete_account(account_id)

    if (account.get("provider") or "claude") == "codex":
        # Codex account: clear its active pointer + remove its per-account auth
        # slot (a Codex account has no ~/.claude credential dir).
        try:
            if db.get_active_account_id("codex") == account_id:
                db.delete_setting(db.active_account_setting_key("codex"))
        except Exception:
            logger.debug("clearing codex active pointer failed", exc_info=True)
        try:
            from jacked.codex.switching import codex_account_home

            slot_dir = codex_account_home(account_id)
            if slot_dir.exists() and slot_dir.is_dir() and not slot_dir.is_symlink():
                shutil.rmtree(slot_dir, ignore_errors=True)
        except Exception:
            logger.debug("removing codex slot dir failed", exc_info=True)
    else:
        # Remove per-account Claude credential dir to prevent orphaned files
        acct_dir = Path.home() / ".claude" / "accounts" / str(account_id)
        if acct_dir.exists() and acct_dir.is_dir() and not acct_dir.is_symlink():
            shutil.rmtree(acct_dir, ignore_errors=True)

    return {"deleted": True, "account_id": account_id}


@router.post("/accounts/reorder")
async def reorder_accounts(body: ReorderRequest, request: Request):
    """Reorder account priorities. Index position becomes priority value."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    if not body.order:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "order list cannot be empty",
                    "code": "VALIDATION_ERROR",
                }
            },
        )

    db.reorder_accounts(body.order)

    # Return updated account list
    rows = db.list_accounts(include_inactive=True)
    return [_account_to_response(row) for row in rows]


@router.post("/accounts/{account_id}/refresh", response_model=RefreshResponse)
async def refresh_token(account_id: int, request: Request):
    """Force token refresh for an account."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    if not account.get("refresh_token"):
        return RefreshResponse(
            success=True,
            error="API key account — no refresh needed (valid for ~1 year)",
        )

    try:
        success = await asyncio.wait_for(
            refresh_account_token(account_id, db),
            timeout=_TOKEN_REFRESH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "refresh_token: account %d refresh_account_token exceeded %.0fs — returning 504",
            account_id,
            _TOKEN_REFRESH_TIMEOUT,
        )
        return _gateway_timeout(
            "Token refresh timed out — server may be recovering a wedged refresh",
            "REFRESH_TIMEOUT",
        )
    if success:
        return RefreshResponse(success=True)

    # Re-read account to get the error that was recorded
    updated = db.get_account(account_id)
    error_msg = (
        updated.get("last_error", "Token refresh failed")
        if updated
        else "Token refresh failed"
    )
    return RefreshResponse(success=False, error=error_msg)


@router.post(
    "/accounts/{account_id}/refresh-usage", response_model=UsageRefreshResponse
)
async def refresh_usage(account_id: int, request: Request):
    """Refresh usage cache for a single account."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    from jacked.api.credential_helpers import read_fresh_active_token

    fresh_token = read_fresh_active_token(account_id)
    # Only pass fresh_token when it differs from DB token — passing a non-None
    # access_token to fetch_usage() bypasses its cache freshness guard (auth.py:339).
    db_token = account.get("access_token")
    effective_token = fresh_token if (fresh_token and fresh_token != db_token) else None
    try:
        usage_data = await asyncio.wait_for(
            fetch_usage(account_id, db, access_token=effective_token, manual=True),
            timeout=_SINGLE_USAGE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "refresh_usage: account %d fetch_usage exceeded %.0fs — "
            "returning 504 and resetting validation_status",
            account_id,
            _SINGLE_USAGE_TIMEOUT,
        )
        # Mirror the bulk route's timeout handling: record the error and
        # reset validation_status in the same call so the row doesn't sit
        # at 'checking' waiting for the watchdog's next 60s tick.
        db.update_account(
            account_id,
            validation_status="unknown",
            last_error=(f"Usage fetch timed out after {int(_SINGLE_USAGE_TIMEOUT)}s"),
            last_error_at=datetime.now(timezone.utc).isoformat(),
        )
        return _gateway_timeout(
            "Usage fetch timed out — server may be recovering a wedged refresh",
            "REFRESH_TIMEOUT",
        )

    if isinstance(usage_data, dict) and usage_data.get("_backed_off"):
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": {
                    "message": "Usage API rate limited. Try again shortly.",
                    "code": "RATE_LIMITED",
                }
            },
        )

    if usage_data is None:
        is_codex = (account.get("provider") or "claude") == "codex"
        msg = (
            "Failed to read Codex usage — is `codex` installed and signed in?"
            if is_codex
            else "Failed to fetch usage from Anthropic API"
        )
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={"error": {"message": msg, "code": "USAGE_FETCH_FAILED"}},
        )

    # Re-read to get updated cache values
    updated = db.get_account(account_id)
    return UsageRefreshResponse(
        success=True,
        account_id=account_id,
        cached_usage_5h=updated.get("cached_usage_5h") if updated else None,
        cached_usage_7d=updated.get("cached_usage_7d") if updated else None,
    )


@router.post("/accounts/refresh-all-usage", response_model=BulkUsageRefreshResponse)
async def refresh_all_usage(
    request: Request,
    skip_account: Optional[int] = Query(None),
    user_initiated: bool = Query(False),
):
    """Refresh usage cache for all active accounts, concurrently.

    ``user_initiated`` distinguishes a human clicking "Refresh All" (gets
    fetch_usage's short manual floor) from the dashboard auto-refresh timer
    (an AUTOMATIC path that must respect the full rate-limit ceiling —
    classifying it as manual let timers ride the 20s floor and stack on top
    of the server's own polling).

    Accounts are fetched in parallel (bounded by _BULK_MAX_CONCURRENCY).
    Anthropic's usage-API rate limit is per-account/per-token, so
    cross-account concurrency doesn't approach it — only repeated hits
    on the SAME account do, and fetch_usage's per-account floors guard
    that. Sends per-account progress via WebSocket so the frontend can
    animate individual cards; `progress` counts completions.
    """
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    # Only one bulk refresh at a time (across tabs / auto-refresh overlap).
    # If a prior holder has been inside the lock longer than the stale
    # threshold, assume it's orphaned and force-reset — we'd rather
    # double-fetch than be permanently wedged at 409.
    #
    # This runtime watchdog complements the lifespan-level reset_locks()
    # above: reset_locks handles *cross-loop* staleness at startup (tray
    # restart installs a fresh loop); this block handles *in-loop* hangs
    # at runtime (a coroutine stuck on a pathological Anthropic response).
    # Both are needed.
    global _bulk_refresh_lock, _bulk_refresh_acquired_at, _bulk_refresh_task
    global _last_bulk_completed_at

    # Pass-throttle: a pass that completed within the last
    # _BULK_PASS_MIN_INTERVAL seconds means every account's cache is at
    # most ~30s-plus-pass-length old — serve straight from the DB with no
    # Anthropic calls. WS broadcasts are skipped entirely (rather than
    # sending a total=0 usage_refresh_started) so other tabs never enter a
    # "refreshing" state that no progress/done events would ever resolve;
    # the requester gets its data in the HTTP response.
    since_last = time.time() - _last_bulk_completed_at
    if _last_bulk_completed_at and since_last < _BULK_PASS_MIN_INTERVAL:
        accounts = db.list_accounts(include_inactive=False)
        results = [
            {
                "account_id": acct["id"],
                "email": acct["email"],
                "success": True,
                "throttled": True,
                "cached_usage_5h": acct.get("cached_usage_5h"),
                "cached_usage_7d": acct.get("cached_usage_7d"),
            }
            for acct in accounts
        ]
        logger.info(
            "Bulk usage refresh throttled: previous pass completed %.1fs ago "
            "(< %.0fs) — returning DB values for %d account(s)",
            since_last,
            _BULK_PASS_MIN_INTERVAL,
            len(results),
        )
        return BulkUsageRefreshResponse(
            refreshed=len(results),
            failed=0,
            throttled=len(results),
            results=results,
        )

    if _bulk_refresh_lock.locked():
        held_for = (
            time.time() - _bulk_refresh_acquired_at if _bulk_refresh_acquired_at else 0
        )
        if held_for > _BULK_REFRESH_STALE_AFTER:
            logger.warning(
                "Bulk refresh lock held %ds (> %ds) — forcing reset",
                int(held_for),
                int(_BULK_REFRESH_STALE_AFTER),
            )
            orphan = _bulk_refresh_task
            if orphan is not None and not orphan.done():
                orphan.cancel()
                # Fire-and-forget: do NOT await the orphan.  The event
                # loop delivers the cancel on its next tick.  Awaiting
                # here would re-raise CancelledError in a way that's
                # impossible to distinguish portably from "we were
                # cancelled ourselves" on Python 3.10 (no
                # Task.cancelling()).  The orphan was hung >180s already
                # — its cancellation-time DB writes are no less suspect
                # with a 2s wait than without one.
            _bulk_refresh_lock = asyncio.Lock()
            _bulk_refresh_acquired_at = 0.0
            _bulk_refresh_task = None
        else:
            return JSONResponse(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                content={
                    "error": {
                        "message": "Usage refresh already in progress",
                        "code": "REFRESH_IN_PROGRESS",
                    }
                },
            )

    async with _bulk_refresh_lock:
        _bulk_refresh_acquired_at = time.time()
        my_task = asyncio.current_task()
        _bulk_refresh_task = my_task
        try:
            accounts = db.list_accounts(include_inactive=False)
            ws_registry = getattr(request.app.state, "ws_registry", None)
            total = len(accounts)

            # Resolve active identity once through the certified store topology.
            from jacked.api.credential_helpers import read_fresh_active_token

            active_acct_id = await asyncio.to_thread(_get_active_account_id_cached)

            if active_acct_id is not None:
                logger.debug(
                    "Bulk refresh: active account from credential file = %s",
                    active_acct_id,
                )
            else:
                logger.debug(
                    "Bulk refresh: no canonical active credential identity; "
                    "all accounts will use DB tokens"
                )

            # Notify frontend: full queue so cards can show "Waiting..." immediately
            if ws_registry:
                await ws_registry.broadcast(
                    "usage_refresh_started",
                    {"account_ids": [a["id"] for a in accounts], "total": total},
                )

            targets = [
                acct
                for acct in accounts
                if not (skip_account is not None and acct["id"] == skip_account)
            ]
            sem = asyncio.Semaphore(_BULK_MAX_CONCURRENCY)
            completed = {
                "n": 0
            }  # mutable closure counter; event loop is single-threaded

            async def _refresh_one(acct: dict) -> dict:
                async with sem:
                    # No progress/total on checking events — under concurrency
                    # the completed-count is 0 for the whole first batch and
                    # the frontend would flash "Refreshing 0/N". The button
                    # label only updates on integer progress, so omitting it
                    # leaves the click-time "Refreshing..." text in place.
                    if ws_registry:
                        await ws_registry.broadcast(
                            "usage_refresh_progress",
                            {"account_id": acct["id"], "status": "checking"},
                        )

                    effective_token = None
                    if acct["id"] == active_acct_id:
                        fresh_token = read_fresh_active_token(acct["id"])
                        db_token = acct.get("access_token")
                        if fresh_token and fresh_token != db_token:
                            effective_token = fresh_token
                    try:
                        usage_data = await asyncio.wait_for(
                            fetch_usage(
                                acct["id"],
                                db,
                                access_token=effective_token,
                                manual=user_initiated,
                            ),
                            timeout=_BULK_PER_ACCOUNT_TIMEOUT,
                        )
                    except asyncio.TimeoutError:
                        logger.warning(
                            "Bulk refresh: account %d fetch_usage exceeded %.0fs — "
                            "marking failed and resetting validation_status",
                            acct["id"],
                            _BULK_PER_ACCOUNT_TIMEOUT,
                        )
                        timeout_error = (
                            f"Usage fetch timed out after {int(_BULK_PER_ACCOUNT_TIMEOUT)}s "
                            f"during bulk refresh"
                        )
                        # Reset validation_status in the same call that records
                        # the error so the row doesn't sit at 'checking' waiting
                        # for the watchdog's next 60s tick (PM3 fix).
                        db.update_account(
                            acct["id"],
                            validation_status="unknown",
                            last_error=timeout_error,
                            last_error_at=datetime.now(timezone.utc).isoformat(),
                        )
                        usage_data = None

                    # Cache hits return {"_cached": True} — read stored values from DB
                    is_cached = isinstance(usage_data, dict) and usage_data.get(
                        "_cached"
                    )
                    if is_cached:
                        usage_data = None  # Treat as skip — use DB values below

                    is_backed_off = isinstance(usage_data, dict) and usage_data.get(
                        "_backed_off"
                    )
                    if is_backed_off:
                        usage_data = None

                    if usage_data is not None:
                        five_hour = usage_data.get("five_hour", {})
                        seven_day = usage_data.get("seven_day", {})
                        row = {
                            "account_id": acct["id"],
                            "email": acct["email"],
                            "success": True,
                            "cached_usage_5h": five_hour.get("utilization"),
                            "cached_usage_7d": seven_day.get("utilization"),
                        }
                    elif is_cached:
                        # Cache hit — report existing DB values, count as success
                        row = {
                            "account_id": acct["id"],
                            "email": acct["email"],
                            "success": True,
                            "cached_usage_5h": acct.get("cached_usage_5h"),
                            "cached_usage_7d": acct.get("cached_usage_7d"),
                        }
                    else:
                        updated_acct = db.get_account(acct["id"])
                        row = {
                            "account_id": acct["id"],
                            "email": acct["email"],
                            "success": False,
                            "error": updated_acct.get("last_error")
                            if updated_acct
                            else None,
                        }

                    completed["n"] += 1
                    # Notify frontend: done or failed (include account data for immediate UI update)
                    progress_status = (
                        "failed" if (not is_cached and usage_data is None) else "done"
                    )
                    if ws_registry:
                        updated_row = db.get_account(acct["id"])
                        acct_payload = (
                            _account_to_response(updated_row).model_dump()
                            if updated_row
                            else None
                        )
                        await ws_registry.broadcast(
                            "usage_refresh_progress",
                            {
                                "account_id": acct["id"],
                                "status": progress_status,
                                "progress": completed["n"],
                                "total": total,
                                "account_data": acct_payload,
                            },
                        )
                    return row

            async def _refresh_one_guarded(acct: dict) -> dict:
                # A DB/validation error for ONE account must not propagate out
                # of the gather — that would release the bulk lock while the
                # sibling coroutines are still in flight, breaking the
                # one-pass-at-a-time invariant for any immediate retry.
                try:
                    return await _refresh_one(acct)
                except Exception as exc:
                    logger.exception(
                        "Bulk refresh: unexpected error for account %d",
                        acct["id"],
                    )
                    completed["n"] += 1
                    return {
                        "account_id": acct["id"],
                        "email": acct["email"],
                        "success": False,
                        "error": f"internal error during refresh: {exc}",
                    }

            results = list(
                await asyncio.gather(*(_refresh_one_guarded(a) for a in targets))
            )
            refreshed = sum(1 for r in results if r["success"])
            failed = sum(1 for r in results if not r["success"])

            # Stamp completion only on a normally finished pass — a
            # cancelled/raised pass must not arm the throttle and mask a
            # legitimate retry.
            _last_bulk_completed_at = time.time()
            return BulkUsageRefreshResponse(
                refreshed=refreshed,
                failed=failed,
                results=results,
            )
        finally:
            # Only clear the slot if we still own it.  A force-reset during
            # our long-running loop may have replaced _bulk_refresh_task
            # with a newer holder — clearing it would wipe the new holder's
            # state (/dc PM2/Q2 fix).  Resetting acquired_at alongside the
            # task keeps the staleness clock from counting a finished run.
            if _bulk_refresh_task is my_task:
                _bulk_refresh_task = None
                _bulk_refresh_acquired_at = 0.0


@router.post("/accounts/{account_id}/validate", response_model=ValidateResponse)
async def validate_token(account_id: int, request: Request):
    """Validate that an account's token is still working (calls profile API)."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    try:
        result = await asyncio.wait_for(
            validate_account(account_id, db),
            timeout=_VALIDATE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "validate_token: account %d validate_account exceeded %.0fs — returning 504",
            account_id,
            _VALIDATE_TIMEOUT,
        )
        return _gateway_timeout(
            "Validation timed out — server may be recovering a wedged refresh",
            "VALIDATE_TIMEOUT",
        )
    return ValidateResponse(
        valid=result["valid"],
        error=result.get("error"),
    )


@router.post("/accounts/{account_id}/authorize-cc")
async def start_cc_auth(account_id: int, request: Request, remote: bool = False):
    """Start OAuth flow for independent Claude Code tokens on existing account.

    Allows upgrading an existing account with separate CC tokens without
    re-authenticating the primary pair.
    """
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    # "CC token" is a Claude Code concept — Codex accounts have none and must not
    # start an Anthropic OAuth flow.
    if (account.get("provider") or "claude") == "codex":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Codex accounts have no Claude Code token to authorize.",
                    "code": "CODEX_NO_CC",
                }
            },
        )

    from jacked.web.oauth import OAuthFlow

    # Always start a fresh flow — every click opens a new browser window.
    # Old flows time out and clean up automatically.
    flow = OAuthFlow(
        db,
        purpose="claude_code",
        target_account_id=account_id,
        manual=_manual_oauth(request, remote),
        allow_credential_activation=_local_mutation_allowed(request),
    )
    result = await flow.start()
    return result


# --- Credential switching ---


@router.post("/accounts/{account_id}/use", response_model=UseAccountResponse)
async def use_account(account_id: int, request: Request):
    """Request a local default-credential activation for one account.

    The response reports separately what was stored, what was committed, and
    what can be claimed about existing sessions.

    Rejects disabled accounts, accounts with invalid validation status,
    and accounts without CC tokens (which would be un-refreshable).
    """
    if not _local_mutation_allowed(request):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "error": {
                    "message": (
                        "Account switching is available only from this computer. "
                        "Remote dashboards are read-only for credentials."
                    ),
                    "code": "CREDENTIAL_MUTATION_LOCAL_ONLY",
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    # Defense-in-depth: get_account() already filters is_deleted=0,
    # but guard here in case that query changes in the future.
    if account.get("is_deleted"):
        return _not_found(f"No account with id={account_id}")

    if not account.get("is_active"):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Account is disabled — enable it first",
                    "code": "ACCOUNT_DISABLED",
                }
            },
        )

    if account.get("validation_status") == "invalid":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Account has invalid credentials — re-auth first",
                    "code": "ACCOUNT_INVALID",
                }
            },
        )

    # Codex accounts switch by swapping ~/.codex/auth.json (guardrailed), not by
    # writing the Claude credential stores — and Codex has no CC tokens, so this
    # branches before the Claude-only checks below.
    if account.get("provider") == "codex":
        from jacked.codex.switching import CodexSwapError, swap_codex_account

        try:
            # swap_codex_account is SYNC and its file lock sleeps up to seconds;
            # it shares the swap lock with the async usage poll, so running it
            # on the event loop would freeze every coroutine (incl. the
            # lock-holding poll) and deadlock the collision. Off-load it — the
            # same convention the Claude swap path follows (usage_monitor).
            result = await asyncio.to_thread(swap_codex_account, db, account_id)
        except CodexSwapError as exc:
            return JSONResponse(
                status_code=status.HTTP_400_BAD_REQUEST,
                content={"error": {"message": str(exc), "code": "CODEX_SWAP_FAILED"}},
            )
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "provider": "codex",
                "account_id": account_id,
                "restart_required": result.restart_required,
                "message": (
                    "Switched the active Codex account. Restart Codex (or the "
                    "Codex app/IDE) to pick it up — it caches auth at startup."
                ),
            },
        )

    if not account.get("cc_access_token"):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": (
                        "Account has no CC tokens — authorize Claude Code "
                        "tokens first (credentials without a refresh token "
                        "would expire in ~8 hours with no way to renew)"
                    ),
                    "code": "CC_TOKEN_MISSING",
                }
            },
        )

    action_id, operation_id, action_state, stored_result = _claim_switch_action(
        db, request, account_id
    )
    if action_state == "complete" and stored_result is not None:
        stored_result = dict(stored_result)
        http_status = int(stored_result.pop("_http_status", status.HTTP_200_OK))
        return JSONResponse(
            status_code=http_status,
            content=stored_result,
            headers={"Cache-Control": "no-store"},
        )
    if action_state in {"invalid", "mismatch", "expired"}:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "message": f"Credential action id is {action_state}",
                    "code": f"CREDENTIAL_ACTION_{action_state.upper()}",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    if action_state == "invalid_session":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "A valid page-session identifier is required.",
                    "code": "CREDENTIAL_PAGE_SESSION_INVALID",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    if action_state == "claimed":
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": {
                    "message": "Credential operation is already in progress",
                    "code": "CREDENTIAL_OPERATION_IN_PROGRESS",
                    "operation_id": operation_id,
                }
            },
            headers={"Cache-Control": "no-store"},
        )

    # Reconcile outgoing account's credentials before writing new ones.
    from jacked.api.credential_helpers import reconcile_credentials_from_live_store
    from jacked.api.usage_monitor import _read_active_account_id

    outgoing_id = await asyncio.to_thread(_read_active_account_id)
    if outgoing_id and outgoing_id != account_id:
        await asyncio.to_thread(
            reconcile_credentials_from_live_store, outgoing_id, db
        )

    from jacked.credentials.models import SwitchContext, SwitchOutcome
    from jacked.credentials.runtime import activate_account

    switcher = getattr(request.app.state, "credential_switcher", activate_account)
    result = await asyncio.to_thread(
        switcher, db, account, SwitchContext.MANUAL, operation_id
    )
    content = _switch_content(result, account.get("email", ""))
    http_status = _switch_http_status(result.outcome)
    db.finish_auth_action(action_id, {**content, "_http_status": http_status})

    truthful_target = result.outcome in {
        SwitchOutcome.COMMITTED,
        SwitchOutcome.COMMITTED_DEGRADED,
        SwitchOutcome.OBSERVED_TARGET_UNFENCED,
    }
    if truthful_target:
        db.mark_global_sessions_pending()

    if not truthful_target:
        return JSONResponse(
            status_code=http_status,
            content=content,
            headers={"Cache-Control": "no-store"},
        )

    # Give the manual choice residency: without this, the auto-swap loop
    # can silently revert a user-chosen account within ~2-5 minutes.
    # note_external_swap() arms the monitor's cooldown + min-residency
    # clocks and clears the emergence streak; the pause setting holds the
    # sweep loop off entirely for at least 15 minutes.
    #
    # Every truthful target runs this, not only a committed one. On macOS the
    # engine reports observed_target_unfenced for every switch, because no
    # shipped Claude build can be fenced. That outcome still names the account
    # the authority now holds, so the DB pointer and the pause must follow it.
    try:
        db.set_setting("active_account_id", str(account_id))
    except Exception:
        logger.exception(
            "Failed to record the active account pointer after a manual switch "
            "(account=%d)",
            account_id,
        )

    try:
        from jacked.api import usage_monitor  # local import: avoids cycle

        usage_monitor.note_external_swap()
        pause_dt = datetime.now(timezone.utc) + timedelta(minutes=15)
        # Only ever EXTEND an active pause — the user may have set a
        # longer explicit pause (POST /api/settings/swap-pause, up to
        # 1440 min); a manual switch must never silently shorten it.
        existing = db.get_setting("auto_swap_paused_until") or ""
        if existing:
            try:
                existing_dt = datetime.fromisoformat(
                    existing.replace("Z", "+00:00"),
                )
                if existing_dt > pause_dt:
                    pause_dt = existing_dt
            except (ValueError, TypeError):
                # Unparseable/naive timestamp (the sweep loop ignores
                # these anyway) — overwrite with the 15-minute pause.
                pass
        db.set_setting("auto_swap_paused_until", pause_dt.isoformat())
    except Exception:
        logger.exception(
            "Failed to arm auto-swap pause after manual switch "
            "(account=%d) — auto-swap may revert the user's choice",
            account_id,
        )

    try:
        from jacked.web.auto_swap import format_account_label

        prev_acct = db.get_account(outgoing_id) if outgoing_id else None
        reason = f"user switched to {format_account_label(account)}"
        ws_registry = getattr(request.app.state, "ws_registry", None)
        if ws_registry:
            await ws_registry.broadcast(
                "auto_swap_triggered",
                {
                    "from_account_id": outgoing_id,
                    "to_account_id": account_id,
                    "from_email": prev_acct.get("email", "") if prev_acct else "",
                    "to_email": account.get("email", ""),
                    "from_label": format_account_label(prev_acct) if prev_acct else "",
                    "to_label": format_account_label(account),
                    "reason": reason,
                },
            )
    except Exception:
        logger.debug("Committed switch broadcast failed", exc_info=True)

    return JSONResponse(
        status_code=http_status,
        content=content,
        headers={"Cache-Control": "no-store"},
    )


@router.get("/credential-operations/{identifier}")
async def get_credential_operation(identifier: str, request: Request):
    """Return local, secret-free status for a credential action/operation."""
    if not _local_mutation_allowed(request):
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "error": {
                    "message": "Credential operation status is local-only.",
                    "code": "CREDENTIAL_STATUS_LOCAL_ONLY",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    if not _ACTION_ID_PATTERN.fullmatch(identifier):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Credential operation identifier is invalid.",
                    "code": "CREDENTIAL_OPERATION_ID_INVALID",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    session_key = request.headers.get("X-Jacked-Page-Session") or ""
    if not _ACTION_ID_PATTERN.fullmatch(session_key):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "A valid page-session identifier is required.",
                    "code": "CREDENTIAL_PAGE_SESSION_INVALID",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    db = _get_db(request)
    if db is None:
        return _db_unavailable()
    action = db.get_auth_action(identifier)
    if action is None:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": {
                    "message": "Credential operation was not found.",
                    "code": "CREDENTIAL_OPERATION_NOT_FOUND",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    if action["session_key"] != session_key:
        return JSONResponse(
            status_code=status.HTTP_403_FORBIDDEN,
            content={
                "error": {
                    "message": "Credential operation belongs to another page session.",
                    "code": "CREDENTIAL_PAGE_SESSION_MISMATCH",
                }
            },
            headers={"Cache-Control": "no-store"},
        )
    action_state = action["state"]
    if action["expires_at"] <= datetime.now(timezone.utc).isoformat():
        action_state = "expired"
    return JSONResponse(
        status_code=(
            status.HTTP_200_OK
            if action_state == "complete"
            else status.HTTP_202_ACCEPTED
        ),
        content={
            "action_id": action["action_id"],
            "operation_id": action["operation_id"],
            "state": action_state,
            "result": action["result"] if action_state == "complete" else None,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/active-credential", response_model=ActiveCredentialResponse)
async def get_active_credential(request: Request):
    """Return the canonical evidence-qualified credential observation."""
    db = _get_db(request)
    if db is None:
        return ActiveCredentialResponse()
    from jacked.credentials.resolver import ResolverState
    from jacked.credentials.runtime import resolve_active_identity

    resolver = getattr(request.app.state, "credential_resolver", None)
    observation = await asyncio.to_thread(resolver or resolve_active_identity)
    evidence = list(observation.evidence)
    if observation.state is not ResolverState.RESOLVED:
        return ActiveCredentialResponse(
            state=observation.state.value, evidence=evidence
        )
    identity = observation.identity
    account = db.get_account(identity.account_id) if identity.account_id else None
    if not account or account.get("is_deleted"):
        return ActiveCredentialResponse(
            state=ResolverState.UNUSABLE.value,
            evidence=[*evidence, "account-stamp-not-found"],
        )
    observed_org = identity.organization_id or ""
    account_org = account.get("organization_uuid") or ""
    if observed_org and observed_org != account_org:
        return ActiveCredentialResponse(
            state=ResolverState.CONFLICT.value,
            evidence=[*evidence, "account-organization-conflict"],
        )
    return ActiveCredentialResponse(
        account_id=account["id"],
        email=account["email"],
        state=ResolverState.RESOLVED.value,
        evidence=evidence,
    )


# --- Session queries ---


@router.get("/session-account")
async def get_session_account(request: Request, session_id: str = ""):
    """Get account records for a specific session."""
    db = _get_db(request)
    if db is None or not session_id:
        return {"records": []}
    if len(session_id) < 36:
        return {"records": db.lookup_session_by_suffix(session_id)}
    return {"records": db.get_session_accounts(session_id)}


@router.get("/accounts/{account_id}/sessions")
async def get_account_sessions(request: Request, account_id: int, limit: int = 50):
    """Get recent sessions that used a given account."""
    db = _get_db(request)
    if db is None:
        return {"sessions": []}
    return {"sessions": db.get_account_sessions(account_id, limit=min(limit, 200))}


@router.get("/active-sessions")
async def get_active_sessions(request: Request, staleness: int = 60):
    """Compatibility view of recent session observations grouped by account.

    The grouping is historical/configuration evidence, not proof of which
    provider identity served the session's latest request. New clients should
    use ``/session-states``.
    """
    db = _get_db(request)
    if db is None:
        return {"sessions": {}}

    rows = db.get_active_sessions(staleness_minutes=staleness)

    grouped: dict = {}
    for row in rows:
        acct_id = row.get("account_id")
        if acct_id is None:
            continue
        key = str(acct_id)
        if key not in grouped:
            grouped[key] = []
        sid = row.get("session_id", "")
        grouped[key].append(
            {
                "repo_path": row.get("repo_path"),
                "detected_at": row.get("detected_at"),
                "last_activity_at": row.get("last_activity_at", ""),
                "session_id": sid[-8:] if sid else "",
                "is_subagent": bool(row.get("is_subagent")),
                "parent_session_id": row.get("parent_session_id", ""),
                "agent_type": row.get("agent_type", ""),
            }
        )

    return JSONResponse(
        content={
            "sessions": grouped,
            "identity_semantics": "historical_observation",
            "deprecated": True,
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("/session-states")
async def get_session_states(request: Request, staleness: int = 60):
    """Return evidence-qualified configuration state for recent sessions."""
    db = _get_db(request)
    if db is None:
        return JSONResponse(
            content={"sessions": [], "desired_global": None},
            headers={"Cache-Control": "no-store"},
        )

    desired = None
    for setting_key in ("desired_account_id", "active_account_id"):
        raw_id = db.get_setting(setting_key)
        try:
            candidate_id = int(raw_id) if raw_id else None
        except (TypeError, ValueError):
            candidate_id = None
        if candidate_id:
            desired = db.get_account(candidate_id)
            if desired is not None:
                break
    desired_public = (
        {
            "account_id": desired["id"],
            "email": desired.get("email"),
            "organization_uuid": desired.get("organization_uuid"),
            "organization_name": desired.get("organization_name"),
        }
        if desired
        else None
    )

    sessions = []
    for row in db.get_active_session_states(staleness_minutes=staleness):
        scope = row.get("credential_scope") or "legacy"
        evidence = row.get("evidence") or "legacy"
        state = row.get("observation_state") or "unknown"
        observed_at = row.get("observed_at")
        observed = None
        if state == "observed" and evidence not in {"legacy", "unknown"}:
            observed = {
                "account_id": row.get("account_id"),
                "email": row.get("email"),
                "credential_revision": row.get("credential_revision"),
                "observed_at": observed_at,
            }
        sessions.append(
            {
                "session_id": row.get("session_id", "")[-8:],
                "repo_path": row.get("repo_path"),
                "detected_at": row.get("detected_at"),
                "last_activity_at": row.get("last_activity_at"),
                "started_as": {
                    "account_id": row.get("started_account_id"),
                    "email": row.get("started_email"),
                    "observed_at": row.get("started_at"),
                    "evidence": row.get("started_method") or "legacy",
                },
                "observed_configuration": observed,
                "desired_global": desired_public,
                "pending": state == "pending",
                "scope": scope,
                "evidence": evidence,
                "freshness": observed_at,
                "runtime_verified": None,
                "state": state,
                "is_subagent": bool(row.get("is_subagent")),
                "parent_session_id": row.get("parent_session_id", ""),
                "agent_type": row.get("agent_type", ""),
            }
        )

    return JSONResponse(
        content={"sessions": sessions, "desired_global": desired_public},
        headers={"Cache-Control": "no-store"},
    )


@router.get("/display-name-audit")
async def get_display_name_audit(request: Request, limit: int = 50):
    """Return recent display_name change audit log entries."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    entries = db.get_label_audit_log(limit=min(max(limit, 1), 200))
    return {"entries": entries}
