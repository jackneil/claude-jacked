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

The pill shows only the limits that the account's plan has
(:mod:`jacked.usage_plans`, the same rule the account API uses). An account
that has no 5-hour window (a verified Ultra account, ChatGPT Pro in Codex)
shows no 5-hour number, even when an old reading is still in the cache. The
model limit that the dashboard shows (for example the weekly Fable limit)
is in the pill too, and it counts toward the pill color.
:mod:`jacked.service.pill_text` turns a summary into the menu-bar text and
into the hover text of the Windows and Linux tray icon.
"""

from __future__ import annotations

import logging
from typing import Iterable, Mapping, Optional

from jacked.service.pill_text import percent_reading
from jacked.service.per_model import (  # noqa: F401  re-exported for callers and tests
    parse_per_model,
    binding_model,
    binding_model_compact,
)
from jacked.service.per_model_sources import (  # noqa: F401  re-exported for callers and tests
    _LEGACY_MODEL_LABELS,
    _epoch_to_iso,
    _log_skipped_limit,
    _claude_scoped_models,
    _codex_named_models,
    _legacy_seven_day_models,
)
from jacked.service.usage_block import (  # noqa: F401  re-exported for callers and tests
    USAGE_STALE_SECS,
    usage_block,
    usage_block_of_row,
)

logger = logging.getLogger(__name__)

# (account id, exception type) pairs whose resolver failure was already
# logged at WARNING in this process. Repeats log at DEBUG: the account list
# and the pill read every account on each poll.
_VIEW_FAILURES_LOGGED: set = set()
# (account id, reason) pairs whose unreadable model limit was already logged
# at WARNING in this process, on the same rule.
_CAP_FAILURES_LOGGED: set = set()
_UNSET = object()


def safe_account_view(acct: Mapping, raw=_UNSET):
    """The plan view of an account row (``usage_plans.account_view``), or None.

    Every surface that lists accounts reads the 5-hour window through this
    view. A payload shape that makes the resolver fail must not stop the
    account list or the pill for every account, so a failure returns None
    and the caller shows the cached readings. The failure is logged as a
    WARNING with the account id and the exception type, once for each pair
    in a process; the WARNING never holds the payload or the exception text.
    Each failure also logs its traceback at DEBUG.

    *raw* is the row's parsed payload when the caller already has it.

    >>> safe_account_view({"subscription_type": "pro"}).plan.label
    'Pro'
    """
    from jacked.usage_plans import account_view

    try:
        return account_view(acct) if raw is _UNSET else account_view(acct, raw)
    except Exception as exc:  # noqa: BLE001 - one bad row must not fail a list
        account_id = acct.get("id") if isinstance(acct, Mapping) else None
        key = (account_id, type(exc).__name__)
        if key not in _VIEW_FAILURES_LOGGED:
            _VIEW_FAILURES_LOGGED.add(key)
            logger.warning(
                "Plan resolver failed for account %s (%s); showing its cached "
                "usage readings without plan and window facts",
                *key,
            )
        logger.debug("Plan resolver failure for account %s (%s)", *key, exc_info=True)
        return None


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
    return (
        bool(acct) and not acct.get("is_deleted") and acct.get("is_active") is not False
    )


def _log_cap_failure(account_id, reason: str, *, with_traceback: bool = False) -> None:
    """WARNING once per (account, reason) in a process, never with the
    payload or the exception text; the traceback goes to DEBUG."""
    key = (account_id, reason)
    if key not in _CAP_FAILURES_LOGGED:
        _CAP_FAILURES_LOGGED.add(key)
        logger.warning(
            "Model limit unreadable for account %s (%s); the pill and the tray "
            "show no model limit for it",
            *key,
        )
    if with_traceback:
        logger.debug("Model limit failure for account %s (%s)", *key, exc_info=True)


def _model_cap(acct: Mapping, view, raw) -> Optional[dict]:
    """The model limit that the pill shows for *acct*, or None.

    The selection is the selection of the dashboard (:func:`binding_model`):
    the limit that the provider marks active, else the limit with the
    highest use. *raw* is the row's parsed payload. A payload that jacked
    cannot read gives None, so one bad row cannot stop the pill. The parser
    skips a limit whose percent is not a finite number (no reading, never
    0%). The result holds only the cleaned name and the finite percent:
    every field of the summary must be JSON-safe, because
    /api/menubar-summary refuses NaN.
    """
    try:
        cap = binding_model_compact(raw, view.plan if view is not None else None)
        if cap is None:
            return None
        return {"label": cap["label"], "utilization": round(cap["utilization"], 1)}
    except Exception as exc:  # noqa: BLE001 - one bad payload must not stop the pill
        _log_cap_failure(acct.get("id"), type(exc).__name__, with_traceback=True)
        return None


def _reading(value) -> Optional[float]:
    """A cached window reading rounded to 0.1, or None when it is not a
    finite number (no reading, never 0%).

    >>> _reading(41.26), _reading("9"), _reading(float("inf")), _reading("x")
    (41.3, 9.0, None, None)
    """
    number = percent_reading(value)
    return None if number is None else round(number, 1)


def summarize_account(acct: dict) -> Optional[dict]:
    """Build a pill summary for a single account, or None if it has no usage.

    The 5-hour slot follows the account's plan (:func:`safe_account_view`):
    it is None when the account has no 5-hour window. A Claude account that
    has the window but no reading has no window open, which is 0% used. With
    no reading, a Codex window and a window that jacked cannot confirm are
    None. When the plan resolver fails, the 5-hour slot shows the cached
    reading.

    The 7-day slot keeps the cached rule: a Claude window with no reading is
    0% used, and a Codex window with no reading is a window Codex does not
    report, so it stays None instead of reading as 0% headroom.
    A cached value that is not a finite number is no reading.
    ``model_cap`` is the model limit that the dashboard shows for the
    account (:func:`_model_cap`), or None. ``max_pct`` and ``color`` come
    from the slots that have a value and from the model limit. With no value
    in the 5-hour slot and the 7-day slot there is no summary. ``blocked``
    carries the provider's own usage-blocked verdict
    (:func:`usage_block_of_row`); a blocked account is red at any percent.

    >>> s = summarize_account({"id": 1, "email": "a@x.com",
    ...                        "cached_usage_5h": 37, "cached_usage_7d": 87})
    >>> s["five_hour"], s["seven_day"], s["max_pct"], s["color"], s["blocked"]
    (37.0, 87.0, 87.0, 'yellow', False)
    >>> summarize_account({"id": 1, "email": "a@x.com"}) is None
    True
    >>> c = summarize_account({"id": 2, "provider": "codex", "cached_usage_7d": 40})
    >>> c["five_hour"], c["seven_day"], c["max_pct"], c["color"]
    (None, 40.0, 40.0, 'green')

    A verified Ultra account lists its limits without a 5-hour session, so
    an old 5-hour value in the cache is not a reading. Its weekly Fable
    limit is the worst limit, so it sets the color:

    >>> import json
    >>> ultra = {"id": 3, "subscription_type": "max",
    ...          "rate_limit_tier": "default_claude_max_20x",
    ...          "cached_usage_5h": 67.0, "cached_usage_7d": 74.0,
    ...          "cached_usage_raw": json.dumps({"five_hour": None, "limits": [
    ...              {"kind": "weekly_all", "percent": 74},
    ...              {"kind": "weekly_scoped", "percent": 100, "is_active": True,
    ...               "scope": {"model": {"display_name": "Fable"}}}]})}
    >>> u = summarize_account(ultra)
    >>> u["five_hour"], u["model_cap"]["label"], u["max_pct"], u["color"]
    (None, 'Fable', 100.0, 'red')
    >>> from jacked.service.pill_text import menubar_title
    >>> menubar_title(u)
    '7d 74%·F100%'
    """
    if not acct:
        return None
    u5 = acct.get("cached_usage_5h")
    u7 = acct.get("cached_usage_7d")
    if u5 is None and u7 is None:
        return None
    provider = acct.get("provider") or "claude"

    from jacked.usage_plans import raw_usage

    raw = raw_usage(acct)  # parsed once for the plan view and the model limit
    view = safe_account_view(acct, raw)
    five = view.five_hour_reading() if view is not None else u5
    f5 = None if five is None else _reading(five)
    if u7 is None and provider == "codex":
        f7 = None
    else:
        f7 = _reading(u7 or 0.0)
    readings = [v for v in (f5, f7) if v is not None]
    if not readings:
        # No window has a reading (a Codex plan without a 5-hour window and
        # no weekly reading): nothing to show.
        return None
    cap = _model_cap(acct, view, raw)
    if cap is not None:
        readings.append(cap["utilization"])
    worst = max(readings)
    blocked = usage_block_of_row(acct) is not None
    return {
        "account_id": acct.get("id"),
        "email": acct.get("email"),
        "provider": provider,
        "organization_uuid": acct.get("organization_uuid") or None,
        "organization_name": acct.get("organization_name"),
        "five_hour": f5,
        "seven_day": f7,
        "model_cap": cap,
        "max_pct": round(worst, 1),
        "color": "red" if blocked else usage_color_class(worst),
        "blocked": blocked,
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

    The "worst" account is the one with the greatest ``max_pct`` (the 5-hour
    window, the 7-day window and the model limit) among enabled, non-deleted
    accounts that have any usage data. Retained for a fleet-wide "is
    anything maxed" view; the pill itself uses the active account.

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
