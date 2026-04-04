# Exhaustion Guard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Never swap to an account that will exhaust immediately — require minimum viable headroom on ALL swap paths (defensive + proactive).

**Architecture:** Two guards, both in `auto_swap.py` pure functions: (1) `has_viable_headroom(account)` — a new predicate that checks whether an account has enough 7d capacity to survive at least one full 5h window's burn (~4.2%). Applied in `pick_best_target`'s filter (defensive path) and in the proactive scanner's candidate loop. (2) Minimum recoverable capacity check in the proactive scanner — skip candidates where `recoverable < burn_per_window`.

**Tech Stack:** Python, pytest

---

## The Problem

The proactive scheduler swapped to an account with 2% unused 7d capacity and 0.4 windows remaining. The account exhausted within minutes, killing all Claude Code sessions. The system should have predicted this and refused the swap.

Two gaps:
1. **Proactive path** has no minimum recoverable capacity — swaps for 1.7% of capacity (less than one window can burn)
2. **ALL swap paths** (defensive + proactive) have no headroom check — `pick_best_target` can return an account at 98% 7d if it passes the urgency filter, even though it will exhaust immediately

## The Fix

**`has_viable_headroom(account, active_start, active_end)`** — pure function that returns False if an account's unused 7d capacity is less than `burn_per_window`. This means: if you can't even fill one 5h window on this account, don't swap to it.

Applied in two places:
- `pick_best_target` filter — defensive swaps won't target near-exhausted accounts
- Proactive scanner loop — proactive swaps won't target near-exhausted accounts

With 06:00-23:00 active hours: `burn_per_window = 4.2%`. An account at 98% 7d has 2% unused < 4.2% → **rejected**. An account at 94% 7d has 6% unused > 4.2% → **OK**.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `jacked/web/auto_swap.py` | Add `has_viable_headroom()` pure function, add to `pick_best_target` filter |
| `jacked/api/usage_monitor.py` | Add recoverable < burn guard + headroom check in proactive scanner |
| `tests/unit/test_auto_swap.py` | Tests for headroom check |

---

### Task 1: Add `has_viable_headroom` + apply in `pick_best_target`

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write the failing tests**

Add `has_viable_headroom` to the import list at the top of `tests/unit/test_auto_swap.py`:

```python
from jacked.web.auto_swap import (
    BurnRate,
    _resets_within,
    compute_7d_deficit,
    compute_effective_working_hours,
    compute_urgency_threshold,
    format_account_label,
    has_viable_headroom,
    pick_best_target,
    score_candidate,
    should_swap,
    tier_critical_threshold,
    update_burn_rate,
)
```

Add test class before `TestComputeUrgencyThreshold`:

```python
# ---------------------------------------------------------------------------
# has_viable_headroom
# ---------------------------------------------------------------------------

class TestHasViableHeadroom:
    def test_plenty_of_headroom(self):
        """Account at 50% 7d has plenty of headroom."""
        acct = {"cached_usage_7d": 50.0}
        assert has_viable_headroom(acct) is True

    def test_near_exhaustion_rejected(self):
        """Account at 98% 7d has only 2% unused < 4.2% burn → rejected."""
        acct = {"cached_usage_7d": 98.0}
        assert has_viable_headroom(acct) is False

    def test_exactly_at_burn_threshold(self):
        """Account with unused == burn_per_window is viable (boundary)."""
        # burn_per_window ≈ 4.2% with default 06:00-23:00
        # So 95.8% 7d → 4.2% unused → exactly viable
        acct = {"cached_usage_7d": 95.8}
        assert has_viable_headroom(acct) is True

    def test_just_below_burn_threshold(self):
        """Account with unused < burn_per_window is not viable."""
        acct = {"cached_usage_7d": 96.0}
        # 4.0% unused < 4.2% burn → not viable
        assert has_viable_headroom(acct) is False

    def test_none_usage_treated_as_zero(self):
        """None usage = 0% used = 100% headroom → viable."""
        acct = {"cached_usage_7d": None}
        assert has_viable_headroom(acct) is True

    def test_narrow_active_hours_higher_burn(self):
        """Narrow active hours (09:00-17:00) = higher burn per window = stricter."""
        # 8h/day → burn = 100/11.2 ≈ 8.9%
        # Account at 93% → 7% unused < 8.9% → not viable
        acct = {"cached_usage_7d": 93.0}
        assert has_viable_headroom(acct, active_start="09:00", active_end="17:00") is False

    def test_pick_best_target_excludes_near_exhausted(self):
        """pick_best_target should NOT return an account at 98% 7d even via urgency."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90),  # current
            _acct(2, usage_5h=0, usage_7d=98),  # near-exhausted
        ]
        # Account 2 is behind schedule (urgency would normally pass it)
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="06:00", active_end="23:00",
        )
        # Should return None — the only candidate is near-exhausted
        assert result is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestHasViableHeadroom -v`
Expected: FAIL — `has_viable_headroom` not found

- [ ] **Step 3: Implement `has_viable_headroom`**

In `jacked/web/auto_swap.py`, after `compute_burn_per_window` (around line 190), add:

```python
def has_viable_headroom(
    account: dict,
    active_start: str = "06:00",
    active_end: str = "23:00",
) -> bool:
    """Check if an account has enough 7d headroom to survive one 5h window.

    Returns False if unused 7d capacity < burn_per_window. Swapping to
    an account that can't even fill one window is pointless and risks
    immediate exhaustion.
    """
    usage_7d = account.get("cached_usage_7d") or 0
    unused = 100.0 - usage_7d
    burn = compute_burn_per_window(active_start, active_end)
    return unused >= burn
```

- [ ] **Step 4: Add headroom check to `pick_best_target` filter**

In `pick_best_target`, add `has_viable_headroom` to the candidate filter. Find the list comprehension that builds `candidates` (around line 527-537). Add one more condition:

```python
    candidates = [
        a for a in accounts
        if a["id"] != current_id
        and a.get("is_active") != 0
        and a.get("is_deleted") != 1
        and (a.get("consecutive_failures") or 0) < 3
        and a.get("validation_status") != "invalid"
        and a.get("cc_access_token") is not None
        and a.get("auto_swap_enabled") != 0
        and has_viable_headroom(a, active_start, active_end)
        and _passes_7d_filter(a)
    ]
```

Note: `has_viable_headroom` goes BEFORE `_passes_7d_filter` because it's cheaper (no `compute_7d_deficit` call) and filters more aggressively.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: has_viable_headroom guard — never swap to near-exhausted accounts"
```

---

### Task 2: Add minimum recoverable + headroom guard in proactive scanner

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Add headroom check in proactive candidate loop**

In the proactive scanner's candidate loop (in `usage_monitor.py`), BEFORE the `compute_7d_deficit` call, add a headroom check. Find the line:

```python
                            dr = compute_7d_deficit(acct, active_start, active_end)
```

Add before it:

```python
                            # Skip accounts that would exhaust immediately
                            from jacked.web.auto_swap import has_viable_headroom
                            if not has_viable_headroom(acct, active_start, active_end):
                                _candidate_summaries.append({
                                    "id": acct["id"],
                                    "email": acct.get("email", ""),
                                    "7d": acct.get("cached_usage_7d"),
                                    "passes": False,
                                    "skip_reason": "near_exhaustion",
                                })
                                continue
```

- [ ] **Step 2: Add minimum recoverable check after recoverable calculation**

Find the `recoverable` calculation in the proactive loop:

```python
                            recoverable = min(
                                dr["unused_7d"],
                                dr["effective_windows_remaining"] * burn,
                            )
                            urgency = recoverable / max(dr["effective_hours_remaining"], 0.5)
```

Insert between `recoverable` and `urgency`:

```python
                            # Skip if recoverable capacity is less than one
                            # window's burn — not worth the disruption of
                            # swapping for scraps that'll exhaust immediately.
                            if recoverable < burn:
                                _candidate_summaries.append({
                                    "id": acct["id"],
                                    "email": acct.get("email", ""),
                                    "7d": acct.get("cached_usage_7d"),
                                    "deficit": round(dr["deficit"], 1),
                                    "recoverable": round(recoverable, 1),
                                    "passes": False,
                                    "skip_reason": "recoverable_too_low",
                                })
                                continue
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: minimum recoverable guard in proactive scanner — no swapping for scraps"
```
