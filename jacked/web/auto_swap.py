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

import logging

logger = logging.getLogger(__name__)


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


def format_account_label(account: dict) -> str:
    """Human-readable account label for swap notifications and logs.

    Format: [Label — ] email [(org)]
    - Personal orgs (ending "'s Organization") show as "(personal)"
    - Real org names shown as-is: "(Hank.ai)"
    - Custom display_name prepended only if it differs from the default
      (default = first name matching email prefix, or generic names)
    """
    email = account.get("email") or "unknown"
    org_name = account.get("organization_name") or ""
    display_name = (account.get("display_name") or "").strip()

    # Build org suffix
    org_suffix = ""
    if org_name:
        if org_name.endswith("\u2019s Organization") or org_name.endswith("'s Organization"):
            org_suffix = " (personal)"
        else:
            org_suffix = f" ({org_name})"

    # Check if display_name is custom (not the Anthropic default)
    label_prefix = ""
    if display_name:
        # Default display_name is typically the first name from the email
        # e.g. "Jack" for jack.neil@hank.ai. Don't show these.
        email_prefix = email.split("@")[0].split(".")[0].lower()
        if display_name.lower() != email_prefix and display_name.lower() != "user":
            label_prefix = f"{display_name} \u2014 "

    return f"{label_prefix}{email}{org_suffix}"


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
SUPPRESS_OVERRIDE_SCORE = 100


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
    active_start: str = "07:00",
    active_end: str = "22:00",
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
# Proactive 7d capacity scheduling
# ---------------------------------------------------------------------------

PROACTIVE_SWAP_THRESHOLD = 15.0  # minimum deficit (%) to trigger proactive swap
URGENCY_HOURS = 24.0  # accounts behind schedule with fewer effective hours remaining
                       # than this pass through the 7d filter for scoring


def compute_7d_deficit(
    account: dict,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> dict | None:
    """Compute 7-day utilization deficit for an account.

    Returns dict with deficit, effective_hours_remaining,
    effective_windows_remaining, unused_7d. Or None if insufficient data.

    Deficit > 0 means behind schedule (underutilized, wasting capacity).
    """
    resets_at_str = account.get("cached_7d_resets_at")
    usage_7d = account.get("cached_usage_7d")

    if resets_at_str is None or usage_7d is None:
        return None

    try:
        resets_at_utc = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at_utc.tzinfo is None:
            resets_at_utc = resets_at_utc.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    now_utc = datetime.now(timezone.utc)
    if resets_at_utc <= now_utc:
        return None  # window already expired

    # Convert to local time for working-hours calculation.
    # Uses rough offset: diff between datetime.now() (local naive) and
    # datetime.now(timezone.utc) stripped of tzinfo. Avoids pytz/zoneinfo.
    from datetime import timedelta as _td

    now_local = datetime.now()
    now_utc_naive = now_utc.replace(tzinfo=None)
    utc_offset_seconds = (now_utc_naive - now_local).total_seconds()
    # Convert resets_at to local by subtracting the UTC offset
    resets_local = resets_at_utc.replace(tzinfo=None) - _td(
        seconds=utc_offset_seconds
    )
    window_start_local = resets_local - _td(days=7)

    # Elapsed and total working hours
    elapsed_hours = compute_effective_working_hours(
        window_start_local, now_local, active_start, active_end,
    )
    total_hours = compute_effective_working_hours(
        window_start_local, resets_local, active_start, active_end,
    )

    if total_hours <= 0:
        return None

    elapsed_fraction = min(elapsed_hours / total_hours, 1.0)
    expected_usage = elapsed_fraction * 100.0
    deficit = expected_usage - usage_7d

    # Remaining capacity
    remaining_hours = compute_effective_working_hours(
        now_local, resets_local, active_start, active_end,
    )
    remaining_windows = remaining_hours / 5.0

    return {
        "deficit": deficit,
        "effective_hours_remaining": remaining_hours,
        "effective_windows_remaining": remaining_windows,
        "unused_7d": 100.0 - usage_7d,
    }


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
    account: dict | None = None,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> bool:
    """Decide whether the current account should be swapped out.

    Returns False when *usage_5h* is None (no data yet — never swap on
    missing data).

    Suppresses all three swap triggers when the relevant window resets
    within ``RESET_SUPPRESS_MINUTES``.  Also suppresses when usage data
    is stale (predates a reset that already happened).

    Suppresses the 7d trigger when *account* has a positive deficit
    (behind schedule) — the proactive scheduler intentionally placed
    us here to burn capacity, so swapping away would cause ping-pong.
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

    # 7-day saturation (unless 7d reset imminent OR we're burning deficit).
    if usage_7d is not None and usage_7d >= threshold_7d and not suppress_7d:
        # If account has a positive deficit, we're here to burn capacity — stay.
        if account is not None:
            deficit_result = compute_7d_deficit(account, active_start, active_end)
            if deficit_result and deficit_result["deficit"] > 0:
                logger.debug(
                    "should_swap: suppressing 7d trigger on deficit account "
                    "(usage_7d=%.1f%%, deficit=%.1f%%)",
                    usage_7d, deficit_result["deficit"],
                )
            else:
                return True
        else:
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

def score_candidate(
    account: dict,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> float:
    """Score an account for swap-to suitability. Higher is better.

    Considers:
    - 5h utilization (most weight)
    - 7d utilization weighted by remaining days in window
    - Tier-aware headroom (room before hitting tier's critical threshold)
    - Inactive 5h window bonus (encourages opening them)
    - 7d deficit bonus (behind-schedule accounts get priority)
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

    # Staleness check — used to gate reset bonus and apply flat penalty.
    _STALENESS_THRESHOLD = 1800  # 30 minutes
    _is_stale = False
    cached_at = account.get("usage_cached_at")
    if cached_at:
        try:
            _is_stale = (int(time.time()) - int(cached_at)) > _STALENESS_THRESHOLD
        except (ValueError, TypeError):
            pass

    # Bonus for imminent 5h reset — encourages swapping TO accounts about
    # to get a fresh window. Max +30 when reset is 0 min away, tapering
    # to 0 at 15 min.
    if resets_5h and not _is_stale:
        try:
            r = datetime.fromisoformat(resets_5h.replace("Z", "+00:00"))
            remaining_min = (r - datetime.now(timezone.utc)).total_seconds() / 60.0
            if 0 < remaining_min <= 15:
                score += 30 * (1 - remaining_min / 15)
        except (ValueError, TypeError):
            pass

    # Flat staleness penalty
    if _is_stale:
        score -= 10

    # 7d deficit bonus: accounts behind schedule on 7d utilization
    # get a bonus proportional to their deficit.
    # 0.5 weight: 30% deficit = +15 points, keeping it moderate.
    deficit_result = compute_7d_deficit(account, active_start, active_end)
    if deficit_result and deficit_result["deficit"] > 0:
        bonus = deficit_result["deficit"] * 0.5
        score += bonus
        logger.debug(
            "score_candidate: account %s deficit bonus +%.1f (deficit=%.1f%%)",
            account.get("id", "?"), bonus, deficit_result["deficit"],
        )

    return score


# ---------------------------------------------------------------------------
# pick_best_target
# ---------------------------------------------------------------------------

def pick_best_target(
    accounts: list[dict],
    current_id: int,
    threshold_7d: float = 85,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> dict | None:
    """Return the best swap-target account, or None if nothing qualifies."""

    def _passes_7d_filter(a: dict) -> bool:
        """Check if account passes the 7d usage filter.

        Passes if: usage below threshold, OR 7d resets within
        RESET_SUPPRESS_MINUTES, OR account has expiring capacity
        (behind schedule with limited time remaining).
        """
        usage_7d = a.get("cached_usage_7d") or 0
        if usage_7d < threshold_7d:
            return True
        if _resets_within(a.get("cached_7d_resets_at"), RESET_SUPPRESS_MINUTES):
            return True
        # Urgency relaxation: behind schedule + limited time = expiring capacity
        deficit_result = compute_7d_deficit(a, active_start, active_end)
        if deficit_result:
            has_deficit = deficit_result["deficit"] > 0
            urgent = deficit_result["effective_hours_remaining"] < URGENCY_HOURS
            if has_deficit and urgent:
                logger.debug(
                    "pick_best_target: account %s passes 7d filter via urgency "
                    "(deficit=%.1f%%, hours_remaining=%.1f)",
                    a.get("id", "?"), deficit_result["deficit"],
                    deficit_result["effective_hours_remaining"],
                )
                return True
        return False

    candidates = [
        a for a in accounts
        if a["id"] != current_id
        and a.get("is_active") != 0
        and a.get("is_deleted") != 1
        and (a.get("consecutive_failures") or 0) < 3
        and a.get("validation_status") != "invalid"
        and a.get("cc_access_token") is not None
        and a.get("auto_swap_enabled") != 0
        and _passes_7d_filter(a)
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda a: (-score_candidate(a, active_start, active_end), a.get("priority", 0)))

    if logger.isEnabledFor(logging.DEBUG):
        for c in candidates[:3]:
            logger.debug(
                "pick_best_target: candidate %s (%s) score=%.1f",
                c.get("id", "?"), c.get("email", "?"),
                score_candidate(c, active_start, active_end),
            )

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
