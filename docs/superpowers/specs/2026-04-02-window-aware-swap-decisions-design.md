# Window-Aware Swap Decisions

**Date:** 2026-04-02
**Status:** Approved

## Problem

The auto-swap algorithm treats high usage as always bad, regardless of when the window resets. This leads to wasteful swaps:

- An account at 90% 5h usage with 2 minutes left on the window triggers a swap. But it's about to reset to 0% — swapping away wastes the imminent reset and burns a fresh window on another account.
- An account at 85% 7d with 5 minutes left in its 7-day window triggers a swap. That 85% is about to become 0%.
- When picking swap targets, an account at 80% with 3 minutes to reset scores poorly, even though it's about to be fresh.

Usage windows reset to 0% instantly at `cached_5h_resets_at` / `cached_7d_resets_at`.

## Solution

Factor time-to-reset into both swap decisions (should we swap away?) and target selection (who should we swap to?).

### 1. `should_swap` — suppress swap when window resets within 10 minutes

Before triggering a swap on high 5h usage, check `cached_5h_resets_at`. If the 5h window resets within 10 minutes, do NOT swap — the account is about to get a free reset to 0%.

Same for 7d: if `cached_7d_resets_at` is within 10 minutes, suppress the 7d threshold swap.

**Logic:**
```
if usage_5h >= critical AND 5h_resets_within(10 min):
    DON'T swap — reset imminent
if usage_7d >= threshold AND 7d_resets_within(10 min):
    DON'T swap — reset imminent
```

The 10-minute cutoff balances: worst case is 10 minutes of degraded rate limits, which is better than wasting a fresh window on another account. The cutoff should be configurable or at least a named constant.

### 2. `score_candidate` — bonus for accounts about to reset

When scoring swap targets, accounts whose 5h windows reset soon should get a significant bonus. An account at 80% with 5 minutes left is about to be fresh — it's a better target than an account at 20% with 4 hours left.

**Scoring:**
- If 5h window resets within 15 minutes: add bonus scaled by proximity (sooner = larger bonus, max +30 points)
- Formula: `bonus = 30 * (1 - minutes_to_reset / 15)` — so 1 minute to reset = +28 points, 10 minutes = +10 points, 15 minutes = 0

### 3. `pick_best_target` — relax 7d filter for imminent resets

The candidate filter currently excludes accounts with `cached_usage_7d >= threshold_7d` (85%). Relax this when the 7d window resets within 10 minutes — that 85% is about to become 0%.

**Filter change:**
```
excluded IF cached_usage_7d >= threshold_7d AND NOT 7d_resets_within(10 min)
```

### 4. Helper function: `_resets_within`

Pure function used by all three changes:
```python
def _resets_within(resets_at: str | None, minutes: float) -> bool:
    """Return True if the window resets within the given number of minutes."""
```

Handles None (no data → False), past timestamps (already reset → False), parsing errors (→ False).

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | Add `_resets_within`, modify `should_swap`, `score_candidate`, `pick_best_target` |
| `tests/unit/test_auto_swap.py` | Tests for window-aware behavior: suppressed swap, candidate bonus, filter relaxation |

## What This Does NOT Change

- Adaptive polling intervals (how often to check)
- Window keeper ping logic
- UI countdown display
- The burn-rate projection logic (still works alongside window awareness)
