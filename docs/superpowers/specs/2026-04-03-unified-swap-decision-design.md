# Unified Swap Decision Engine

**Date:** 2026-04-03
**Status:** Approved (revised after DCR wave 1)

## Problem

The defensive swap and proactive 7d scheduler are TWO separate code paths with FOUR bugs:

1. **Scoring gap:** When a defensive swap fires, `pick_best_target` chooses the target using `score_candidate` which doesn't factor in 7d deficit. Result: Account 1 hits 85% 7d, defensive swap fires, picks Account 2 (18% 7d, 6.8 days left) instead of Account 3 (85% 7d, 0.5 days left) — even though Account 3's remaining 15% capacity will be wasted if not used before tomorrow morning.

2. **Filter gap:** `pick_best_target` filters out accounts with `cached_usage_7d >= threshold_7d` (85%) UNLESS the 7d window resets within 10 minutes. Account 3 at 85% 7d with 12 hours to reset gets excluded before it's ever scored.

3. **Duplicate code path:** The proactive scheduler bypasses `pick_best_target` entirely, doing its own deficit scan and target selection without the benefit of scoring.

4. **Ping-pong:** Proactive swap fires, sends user TO a high-7d account to burn capacity. Next tick, `should_swap` fires the 7d defensive trigger (`85% >= 85%`) and swaps AWAY. The two paths issue contradictory directives, causing 5-minute oscillation.

### Key insight: deficit vs urgency

"Deficit" (how far behind schedule) and "urgency" (capacity about to expire) are different metrics. An account at 85% 7d with 12 hours left has a deficit of only ~8% (barely behind schedule) but has 15% capacity that will be permanently lost. The filter relaxation must use **urgency** (behind schedule + limited time remaining), not raw deficit threshold.

## Solution

### 1. Relax 7d filter in `pick_best_target` using urgency criterion

The current filter:
```python
and (
    (a.get("cached_usage_7d") or 0) < threshold_7d
    or _resets_within(a.get("cached_7d_resets_at"), RESET_SUPPRESS_MINUTES)
)
```

Add a third relaxation using urgency: accounts that are behind schedule AND have limited time remaining pass through. The filter's job is just to prevent scoring obviously-bad candidates — the scorer will rank them appropriately.

```python
and (
    (a.get("cached_usage_7d") or 0) < threshold_7d
    or _resets_within(a.get("cached_7d_resets_at"), RESET_SUPPRESS_MINUTES)
    or _has_expiring_capacity(a, active_start, active_end)
)
```

`_has_expiring_capacity`: account has `deficit > 0` (any amount behind schedule) AND `effective_hours_remaining < URGENCY_HOURS` (24 working hours — one working day). This directly captures "capacity that will be lost if not used soon."

Why 24 hours: one working day (15 active hours) is roughly 3 five-hour windows. If an account is behind schedule with only 3 windows left, there's no time to defer — use it now or lose it.

Math validation for the original bug scenario:
- Account 3: 85% 7d, 12 hours until reset
- elapsed_fraction ≈ 93%, expected = 93%, deficit = 93% - 85% = 8% (> 0 ✓)
- effective_hours_remaining ≈ 12 hours (< 24 ✓)
- **Passes filter.** Scorer then ranks it.

`pick_best_target` signature changes to accept `active_start` and `active_end`:

```python
def pick_best_target(
    accounts: list[dict],
    current_id: int,
    threshold_7d: float = 85,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> dict | None:
```

### 2. Add 7d deficit bonus to `score_candidate`

`score_candidate` already has many factors. Add 7d deficit as one more. This ensures the scoring ranks deficit-heavy accounts higher.

`score_candidate` signature changes to accept `active_start` and `active_end`:

```python
def score_candidate(
    account: dict,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> float:
```

New scoring factor:
```python
# 7d deficit bonus: accounts behind schedule on 7d utilization
# get a bonus proportional to their deficit.
deficit_result = compute_7d_deficit(account, active_start, active_end)
if deficit_result and deficit_result["deficit"] > 0:
    # 0.5 weight: 30% deficit = +15 points, keeping it below
    # the inactive-window bonus (15) for moderate deficits.
    score += deficit_result["deficit"] * 0.5
```

### 3. Suppress 7d defensive trigger on deficit accounts (anti-ping-pong)

`should_swap` currently fires the 7d defensive trigger unconditionally when `usage_7d >= threshold_7d`. But if the current account has a positive deficit, we INTENTIONALLY put the user there to burn capacity — don't undo it.

Add `account`, `active_start`, `active_end` params to `should_swap`:

```python
def should_swap(
    ...,
    account: dict | None = None,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> bool:
```

In the 7d trigger:
```python
# 7-day saturation (unless 7d reset imminent OR we're burning deficit capacity).
if usage_7d is not None and usage_7d >= threshold_7d and not suppress_7d:
    if account is not None:
        deficit_result = compute_7d_deficit(account, active_start, active_end)
        if deficit_result and deficit_result["deficit"] > 0:
            pass  # suppress — we're intentionally burning this capacity
        else:
            return True
    else:
        return True
```

### 4. Raise escape hatch override threshold

The deficit bonus adds up to ~25 points for realistic scenarios (50% deficit × 0.5 = +25). The `SUPPRESS_OVERRIDE_SCORE` of 80 was calibrated for the pre-deficit scoring range. Raise to 100 to maintain the intent of "candidate must be exceptionally good."

### 5. Update all callers

Pass `active_start` and `active_end` from settings:
- `pick_best_target` — add to signature, pass through to urgency check and scoring
- `should_swap` — add account + active hours (for deficit-aware 7d suppression)
- ALL `score_candidate` calls in `usage_monitor.py` (escape hatch reason at line 377 too)
- The sort key lambda in `pick_best_target`

### 6. Simplify proactive scheduler

Since `pick_best_target` now factors in deficit via both filtering and scoring, the proactive scheduler doesn't need its own separate deficit scan. Instead:

```
if not want_swap and not escape_override:
    if usage_5h < warning_5h:  # active account comfortable
        target = pick_best_target(accounts, ..., active_start, active_end)
        if target:
            deficit = compute_7d_deficit(target, active_start, active_end)
            if deficit and deficit["deficit"] > PROACTIVE_SWAP_THRESHOLD:
                execute_swap(target, reason=proactive_reason)
```

### 7. Add logging to decision functions

Add module-level `logger` to `auto_swap.py`. Log at DEBUG level:
- `_has_expiring_capacity`: account id, deficit value, hours remaining, pass/fail
- `score_candidate`: when deficit bonus applied (account id, deficit, bonus amount)
- `pick_best_target`: candidate count after filtering, top 3 candidates with scores
- `should_swap`: when 7d trigger suppressed due to deficit (account id, deficit value)

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | Add logger. `pick_best_target`: add active hours params, urgency-based filter relaxation. `score_candidate`: add active hours params + deficit bonus. `should_swap`: add account + active hours, suppress 7d trigger on deficit accounts. Raise `SUPPRESS_OVERRIDE_SCORE` to 100. |
| `jacked/api/usage_monitor.py` | Update ALL callers to pass active hours and account. Simplify proactive scheduler. |
| `tests/unit/test_auto_swap.py` | Tests for urgency filter relaxation, deficit bonus, ping-pong prevention, end-to-end original bug scenario. |
