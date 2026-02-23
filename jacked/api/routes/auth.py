"""Auth routes -- account management, credential switching, session queries.

Handles OAuth flow initiation/polling, account CRUD, token refresh,
usage cache refresh, account validation, credential switching, and
session-account queries.
"""

import json
import logging
import shutil
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jacked.web.auth import (
    fetch_usage,
    refresh_account_token,
    should_refresh,
    validate_account,
)
from jacked.web.oauth import OAuthFlow, get_flow

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Pydantic v2 request/response models ---


class ModelUsage(BaseModel):
    """Per-model 7-day usage breakdown."""

    utilization: float = 0
    resets_at: Optional[str] = None


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
    extra_usage: Optional[ExtraUsage] = None


class AccountResponse(BaseModel):
    """Account data with computed fields for API responses."""

    id: int
    email: str
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
    # Computed / enriched fields
    is_default: bool = False
    is_expired: bool = False
    expires_in_seconds: int = 0
    usage: Optional[AccountUsage] = None


class AccountPatchRequest(BaseModel):
    display_name: Optional[str] = None
    is_active: Optional[bool] = None


class ReorderRequest(BaseModel):
    order: list[int]


class FlowStatusResponse(BaseModel):
    status: str
    flow_id: str
    account_id: Optional[int] = None
    email: Optional[str] = None
    error: Optional[str] = None


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
    results: list[dict] = []


class UseAccountResponse(BaseModel):
    status: str
    email: str


class ActiveCredentialResponse(BaseModel):
    account_id: Optional[int] = None
    email: Optional[str] = None


# --- Helpers ---


def _parse_usage_details(
    raw_json: Optional[str],
) -> tuple[Optional[dict[str, ModelUsage]], Optional[ExtraUsage]]:
    """Parse cached_usage_raw JSON into per-model dict and ExtraUsage.

    >>> _parse_usage_details(None)
    (None, None)
    >>> _parse_usage_details("not json")
    (None, None)
    >>> pm, eu = _parse_usage_details('{"seven_day_sonnet": {"utilization": 42.5, "resets_at": "2025-02-08T00:00:00Z"}}')
    >>> pm["sonnet"].utilization
    42.5
    >>> eu is None
    True
    """
    if not raw_json:
        return None, None
    try:
        data = json.loads(raw_json)
    except (json.JSONDecodeError, TypeError):
        return None, None

    # Per-model: extract seven_day_* keys
    per_model: dict[str, ModelUsage] = {}
    for key in (
        "seven_day_sonnet",
        "seven_day_opus",
        "seven_day_oauth_apps",
        "seven_day_cowork",
    ):
        val = data.get(key)
        if val is not None and isinstance(val, dict):
            per_model[key.removeprefix("seven_day_")] = ModelUsage(
                utilization=val.get("utilization", 0),
                resets_at=val.get("resets_at"),
            )
        elif val is not None and isinstance(val, (int, float)):
            per_model[key.removeprefix("seven_day_")] = ModelUsage(utilization=val)

    # Extra usage credits
    extra_raw = data.get("extra_usage")
    extra = None
    if isinstance(extra_raw, dict):
        raw_limit = extra_raw.get("monthly_limit")
        raw_used = extra_raw.get("used_credits")
        extra = ExtraUsage(
            is_enabled=extra_raw.get("is_enabled", False),
            monthly_limit=raw_limit / 100 if raw_limit is not None else None,
            used_credits=raw_used / 100 if raw_used is not None else None,
            utilization=extra_raw.get("utilization"),
        )

    return (per_model or None), extra


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
    per_model, extra_usage = _parse_usage_details(row.get("cached_usage_raw"))
    return AccountUsage(
        five_hour=row.get("cached_usage_5h", 0) or 0,
        seven_day=row.get("cached_usage_7d", 0) or 0,
        five_hour_resets_at=row.get("cached_5h_resets_at"),
        seven_day_resets_at=row.get("cached_7d_resets_at"),
        per_model=per_model,
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


def _account_to_response(row: dict) -> AccountResponse:
    """Convert a DB account row to an API response with computed fields."""
    now = int(time.time())
    # Build response without access_token or refresh_token (never expose)
    return AccountResponse(
        id=row["id"],
        email=row["email"],
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
        # Computed fields per design doc
        is_default=row.get("priority", 0) == 0,
        is_expired=now >= row["expires_at"],
        expires_in_seconds=max(0, row["expires_at"] - now),
        usage=_build_account_usage(row),
    )


# --- Routes ---


@router.post("/accounts/add")
async def start_add_account(request: Request):
    """Start OAuth flow to add a new account. Returns flow_id for polling."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    flow = OAuthFlow(db)
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

    status_data = flow.get_status()
    return FlowStatusResponse(
        status=status_data["status"],
        flow_id=status_data["flow_id"],
        account_id=status_data.get("account_id"),
        email=status_data.get("email"),
        error=status_data.get("error"),
    )


@router.get("/accounts", response_model=list[AccountResponse])
async def list_accounts(request: Request, include_inactive: bool = False):
    """List all accounts, ordered by priority. Active only by default."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    rows = db.list_accounts(include_inactive=include_inactive)
    return [_account_to_response(row) for row in rows]


@router.patch("/accounts/{account_id}")
async def update_account(account_id: int, body: AccountPatchRequest, request: Request):
    """Update display_name and/or is_active for an account."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    updates: dict = {}
    if body.display_name is not None:
        updates["display_name"] = body.display_name
    if body.is_active is not None:
        updates["is_active"] = body.is_active

    if not updates:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {"message": "No fields to update", "code": "VALIDATION_ERROR"}
            },
        )

    db.update_account(account_id, **updates)

    updated = db.get_account(account_id)
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

    # Remove per-account credential dir to prevent orphaned files
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

    success = await refresh_account_token(account_id, db)
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

    usage_data = await fetch_usage(account_id, db)

    if usage_data is None:
        return JSONResponse(
            status_code=status.HTTP_502_BAD_GATEWAY,
            content={
                "error": {
                    "message": "Failed to fetch usage from Anthropic API",
                    "code": "USAGE_FETCH_FAILED",
                }
            },
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
async def refresh_all_usage(request: Request):
    """Refresh usage cache for all active accounts."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    accounts = db.list_accounts(include_inactive=False)
    refreshed = 0
    failed = 0
    results = []

    for acct in accounts:
        usage_data = await fetch_usage(acct["id"], db)
        if usage_data is not None:
            refreshed += 1
            five_hour = usage_data.get("five_hour", {})
            seven_day = usage_data.get("seven_day", {})
            results.append(
                {
                    "account_id": acct["id"],
                    "email": acct["email"],
                    "success": True,
                    "cached_usage_5h": five_hour.get("utilization"),
                    "cached_usage_7d": seven_day.get("utilization"),
                }
            )
        else:
            failed += 1
            updated_acct = db.get_account(acct["id"])
            results.append(
                {
                    "account_id": acct["id"],
                    "email": acct["email"],
                    "success": False,
                    "error": updated_acct.get("last_error") if updated_acct else None,
                }
            )

    return BulkUsageRefreshResponse(
        refreshed=refreshed,
        failed=failed,
        results=results,
    )


@router.post("/accounts/{account_id}/validate", response_model=ValidateResponse)
async def validate_token(account_id: int, request: Request):
    """Validate that an account's token is still working (calls profile API)."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    result = await validate_account(account_id, db)
    return ValidateResponse(
        valid=result["valid"],
        error=result.get("error"),
    )


# --- Credential switching ---


@router.post("/accounts/{account_id}/use", response_model=UseAccountResponse)
async def use_account(account_id: int, request: Request):
    """Write account credentials to Claude Code's credential stores.

    Overwrites ~/.claude/.credentials.json, macOS Keychain, and ~/.claude.json
    so the next Claude Code session starts with this account's tokens.
    """
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    if not account["is_active"]:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {"message": "Account is disabled", "code": "ACCOUNT_DISABLED"}
            },
        )

    access_token = account.get("access_token", "")
    if not access_token or not access_token.strip():
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {"message": "Account has no access token", "code": "NO_TOKEN"}
            },
        )

    if account.get("validation_status") == "invalid":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Token is invalid — re-authenticate first",
                    "code": "TOKEN_INVALID",
                }
            },
        )

    # Refresh token if near-expiry before writing
    if account.get("refresh_token") and should_refresh(account):
        refreshed = await refresh_account_token(account_id, db)
        if refreshed:
            account = db.get_account(account_id)
            if not account:
                return _not_found(f"No account with id={account_id}")
        else:
            logger.warning(
                "Token refresh failed for account %d before /use — "
                "proceeding with current token",
                account_id,
            )

    # Write to all credential stores in one call
    from jacked.api.credential_helpers import sync_credential_to_all_stores

    sync_credential_to_all_stores(account_id, account)

    return UseAccountResponse(status="ok", email=account["email"])


@router.get("/active-credential", response_model=ActiveCredentialResponse)
async def get_active_credential(request: Request):
    """Read credential file/keychain and match to a jacked account.

    Simple 2-layer matching: stamp, then access_token.
    """
    db = _get_db(request)
    if db is None:
        return ActiveCredentialResponse()

    from jacked.api.credential_helpers import read_platform_credentials

    # Read credential file
    cred_path = Path.home() / ".claude" / ".credentials.json"
    cred_data = None
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            cred_data = json.loads(cred_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, AttributeError):
            pass

    # Fallback: macOS Keychain
    if cred_data is None or not cred_data.get("claudeAiOauth", {}).get("accessToken"):
        platform_data = read_platform_credentials()
        if platform_data:
            cred_data = platform_data

    if not cred_data:
        return ActiveCredentialResponse()

    # Layer 1: _jackedAccountId stamp
    jacked_id = cred_data.get("_jackedAccountId")
    if jacked_id is not None:
        account = db.get_account(jacked_id)
        if account and not account.get("is_deleted"):
            return ActiveCredentialResponse(
                account_id=account["id"], email=account["email"]
            )

    # Layer 2: Exact access_token match
    access_token = cred_data.get("claudeAiOauth", {}).get("accessToken")
    if access_token:
        accounts = db.list_accounts(include_inactive=True)
        for acct in accounts:
            if acct.get("access_token") == access_token and not acct.get("is_deleted"):
                return ActiveCredentialResponse(
                    account_id=acct["id"], email=acct["email"]
                )

    return ActiveCredentialResponse()


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
    """Get all currently active sessions, grouped by account_id."""
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

    return {"sessions": grouped}
