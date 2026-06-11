"""Burn-rate tracking, 5h-window math, and active-hours helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class BurnRate:
    rate_5h_per_min: float = 0.0
    last_check_5h: float = 0.0
    rate_7d_per_min: float = 0.0
    last_check_7d: float = 0.0
    last_check_time: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Window-reset awareness
# ---------------------------------------------------------------------------

# INVARIANT: selection's imminent-reset lookahead (_FIVE_H_HEADROOM_RESET_MIN)
# must be <= this suppression window. An account admitted because its 5h reset
# is N minutes out must keep 5h-critical suppressed for those N minutes after
# the swap, else should_swap_now's 5h-critical rule ejects it immediately
# (deterministic ping-pong).
RESET_SUPPRESS_MINUTES = 30


def _resets_within(resets_at: str | None, minutes: float) -> bool:
    """Return True if the window resets within the given number of minutes.

    Returns False for: None, past timestamps, parsing errors.
    Assumes system clock is NTP-synchronized within ~1 minute.
    """
    if resets_at is None:
        return False
    try:
        reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        if reset_dt <= now:
            return False
        remaining = (reset_dt - now).total_seconds() / 60.0
        return remaining <= minutes
    except (ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# compute_effective_working_hours
# ---------------------------------------------------------------------------

def compute_effective_working_hours(
    start_dt: datetime,
    end_dt: datetime,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> float:
    """Count working hours between two LOCAL datetimes, excluding overnight.

    Only counts hours within [active_start, active_end) each day.
    Both start_dt and end_dt must be in local time.
    """
    if end_dt <= start_dt:
        return 0.0

    from datetime import timedelta

    s_h, s_m = map(int, active_start.split(":"))
    e_h, e_m = map(int, active_end.split(":"))
    active_hours_per_day = (e_h * 60 + e_m - s_h * 60 - s_m) / 60.0

    if active_hours_per_day <= 0:
        return 0.0

    total = 0.0
    current_day = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    while current_day < end_day:
        day_active_start = current_day.replace(hour=s_h, minute=s_m)
        day_active_end = current_day.replace(hour=e_h, minute=e_m)

        effective_start = max(start_dt, day_active_start)
        effective_end = min(end_dt, day_active_end)

        if effective_end > effective_start:
            total += (effective_end - effective_start).total_seconds() / 3600.0

        current_day += timedelta(days=1)

    return total


# ---------------------------------------------------------------------------
# 5h-window capacity math
# ---------------------------------------------------------------------------

def compute_burn_per_window(active_start: str = "06:00", active_end: str = "23:00") -> float:
    """Max 7d capacity (%) that can be burned in one 5h window.

    Depends on active hours: more working hours/day = more windows/week
    = less burn per window.
    """
    s_h, s_m = map(int, active_start.split(":"))
    e_h, e_m = map(int, active_end.split(":"))
    working_hours_per_day = (e_h * 60 + e_m - s_h * 60 - s_m) / 60.0
    if working_hours_per_day <= 0:
        return 0.0
    windows_per_week = 7.0 * working_hours_per_day / 5.0
    return 100.0 / windows_per_week


def has_viable_headroom(
    account: dict,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> bool:
    """Check if an account has enough 7d headroom to survive one 5h window.

    Returns False if unused 7d capacity < burn_per_window. Swapping to
    an account that can't even fill one window is pointless and risks
    immediate exhaustion.
    """
    usage_7d = account.get("cached_usage_7d") or 0
    unused = 100.0 - usage_7d
    burn = compute_burn_per_window(active_start, active_end)
    return unused >= burn


# ---------------------------------------------------------------------------
# update_burn_rate
# ---------------------------------------------------------------------------

def update_burn_rate(
    rates: dict[int, BurnRate],
    account_id: int,
    current_5h: float,
    current_7d: float,
) -> BurnRate:
    """Update (or initialise) the burn-rate entry for *account_id*.

    On first observation the rate is set to 0 — we do NOT compute a delta
    from 0 -> current because that would cause a false spike after restart.
    """
    now = time.time()

    prev = rates.get(account_id)
    if prev is None:
        # First observation — seed with current values, zero rate.
        br = BurnRate(
            rate_5h_per_min=0.0,
            last_check_5h=current_5h,
            rate_7d_per_min=0.0,
            last_check_7d=current_7d,
            last_check_time=now,
        )
        rates[account_id] = br
        return br

    elapsed_min = (now - prev.last_check_time) / 60.0
    if elapsed_min <= 0:
        # Clock skew guard — keep previous rates.
        prev.last_check_5h = current_5h
        prev.last_check_7d = current_7d
        prev.last_check_time = now
        return prev

    rate_5h = max(0.0, (current_5h - prev.last_check_5h) / elapsed_min)
    rate_7d = max(0.0, (current_7d - prev.last_check_7d) / elapsed_min)

    prev.rate_5h_per_min = rate_5h
    prev.last_check_5h = current_5h
    prev.rate_7d_per_min = rate_7d
    prev.last_check_7d = current_7d
    prev.last_check_time = now

    return prev
