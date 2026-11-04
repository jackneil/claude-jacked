"""Wall policy: the one predicate for auto-swap departure and admission (#224i).

An account is walled when its weekly limit (rule 2a) or its binding model
limit (rule 2b) is at or above the plan's ``weekly_critical_pct`` and that
limit does not reset soon. A walled account cannot serve. Every consumer uses
the predicates in this module, so they agree on what a wall is:

- ``should_swap_now`` leaves a walled active account, with or without a
  target (:func:`walled_reason`).
- ``pick_best_target`` and the monitor's poll clamp never offer a walled
  candidate, or one that cannot serve the model the active account binds on
  (:func:`_cannot_serve`).
- The monitor carries the active account's wall apart from the departure
  reason. A wall bypasses emergence persistence and the minimum residency
  whichever reason wins, a walled account with no target is exhaustion and
  not a stall, and the decision log shows the wall of each candidate
  (:func:`candidate_walls`).

The threshold is always the account's own plan field
(``weekly_critical_threshold``). No caller can give another value: a value
that is not the plan field lets the departure rule and the admission rule
disagree, and the swap undoes itself at the next tick (a ping-pong).
"""

from __future__ import annotations

from datetime import datetime, timezone

from jacked.service.menubar_summary import binding_model, parse_per_model
from jacked.service.usage_pacing import coerce_pct
from jacked.usage_plans import raw_usage
from jacked.web.auto_swap.burn import RESET_SUPPRESS_MINUTES
from jacked.web.auto_swap.diagnostics import weekly_critical_threshold
from jacked.web.auto_swap.tiers import _resolve_now

# Reason-string prefixes. DO NOT change these without updating
# usage_monitor's `_trigger_for_reason` mapping.
REASON_PREFIX_WEEKLY_CRITICAL = "weekly critical:"
REASON_PREFIX_MODEL_WEEKLY = "model weekly critical:"


def _epoch_or_inf(resets_at: str | None) -> float:
    """Parse an ISO reset timestamp to UTC epoch seconds for chronological
    sorting. Lexicographic string sort diverges from chronological for mixed
    formats (Z suffix vs explicit offsets), so the key must be numeric.
    float('inf') on missing, non-string or unparseable values (sorts last).

    >>> _epoch_or_inf("1970-01-01T00:01:00Z"), _epoch_or_inf(1793780314)
    (60.0, inf)
    """
    if not resets_at or not isinstance(resets_at, str):
        return float("inf")
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return float("inf")


def _model_limits(account: dict) -> list[dict]:
    """The account's per-model weekly limits from its cached payload.

    The parser the pill, the dashboard bar and the poll urgency use
    (:func:`jacked.service.menubar_summary.parse_per_model`).

    >>> raw = '{"limits": [{"kind": "weekly_scoped", "percent": 99, "is_active": true,'
    >>> raw += ' "scope": {"model": {"display_name": "Fable"}}}]}'
    >>> [(m["key"], m["utilization"]) for m in _model_limits({"cached_usage_raw": raw})]
    [('fable', 99)]
    >>> _model_limits({})
    []
    """
    return parse_per_model(raw_usage(account))


def _resets_by(resets_at, now: datetime, within_seconds: float = 0.0) -> bool:
    """True when the limit has reset, or resets within *within_seconds*.

    A reading of a limit whose reset has passed is from before the reset, so
    it is stale. False for a missing, non-string or unparseable reset:
    nothing then shows that the reading is stale or that the limit lifts.

    >>> now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    >>> _resets_by("2026-05-04T11:55:00+00:00", now)
    True
    >>> _resets_by("2026-05-04T12:20:00+00:00", now), _resets_by("2026-05-04T12:20:00Z", now, 1800)
    (False, True)
    >>> _resets_by(None, now, 1800), _resets_by(1793780314, now, 1800)
    (False, False)
    """
    return _epoch_or_inf(resets_at) <= _resolve_now(now).timestamp() + within_seconds


def _walled_on(per_model: list[dict], key: str, threshold: float, now: datetime) -> bool:
    """True when *per_model* holds the model limit *key* at *threshold* or
    above, and that limit has not reset yet (a reading from before its
    reset is stale and holds nothing back).

    >>> now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    >>> _walled_on([{"key": "fable", "utilization": 98}], "fable", 98.0, now)
    True
    >>> _walled_on([{"key": "sonnet", "utilization": 100}], "fable", 98.0, now)
    False
    >>> _walled_on([{"key": "fable", "utilization": 100,
    ...              "resets_at": "2026-05-04T11:55:00+00:00"}], "fable", 98.0, now)
    False
    """
    for limit in per_model:
        if limit.get("key") == key:
            pct = coerce_pct(limit.get("utilization"))
            return (
                pct is not None
                and pct >= threshold
                and not _resets_by(limit.get("resets_at"), now)
            )
    return False


def _walled_reason(
    account: dict,
    threshold: float,
    now: datetime,
    per_model: list[dict] | None = None,
) -> str | None:
    """The walled departure (rule 2a, then 2b) that fires on *account* as the
    active account, or None.

    2a. The weekly limit (``cached_usage_7d``) is at *threshold* or above,
        and its reset (``cached_7d_resets_at``) is still ahead and not
        within RESET_SUPPRESS_MINUTES.
    2b. The binding model limit is at *threshold* or above, and its reset is
        still ahead and not within RESET_SUPPRESS_MINUTES. The binding model
        is the limit the provider flags ``is_active``, else the highest: the
        same selection as the pill and the poll urgency
        (:func:`binding_model`).

    A reset that has already passed makes the reading stale (it is from
    before the reset): like a reset within the suppression window, it is
    neither a departure nor an admission block. A missing or unreadable
    reset keeps the wall. A missing reading is never a wall.
    ``pick_best_target`` applies this same predicate to each candidate, so
    a swap never lands on an account that this rule ejects on the next
    tick. *per_model* is the account's parsed per-model limits when the
    caller has them. *threshold* is the account's plan field: callers go
    through :func:`walled_reason` or :func:`candidate_walls`.

    >>> now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    >>> _walled_reason({"cached_usage_7d": 100.0}, 98.0, now)
    'weekly critical: 7d usage 100.0% >= 98%'
    >>> _walled_reason({"cached_usage_7d": 60.0}, 98.0, now,
    ...                [{"key": "fable", "label": "Fable", "utilization": 100,
    ...                  "resets_at": None, "is_active": True}])
    'model weekly critical: Fable 100.0% >= 98%'
    >>> _walled_reason({"cached_usage_7d": 99.0,
    ...                 "cached_7d_resets_at": "2026-05-04T12:10:00+00:00"}, 98.0, now) is None
    True
    >>> _walled_reason({"cached_usage_7d": 100.0,
    ...                 "cached_7d_resets_at": "2026-05-04T11:55:00+00:00"}, 98.0, now) is None
    True
    """
    lift_window = RESET_SUPPRESS_MINUTES * 60
    weekly = coerce_pct(account.get("cached_usage_7d"))
    if (
        weekly is not None
        and weekly >= threshold
        and not _resets_by(account.get("cached_7d_resets_at"), now, lift_window)
    ):
        return (
            f"{REASON_PREFIX_WEEKLY_CRITICAL} 7d usage {weekly:.1f}% >= "
            f"{threshold:g}%"
        )
    model = binding_model(per_model if per_model is not None else _model_limits(account))
    pct = coerce_pct(model.get("utilization")) if model else None
    if (
        pct is not None
        and pct >= threshold
        and not _resets_by(model.get("resets_at"), now, lift_window)
    ):
        return (
            f"{REASON_PREFIX_MODEL_WEEKLY} {model['label']} {pct:.1f}% >= "
            f"{threshold:g}%"
        )
    return None


def walled_reason(account: dict, now: datetime | None = None) -> str | None:
    """The walled departure (rule 2a, then 2b) of *account* as the active
    account, or None, at the account's own plan threshold.

    ``should_swap_now`` gives this reason when rules 1, 1b and 2 do not fire
    first. The monitor calls it once per tick to carry the wall apart from
    the reason that wins.

    >>> now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    >>> walled_reason({"cached_usage_7d": 99.0}, now)
    'weekly critical: 7d usage 99.0% >= 98%'
    >>> walled_reason({"cached_usage_7d": 97.0}, now) is None
    True
    """
    return _walled_reason(account, weekly_critical_threshold(account), _resolve_now(now))


def _protected_model_key(accounts: list[dict], current_id) -> str | None:
    """The key of the current account's binding model limit, or None.

    The model the user works on: a swap target must still serve it. None
    when the current account is not in *accounts* or reports no model limit.

    >>> raw = '{"limits": [{"kind": "weekly_scoped", "percent": 40, "is_active": true,'
    >>> raw += ' "scope": {"model": {"display_name": "Fable"}}}]}'
    >>> _protected_model_key([{"id": 1, "cached_usage_raw": raw}], 1)
    'fable'
    >>> _protected_model_key([{"id": 1}], 1) is None
    True
    """
    current = next((a for a in accounts if a["id"] == current_id), None)
    model = binding_model(_model_limits(current)) if current is not None else None
    return model.get("key") if model else None


def candidate_walls(
    account: dict, protected_key: str | None, now: datetime,
) -> tuple[str | None, bool]:
    """The two wall checks on a swap candidate (#224i), as
    ``(walled departure reason or None, serves the protected model)``.

    1. :func:`walled_reason`: rules 2a/2b would leave the account at the
       next tick once it is active, so a swap to it undoes itself.
    2. Whether it serves *protected_key*, the binding model of the active
       account (:func:`_protected_model_key`): False when its limit for that
       model is at the threshold and has not reset yet, even when that model
       is not its own binding one. True when *protected_key* is None.

    Both checks read the account's own plan field, which is the threshold
    that rules 2a/2b apply to it after a swap. The decision log shows both
    on each candidate row.

    >>> now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    >>> candidate_walls({"cached_usage_7d": 98.5}, None, now)
    ('weekly critical: 7d usage 98.5% >= 98%', True)
    >>> raw = '{"limits": [{"kind": "weekly_scoped", "percent": 10, "is_active": true,'
    >>> raw += ' "scope": {"model": {"display_name": "Sonnet"}}}, {"kind": "weekly_scoped",'
    >>> raw += ' "percent": 99, "scope": {"model": {"display_name": "Fable"}}}]}'
    >>> candidate_walls({"cached_usage_raw": raw}, "fable", now)
    (None, False)
    """
    threshold = weekly_critical_threshold(account)
    per_model = _model_limits(account)
    serves = protected_key is None or not _walled_on(per_model, protected_key, threshold, now)
    return _walled_reason(account, threshold, now, per_model), serves


def _cannot_serve(account: dict, protected_key: str | None, now: datetime) -> bool:
    """True when *account* fails a check of :func:`candidate_walls`, and so
    is never a swap target (#224i).

    ``pick_best_target`` and the monitor's poll clamp both use this
    predicate, so they agree on what a target is.

    >>> now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
    >>> _cannot_serve({"cached_usage_7d": 98.5}, None, now)
    True
    >>> _cannot_serve({"cached_usage_7d": 50.0}, "fable", now)
    False
    """
    wall, serves = candidate_walls(account, protected_key, now)
    return wall is not None or not serves
