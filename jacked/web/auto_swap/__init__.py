"""Auto-swap decision engine — facade re-exporting public API.

See docs/architecture/auto-swap-system.md for module responsibilities.
"""
from .burn import (
    BurnRate,
    RESET_SUPPRESS_MINUTES,
    _resets_within,
    compute_burn_per_window,
    compute_effective_working_hours,
    has_viable_headroom,
    update_burn_rate,
)
from .diagnostics import (
    compute_7d_deficit,
    format_account_label,
    tier_critical_threshold,
    tier_label,
)
from .selection import (
    REASON_PREFIX_BURN_RATE,
    REASON_PREFIX_DRAINED,
    REASON_PREFIX_FIVE_H,
    REASON_PREFIX_HIGHER_TIER,
    REASON_PREFIX_NO_DATA,
    _has_5h_headroom,
    pick_best_target,
    should_swap_now,
)
from .tiers import (
    TIER_EXCLUDED,
    TIER_T0,
    TIER_T1,
    TIER_T2,
    TIER_T3,
    deficit_vs_target,
    target_7d,
    tier_for,
    white_bar,
)

__all__ = [
    "BurnRate", "RESET_SUPPRESS_MINUTES", "_resets_within",
    "compute_burn_per_window", "compute_effective_working_hours",
    "has_viable_headroom", "update_burn_rate",
    "compute_7d_deficit", "format_account_label",
    "tier_critical_threshold", "tier_label",
    "pick_best_target", "should_swap_now",
    "_has_5h_headroom",
    "REASON_PREFIX_HIGHER_TIER", "REASON_PREFIX_DRAINED",
    "REASON_PREFIX_FIVE_H", "REASON_PREFIX_BURN_RATE",
    "REASON_PREFIX_NO_DATA",
    "TIER_T0", "TIER_T1", "TIER_T2", "TIER_T3", "TIER_EXCLUDED",
    "tier_for", "white_bar", "target_7d", "deficit_vs_target",
]
