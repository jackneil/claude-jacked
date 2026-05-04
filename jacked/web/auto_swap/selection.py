"""Selection rule and departure decision (legacy implementation; rewritten in later tasks)."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from jacked.web.auto_swap.burn import (
    BurnRate,
    RESET_SUPPRESS_MINUTES,
    _resets_within,
    compute_burn_per_window,
    has_viable_headroom,
)
from jacked.web.auto_swap.diagnostics import (
    compute_7d_deficit,
    tier_critical_threshold,
)
from jacked.web.auto_swap.tiers import (
    TIER_EXCLUDED,
    deficit_vs_target,
    tier_for,
    _resolve_now,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SUPPRESS_OVERRIDE_SCORE = 100


# ---------------------------------------------------------------------------
# Proactive 7d capacity scheduling
# ---------------------------------------------------------------------------

PROACTIVE_SWAP_THRESHOLD = 15.0  # minimum deficit (%) to trigger proactive swap
URGENCY_HOURS = 24.0  # accounts behind schedule with fewer effective hours remaining
                       # than this pass through the 7d filter for scoring
MIN_PROACTIVE_MINUTES = 30  # don't proactively swap if fewer than this many
                             # working minutes remain today — not worth opening
                             # a 5h window for a few minutes of use


def compute_urgency_threshold(
    effective_windows_remaining: float,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> float:
    """Compute the deficit threshold for proactive swaps based on urgency.

    The closer to expiry, the lower the threshold — ensuring expiring
    capacity is not wasted. Uses remaining 5h windows as the urgency signal.

    Tiers:
      < 1 window:  CRITICAL — any deficit > 0 triggers (last chance)
      1-2 windows: HIGH — deficit > burn_per_window (~4%)
      3-4 windows: MEDIUM — deficit > 2 * burn_per_window (~8%)
      5+ windows:  NORMAL — deficit > PROACTIVE_SWAP_THRESHOLD (15%)
    """
    burn = compute_burn_per_window(active_start, active_end)

    if effective_windows_remaining < 1.0:
        return 0.0
    if effective_windows_remaining < 3.0:
        return burn
    if effective_windows_remaining < 5.0:
        return burn * 2.0
    return PROACTIVE_SWAP_THRESHOLD


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
    active_start: str = "06:00",
    active_end: str = "23:00",
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
    active_start: str = "06:00",
    active_end: str = "23:00",
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

_FIVE_H_HEADROOM_LIMIT = 90  # >= 90 means "no usable room unless reset imminent"
_FIVE_H_HEADROOM_RESET_MIN = 30  # imminent-reset window for 5h headroom


def _has_5h_headroom(account: dict) -> bool:
    """Return True if the account's 5h window has room now OR is about to reset.

    Pure module-level helper (testable, reusable). Accounts saturated at 5h
    get one chance: if their reset is within ~30 min, they're still viable
    targets because the swap settles + the window flips fresh.
    """
    usage_5h = account.get("cached_usage_5h") or 0
    if usage_5h < _FIVE_H_HEADROOM_LIMIT:
        return True
    return _resets_within(
        account.get("cached_5h_resets_at"), _FIVE_H_HEADROOM_RESET_MIN,
    )


@dataclass(frozen=True, order=True)
class _SortKey:
    """Sort key for pick_best_target.

    Smaller wins (Python ``min`` semantics). Ordering: (tier_index_lower=more_urgent,
    earlier_resets_at, more_negative_neg_deficit means larger raw deficit).
    All three fields are negated/encoded so that "smaller tuple = better
    candidate" — never edit one field without re-reading the others.
    """
    tier_index: int           # 0=T0 most urgent .. 3=T3 least urgent (smaller = better)
    resets_at_iso: str        # ISO timestamp; lex-sort = chronological (smaller = better)
    neg_deficit: float        # negated deficit so larger raw deficit -> smaller key


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
       no viable 7d headroom, deficit_vs_target<=0, tier=excluded.
    2. Pick the candidate that minimizes (tier_index, resets_at_iso,
       -deficit_vs_target). See ``_SortKey`` for the sign convention.
    3. ``active_start``/``active_end`` are passed only to
       ``has_viable_headroom`` (the 7d-burn-floor check). They do NOT
       gate the algorithm in any other way.
    4. ``prev_tiers``: optional {account_id: last_tier_observed} map for
       hysteresis. When provided, ``tier_for`` ignores tier flips toward
       a more-urgent tier that fall within ``_TIER_HYSTERESIS_MIN``
       minutes of the boundary.

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
        if not has_viable_headroom(a, active_start, active_end):
            continue
        if not _has_5h_headroom(a):
            continue
        deficit = deficit_vs_target(a, now=now)
        if deficit is None or deficit <= 0:
            continue

        key = _SortKey(
            tier_index=tier,
            resets_at_iso=a.get("cached_7d_resets_at") or "",
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
                key.tier_index, key.resets_at_iso, -key.neg_deficit,
            )

    return best
