"""The per-model limit parsers for each payload shape, and their helpers.

Claude, Codex and the legacy ``seven_day_<model>`` keys report per-model
limits in different shapes (the comment block below shows them). Each
parser here reads one shape, and
:func:`jacked.service.per_model.parse_per_model` merges the results.
:mod:`jacked.service.menubar_summary` re-exports each function of this
module and ``_LEGACY_MODEL_LABELS`` for its callers and tests.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from jacked.service.pill_text import percent_reading
from jacked.web.window_keeper import cache_reset_value

logger = logging.getLogger(__name__)

# (source, entry key) pairs whose skipped model limit was already logged
# at WARNING in this process. Repeats log at DEBUG: every poll and every
# page load parses the cached payloads again.
_SKIPPED_LIMITS_LOGGED: set = set()


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

    The canonical home of this helper is ``jacked.service.per_model_sources``.
    ``usage_block`` and ``jacked/codex/usage.py`` (through ``menubar_summary``)
    import it, so keep the name stable: a rename must update those imports too.

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


def _log_skipped_limit(source: str, key: str) -> None:
    """Log a model limit that the parser skipped: WARNING once per (source,
    key) in a process (the service logs at INFO, so a DEBUG-only line would
    never show), DEBUG after that. The line names the entry key and its
    source, never the value."""
    seen = (source, key)
    level = logging.DEBUG if seen in _SKIPPED_LIMITS_LOGGED else logging.WARNING
    _SKIPPED_LIMITS_LOGGED.add(seen)
    logger.log(
        level,
        "Model limit entry %s from %s has no finite percent; jacked shows no reading for it",
        key,
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
            _log_skipped_limit("limits", str(label).lower())
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
            _log_skipped_limit("rateLimitsByLimitId", str(limit_id).lower())
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
            _log_skipped_limit("seven_day", suffix)
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
