# 7-Day Capacity Scheduler: Proactive Account Rotation

**Date:** 2026-04-03
**Status:** SUPERSEDED — decisioning portion replaced by `2026-05-04-auto-swap-utilization-redesign-design.md`

## Problem

The swap algorithm is purely defensive — it only swaps when the active account is in trouble. This leads to wasted 7-day capacity across accounts. The 7-day window is the scarce resource (takes a full week to reset). A 5-hour window resets in 5 hours.

## Constraints

- **5h-to-7d burn rate cap:** Each 5h window can only burn a fraction of 7d capacity. You need multiple windows across the week to fully utilize a 7d window.
- **Active hours only:** User sleeps ~10 PM - 7 AM. Only active hours count. Uses window keeper settings.
- **Current 5h window state matters:** Each account's 5h window has its own clock.
- **One account active at a time.**

## Solution

### 1. Deficit-based scheduling model

For each account, compute how far behind schedule its 7-day utilization is.

**Window start derivation:** Subtract 7 days from `cached_7d_resets_at` to estimate when the window opened. Assumption: Anthropic's 7-day window is exactly 168 hours (rolling). This assumption is documented and can be adjusted if proven wrong.

**Deficit formula:**
```
window_start = cached_7d_resets_at - 7 days
elapsed_working_hours = working hours between window_start and now (active hours only)
total_working_hours = working hours in the full 7d window (active hours only, computed from settings)
elapsed_fraction = elapsed_working_hours / total_working_hours

expected_usage = elapsed_fraction * 100%
actual_usage = cached_usage_7d
deficit = expected_usage - actual_usage
```

Positive deficit = behind schedule (underutilized). Negative = ahead of schedule.

**Timezone handling:** `cached_7d_resets_at` is stored as a UTC ISO string from the Anthropic API. The caller converts to local time before passing to `compute_effective_working_hours`. Active hours (`window_keeper_active_start/end`) are in local time. All working-hours calculations happen in local time. This is consistent with how `window_keeper.py` already handles active hours.

**Tier simplification:** The linear burn model treats all tiers equally. Higher-tier accounts (20x) can technically burn more per 5h window, but the deficit model works as a first approximation. Tier weighting can be added as a refinement if needed.

### 2. Effective remaining capacity

For each account:
1. **Current 5h window:** If open, remaining time capped by active hours end. If expired, 0.
2. **Future 5h windows:** Count how many fresh windows fit within active hours between now and `cached_7d_resets_at`.
3. **Total effective hours:** current_remaining + (future_windows × 5h).

This is an upper-bound estimate. Actual capacity depends on the window keeper successfully pinging accounts to open their 5h windows.

### 3. Proactive swap trigger

Each tick of the active poll loop, after defensive checks:

1. Compute `7d_deficit` for all non-active accounts
2. Skip accounts where `cached_7d_resets_at` or `cached_usage_7d` is None (no data)
3. Find the account with the highest deficit
4. If `deficit > PROACTIVE_SWAP_THRESHOLD` (e.g., 15%) AND active account is NOT in critical need (`usage_5h < warning_5h`):
   - Trigger proactive swap to the highest-deficit account
   - Reason: "Proactive: burning X% unused 7d on Account B — Y effective hours left (Z windows)"
5. If no account exceeds the threshold, no proactive swap occurs (system is performing well)

**Shares swap cooldown:** The proactive swap uses the same `_SWAP_COOLDOWN_SECONDS` (300s) as defensive swaps. This prevents swap-chains where defensive and proactive swaps alternate rapidly.

### 4. Active hours calculation

**`compute_effective_working_hours(start_dt, end_dt, active_start, active_end)`**

Both `start_dt` and `end_dt` are in LOCAL time (caller converts from UTC). For each calendar day in the range, adds `min(active_end, day_end) - max(active_start, day_start)` clamped to 0. Skips overnight hours. Working hours per day computed dynamically from settings (not hardcoded).

### 5. None/missing data guards

`compute_7d_deficit` returns `None` (or `{"deficit": 0}`) when:
- `cached_7d_resets_at` is None
- `cached_usage_7d` is None
- The 7d window has already expired (resets_at in the past)

The caller in `usage_monitor.py` filters out None results before finding the max-deficit account.

### 6. Priority ordering

```
1. Defensive swap (should_swap) — active account in trouble
2. Escape hatch — suppression override with clearly better candidate
3. Proactive 7d scheduler — highest-deficit account above threshold
```

Defensive always wins. Proactive only fires when the active account is comfortable.

## New functions

**`compute_7d_deficit(account, active_start, active_end)`** → `dict | None`
- Returns `{"deficit": float, "effective_hours_remaining": float, "effective_windows_remaining": float, "unused_7d": float}` or `None` if data insufficient.
- Pure function in `auto_swap.py`.

**`compute_effective_working_hours(start_dt, end_dt, active_start, active_end)`** → `float`
- Counts working hours between two LOCAL datetimes, excluding overnight.
- Pure function in `auto_swap.py`.

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | Add `compute_effective_working_hours`, `compute_7d_deficit` |
| `jacked/api/usage_monitor.py` | Add proactive swap check after defensive checks |
| `tests/unit/test_auto_swap.py` | Tests for deficit calculation, working hours, scenarios |

## Swap Reason

"Proactive: burning X% unused 7d on Account B — Y effective hours left (Z windows)"
