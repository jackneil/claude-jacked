"""Tier classification, white-bar progress, and tier-based usage targets.

See docs/architecture/auto-swap-system.md for the algorithm overview.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone


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
    instantaneous tier is one step MORE urgent than prev_tier (e.g. prev=T1,
    now=T0), require the new tier to be at least
    ``_TIER_HYSTERESIS_MIN`` minutes deep into the boundary before flipping.
    Within the hysteresis band, returns ``prev_tier``. Movement TOWARD less
    urgent (T0→T1, T1→T2, etc.) flips immediately — only the dangerous
    "becoming more urgent" direction is dampened. This prevents
    Anthropic-API timestamp jitter (±30s) from oscillating across the
    24h or 48h boundary.
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

    # Hysteresis: only damp transitions toward more urgent (smaller index).
    if instant >= prev_tier or prev_tier == TIER_EXCLUDED:
        return instant

    # Compute hours past the boundary into the new (more urgent) tier.
    boundary_hours = _TIER_BOUNDARIES_HOURS[instant]  # the upper edge of `instant`
    hours_into_new_tier = boundary_hours - hours_left  # positive if past boundary
    if hours_into_new_tier * 60 >= _TIER_HYSTERESIS_MIN:
        return instant
    return prev_tier


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
