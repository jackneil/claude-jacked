"""Per-model usage limits: the merged list and the limit to show.

:func:`parse_per_model` merges the per-model limits (for example the
weekly Fable limit) that the parsers in
:mod:`jacked.service.per_model_sources` read from ``cached_usage_raw``.
:func:`binding_model` selects the limit that the dashboard, the pill and
the tray show. :mod:`jacked.service.menubar_summary` re-exports each
function of this module for its callers and tests.
"""

from __future__ import annotations

from typing import Optional

from jacked.service.per_model_sources import (
    _claude_scoped_models,
    _codex_named_models,
    _legacy_seven_day_models,
)


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
