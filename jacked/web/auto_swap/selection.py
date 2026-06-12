"""Selection rule and departure decision (tier-aware unified flow)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from jacked.web.auto_swap.burn import (
    BurnRate,
    RESET_SUPPRESS_MINUTES,
    has_viable_headroom,
)
from jacked.web.auto_swap.tiers import (
    TIER_EXCLUDED,
    TIER_T0,
    TIER_T1,
    TIER_T2,
    TIER_T3,
    _resolve_now,
    target_for_tier,
    tier_for,
)

logger = logging.getLogger(__name__)


def _resets_within_at(
    resets_at: str | None, minutes: float, now: datetime,
) -> bool:
    """``now``-aware variant of ``_resets_within`` for testable code paths.

    Returns True if the window resets within ``minutes`` of the supplied
    ``now`` (must be tz-aware UTC). False for None, past timestamps, or
    parse errors. Mirrors ``burn._resets_within`` but accepts an injected
    clock so tests using a frozen ``now`` are deterministic.
    """
    if resets_at is None:
        return False
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        if reset_dt <= now:
            return False
        remaining = (reset_dt - now).total_seconds() / 60.0
        return remaining <= minutes
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# pick_best_target
# ---------------------------------------------------------------------------

_FIVE_H_HEADROOM_LIMIT = 90  # >= 90 means "no usable room unless reset imminent"
# Imminent-reset lookahead for admitting 5h-saturated candidates. MUST stay
# <= RESET_SUPPRESS_MINUTES: accounts selected via this branch need 5h-critical
# suppressed until the reset lands, else they're immediately ejected
# (deterministic ping-pong). See the invariant comment in burn.py.
_FIVE_H_HEADROOM_RESET_MIN = RESET_SUPPRESS_MINUTES


def _has_5h_headroom(account: dict, now: datetime | None = None) -> bool:
    """Return True if the account's 5h window has room now OR is about to reset.

    Pure module-level helper (testable, reusable). Accounts saturated at 5h
    get one chance: if their reset is imminent, they're still viable
    targets because the swap settles + the window flips fresh. A PAST
    cached_5h_resets_at is headroom too — the window already reset and the
    >=90 cached usage is stale, so the most-usable candidate must not be
    excluded until the cache refreshes.
    """
    usage_5h = account.get("cached_usage_5h") or 0
    if usage_5h < _FIVE_H_HEADROOM_LIMIT:
        return True
    resets_at = account.get("cached_5h_resets_at")
    if resets_at is None:
        return False
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    now = _resolve_now(now)
    if reset_dt <= now:
        return True
    remaining = (reset_dt - now).total_seconds() / 60.0
    return remaining <= _FIVE_H_HEADROOM_RESET_MIN


@dataclass(frozen=True, order=True)
class _SortKey:
    """Sort key for pick_best_target.

    Smaller wins (Python ``min`` semantics). Ordering: (tier_index_lower=more_urgent,
    earlier_resets_at, more_negative_neg_deficit means larger raw deficit).
    All three fields are negated/encoded so that "smaller tuple = better
    candidate" — never edit one field without re-reading the others.
    """
    tier_index: int           # 0=T0 most urgent .. 3=T3 least urgent (smaller = better)
    resets_at_epoch: float    # UTC epoch seconds; inf on parse failure (smaller = better)
    neg_deficit: float        # negated deficit so larger raw deficit -> smaller key


def _epoch_or_inf(resets_at: str | None) -> float:
    """Parse an ISO reset timestamp to UTC epoch seconds for chronological
    sorting. Lexicographic string sort diverges from chronological for mixed
    formats (Z suffix vs explicit offsets), so the key must be numeric.
    float('inf') on missing/unparseable values (sorts last).
    """
    if not resets_at:
        return float("inf")
    try:
        dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return float("inf")


def pick_best_target(
    accounts: list[dict],
    current_id: int,
    *,
    active_start: str = "06:00",
    active_end: str = "23:00",
    now: datetime | None = None,
    prev_tiers: dict[int, int] | None = None,
) -> dict | None:
    """Return the best swap-target account, or None if nothing qualifies.

    Selection rule (tier-strict; see spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md):

    1. Filter out: current account, inactive/deleted, failures>=3,
       invalid, no token, auto_swap_enabled=0, no 5h headroom,
       no viable 7d headroom (bypassed for T0 with deficit >= 1.0 —
       drain-to-100 must not strand the final slice below the
       burn-per-window floor), deficit<=0, tier=excluded.
    2. Pick the candidate that minimizes (tier_index, resets_at_epoch,
       -deficit). See ``_SortKey`` for the sign convention.
    3. ``active_start``/``active_end`` feed ``has_viable_headroom`` (the
       7d-burn-floor check) and the deadline-aware T1 target. They do
       NOT gate the algorithm in any other way.
    4. ``prev_tiers``: optional {account_id: last_tier_observed} map for
       hysteresis. When provided, ``tier_for`` ignores single-step tier
       flips that fall within ``_TIER_HYSTERESIS_MIN`` minutes of the
       boundary. The deficit is computed against the DAMPED tier so an
       account ranked T1 is admitted with T1's target, never T0's.

    Inputs read from each account: ``id``, ``is_active``, ``is_deleted``,
    ``consecutive_failures``, ``validation_status``, ``cc_access_token``,
    ``auto_swap_enabled``, ``cached_5h_resets_at``, ``cached_7d_resets_at``,
    ``cached_usage_5h``, ``cached_usage_7d``. Nothing else is consulted —
    do NOT add fields here without updating callers and tests.
    """
    now = _resolve_now(now)
    prev_tiers = prev_tiers or {}

    candidates: list[tuple[_SortKey, dict]] = []
    for a in accounts:
        if a["id"] == current_id:
            continue
        if a.get("is_active") == 0 or a.get("is_deleted") == 1:
            continue
        if (a.get("consecutive_failures") or 0) >= 3:
            continue
        if a.get("validation_status") == "invalid":
            continue
        if a.get("cc_access_token") is None:
            continue
        if a.get("auto_swap_enabled") == 0:
            continue

        tier = tier_for(a, now=now, prev_tier=prev_tiers.get(a["id"]))
        if tier == TIER_EXCLUDED:
            continue
        # Deficit against the DAMPED tier — deficit_vs_target's internal
        # undamped tier_for would admit a hysteresis-held T1 with T0's
        # target.
        target = target_for_tier(tier, a, now, active_start, active_end)
        usage_7d = a.get("cached_usage_7d")
        if target is None or usage_7d is None:
            continue
        deficit = target - usage_7d
        if deficit <= 0:
            continue
        # T0 drains to 100: the burn-per-window floor (~4.2%) would
        # permanently strand the final slice of every account.
        if not (tier == TIER_T0 and deficit >= 1.0):
            if not has_viable_headroom(a, active_start, active_end):
                continue
        if not _has_5h_headroom(a, now=now):
            continue

        key = _SortKey(
            tier_index=tier,
            resets_at_epoch=_epoch_or_inf(a.get("cached_7d_resets_at")),
            neg_deficit=-deficit,
        )
        candidates.append((key, a))

    if not candidates:
        return None

    best_key, best = min(candidates, key=lambda kv: kv[0])

    if logger.isEnabledFor(logging.DEBUG):
        sorted_for_log = sorted(candidates, key=lambda kv: kv[0])[:3]
        for key, cand in sorted_for_log:
            logger.debug(
                "pick_best_target: candidate %s (%s) tier=%d resets=%s deficit=%.1f",
                cand.get("id", "?"), cand.get("email", "?"),
                key.tier_index, cand.get("cached_7d_resets_at"), -key.neg_deficit,
            )

    return best


# ---------------------------------------------------------------------------
# should_swap_now — tier-aware departure rule
# ---------------------------------------------------------------------------

_TIER_NAMES = {
    TIER_T0: "T0 (<24h)",
    TIER_T1: "T1 (24-48h)",
    TIER_T2: "T2 (48h-4d)",
    TIER_T3: "T3 (4-7d)",
    TIER_EXCLUDED: "T?",
}


# Reason-string prefixes — DO NOT change these without updating
# usage_monitor's `_trigger_for_reason` mapping.
REASON_PREFIX_HIGHER_TIER = "higher tier emerged"
REASON_PREFIX_INTRA_TIER = "intra-tier preemption:"
REASON_PREFIX_DRAINED = "drained:"
REASON_PREFIX_FIVE_H = "5h critical:"
REASON_PREFIX_BURN_RATE = "burn-rate projection:"
REASON_PREFIX_NO_DATA = "active has no 7d data"


def _hours_to_expiry(account: dict, now: datetime) -> float | None:
    """Hours until the account's 7d window expires (negative if past).

    None when cached_7d_resets_at is missing or unparseable.
    """
    resets_at = account.get("cached_7d_resets_at")
    if resets_at is None:
        return None
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if reset_dt.tzinfo is None:
            reset_dt = reset_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None
    return (reset_dt - now).total_seconds() / 3600.0


def should_swap_now(
    active: dict,
    best: dict | None,
    *,
    burn_rate: BurnRate | None = None,
    check_interval_min: float = 5,  # MINUTES (not seconds). Caller converts.
    critical_5h: float = 90,
    warning_5h: float = 80,
    now: datetime | None = None,
    prev_tiers: dict[int, int] | None = None,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> str | None:
    """Return a reason string if the algorithm should swap off ``active``,
    or None to stay.

    Departure rules — DO NOT REORDER. Rules 1/1b are the ONLY rules that
    override 5h-reset suppression, and rule 1 is the ONLY rule gated by
    emergence persistence in the caller. Reordering breaks both
    invariants. See spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md.

    1. Higher-tier candidate emerged: ``best`` exists with strictly
       lower tier_index than ``active``. (T0 emerged while on T2/T3
       always overrides — even when 5h reset suppresses critical.)
       Active is treated as TIER_T3+1 when its tier is EXCLUDED, so
       any candidate with a real tier wins over an unclassified
       active account — but ONLY if ``best`` is already a real tier.
    1b. Intra-T0 preemption: strict tier inequality alone lets an active
       T0 with a long runway block a T0 candidate expiring in hours —
       the largest stranding mechanism observed. Preempt when ``best``
       expires strictly earlier, its deficit is >= 5%, and its loss
       rate (deficit / hours_to_expiry) is >= 1.5x the active's, so
       near-equal T0s can't ping-pong the active slot.
    2. Active drained (T0/T1 only): usage_7d >= the active tier's
       target. T2/T3 targets are FLOORS (white_bar baseline), not
       drain-to goals — being above the floor is the desired state.
    3. Active 5h critical: usage_5h >= critical_5h, AND 5h reset NOT
       imminent (within RESET_SUPPRESS_MINUTES). Also evaluated when
       the active has no 7d data (TIER_EXCLUDED) — exhaustion alerting
       must not depend on 7d cache presence.
    4. Burn-rate projection: usage_5h >= warning_5h AND projected to
       cross critical within 2 * check_interval_min, AND 5h reset
       not imminent.

    ``active_start``/``active_end`` feed the deadline-aware T1 target in
    rule 2 (defaults preserve prior call sites).

    Returns None when none fire (stay; ride out the 5h window).

    Reason strings always start with one of the ``REASON_PREFIX_*``
    constants — callers (usage_monitor) parse these prefixes to derive
    the decision-log ``trigger`` taxonomy.

    ``prev_tiers``: optional {account_id: last_observed_tier} map. When
    provided, BOTH active and best run their tier classification with
    hysteresis (5-min margin past the boundary before transitioning to
    a more-urgent tier). Without this, Anthropic API timestamp jitter
    at the 24h/48h tier boundaries flickers the active account between
    tiers each tick — when active flickers from T2 down to T1, a T1
    candidate is no longer "higher tier", `should_swap_now` returns
    None, and emergence-persistence in the caller clears the streak.
    Net effect: a guaranteed swap takes 6+ minutes instead of 2 ticks.
    See `tests/unit/test_auto_swap.py::TestActiveTierHysteresis`.

    NOTE: ``check_interval_min`` is the poll interval in MINUTES, not
    seconds. Caller computes from the DB setting `usage_check_interval`
    (which is in seconds) by dividing by 60.
    """
    now = _resolve_now(now)
    prev_tiers = prev_tiers or {}
    usage_5h = active.get("cached_usage_5h") or 0
    usage_7d = active.get("cached_usage_7d") or 0
    active_tier = tier_for(
        active, now=now, prev_tier=prev_tiers.get(active.get("id")),
    )
    suppress_5h = _resets_within_at(
        active.get("cached_5h_resets_at"), RESET_SUPPRESS_MINUTES, now,
    )

    # 1. Higher-tier candidate (overrides 5h reset suppression)
    if best is not None:
        best_tier = tier_for(
            best, now=now, prev_tier=prev_tiers.get(best.get("id")),
        )
        active_rank = active_tier if active_tier != TIER_EXCLUDED else TIER_T3 + 1
        if best_tier < active_rank and best_tier != TIER_EXCLUDED:
            return (
                f"{REASON_PREFIX_HIGHER_TIER}: {_TIER_NAMES[best_tier]} "
                f"candidate vs active {_TIER_NAMES[active_tier]}"
            )

        # 1b. Intra-T0 preemption — an active T0 with a long runway must
        # not block a T0 candidate expiring in hours. Margin gates
        # (earlier expiry, deficit >= 5, loss rate >= 1.5x) prevent
        # ping-pong between near-equal T0s.
        if best_tier == TIER_T0 and active_tier == TIER_T0:
            best_hours = _hours_to_expiry(best, now)
            active_hours = _hours_to_expiry(active, now)
            if (best_hours is not None and active_hours is not None
                    and 0 < best_hours < active_hours):
                best_target = target_for_tier(
                    TIER_T0, best, now, active_start, active_end,
                ) or 100.0
                best_deficit = best_target - (best.get("cached_usage_7d") or 0)
                active_deficit = 100.0 - usage_7d
                best_rate = best_deficit / best_hours
                active_rate = active_deficit / active_hours
                if best_deficit >= 5.0 and best_rate >= 1.5 * active_rate:
                    return (
                        f"{REASON_PREFIX_INTRA_TIER} T0 candidate losing "
                        f"{best_deficit:.1f}% in {best_hours:.1f}h vs active "
                        f"{active_deficit:.1f}% in {active_hours:.1f}h"
                    )

    # 2. Active drained vs tier target — ONLY for T0/T1 (drain-to goals).
    # Use the hysteresis-aware ``active_tier`` (not target_7d's internal
    # tier_for call) so the drained gate stays consistent with rule 1's
    # tier comparison. T0 target = 100, T1 target = deadline-aware.
    if active_tier in (TIER_T0, TIER_T1):
        target = target_for_tier(
            active_tier, active, now, active_start, active_end,
        )
        if target is not None and usage_7d >= target:
            return (
                f"{REASON_PREFIX_DRAINED} 7d usage {usage_7d:.1f}% >= "
                f"tier target {target:.1f}%"
            )

    # 3. 5h critical (suppressed if reset imminent)
    if usage_5h >= critical_5h and not suppress_5h:
        return f"{REASON_PREFIX_FIVE_H} {usage_5h:.1f}% >= {critical_5h:.0f}%"

    # 4. Burn-rate projection
    if (usage_5h >= warning_5h
            and burn_rate is not None
            and not suppress_5h):
        rate = burn_rate.rate_5h_per_min
        if rate > 0:
            mins_to_critical = max(0, critical_5h - usage_5h) / rate
            window_min = max(1.0, 2 * check_interval_min)
            if mins_to_critical <= window_min:
                projected = usage_5h + rate * window_min
                return (
                    f"{REASON_PREFIX_BURN_RATE} {usage_5h:.1f}% -> "
                    f"{projected:.1f}% in {int(window_min)}min"
                )

    return None
