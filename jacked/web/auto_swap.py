"""Auto-swap decision engine — pure functions, NO I/O.

All side effects (DB reads, HTTP calls, account switching) belong in the
caller.  This module only answers: *should* we swap, *to whom*, and tracks
burn-rate state.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field


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
) -> bool:
    """Decide whether the current account should be swapped out.

    Returns False when *usage_5h* is None (no data yet — never swap on
    missing data).
    """
    if usage_5h is None:
        return False

    # Hard ceiling — swap immediately.
    if usage_5h >= critical_5h:
        return True

    # 7-day saturation — swap even if the 5-hour window looks OK.
    if usage_7d is not None and usage_7d >= threshold_7d:
        return True

    # Warning zone + burn-rate projection.
    if usage_5h >= warning_5h and burn_rate is not None:
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
    """Score an account as a swap target.  Higher is better."""
    score = 100.0

    usage_5h = account.get("cached_usage_5h") or 0
    usage_7d = account.get("cached_usage_7d") or 0

    score -= usage_5h
    score -= usage_7d * 0.5

    # Inactive-window bonus: resets_at is None (never opened) or in the past.
    resets_at = account.get("cached_5h_resets_at")
    if resets_at is None or resets_at < time.time():
        score += 15

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
        and (a.get("cached_usage_7d") or 0) < threshold_7d
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
