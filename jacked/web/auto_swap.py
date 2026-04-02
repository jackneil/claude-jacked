"""Auto-swap decision engine — pure functions, NO I/O.

All side effects (DB reads, HTTP calls, account switching) belong in the
caller.  This module only answers: *should* we swap, *to whom*, and tracks
burn-rate state.
"""

from __future__ import annotations

import re
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


def tier_label(account: dict) -> str:
    """Return a human-readable tier label like '(tier 20x)' or ''."""
    tier = (account.get("rate_limit_tier") or "").lower()
    match = re.search(r"(\d+)x", tier)
    return f" (tier {match.group(1)}x)" if match else ""


def tier_critical_threshold(account: dict) -> float:
    """Compute the auto-swap critical threshold based on account tier.

    Higher-tier accounts can sustain usage longer before needing a swap.
    Known tiers from Anthropic: default_claude_max_20x, default_claude_max_5x.
    Test fixtures use t1/t2 which are not real tiers — they fall to the
    subscription_type fallback.
    """
    tier = (account.get("rate_limit_tier") or "").lower()
    match = re.search(r"(\d+)x", tier)
    if match:
        multiplier = int(match.group(1))
        if multiplier >= 20:
            return 95.0
        if multiplier >= 5:
            return 90.0
    # Fallback: subscription_type when tier is missing or unrecognized
    sub = (account.get("subscription_type") or "").lower()
    if sub == "max":
        return 90.0  # max without tier info — conservative
    return 80.0  # pro, free, or unknown


# ---------------------------------------------------------------------------
# Window-reset awareness
# ---------------------------------------------------------------------------

RESET_SUPPRESS_MINUTES = 10
SUPPRESS_OVERRIDE_SCORE = 80


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
# should_swap
# ---------------------------------------------------------------------------

def should_swap(
    usage_5h: float | None,
    usage_7d: float | None,
    critical_5h: float = 90,
    warning_5h: float = 80,
    threshold_7d: float = 85,
    burn_rate: BurnRate | None = None,
    check_interval_min: float = 5,
    resets_5h_at: str | None = None,
    resets_7d_at: str | None = None,
    usage_cached_at: int | None = None,
) -> bool:
    """Decide whether the current account should be swapped out.

    Returns False when *usage_5h* is None (no data yet — never swap on
    missing data).

    Suppresses all three swap triggers when the relevant window resets
    within ``RESET_SUPPRESS_MINUTES``.  Also suppresses when usage data
    is stale (predates a reset that already happened).
    """
    if usage_5h is None:
        return False

    # Stale-data guard: if the 5h reset is in the past but our usage data
    # is older than the reset, the usage is stale (a real reset happened
    # but we couldn't fetch). Don't trust the data — suppress swap.
    if resets_5h_at is not None and usage_cached_at is not None:
        try:
            reset_dt = datetime.fromisoformat(resets_5h_at.replace("Z", "+00:00"))
            if reset_dt <= datetime.now(timezone.utc):
                reset_epoch = reset_dt.timestamp()
                if usage_cached_at < reset_epoch:
                    return False  # usage data predates the reset
        except (ValueError, TypeError):
            pass

    suppress_5h = _resets_within(resets_5h_at, RESET_SUPPRESS_MINUTES)
    suppress_7d = _resets_within(resets_7d_at, RESET_SUPPRESS_MINUTES)

    # Hard ceiling (unless 5h reset imminent).
    if usage_5h >= critical_5h and not suppress_5h:
        return True

    # 7-day saturation (unless 7d reset imminent).
    if usage_7d is not None and usage_7d >= threshold_7d and not suppress_7d:
        return True

    # Warning zone + burn-rate projection (unless 5h reset imminent).
    if usage_5h >= warning_5h and burn_rate is not None and not suppress_5h:
        minutes_to_critical = _minutes_until(
            usage_5h, critical_5h, burn_rate.rate_5h_per_min,
        )
        if minutes_to_critical is not None and minutes_to_critical <= 2 * check_interval_min:
            return True

    return False


def _minutes_until(
    current: float, target: float, rate_per_min: float,
) -> float | None:
    """Minutes until *current* reaches *target* at *rate_per_min*.

    Returns None when the rate is zero or negative (will never reach).
    """
    if rate_per_min <= 0:
        return None
    gap = target - current
    if gap <= 0:
        return 0.0
    return gap / rate_per_min


# ---------------------------------------------------------------------------
# score_candidate
# ---------------------------------------------------------------------------

def score_candidate(account: dict) -> float:
    """Score an account for swap-to suitability. Higher is better.

    Considers:
    - 5h utilization (most weight)
    - 7d utilization weighted by remaining days in window
    - Tier-aware headroom (room before hitting tier's critical threshold)
    - Inactive 5h window bonus (encourages opening them)
    """
    score = 100.0
    score -= (account.get("cached_usage_5h") or 0)

    # 7-day: weight by remaining time in window.
    # Account resetting sooner has burned through more of its window and has
    # less capacity left per day. Account resetting later has more room.
    usage_7d = account.get("cached_usage_7d") or 0
    resets_7d = account.get("cached_7d_resets_at")
    if resets_7d:
        try:
            reset_dt = datetime.fromisoformat(resets_7d.replace("Z", "+00:00"))
            days_left = max(0.1, (reset_dt - datetime.now(timezone.utc)).total_seconds() / 86400)
            days_factor = min(days_left / 7.0, 1.0)  # 0-1: more days = more room
            score -= usage_7d * (1.0 - days_factor)  # penalize less when more days remain
        except (ValueError, TypeError):
            score -= usage_7d * 0.5
    else:
        score -= usage_7d * 0.5

    # Tier-aware headroom: bonus for accounts with more room before their tier limit
    tier_crit = tier_critical_threshold(account)
    headroom = max(0, tier_crit - (account.get("cached_usage_5h") or 0))
    score += headroom * 0.3

    # Bonus for inactive/expired 5h window — encourages opening them
    resets_5h = account.get("cached_5h_resets_at")
    if not resets_5h:
        score += 15.0
    else:
        try:
            r = datetime.fromisoformat(resets_5h.replace("Z", "+00:00"))
            if r < datetime.now(timezone.utc):
                score += 15.0
        except (ValueError, TypeError):
            pass

    # Bonus for imminent 5h reset — encourages swapping TO accounts about
    # to get a fresh window. Max +30 when reset is 0 min away, tapering
    # to 0 at 15 min.
    if resets_5h:
        try:
            r = datetime.fromisoformat(resets_5h.replace("Z", "+00:00"))
            remaining_min = (r - datetime.now(timezone.utc)).total_seconds() / 60.0
            if 0 < remaining_min <= 15:
                score += 30 * (1 - remaining_min / 15)
        except (ValueError, TypeError):
            pass

    return score


# ---------------------------------------------------------------------------
# pick_best_target
# ---------------------------------------------------------------------------

def pick_best_target(
    accounts: list[dict],
    current_id: int,
    threshold_7d: float = 85,
) -> dict | None:
    """Return the best swap-target account, or None if nothing qualifies."""
    candidates = [
        a for a in accounts
        if a["id"] != current_id
        and a.get("is_active") != 0
        and a.get("is_deleted") != 1
        and (a.get("consecutive_failures") or 0) < 3
        and a.get("validation_status") != "invalid"
        and a.get("cc_access_token") is not None
        and a.get("auto_swap_enabled") != 0
        and (
            (a.get("cached_usage_7d") or 0) < threshold_7d
            or _resets_within(a.get("cached_7d_resets_at"), RESET_SUPPRESS_MINUTES)
        )
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda a: (-score_candidate(a), a.get("priority", 0)))
    return candidates[0]


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
