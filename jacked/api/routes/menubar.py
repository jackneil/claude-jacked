"""Menu-bar summary route.

A single, lightweight endpoint the macOS status-item timer polls to set the
live pill. The pill tracks the **active** account (the one selected in Claude
Code) — that's the usage the user is actually working against — so we resolve
the active account via the same 3-layer logic as ``/api/auth/active-credential``
and summarize it. ``worst`` (worst across all accounts) is included too for a
fleet-wide glance. Pure summary helpers live in
``jacked.service.menubar_summary`` (no rumps/pyobjc here).
"""

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from jacked.service.menubar_summary import (
    compute_active_account_summary,
    compute_worst_account_summary,
)

router = APIRouter()


@router.get("/menubar-summary")
async def menubar_summary(request: Request):
    """Active-account 5h·7d summary for the menu-bar pill.

    Returns ``{"active": {...}|null, "active_account_id": N|null,
    "worst": {...}|null, "account_count": N}``. ``active`` is what the pill
    renders (its ``color`` tints the arm icon); it's null when no active account
    is detected or it has no usage yet. A missing DB yields 503 so the agent can
    show a degraded pill rather than a wrong number.
    """
    db = getattr(request.app.state, "db", None)
    if db is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"error": {"message": "Database unavailable", "code": "DB_UNAVAILABLE"}},
        )

    rows = db.list_accounts(include_inactive=False)

    # Resolve the active account via the SAME 3-layer matching the
    # active-credential route uses (stamp → token → email+org), so the pill and
    # the dashboard never disagree on which account is active.
    active_id = None
    try:
        from jacked.api.routes.auth import get_active_credential

        cred = await get_active_credential(request)
        active_id = getattr(cred, "account_id", None)
    except Exception:
        active_id = None

    active = compute_active_account_summary(rows, active_id)
    if active is not None:
        _attach_next_refresh(active, rows, request.app.state)
    worst = compute_worst_account_summary(rows)
    return {
        "active": active,
        "active_account_id": active_id,
        "worst": worst,
        "account_count": len(rows),
    }


def _attach_next_refresh(active: dict, rows: list, state) -> None:
    """Annotate the active-account summary with when its usage will next refresh
    (unix seconds) + the poll interval/tier, so the panel can show a countdown.

    Preferred source is the REAL schedule the active-poll loop publishes to
    app.state (exact interval, incl. clamps/burn). Falls back to a tier-table
    estimate (compute_urgency_tier — same _TIER_INTERVALS, no hardcoding) when
    the loop hasn't ticked yet this process.
    """
    next_at = interval = tier = None
    acct_id = active.get("account_id")

    if getattr(state, "active_poll_account_id", None) == acct_id:
        at = getattr(state, "active_poll_at", None)
        iv = getattr(state, "active_poll_interval", None)
        if at and iv:
            next_at, interval, tier = at + iv, iv, getattr(state, "active_poll_tier", None)

    if next_at is None:
        row = next((r for r in rows if r.get("id") == acct_id), None)
        cached_at = row.get("usage_cached_at") if row else None
        if cached_at:
            try:
                from jacked.web.auth import compute_urgency_tier

                tier, interval = compute_urgency_tier(
                    row.get("cached_usage_5h"), row.get("cached_usage_7d"), 0.0
                )
                next_at = cached_at + interval
            except Exception:
                pass

    active["next_refresh_at"] = next_at
    active["poll_interval"] = interval
    active["poll_tier"] = tier
