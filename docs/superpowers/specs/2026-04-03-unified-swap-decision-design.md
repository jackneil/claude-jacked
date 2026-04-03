# Unified Swap Decision Engine

**Date:** 2026-04-03
**Status:** Approved

## Problem

The defensive swap and proactive 7d scheduler are TWO separate code paths. When a defensive swap fires, `pick_best_target` chooses the target using `score_candidate` which doesn't factor in 7d deficit. Result: Account 1 hits 85% 7d, defensive swap fires, picks Account 2 (18% 7d, 6.8 days left) instead of Account 3 (85% 7d, 0.5 days left) — even though Account 3's remaining 15% capacity will be wasted if not used before tomorrow morning.

## Solution

### 1. Add 7d deficit bonus to `score_candidate`

`score_candidate` already has many factors. Add 7d deficit as one more. This unifies defensive and proactive swap target selection — both use the same scoring.

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
# get a bonus proportional to their deficit. This ensures defensive
# swaps prefer accounts with expiring capacity.
deficit_result = compute_7d_deficit(account, active_start, active_end)
if deficit_result and deficit_result["deficit"] > 0:
    score += deficit_result["deficit"] * 0.5
```

An account 30% behind schedule gets +15 points. Combined with the low 5h usage, Account 3 (5% 5h, 30%+ deficit) would score much higher than Account 2 (74% 5h, negative deficit).

### 2. Update all `score_candidate` callers

Pass `active_start` and `active_end` from settings:
- `pick_best_target` needs the params (add to its signature, pass through)
- The escape hatch `score_candidate` call
- The proactive scheduler (already has the settings)

### 3. Simplify proactive scheduler

Since `pick_best_target` now factors in deficit via scoring, the proactive scheduler doesn't need its own separate deficit scan. Instead:

```
if not want_swap and not escape_override:
    # Just check: is there a candidate with score > threshold
    # that we should proactively switch to?
    if usage_5h < warning_5h:  # active account comfortable
        target = pick_best_target(accounts, ...)
        if target:
            deficit = compute_7d_deficit(target, ...)
            if deficit and deficit["deficit"] > PROACTIVE_SWAP_THRESHOLD:
                # Proactive swap — the scoring already picked the best target
                execute_swap(target, reason=proactive_reason)
```

The scoring picks the right target. The proactive check just gates on whether the deficit is high enough to justify an unsolicited swap.

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | `score_candidate`: add active hours params + 7d deficit bonus. `pick_best_target`: pass through active hours. |
| `jacked/api/usage_monitor.py` | Update all `score_candidate` / `pick_best_target` callers to pass active hours. Simplify proactive scheduler. |
| `tests/unit/test_auto_swap.py` | Tests for deficit bonus in scoring, updated signatures |
