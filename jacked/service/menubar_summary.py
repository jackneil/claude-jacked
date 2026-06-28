"""Worst-account usage summary for the macOS menu-bar pill.

Pure Python — NO rumps / pyobjc imports — so it is safe to import from the API
layer, unit tests, and the mac agent alike. The mac agent's status-item timer
polls ``GET /api/menubar-summary`` (which calls :func:`compute_worst_account_summary`)
and renders :func:`menubar_title`.

The green/yellow/red thresholds here are the Python mirror of
``usageColorClass`` in ``jacked/data/web/js/components/usage.js`` — keep them in
lockstep so the pill's color can never disagree with the bar a user sees in the
panel/dashboard.
"""

from __future__ import annotations

from typing import Iterable, Optional

# Glyphs that carry color in a plain menu-bar string without needing an
# NSAttributedString — green/yellow/red filled circles.
_COLOR_GLYPH = {"green": "🟢", "yellow": "🟡", "red": "🔴"}


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


def compute_worst_account_summary(
    accounts: Iterable[dict],
) -> Optional[dict]:
    """Return a summary of the highest-utilization account, or None.

    The "worst" account is the one with the greatest ``max(5h, 7d)`` utilization
    among enabled, non-deleted accounts that have any usage data. Disabled
    (``is_active is False``) and soft-deleted accounts are skipped, as are
    accounts with no cached usage at all.

    >>> s = compute_worst_account_summary([
    ...     {"id": 1, "email": "a@x.com", "cached_usage_5h": 30, "cached_usage_7d": 40},
    ...     {"id": 2, "email": "b@x.com", "cached_usage_5h": 96, "cached_usage_7d": 78},
    ... ])
    >>> s["email"], s["five_hour"], s["seven_day"], s["max_pct"], s["color"]
    ('b@x.com', 96.0, 78.0, 96.0, 'red')
    >>> compute_worst_account_summary([]) is None
    True
    """
    best: Optional[tuple[float, float, float, dict]] = None
    for acct in accounts:
        if not acct:
            continue
        if acct.get("is_deleted"):
            continue
        if acct.get("is_active") is False:
            continue
        u5 = acct.get("cached_usage_5h")
        u7 = acct.get("cached_usage_7d")
        if u5 is None and u7 is None:
            continue
        f5 = float(u5 or 0.0)
        f7 = float(u7 or 0.0)
        worst = max(f5, f7)
        # Strict > keeps the first-seen account on ties, which matches the
        # priority-ordered list the API returns (lower priority wins ties).
        if best is None or worst > best[0]:
            best = (worst, f5, f7, acct)

    if best is None:
        return None

    worst, f5, f7, acct = best
    return {
        "account_id": acct.get("id"),
        "email": acct.get("email"),
        "organization_uuid": acct.get("organization_uuid") or None,
        "organization_name": acct.get("organization_name"),
        "five_hour": round(f5, 1),
        "seven_day": round(f7, 1),
        "max_pct": round(worst, 1),
        "color": usage_color_class(worst),
    }


def menubar_title(summary: Optional[dict]) -> str:
    """Render the menu-bar pill title from a summary.

    No data (no accounts, or none with usage) → an em dash placeholder. The
    agent handles the *server-down* (degraded) state separately; this function
    only ever sees live data.

    >>> menubar_title(None)
    '—'
    >>> menubar_title({"five_hour": 96.0, "seven_day": 78.0, "color": "red"})
    '🔴 96%·78%'
    >>> menubar_title({"five_hour": 40.4, "seven_day": 30.6, "color": "green"})
    '🟢 40%·31%'
    """
    if not summary:
        return "—"
    glyph = _COLOR_GLYPH.get(summary.get("color", ""), "")
    five = round(summary.get("five_hour") or 0)
    seven = round(summary.get("seven_day") or 0)
    return f"{glyph} {five}%·{seven}%".strip()
