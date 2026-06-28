"""Menu-bar summary route.

A single, lightweight endpoint the macOS status-item timer polls to set the
live pill text. Deliberately tiny so the timer stays cheap — it reuses the same
account rows the dashboard reads and the pure
``compute_worst_account_summary`` helper (no rumps/pyobjc here).
"""

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from jacked.service.menubar_summary import compute_worst_account_summary

router = APIRouter()


@router.get("/menubar-summary")
async def menubar_summary(request: Request):
    """Worst-account 5h·7d summary for the menu-bar pill.

    Returns ``{"worst": {...}|null, "account_count": N}``. ``worst`` is null
    when no enabled account has usage data yet. A missing DB yields 503 so the
    agent can show a degraded pill rather than a wrong number.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    rows = db.list_accounts(include_inactive=False)
    summary = compute_worst_account_summary(rows)
    return {"worst": summary, "account_count": len(rows)}
