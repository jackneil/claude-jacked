# Window-Aware Swap Decisions

**Date:** 2026-04-02
**Status:** Approved (revised after DCR)

## Problem

The auto-swap algorithm treats high usage as always bad, regardless of when the window resets. This leads to wasteful swaps:

- An account at 90% 5h usage with 2 minutes left on the window triggers a swap. But it's about to reset to 0% — swapping away wastes the imminent reset and burns a fresh window on another account.
- An account at 85% 7d with 5 minutes left in its 7-day window triggers a swap. That 85% is about to become 0%.
- When picking swap targets, an account at 80% with 3 minutes to reset scores poorly, even though it's about to be fresh.

Usage windows reset to 0% instantly at `cached_5h_resets_at` / `cached_7d_resets_at`.

## Solution

Factor time-to-reset into both swap decisions (should we swap away?) and target selection (who should we swap to?).

### 1. `should_swap` — suppress swap when window resets within 10 minutes

Before triggering a swap on ANY of the three swap triggers (5h critical, 7d threshold, OR burn-rate projection), check the relevant `cached_*_resets_at`. If the window resets within 10 minutes, suppress the swap.

**Logic (applies to ALL three triggers):**
```
for each swap trigger that would fire:
    if the relevant window resets within RESET_SUPPRESS_MINUTES (10):
        suppress this trigger
```

Specifically:
- 5h critical trigger: suppress if `cached_5h_resets_at` within 10 min
- 7d threshold trigger: suppress if `cached_7d_resets_at` within 10 min
- Burn-rate projection trigger: suppress if `cached_5h_resets_at` within 10 min (burn rate projects 5h, so 5h reset suppresses it)

**Escape hatch:** Suppression is overridden if a clearly better candidate exists. After computing `pick_best_target`, if any candidate scores above `SUPPRESS_OVERRIDE_SCORE` (80), swap anyway — the user-experience cost of staying on a degraded account isn't worth saving a window when a much better option is available.

**Stale data guard:** If `cached_5h_resets_at` is in the past AND `usage_cached_at` is older than the reset timestamp, the usage data is stale (a real reset likely happened but we couldn't fetch). In this case, do NOT trust `cached_usage_5h` for swap decisions — suppress the swap and wait for the next successful fetch to get accurate data.

The 10-minute cutoff is a named constant `RESET_SUPPRESS_MINUTES = 10`.

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

Pure function used by all changes:
```python
def _resets_within(resets_at: str | None, minutes: float) -> bool:
    """Return True if the window resets within the given number of minutes.

    Returns False for: None, past timestamps, parsing errors.
    Assumes system clock is NTP-synchronized within ~1 minute.
    """
```

### 5. `should_swap` signature change

`should_swap` needs access to reset timestamps and `usage_cached_at` to implement suppression and the stale-data guard. Add these parameters:

```python
def should_swap(
    usage_5h, usage_7d,
    critical_5h, warning_5h, threshold_7d,
    burn_rate, check_interval_min,
    resets_5h_at=None,    # NEW: cached_5h_resets_at ISO string
    resets_7d_at=None,    # NEW: cached_7d_resets_at ISO string
    usage_cached_at=None, # NEW: for stale-data guard
) -> bool:
```

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | Add `_resets_within`, `RESET_SUPPRESS_MINUTES`, `SUPPRESS_OVERRIDE_SCORE`. Modify `should_swap` (new params, suppression on all 3 triggers, stale-data guard), `score_candidate` (reset bonus), `pick_best_target` (relax 7d filter) |
| `jacked/api/usage_monitor.py` | Pass new params to `should_swap` from the active poll loop |
| `tests/unit/test_auto_swap.py` | Tests: suppressed swap (5h, 7d, burn-rate), escape hatch override, stale-data guard, candidate reset bonus, 7d filter relaxation, burn-rate + imminent reset interaction |

## What This Does NOT Change

- Adaptive polling intervals (how often to check)
- Window keeper ping logic
- UI countdown display
- Suppression does NOT affect urgency tier calculation (polling stays fast during suppression)
