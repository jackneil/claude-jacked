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

import json
import logging
from datetime import datetime, timezone
from typing import Iterable, Mapping, Optional

from jacked.service.pill_text import percent_reading
from jacked.web.window_keeper import cache_reset_value

logger = logging.getLogger(__name__)

# (account id, exception type) pairs whose resolver failure was already
# logged at WARNING in this process. Repeats log at DEBUG: the account list
# and the pill read every account on each poll.
_VIEW_FAILURES_LOGGED: set = set()
# (account id, reason) pairs whose unreadable model limit was already logged
# at WARNING in this process, on the same rule.
_CAP_FAILURES_LOGGED: set = set()
# (source, model label) pairs whose skipped model limit was already logged
# at WARNING in this process, on the same rule: every poll and every page
# load parses the cached payloads again.
_SKIPPED_LIMITS_LOGGED: set = set()
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


# ---------------------------------------------------------------------------
# Per-model usage caps
# ---------------------------------------------------------------------------
#
# Both providers report per-model weekly caps in ``cached_usage_raw``, but in
# different shapes:
#   - Claude: a ``limits`` array; model caps are the ``weekly_scoped`` entries
#     whose ``scope.model.display_name`` names the model (e.g. "Fable"). One
#     entry may carry ``is_active: true`` — the binding constraint.
#   - Codex: a ``rateLimitsByLimitId`` map; named limits carry a ``limitName``
#     (e.g. "GPT-5.3-Codex-Spark"). The bare overall ``codex`` entry has no
#     ``limitName`` and is already covered by the 5h/7d bars.
# The legacy ``seven_day_<model>`` keys are a fallback for older cached
# payloads (Anthropic now leaves them null in favor of ``limits``).

_LEGACY_MODEL_LABELS = {
    "sonnet": "Sonnet",
    "opus": "Opus",
    "oauth_apps": "OAuth Apps",
    "cowork": "Cowork",
}


def _epoch_to_iso(epoch) -> Optional[str]:
    """Unix epoch seconds → ISO-8601 UTC string, or None if not convertible.

    Canonical home for this helper; also imported by ``jacked/codex/usage.py``,
    so keep the name stable (a rename must update that import too).

    >>> _epoch_to_iso(None) is None
    True
    >>> _epoch_to_iso(0)
    '1970-01-01T00:00:00+00:00'
    >>> _epoch_to_iso("nope") is None, _epoch_to_iso(True) is None
    (True, True)
    """
    if epoch is None or isinstance(epoch, bool):
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _log_skipped_limit(source: str, label: str) -> None:
    """Log a model limit that the parser skipped: WARNING once per (source,
    label) in a process, DEBUG after that. The line names the limit, never
    the value."""
    key = (source, label)
    level = logging.DEBUG if key in _SKIPPED_LIMITS_LOGGED else logging.WARNING
    _SKIPPED_LIMITS_LOGGED.add(key)
    logger.log(
        level,
        "Model limit %s from %s has no finite percent; jacked shows no reading for it",
        label,
        source,
    )


def _claude_scoped_models(raw: dict, plan=None) -> list[dict]:
    """Claude ``weekly_scoped`` model caps from the ``limits`` array.

    An entry whose percent is not a finite number is skipped (no reading,
    never 0%). A reset that is not a timestamp is no reset, and a severity
    that is not text is no severity.
    """
    from jacked.usage_plans import clean_label, model_window_seconds

    window_secs = model_window_seconds("claude", None, "weekly_model", plan)
    out: list[dict] = []
    limits = raw.get("limits")
    if not isinstance(limits, list):
        return out
    for lim in limits:
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        scope = lim.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        # Provider text reaches the dashboard and the terminal: no control
        # or invisible characters, at most 40 characters.
        label = clean_label(model.get("display_name")) if isinstance(model, dict) else None
        if not label:
            continue
        utilization = percent_reading(lim.get("percent"))
        if utilization is None:
            _log_skipped_limit("limits", label)
            continue
        severity = lim.get("severity")
        out.append(
            {
                "key": str(label).lower(),
                "label": label,
                "utilization": utilization,
                "resets_at": cache_reset_value(lim.get("resets_at")),
                "severity": severity if isinstance(severity, str) else None,
                "is_active": bool(lim.get("is_active")),
                "window_seconds": window_secs,
            }
        )
    return out


def _codex_named_models(raw: dict, plan=None) -> list[dict]:
    """Codex named per-model limits from ``rateLimitsByLimitId``.

    The weekly (secondary) window gives the percent; when it has no finite
    percent, the primary (5-hour) window gives it. A limit with no finite
    percent in either window is skipped (no reading, never 0%).
    ``window_seconds`` is the chosen window's own ``windowDurationMins`` when
    it has one, else the length of the slot it came from (``weekly`` for a
    secondary window, ``session`` for a primary one).
    """
    from jacked.usage_plans import clean_label, model_window_seconds

    out: list[dict] = []
    by_limit = raw.get("rateLimitsByLimitId")
    if not isinstance(by_limit, dict):
        return out
    for limit_id, entry in by_limit.items():
        if not isinstance(entry, dict):
            continue
        label = clean_label(entry.get("limitName"))
        if not label:
            continue  # bare overall limit — already the 5h/7d bars
        # Prefer the weekly (secondary) window; fall back to primary (5h).
        window = entry.get("secondary")
        slot = "weekly"
        utilization = (
            percent_reading(window.get("usedPercent")) if isinstance(window, dict) else None
        )
        if utilization is None:
            window = (
                entry.get("primary") if isinstance(entry.get("primary"), dict) else {}
            )
            slot = "session"
            utilization = percent_reading(window.get("usedPercent"))
        if utilization is None:
            _log_skipped_limit("rateLimitsByLimitId", label)
            continue
        out.append(
            {
                "key": str(limit_id).lower(),
                "label": label,
                "utilization": utilization,
                "resets_at": _epoch_to_iso(window.get("resetsAt")),
                "severity": None,
                "is_active": False,
                "window_seconds": model_window_seconds("codex", window, slot, plan),
            }
        )
    return out


def _legacy_seven_day_models(raw: dict, plan=None) -> list[dict]:
    """Legacy ``seven_day_<model>`` caps — dict OR bare numeric (old payloads).

    An entry whose percent is not a finite number is skipped (no reading,
    never 0%), and a reset that is not a timestamp is no reset.
    """
    from jacked.usage_plans import model_window_seconds

    window_secs = model_window_seconds("claude", None, "weekly_model", plan)
    out: list[dict] = []
    for suffix, label in _LEGACY_MODEL_LABELS.items():
        val = raw.get(f"seven_day_{suffix}")
        if isinstance(val, dict):
            percent, resets_at = val.get("utilization"), val.get("resets_at")
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            percent, resets_at = val, None
        else:
            continue
        utilization = percent_reading(percent)
        if utilization is None:
            _log_skipped_limit(f"seven_day_{suffix}", label)
            continue
        out.append(
            {
                "key": suffix,
                "label": label,
                "utilization": utilization,
                "resets_at": cache_reset_value(resets_at),
                "severity": None,
                "is_active": False,
                "window_seconds": window_secs,
            }
        )
    return out


def parse_per_model(raw: Optional[dict], plan=None) -> list[dict]:
    """Extract per-model usage caps from a raw usage payload (Claude or Codex).

    Returns a list of dicts, each shaped ``{"key", "label", "utilization",
    "resets_at", "severity", "is_active", "window_seconds"}``, sorted by
    utilization descending. ``window_seconds`` is the length of the limit's
    window (the dashboard draws the elapsed marker from it); *plan* (a
    :class:`jacked.usage_plans.PlanMatch`) supplies plan-specific lengths. Providers are merged with the first
    source (Claude limits) winning on key collisions; the legacy source only
    fills keys not already present. Empty list when the payload has no per-model
    breakdown or is not a dict. A limit whose percent is not a finite number
    is left out (no reading, never 0%), and every ``resets_at`` is an ISO
    string or None, so the result is always strict JSON.

    Claude reports model caps as ``weekly_scoped`` entries in ``limits``:

    >>> claude = {"limits": [
    ...     {"kind": "weekly_all", "percent": 52, "is_active": False},
    ...     {"kind": "weekly_scoped", "percent": 92, "severity": "critical",
    ...      "resets_at": "2026-07-06T05:00:00+00:00", "is_active": True,
    ...      "scope": {"model": {"id": None, "display_name": "Fable"}}},
    ... ]}
    >>> [(m["label"], m["utilization"], m["is_active"]) for m in parse_per_model(claude)]
    [('Fable', 92.0, True)]

    Codex reports them in ``rateLimitsByLimitId`` (named limits only; the bare
    ``codex`` overall entry is skipped):

    >>> codex = {"rateLimitsByLimitId": {
    ...     "codex": {"primary": {"usedPercent": 2}},
    ...     "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark",
    ...                          "primary": {"usedPercent": 7}},
    ... }}
    >>> [(m["label"], m["utilization"]) for m in parse_per_model(codex)]
    [('GPT-5.3-Codex-Spark', 7.0)]

    >>> parse_per_model(None)
    []
    """
    if not isinstance(raw, dict):
        return []

    out: dict[str, dict] = {}
    for entry in (
        *_claude_scoped_models(raw, plan),
        *_codex_named_models(raw, plan),
        *_legacy_seven_day_models(raw, plan),
    ):
        out.setdefault(entry["key"], entry)  # first source wins per key

    return sorted(out.values(), key=lambda m: m["utilization"], reverse=True)


def binding_model(per_model: list[dict]) -> Optional[dict]:
    """The per-model cap to surface inline (under the 5h/7d bars), or None.

    Shown for every account that reports a per-model cap, regardless of level —
    users want to always see e.g. their Fable usage, not only when it's near the
    limit. Selection when an account reports more than one model:
    1. the entry the provider flags ``is_active`` — THE current binding
       constraint; else
    2. the highest-utilization model (``per_model`` is sorted descending).

    None only when the account reports no per-model cap at all.

    >>> binding_model([{"label": "Fable", "utilization": 40, "is_active": True},
    ...                 {"label": "Sonnet", "utilization": 88, "is_active": False}])["label"]
    'Fable'
    >>> binding_model([{"label": "Fable", "utilization": 92, "is_active": False},
    ...                 {"label": "Sonnet", "utilization": 4, "is_active": False}])["label"]
    'Fable'
    >>> binding_model([{"label": "Sonnet", "utilization": 0, "is_active": False}])["label"]
    'Sonnet'
    >>> binding_model([]) is None
    True
    """
    for m in per_model:
        if m.get("is_active"):
            return m
    return per_model[0] if per_model else None


def binding_model_compact(raw: Optional[dict], plan=None) -> Optional[dict]:
    """Compact binding-model summary for a raw usage payload, for the inline
    bar's WebSocket payloads. Returns ``{label, utilization, resets_at,
    severity, is_active, window_seconds}`` or None. *plan* supplies
    plan-specific window lengths (see :func:`parse_per_model`).

    >>> binding_model_compact({"limits": [
    ...     {"kind": "weekly_scoped", "percent": 92, "severity": "critical",
    ...      "is_active": True, "scope": {"model": {"display_name": "Fable"}}}]})
    {'label': 'Fable', 'utilization': 92.0, 'resets_at': None, 'severity': 'critical', 'is_active': True, 'window_seconds': 604800}
    >>> binding_model_compact({}) is None
    True
    """
    bm = binding_model(parse_per_model(raw, plan))
    if bm is None:
        return None
    return {
        "label": bm["label"],
        "utilization": bm["utilization"],
        "resets_at": bm.get("resets_at"),
        "severity": bm.get("severity"),
        "is_active": bool(bm.get("is_active")),
        "window_seconds": bm.get("window_seconds"),
    }


# ---------------------------------------------------------------------------
# Provider-reported usage block (Codex)
# ---------------------------------------------------------------------------
#
# A Codex ``account/rateLimits/read`` result says outright whether the account
# may send requests: ``ordinaryUsageAllowed`` is the backend's own verdict, and
# ``rateLimits.rateLimitReachedType`` / ``spendControlReached`` name why. Only
# an explicit False blocks: once the included usage runs out, credits can keep
# ordinary requests allowed while a reached type is still set. The verdict can
# hold below 100% (a spend control or a workspace limit), so it is surfaced
# as-is instead of inferred from percents.

# A reading older than this is stale: six 5-minute poll intervals. Mirror of
# USAGE_STALE_SECS in jacked/data/web/js/components/usage.js.
USAGE_STALE_SECS = 30 * 60


def usage_block(raw: Optional[dict], now: Optional[float] = None) -> Optional[dict]:
    """The provider's "usage is blocked" verdict from a cached payload, or None.

    Returns ``{"reason": str, "resets_at": iso|None}`` where reason is
    ``limit_reached``, ``spend_limit`` or ``blocked``. Only a Codex payload
    with ``ordinaryUsageAllowed: false`` blocks; any other shape returns None.
    A block whose reset time has passed returns None: the reading is older
    than the reset, so it no longer says anything about the account.

    >>> usage_block(None) is None
    True
    >>> usage_block({"ordinaryUsageAllowed": True,
    ...              "rateLimits": {"rateLimitReachedType": "rate_limit_reached"}}) is None
    True
    >>> usage_block({"ordinaryUsageAllowed": False,
    ...              "rateLimits": {"rateLimitReachedType": "rate_limit_reached"},
    ...              "rateLimitUpsell": {"reset_at": 4102444800}}, now=0)
    {'reason': 'limit_reached', 'resets_at': '2100-01-01T00:00:00+00:00'}
    >>> usage_block({"ordinaryUsageAllowed": False,
    ...              "rateLimits": {"spendControlReached": True}}, now=0)
    {'reason': 'spend_limit', 'resets_at': None}
    >>> usage_block({"ordinaryUsageAllowed": False, "rateLimits": {},
    ...              "rateLimitUpsell": {"reset_at": 100}}, now=200) is None
    True
    """
    if not isinstance(raw, dict) or raw.get("ordinaryUsageAllowed") is not False:
        return None
    rl = raw.get("rateLimits") if isinstance(raw.get("rateLimits"), dict) else {}
    if rl.get("spendControlReached") is True:
        reason = "spend_limit"
    elif rl.get("rateLimitReachedType"):
        reason = "limit_reached"
    else:
        reason = "blocked"
    upsell = raw.get("rateLimitUpsell") if isinstance(raw.get("rateLimitUpsell"), dict) else {}
    reset = upsell.get("reset_at")
    if not isinstance(reset, (int, float)) or isinstance(reset, bool):
        # No upsell reset: the latest reset among exhausted windows.
        resets = [
            w.get("resetsAt")
            for w in (rl.get("primary"), rl.get("secondary"))
            if isinstance(w, dict)
            and isinstance(w.get("usedPercent"), (int, float))
            and w["usedPercent"] >= 100
            and isinstance(w.get("resetsAt"), (int, float))
        ]
        reset = max(resets) if resets else None
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    if reset is not None and reset <= current:
        return None
    return {"reason": reason, "resets_at": _epoch_to_iso(reset)}


def usage_block_of_row(row: dict, now: Optional[float] = None) -> Optional[dict]:
    """:func:`usage_block` for a DB account row (parses ``cached_usage_raw``).

    Adds ``read_at`` (the row's ``usage_cached_at``). A block with no reset
    time counts only while the reading is fresh (:data:`USAGE_STALE_SECS`):
    nothing else can end it, and a row the poller no longer reads would
    otherwise say "blocked" forever. A block with a reset time stands until
    that reset. A malformed cached payload reads as "no block".

    >>> import json as _j
    >>> raw = _j.dumps({"ordinaryUsageAllowed": False,
    ...                 "rateLimits": {"spendControlReached": True}})
    >>> usage_block_of_row({"cached_usage_raw": raw, "usage_cached_at": 1000}, now=1060)
    {'reason': 'spend_limit', 'resets_at': None, 'read_at': 1000}
    >>> usage_block_of_row({"cached_usage_raw": raw, "usage_cached_at": 1000},
    ...                    now=1000 + USAGE_STALE_SECS + 1) is None
    True
    >>> usage_block_of_row({"cached_usage_raw": "not json"}) is None
    True
    """
    text = (row or {}).get("cached_usage_raw")
    if not text:
        return None
    try:
        block = usage_block(json.loads(text), now=now)
    except (TypeError, ValueError):
        return None
    if block is None:
        return None
    read_at = row.get("usage_cached_at")
    read_at = int(read_at) if isinstance(read_at, (int, float)) and not isinstance(read_at, bool) else None
    if block["resets_at"] is None:
        current = datetime.now(timezone.utc).timestamp() if now is None else now
        if read_at is None or current - read_at > USAGE_STALE_SECS:
            return None
    block["read_at"] = read_at
    return block
