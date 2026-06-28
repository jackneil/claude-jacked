"""Account usage summary for the macOS menu-bar pill.

Pure Python — NO rumps / pyobjc imports — so it is safe to import from the API
layer, unit tests, and the mac agent alike. The mac agent's status-item timer
polls ``GET /api/menubar-summary`` and renders the result: a "J" icon tinted by
``summary["color"]`` plus :func:`menubar_title` as the text.

The pill tracks the **active** account (the one currently selected in Claude
Code) — that's the usage the user actually cares about — via
:func:`compute_active_account_summary`. :func:`compute_worst_account_summary`
(worst across all accounts) is retained for callers that want the fleet-wide
"is anything maxed" view.

The green/yellow/red thresholds here are the Python mirror of
``usageColorClass`` in ``jacked/data/web/js/components/usage.js`` — keep them in
lockstep so the pill's color can never disagree with the bar a user sees in the
panel/dashboard.
"""

from __future__ import annotations

from typing import Iterable, Optional


def usage_color_class(pct: Optional[float]) -> str:
    """Color class for a usage percentage (mirror of JS ``usageColorClass``).

    >>> usage_color_class(0)
    'green'
    >>> usage_color_class(70.9)
    'green'
    >>> usage_color_class(71)
    'yellow'
    >>> usage_color_class(89.9)
    'yellow'
    >>> usage_color_class(90)
    'red'
    >>> usage_color_class(150)
    'red'
    """
    p = max(0.0, min(100.0, float(pct or 0.0)))
    if p >= 90:
        return "red"
    if p >= 71:
        return "yellow"
    return "green"


def _eligible(acct: dict) -> bool:
    """True if an account should count toward the pill (enabled, not deleted)."""
    return bool(acct) and not acct.get("is_deleted") and acct.get("is_active") is not False


def summarize_account(acct: dict) -> Optional[dict]:
    """Build a pill summary for a single account, or None if it has no usage.

    >>> s = summarize_account({"id": 1, "email": "a@x.com",
    ...                        "cached_usage_5h": 37, "cached_usage_7d": 87})
    >>> s["five_hour"], s["seven_day"], s["max_pct"], s["color"]
    (37.0, 87.0, 87.0, 'yellow')
    >>> summarize_account({"id": 1, "email": "a@x.com"}) is None
    True
    """
    if not acct:
        return None
    u5 = acct.get("cached_usage_5h")
    u7 = acct.get("cached_usage_7d")
    if u5 is None and u7 is None:
        return None
    f5 = float(u5 or 0.0)
    f7 = float(u7 or 0.0)
    worst = max(f5, f7)
    return {
        "account_id": acct.get("id"),
        "email": acct.get("email"),
        "provider": acct.get("provider") or "claude",
        "organization_uuid": acct.get("organization_uuid") or None,
        "organization_name": acct.get("organization_name"),
        "five_hour": round(f5, 1),
        "seven_day": round(f7, 1),
        "max_pct": round(worst, 1),
        "color": usage_color_class(worst),
    }


def compute_active_account_summary(
    accounts: Iterable[dict], active_account_id: Optional[int]
) -> Optional[dict]:
    """Summary of the account currently active in Claude Code, or None.

    This is what the pill shows — the usage of the account the user is actually
    working in, not the worst account in the fleet. Returns None when there is
    no active account, it's disabled/deleted, or it has no usage data yet.

    >>> accts = [
    ...     {"id": 1, "email": "me@x.com", "cached_usage_5h": 37, "cached_usage_7d": 87},
    ...     {"id": 5, "email": "idle@x.com", "cached_usage_5h": 0, "cached_usage_7d": 100},
    ... ]
    >>> s = compute_active_account_summary(accts, 1)
    >>> s["email"], s["five_hour"], s["seven_day"], s["color"]
    ('me@x.com', 37.0, 87.0, 'yellow')
    >>> compute_active_account_summary(accts, None) is None
    True
    >>> compute_active_account_summary(accts, 999) is None
    True
    """
    if active_account_id is None:
        return None
    for acct in accounts:
        if acct and acct.get("id") == active_account_id and _eligible(acct):
            return summarize_account(acct)
    return None


def compute_worst_account_summary(accounts: Iterable[dict]) -> Optional[dict]:
    """Return a summary of the highest-utilization account, or None.

    The "worst" account is the one with the greatest ``max(5h, 7d)`` utilization
    among enabled, non-deleted accounts that have any usage data. Retained for a
    fleet-wide "is anything maxed" view; the pill itself uses the active account.

    >>> s = compute_worst_account_summary([
    ...     {"id": 1, "email": "a@x.com", "cached_usage_5h": 30, "cached_usage_7d": 40},
    ...     {"id": 2, "email": "b@x.com", "cached_usage_5h": 96, "cached_usage_7d": 78},
    ... ])
    >>> s["email"], s["max_pct"], s["color"]
    ('b@x.com', 96.0, 'red')
    >>> compute_worst_account_summary([]) is None
    True
    """
    best: Optional[dict] = None
    for acct in accounts:
        if not _eligible(acct):
            continue
        s = summarize_account(acct)
        # Strict > keeps the first-seen account on ties, matching the
        # priority-ordered list the API returns (lower priority wins ties).
        if s is not None and (best is None or s["max_pct"] > best["max_pct"]):
            best = s
    return best


def menubar_title(summary: Optional[dict]) -> str:
    """Render the menu-bar pill text — just the percentages; the "J" icon
    carries the color. No data → an em-dash placeholder. (The agent handles the
    server-down/degraded state separately; this only ever sees live data.)

    >>> menubar_title(None)
    '—'
    >>> menubar_title({"five_hour": 37.0, "seven_day": 87.0, "color": "yellow"})
    '37%·87%'
    >>> menubar_title({"five_hour": 40.4, "seven_day": 30.6, "color": "green"})
    '40%·31%'
    """
    if not summary:
        return "—"
    five = round(summary.get("five_hour") or 0)
    seven = round(summary.get("seven_day") or 0)
    return f"{five}%·{seven}%"
