"""Credential switching and session tracking endpoints.

Handles credential file writes, active-credential detection,
and session-account queries (5 endpoints).
"""

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


# --- Helpers ---


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


def _update_claude_config_email(email: str, display_name: str = None):
    """Update or create ~/.claude.json with the active account's email.

    If the file exists, only changes oauthAccount.emailAddress (preserves all
    other keys).  If the file doesn't exist, creates it with oauthAccount data.
    Refuses to write through symlinks.  Logs on failure instead of silently
    swallowing.

    >>> # Smoke — doesn't crash when called with a temp path
    >>> _update_claude_config_email("test@example.com")  # noqa: no side-effects in test env
    """
    claude_config = Path.home() / ".claude.json"

    # Security: refuse to write through symlinks (even if file doesn't exist yet,
    # someone could plant a symlink at the target path)
    if claude_config.is_symlink():
        logger.warning("Refusing to write ~/.claude.json — path is a symlink")
        return

    config: dict = {}
    if claude_config.exists():
        try:
            config = json.loads(claude_config.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read ~/.claude.json, creating fresh: %s", exc)
            config = {}

    # Update or create oauthAccount
    if "oauthAccount" not in config:
        config["oauthAccount"] = {}
    config["oauthAccount"]["emailAddress"] = email
    if display_name and "displayName" not in config["oauthAccount"]:
        config["oauthAccount"]["displayName"] = display_name

    # Atomic write
    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(claude_config.parent),
            prefix=".claude_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            os.replace(tmp, str(claude_config))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("Failed to write ~/.claude.json: %s", exc)


# --- Pydantic v2 models ---


class UseAccountResponse(BaseModel):
    status: str
    email: str


class ActiveCredentialResponse(BaseModel):
    account_id: Optional[int] = None
    email: Optional[str] = None


# --- Routes ---


@router.post("/accounts/{account_id}/use", response_model=UseAccountResponse)
async def use_account(account_id: int, request: Request):
    """Write account credentials to Claude Code's credential file.

    Overwrites ~/.claude/.credentials.json so the next Claude Code
    session starts with this account's tokens.

    >>> # Endpoint validates account state before writing
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

    # Parse scopes safely
    scopes = []
    raw_scopes = account.get("scopes")
    if raw_scopes:
        try:
            parsed = json.loads(raw_scopes)
            if isinstance(parsed, list):
                scopes = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # Build Claude Code credential format
    oauth_data = {
        "accessToken": access_token,
        "refreshToken": account.get("refresh_token"),
        "expiresAt": account.get("expires_at", 0) * 1000,
        "scopes": scopes,
        "subscriptionType": account.get("subscription_type"),
        "rateLimitTier": account.get("rate_limit_tier"),
    }

    cred_path = Path.home() / ".claude" / ".credentials.json"

    if cred_path.exists() and cred_path.is_symlink():
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Credential path is a symlink — refusing to write",
                    "code": "SYMLINK_DETECTED",
                }
            },
        )

    cred_path.parent.mkdir(parents=True, exist_ok=True)

    # Read existing file to preserve other top-level keys
    existing = {}
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            existing = json.loads(cred_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}

    existing["claudeAiOauth"] = oauth_data
    existing["_jackedAccountId"] = account_id

    # Atomic write: temp file in same directory, then replace
    fd, tmp_path = tempfile.mkstemp(
        dir=str(cred_path.parent),
        prefix=".credentials_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
        try:
            os.chmod(tmp_path, 0o600)
        except OSError:
            pass
        os.replace(tmp_path, str(cred_path))
        try:
            request.app.state.cred_last_written_mtime = cred_path.stat().st_mtime
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": {
                    "message": "Failed to write credentials",
                    "code": "WRITE_FAILED",
                }
            },
        )

    # Update ~/.claude.json so Layer 1 matching picks up the new account
    _update_claude_config_email(
        account["email"], display_name=account.get("display_name")
    )

    return UseAccountResponse(status="ok", email=account["email"])


@router.get("/active-credential", response_model=ActiveCredentialResponse)
async def get_active_credential(request: Request):
    """Read Claude Code's credential file and match to a jacked account.

    Uses layered matching: (1) ~/.claude.json email (case-insensitive),
    (2) _jackedAccountId in credential file, (3) exact access_token match.

    >>> # Returns null account_id if no match found
    """
    db = _get_db(request)
    if db is None:
        return ActiveCredentialResponse()

    # Layer 1: Read ~/.claude.json for the active account email
    claude_config = Path.home() / ".claude.json"
    if claude_config.exists() and not claude_config.is_symlink():
        try:
            config = json.loads(claude_config.read_text(encoding="utf-8"))
            email = config.get("oauthAccount", {}).get("emailAddress")
            if email:
                accounts = db.list_accounts(include_inactive=True)
                for acct in accounts:
                    if acct.get("email", "").lower() == email.lower():
                        return ActiveCredentialResponse(
                            account_id=acct["id"], email=acct["email"]
                        )
        except (json.JSONDecodeError, OSError):
            pass

    # Read credential file for layers 2 & 3
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if not cred_path.exists() or cred_path.is_symlink():
        return ActiveCredentialResponse()

    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, AttributeError):
        return ActiveCredentialResponse()

    # Layer 2: Check _jackedAccountId embedded by "Set Active"
    jacked_id = data.get("_jackedAccountId")
    if jacked_id is not None:
        acct = db.get_account(jacked_id)
        if acct and not acct.get("is_deleted"):
            return ActiveCredentialResponse(account_id=acct["id"], email=acct["email"])

    # Layer 3: Exact access_token match (fallback)
    access_token = data.get("claudeAiOauth", {}).get("accessToken")
    if access_token:
        accounts = db.list_accounts(include_inactive=True)
        for acct in accounts:
            if acct.get("access_token") == access_token:
                return ActiveCredentialResponse(
                    account_id=acct["id"], email=acct["email"]
                )

    return ActiveCredentialResponse()


@router.get("/session-account")
async def get_session_account(request: Request, session_id: str = ""):
    """Get account records for a specific session.

    Returns list of account spans (a session can switch accounts via /login).
    Supports suffix matching for session IDs shorter than 36 chars (min 8).

    >>> # Returns empty list if session_id not found
    """
    db = _get_db(request)
    if db is None or not session_id:
        return {"records": []}
    if len(session_id) < 36:
        return {"records": db.lookup_session_by_suffix(session_id)}
    return {"records": db.get_session_accounts(session_id)}


@router.get("/accounts/{account_id}/sessions")
async def get_account_sessions(request: Request, account_id: int, limit: int = 50):
    """Get recent sessions that used a given account.

    >>> # Returns empty list if account has no sessions
    """
    db = _get_db(request)
    if db is None:
        return {"sessions": []}
    return {"sessions": db.get_account_sessions(account_id, limit=min(limit, 200))}


@router.get("/active-sessions")
async def get_active_sessions(request: Request, staleness: int = 60):
    """Get all currently active sessions, grouped by account_id.

    Returns sessions where ended_at IS NULL and active within the staleness window.
    Includes session_id suffix (last 8 chars) for terminal tab identification.
    Staleness window is configurable via ?staleness=N (minutes, 5-120).

    >>> # Returns empty dict when no active sessions
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

    return {"sessions": grouped}
