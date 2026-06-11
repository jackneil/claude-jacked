"""Tier classification, white-bar progress, and tier-based usage targets.

See docs/architecture/auto-swap-system.md for the algorithm overview.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jacked.web.auto_swap.burn import (
    compute_burn_per_window,
    compute_effective_working_hours,
)


# ---------------------------------------------------------------------------
# Tier classification (deadline-aware)
# ---------------------------------------------------------------------------

# Lower index = higher priority. Tier boundaries belong to the higher-numbered
# tier (e.g., exactly 24h → T1, not T0). T4 is the sentinel for "no usable
# 7d data" or "already expired".
TIER_T0 = 0  # < 24h to expiry
TIER_T1 = 1  # 24h - 48h
TIER_T2 = 2  # 48h - 96h (4d)
TIER_T3 = 3  # 96h - 168h (7d)
TIER_EXCLUDED = 4  # no data or already expired


_TIER_BOUNDARIES_HOURS = (24, 48, 96, 168)  # T0|T1|T2|T3 cutoffs
_TIER_HYSTERESIS_MIN = 5.0  # minutes inside a more-urgent tier before flip


def _resolve_now(now: datetime | None = None) -> datetime:
    """Coerce an optional ``now`` to a UTC-aware datetime."""
    n = now or datetime.now(timezone.utc)
    return n if n.tzinfo else n.replace(tzinfo=timezone.utc)


def tier_for(
    account: dict,
    now: datetime | None = None,
    *,
    prev_tier: int | None = None,
) -> int:
    """Classify an account by its 7d expiry deadline (T0..T3 or 4=excluded).

    Returns 0..3 for T0..T3 or 4 (excluded) when 7d data is missing or the
    window has already expired. Boundaries belong to the higher-numbered
    (less urgent) tier — exactly 24h is T1, exactly 48h is T2.

    Hysteresis (anti-jitter): if ``prev_tier`` is provided and the account's
    instantaneous tier is MORE urgent than prev_tier (e.g. prev=T1,
    now=T0), require the new tier to be at least
    ``_TIER_HYSTERESIS_MIN`` minutes deep into the boundary before flipping.
    Within the hysteresis band, returns ``prev_tier``. This prevents
    Anthropic-API timestamp jitter (±30s) from oscillating across the
    24h or 48h boundary.

    Symmetric damping for single-step LESS-urgent flips (e.g. prev=T0,
    now=T1 within 5 min past the 24h boundary): real time only moves
    accounts MORE urgent, so a single-step less-urgent flip is refetch
    noise on cached_7d_resets_at. Un-damped, it fires a premature rule-2
    'drained' (target jumps 100->90) followed by a pull-back swap. Jumps
    of more than one step flip immediately — that's a genuine window
    reset, not jitter.
    """
    resets_at_str = account.get("cached_7d_resets_at")
    if resets_at_str is None:
        return TIER_EXCLUDED
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return TIER_EXCLUDED

    now = _resolve_now(now)
    seconds_left = (resets_at - now).total_seconds()
    if seconds_left <= 0:
        return TIER_EXCLUDED

    hours_left = seconds_left / 3600.0
    if hours_left < 24:
        instant = TIER_T0
    elif hours_left < 48:
        instant = TIER_T1
    elif hours_left < 96:
        instant = TIER_T2
    else:
        instant = TIER_T3

    if prev_tier is None:
        return instant
    if instant == prev_tier or prev_tier == TIER_EXCLUDED:
        return instant

    if instant < prev_tier:
        # More urgent: hours past the boundary into the new tier.
        boundary_hours = _TIER_BOUNDARIES_HOURS[instant]  # the upper edge of `instant`
        hours_into_new_tier = boundary_hours - hours_left  # positive if past boundary
        if hours_into_new_tier * 60 >= _TIER_HYSTERESIS_MIN:
            return instant
        return prev_tier

    # Less urgent: only a single-step flip can be timestamp jitter — real
    # time never adds runway, so damp it within the hysteresis band past
    # the boundary. Multi-step jumps are genuine window resets.
    if instant != prev_tier + 1:
        return instant
    boundary_hours = _TIER_BOUNDARIES_HOURS[prev_tier]  # edge between prev and instant
    if (hours_left - boundary_hours) * 60 <= _TIER_HYSTERESIS_MIN:
        return prev_tier
    return instant


def white_bar(account: dict, now: datetime | None = None) -> float | None:
    """Wall-clock elapsed fraction (0.0-1.0) of the 7d window.

    Matches the UI's computeElapsedFraction7d in
    jacked/data/web/js/components/usage.js — same formula:
    (now - (resets_at - 7d)) / 7d. No active-hours adjustment.
    Clamped to [0, 1] (also matches the UI's Math.max/min clamp).

    Returns None when 7d data is missing.
    """
    resets_at_str = account.get("cached_7d_resets_at")
    if resets_at_str is None:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    now = _resolve_now(now)
    window_seconds = 7 * 24 * 3600
    start = resets_at - timedelta(seconds=window_seconds)
    elapsed = (now - start).total_seconds() / window_seconds
    return max(0.0, min(1.0, elapsed))


# Tier targets — see spec 2026-05-04-auto-swap-utilization-redesign-design.md
T1_TARGET = 90.0  # 24-48h: FLOOR for the deadline-aware T1 target
T2_LEAD = 5.0     # 48h-4d: stay slightly ahead of white bar


def _t1_deadline_target(
    account: dict,
    active_start: str,
    active_end: str,
) -> float:
    """Deadline-aware T1 target: 100 minus what the final day can still burn.

    achievable_final_day_burn = effective working hours before expiry on
    the expiry's LOCAL calendar day, in 5h windows, times burn-per-window.
    A fixed 90 strands the 10% buffer whenever expiry lands outside
    working hours (e.g. 03:00 local: nothing is burnable on the expiry
    day, so T1 must drain toward 100 directly). The window is [local
    midnight of the expiry day, expiry] — a full [expiry-24h, expiry]
    span always contains exactly one day's active hours (a constant
    100/7 ≈ 14.3% achievable), which would make this unconditionally 90.
    Never returns below the T1_TARGET floor.
    """
    resets_at_str = account.get("cached_7d_resets_at")
    if resets_at_str is None:
        return T1_TARGET
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return T1_TARGET
    expiry_local = resets_at.astimezone().replace(tzinfo=None)
    day_start = expiry_local.replace(hour=0, minute=0, second=0, microsecond=0)
    final_day_hours = compute_effective_working_hours(
        day_start, expiry_local, active_start, active_end,
    )
    achievable = (final_day_hours / 5.0) * compute_burn_per_window(
        active_start, active_end,
    )
    return max(T1_TARGET, 100.0 - min(10.0, achievable))


def target_for_tier(
    tier: int,
    account: dict,
    now: datetime | None = None,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> float | None:
    """Usage target (0-100) for an account ranked at ``tier``.

    Takes the (possibly hysteresis-damped) tier from the caller instead of
    recomputing it — keeps selection's admit math consistent with the tier
    the account was ranked at. T0 → 100 (drain). T1 → deadline-aware
    (>= T1_TARGET floor). T2 → white_bar*100 + T2_LEAD (capped at 100).
    T3 → white_bar*100 (floor). None for TIER_EXCLUDED or when white-bar
    data is missing.
    """
    if tier == TIER_T0:
        return 100.0
    if tier == TIER_T1:
        return _t1_deadline_target(account, active_start, active_end)
    if tier in (TIER_T2, TIER_T3):
        wb = white_bar(account, now=now)
        if wb is None:
            return None
        if tier == TIER_T2:
            return min(100.0, wb * 100.0 + T2_LEAD)
        return wb * 100.0
    return None


def target_7d(
    account: dict,
    now: datetime | None = None,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> float | None:
    """Tier-based 7d usage target as a percentage (0-100).

    Classifies the account (no hysteresis) and delegates to
    ``target_for_tier``. Returns None when 7d data is missing or already
    expired.
    """
    tier = tier_for(account, now=now)
    return target_for_tier(tier, account, now, active_start, active_end)


def deficit_vs_target(
    account: dict,
    now: datetime | None = None,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> float | None:
    """Difference between tier target and current 7d usage.

    Positive = behind tier target (eligible for selection).
    Negative = at/above tier target (not a candidate).
    None when 7d data missing, expired, or usage is None.
    """
    target = target_7d(account, now, active_start, active_end)
    if target is None:
        return None
    usage = account.get("cached_usage_7d")
    if usage is None:
        return None
    return target - usage
