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

import json
from datetime import datetime, timezone
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
    return (
        bool(acct) and not acct.get("is_deleted") and acct.get("is_active") is not False
    )


def summarize_account(acct: dict) -> Optional[dict]:
    """Build a pill summary for a single account, or None if it has no usage.

    A Claude window with no reading is "no active window", which is 0% used.
    A Codex window with no reading is a window Codex does not report for the
    account (a weekly-only plan has no 5h window), so it stays None instead
    of reading as 0% headroom. ``blocked`` carries the provider's own
    usage-blocked verdict (:func:`usage_block_of_row`); a blocked account is
    red at any percent.

    >>> s = summarize_account({"id": 1, "email": "a@x.com",
    ...                        "cached_usage_5h": 37, "cached_usage_7d": 87})
    >>> s["five_hour"], s["seven_day"], s["max_pct"], s["color"], s["blocked"]
    (37.0, 87.0, 87.0, 'yellow', False)
    >>> summarize_account({"id": 1, "email": "a@x.com"}) is None
    True
    >>> c = summarize_account({"id": 2, "provider": "codex", "cached_usage_7d": 40})
    >>> c["five_hour"], c["seven_day"], c["max_pct"], c["color"]
    (None, 40.0, 40.0, 'green')
    """
    if not acct:
        return None
    u5 = acct.get("cached_usage_5h")
    u7 = acct.get("cached_usage_7d")
    if u5 is None and u7 is None:
        return None
    provider = acct.get("provider") or "claude"

    def _window(value):
        if value is None and provider == "codex":
            return None
        return round(float(value or 0.0), 1)

    f5 = _window(u5)
    f7 = _window(u7)
    worst = max(v for v in (f5, f7) if v is not None)
    blocked = usage_block_of_row(acct) is not None
    return {
        "account_id": acct.get("id"),
        "email": acct.get("email"),
        "provider": provider,
        "organization_uuid": acct.get("organization_uuid") or None,
        "organization_name": acct.get("organization_name"),
        "five_hour": f5,
        "seven_day": f7,
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
    carries the color. No data → a hyphen placeholder, and a window the
    provider does not report (None) → a hyphen in its slot. (The agent
    handles the server-down/degraded state separately; this only ever sees
    live data.)

    >>> menubar_title(None)
    '-'
    >>> menubar_title({"five_hour": 37.0, "seven_day": 87.0, "color": "yellow"})
    '37%·87%'
    >>> menubar_title({"five_hour": 40.4, "seven_day": 30.6, "color": "green"})
    '40%·31%'
    >>> menubar_title({"five_hour": None, "seven_day": 100.0, "color": "red"})
    '-·100%'
    """
    if not summary:
        return "-"

    def _slot(value) -> str:
        return "-" if value is None else f"{round(value)}%"

    return f"{_slot(summary.get('five_hour'))}·{_slot(summary.get('seven_day'))}"


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
    >>> _epoch_to_iso("nope") is None
    True
    """
    if epoch is None:
        return None
    try:
        return datetime.fromtimestamp(int(epoch), tz=timezone.utc).isoformat()
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _as_num(v) -> float | int:
    """Coerce a utilization value to a number; unparseable → 0.

    Provider payloads are external input — a stray string percent ("92") or a
    null must never reach the ``sorted``/``>=`` comparisons below as a
    non-number (that would raise TypeError and, on the API path, 500 the
    accounts endpoint). ``bool`` maps to 0 (it is an int subclass but is never a
    real utilization).

    >>> _as_num(92), _as_num(42.5), _as_num("92"), _as_num(None), _as_num(True)
    (92, 42.5, 92.0, 0, 0)
    """
    if isinstance(v, bool):
        return 0
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def _claude_scoped_models(raw: dict) -> list[dict]:
    """Claude ``weekly_scoped`` model caps from the ``limits`` array."""
    out: list[dict] = []
    limits = raw.get("limits")
    if not isinstance(limits, list):
        return out
    for lim in limits:
        if not isinstance(lim, dict) or lim.get("kind") != "weekly_scoped":
            continue
        scope = lim.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        label = model.get("display_name") if isinstance(model, dict) else None
        if not label:
            continue
        out.append(
            {
                "key": str(label).lower(),
                "label": label,
                "utilization": _as_num(lim.get("percent")),
                "resets_at": lim.get("resets_at"),
                "severity": lim.get("severity"),
                "is_active": bool(lim.get("is_active")),
            }
        )
    return out


def _codex_named_models(raw: dict) -> list[dict]:
    """Codex named per-model limits from ``rateLimitsByLimitId``."""
    out: list[dict] = []
    by_limit = raw.get("rateLimitsByLimitId")
    if not isinstance(by_limit, dict):
        return out
    for limit_id, entry in by_limit.items():
        if not isinstance(entry, dict):
            continue
        label = entry.get("limitName")
        if not label:
            continue  # bare overall limit — already the 5h/7d bars
        # Prefer the weekly (secondary) window; fall back to primary (5h).
        window = entry.get("secondary")
        if not isinstance(window, dict) or window.get("usedPercent") is None:
            window = (
                entry.get("primary") if isinstance(entry.get("primary"), dict) else {}
            )
        out.append(
            {
                "key": str(limit_id).lower(),
                "label": label,
                "utilization": _as_num(window.get("usedPercent")),
                "resets_at": _epoch_to_iso(window.get("resetsAt")),
                "severity": None,
                "is_active": False,
            }
        )
    return out


def _legacy_seven_day_models(raw: dict) -> list[dict]:
    """Legacy ``seven_day_<model>`` caps — dict OR bare numeric (old payloads)."""
    out: list[dict] = []
    for suffix, label in _LEGACY_MODEL_LABELS.items():
        val = raw.get(f"seven_day_{suffix}")
        if isinstance(val, dict):
            out.append(
                {
                    "key": suffix,
                    "label": label,
                    "utilization": _as_num(val.get("utilization")),
                    "resets_at": val.get("resets_at"),
                    "severity": None,
                    "is_active": False,
                }
            )
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            out.append(
                {
                    "key": suffix,
                    "label": label,
                    "utilization": val,
                    "resets_at": None,
                    "severity": None,
                    "is_active": False,
                }
            )
    return out


def parse_per_model(raw: Optional[dict]) -> list[dict]:
    """Extract per-model usage caps from a raw usage payload (Claude or Codex).

    Returns a list of dicts, each shaped
    ``{"key", "label", "utilization", "resets_at", "severity", "is_active"}``,
    sorted by utilization descending. Providers are merged with the first
    source (Claude limits) winning on key collisions; the legacy source only
    fills keys not already present. Empty list when the payload has no per-model
    breakdown or is not a dict.

    Claude reports model caps as ``weekly_scoped`` entries in ``limits``:

    >>> claude = {"limits": [
    ...     {"kind": "weekly_all", "percent": 52, "is_active": False},
    ...     {"kind": "weekly_scoped", "percent": 92, "severity": "critical",
    ...      "resets_at": "2026-07-06T05:00:00+00:00", "is_active": True,
    ...      "scope": {"model": {"id": None, "display_name": "Fable"}}},
    ... ]}
    >>> [(m["label"], m["utilization"], m["is_active"]) for m in parse_per_model(claude)]
    [('Fable', 92, True)]

    Codex reports them in ``rateLimitsByLimitId`` (named limits only; the bare
    ``codex`` overall entry is skipped):

    >>> codex = {"rateLimitsByLimitId": {
    ...     "codex": {"primary": {"usedPercent": 2}},
    ...     "codex_bengalfox": {"limitName": "GPT-5.3-Codex-Spark",
    ...                          "primary": {"usedPercent": 7}},
    ... }}
    >>> [(m["label"], m["utilization"]) for m in parse_per_model(codex)]
    [('GPT-5.3-Codex-Spark', 7)]

    >>> parse_per_model(None)
    []
    """
    if not isinstance(raw, dict):
        return []

    out: dict[str, dict] = {}
    for entry in (
        *_claude_scoped_models(raw),
        *_codex_named_models(raw),
        *_legacy_seven_day_models(raw),
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


def binding_model_compact(raw: Optional[dict]) -> Optional[dict]:
    """Compact binding-model summary for a raw usage payload, for the inline
    bar's WebSocket payloads. Returns ``{label, utilization, resets_at,
    severity}`` or None.

    >>> binding_model_compact({"limits": [
    ...     {"kind": "weekly_scoped", "percent": 92, "severity": "critical",
    ...      "is_active": True, "scope": {"model": {"display_name": "Fable"}}}]})
    {'label': 'Fable', 'utilization': 92, 'resets_at': None, 'severity': 'critical'}
    >>> binding_model_compact({}) is None
    True
    """
    bm = binding_model(parse_per_model(raw))
    if bm is None:
        return None
    return {
        "label": bm["label"],
        "utilization": bm["utilization"],
        "resets_at": bm.get("resets_at"),
        "severity": bm.get("severity"),
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
