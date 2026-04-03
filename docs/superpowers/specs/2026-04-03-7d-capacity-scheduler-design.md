# 7-Day Capacity Scheduler: Proactive Account Rotation

**Date:** 2026-04-03
**Status:** Approved

## Problem

The swap algorithm is purely defensive — it only swaps when the active account is in trouble (high usage, approaching limits). This leads to wasted capacity:

- An account with 80% unused 7-day capacity expires tomorrow and is never used
- One account gets burned out in 2 days while others sit idle
- The user loses aggregate capacity because 7-day windows reset unused

The 7-day window is the scarce resource. It takes a full week to reset. A 5-hour window resets in 5 hours. Wasting 7-day capacity is far more costly than wasting 5-hour capacity.

## Constraints

- **5h-to-7d burn rate cap:** You can only burn a fraction of the 7-day limit in any single 5-hour window (~1/15th assuming ~15 effective 5h windows per week). You cannot use up the entire 7-day limit in one sitting.
- **Active hours only:** The user is asleep roughly 10 PM - 7 AM. Only active hours count for capacity planning. These come from window keeper settings (`window_keeper_active_start`, `window_keeper_active_end`).
- **Current 5h window state matters:** Each account's 5h window has its own clock. If a 5h window expires at 8 PM, you only have until 8 PM on that window. A new window starts on the next API call.
- **One account active at a time:** All Claude Code sessions on the machine use the same active account.

## Solution

### 1. Deficit-based scheduling model

For each account, compute a **7-day utilization deficit**: how far behind schedule is this account compared to where it should be, given elapsed time in its 7-day window?

```
elapsed_working_hours = working hours elapsed since 7d window opened
total_working_hours = total working hours in the full 7d window (active hours only, ~70h)
elapsed_fraction = elapsed_working_hours / total_working_hours

expected_usage = elapsed_fraction * 100%
actual_usage = cached_usage_7d
deficit = expected_usage - actual_usage
```

Positive deficit = account is behind schedule (underutilized, wasting capacity).
Negative deficit = account is ahead of schedule (overutilized relative to time).

### 2. Effective remaining capacity calculation

For each account, compute how many effective working hours remain before the 7-day window expires:

1. **Current 5h window:** If open (`cached_5h_resets_at` in the future), remaining time capped by active hours end. If expired/closed, 0 for this window.
2. **Future 5h windows:** Count how many fresh 5h windows can be opened during active hours between now and `cached_7d_resets_at`. Each window provides up to 5 hours of burn time.
3. **Total effective hours:** current_window_remaining + (future_windows × 5h), all capped by active hours and 7d expiry.

This tells us the MAXIMUM amount of 7d capacity we CAN still burn. If the deficit exceeds this maximum, we've already lost some capacity — the scheduler should have caught it earlier.

### 3. Proactive swap trigger

Each tick of the active poll loop, after the defensive swap check (`should_swap`):

1. Compute `7d_deficit` for all non-active accounts
2. Find the account with the highest deficit
3. If `deficit > PROACTIVE_SWAP_THRESHOLD` (e.g., 15%) AND the active account is NOT in critical need (usage < warning_5h):
   - Trigger a proactive swap to the highest-deficit account
   - Reason: "Proactive: Account B is X% behind 7d schedule (Y effective hours remaining)"

The threshold prevents constant swapping for tiny deficits. Only swap when an account is meaningfully behind.

### 4. Active hours calculation

Uses the window keeper settings:
- `window_keeper_active_start` (e.g., "07:00")
- `window_keeper_active_end` (e.g., "22:00")

Working hours per day = end - start (e.g., 15 hours).

To count working hours between two datetimes:
- For each calendar day in the range, add `min(active_end, day_end) - max(active_start, day_start)` clamped to 0.
- Skip overnight hours entirely.

### 5. Integration with existing swap logic

The proactive scheduler runs AFTER the defensive checks:

```
1. should_swap (defensive) → swap if active account in trouble
2. escape hatch → swap if clearly better candidate during suppression
3. proactive 7d scheduler → swap to highest-deficit account if above threshold
```

Priority: defensive > escape hatch > proactive. We never proactively swap when the active account itself needs attention (that's handled by steps 1-2).

### 6. Interaction with window-aware swap decisions

The window-aware logic (don't swap away from accounts about to reset, prefer targets about to reset) complements the scheduler:
- Scheduler says "swap TO Account B (highest deficit)"
- Window-aware logic says "Account B's 5h window resets in 3 min, add +30 bonus"
- Both agree: Account B is the right target

If the scheduler wants to swap to B but B's 7d window is about to reset (within 10 min), the window-aware suppression would prevent it — correctly, because the deficit is about to disappear anyway.

## New function: `compute_7d_deficit`

```python
def compute_7d_deficit(
    account: dict,
    active_start: str,  # "07:00"
    active_end: str,     # "22:00"
) -> dict:
    """Compute 7-day utilization deficit for an account.

    Returns dict with:
        deficit: float (positive = behind schedule)
        effective_hours_remaining: float
        effective_windows_remaining: float
        unused_7d: float
    """
```

Pure function in `auto_swap.py`. No I/O.

## New function: `compute_effective_working_hours`

```python
def compute_effective_working_hours(
    start_dt: datetime,
    end_dt: datetime,
    active_start: str,
    active_end: str,
) -> float:
    """Count working hours between two datetimes, excluding overnight."""
```

Pure function. Used by `compute_7d_deficit` for both elapsed and remaining calculations.

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | Add `compute_effective_working_hours`, `compute_7d_deficit` |
| `jacked/api/usage_monitor.py` | Add proactive swap check after defensive checks in active poll loop |
| `tests/unit/test_auto_swap.py` | Tests for deficit calculation, working hours, scheduling scenarios |

## What This Does NOT Change

- The defensive swap logic (should_swap, pick_best_target)
- The window-aware suppression/bonus
- The adaptive polling intervals
- The coordinator ceiling / 429 recovery
- The sweep loop changes (on-demand fetch spec)

## Swap Reason String

"Proactive: burning X% unused 7d on Account B — Y effective hours left (Z windows)"
