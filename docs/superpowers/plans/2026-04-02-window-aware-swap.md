# Window-Aware Swap Decisions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the auto-swap algorithm factor in time-to-window-reset: don't swap away from accounts about to reset, prefer swap targets about to reset, and relax filters for imminent resets.

**Architecture:** Pure function changes in `auto_swap.py` — add `_resets_within` helper, modify `should_swap` (new params, suppression on all 3 triggers, escape hatch, stale-data guard), modify `score_candidate` (reset bonus), modify `pick_best_target` (relax 7d filter). Update the caller in `usage_monitor.py` to pass the new params.

**Tech Stack:** Python 3.12+, pytest

**Design spec:** `docs/superpowers/specs/2026-04-02-window-aware-swap-decisions-design.md`

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/web/auto_swap.py` | Swap decision engine | Add `_resets_within`, constants, modify 3 functions |
| `jacked/api/usage_monitor.py` | Poll loop caller | Pass new params to `should_swap` |
| `tests/unit/test_auto_swap.py` | Tests | New test classes for window-aware behavior |

---

### Task 1: Add `_resets_within` helper and constants

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_auto_swap.py`:

```python
from jacked.web.auto_swap import _resets_within


class TestResetsWithin:
    def test_resets_in_5_min(self):
        """Window resetting in 5 min, check within 10 -> True."""
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert _resets_within(future, 10) is True

    def test_resets_in_15_min(self):
        """Window resetting in 15 min, check within 10 -> False."""
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
        assert _resets_within(future, 10) is False

    def test_already_reset(self):
        """Past timestamp -> False (already reset)."""
        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        assert _resets_within(past, 10) is False

    def test_none_returns_false(self):
        assert _resets_within(None, 10) is False

    def test_garbage_string_returns_false(self):
        assert _resets_within("not-a-date", 10) is False

    def test_z_suffix_parsed(self):
        """ISO string with Z suffix should parse correctly."""
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        assert _resets_within(future, 10) is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestResetsWithin -v --tb=short`

Expected: FAIL — `_resets_within` doesn't exist

- [ ] **Step 3: Implement**

In `jacked/web/auto_swap.py`, add after the `tier_label` function (around line 33) and before the `should_swap` section:

```python
# ---------------------------------------------------------------------------
# Window-reset awareness
# ---------------------------------------------------------------------------

# Don't swap away from an account whose window resets within this many minutes.
RESET_SUPPRESS_MINUTES = 10

# Override suppression if a candidate scores above this threshold.
# Prevents staying on a degraded account when a clearly better option exists.
SUPPRESS_OVERRIDE_SCORE = 80


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
            return False  # already reset
        remaining = (reset_dt - now).total_seconds() / 60.0
        return remaining <= minutes
    except (ValueError, TypeError):
        return False
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestResetsWithin -v --tb=short`

Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: _resets_within helper for window-aware swap decisions"
```

---

### Task 2: Modify `should_swap` — reset suppression on all 3 triggers

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_auto_swap.py`:

```python
class TestShouldSwapWindowAware:
    def test_suppress_5h_critical_when_reset_imminent(self):
        """5h at 95% but resets in 5 min -> DON'T swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert should_swap(
            usage_5h=95, usage_7d=0,
            resets_5h_at=resets,
        ) is False

    def test_swap_5h_critical_when_reset_far(self):
        """5h at 95% and resets in 3 hours -> swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        assert should_swap(
            usage_5h=95, usage_7d=0,
            resets_5h_at=resets,
        ) is True

    def test_suppress_7d_threshold_when_reset_imminent(self):
        """7d at 90% but resets in 5 min -> DON'T swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        assert should_swap(
            usage_5h=50, usage_7d=90,
            resets_7d_at=resets,
        ) is False

    def test_suppress_burn_rate_when_reset_imminent(self):
        """Burn rate projects critical but 5h resets in 3 min -> DON'T swap."""
        from datetime import datetime, timezone, timedelta
        resets = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        br = BurnRate(rate_5h_per_min=5.0, last_check_5h=82.0)
        assert should_swap(
            usage_5h=82, usage_7d=0,
            burn_rate=br,
            resets_5h_at=resets,
        ) is False

    def test_no_suppression_without_reset_data(self):
        """No resets_at data -> normal behavior, swap on critical."""
        assert should_swap(usage_5h=95, usage_7d=0) is True

    def test_stale_data_guard(self):
        """Reset is past but usage_cached_at is older than reset -> suppress.
        Usage data is stale (429 prevented fetch after reset)."""
        from datetime import datetime, timezone, timedelta
        import time as _time
        # Reset happened 2 min ago, usage was cached 5 min ago (before reset)
        reset_time = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
        cached_at = int(_time.time()) - 300  # 5 min ago
        assert should_swap(
            usage_5h=95, usage_7d=0,
            resets_5h_at=reset_time,
            usage_cached_at=cached_at,
        ) is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestShouldSwapWindowAware -v --tb=short`

- [ ] **Step 3: Modify `should_swap`**

Change the signature to add new params:

```python
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
) -> bool:
```

Replace the body with:

```python
    if usage_5h is None:
        return False

    # Stale-data guard: if the 5h reset is in the past but our usage data
    # is older than the reset, the usage is stale (a real reset happened
    # but we couldn't fetch). Don't trust the data — suppress swap.
    if resets_5h_at and usage_cached_at:
        try:
            reset_dt = datetime.fromisoformat(resets_5h_at.replace("Z", "+00:00"))
            if reset_dt <= datetime.now(timezone.utc):
                reset_epoch = reset_dt.timestamp()
                if usage_cached_at < reset_epoch:
                    return False  # usage data predates the reset
        except (ValueError, TypeError):
            pass

    # Helper: should this trigger be suppressed due to imminent reset?
    suppress_5h = _resets_within(resets_5h_at, RESET_SUPPRESS_MINUTES)
    suppress_7d = _resets_within(resets_7d_at, RESET_SUPPRESS_MINUTES)

    # Hard ceiling — swap immediately (unless 5h reset imminent).
    if usage_5h >= critical_5h and not suppress_5h:
        return True

    # 7-day saturation (unless 7d reset imminent).
    if usage_7d is not None and usage_7d >= threshold_7d and not suppress_7d:
        return True

    # Warning zone + burn-rate projection (unless 5h reset imminent).
    if usage_5h >= warning_5h and burn_rate is not None and not suppress_5h:
        minutes_to_critical = _minutes_until(
            usage_5h, critical_5h, burn_rate.rate_5h_per_min,
        )
        if minutes_to_critical is not None and minutes_to_critical <= 2 * check_interval_min:
            return True

    return False
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v --tb=short`

Expected: ALL PASS (including existing tests — the new params are optional with defaults)

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: should_swap suppresses all 3 triggers when reset is imminent"
```

---

### Task 3: Modify `score_candidate` — reset proximity bonus

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_auto_swap.py`:

```python
class TestScoreResetBonus:
    def test_imminent_reset_gets_bonus(self):
        """Account with 5h reset in 3 min should score higher than one without."""
        from datetime import datetime, timezone, timedelta
        resets_soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        a = _acct(1, usage_5h=80, resets_5h=resets_soon)
        b = _acct(2, usage_5h=80)  # no imminent reset
        assert score_candidate(a) > score_candidate(b)

    def test_no_bonus_beyond_15_min(self):
        """Account with reset in 20 min should NOT get the bonus."""
        from datetime import datetime, timezone, timedelta
        resets_far = (datetime.now(timezone.utc) + timedelta(minutes=20)).isoformat()
        a = _acct(1, usage_5h=80, resets_5h=resets_far)
        b = _acct(2, usage_5h=80)  # no reset info
        # Scores should be approximately equal (no significant bonus)
        assert abs(score_candidate(a) - score_candidate(b)) < 2
```

Note: The `_acct` helper may need a `resets_5h` parameter. Check the existing helper and add it if missing.

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Add reset bonus to `score_candidate`**

In `score_candidate`, after the existing "Bonus for inactive/expired 5h window" block (around line 161), add:

```python
    # Bonus for imminent 5h reset — encourages swapping TO accounts about
    # to get a fresh window. Max +30 when reset is 0 min away, tapering to
    # 0 at 15 min.
    if resets_5h:
        try:
            r = datetime.fromisoformat(resets_5h.replace("Z", "+00:00"))
            remaining_min = (r - datetime.now(timezone.utc)).total_seconds() / 60.0
            if 0 < remaining_min <= 15:
                score += 30 * (1 - remaining_min / 15)
        except (ValueError, TypeError):
            pass
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v --tb=short`

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: score_candidate adds reset proximity bonus (max +30)"
```

---

### Task 4: Modify `pick_best_target` — relax 7d filter for imminent resets

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_auto_swap.py`:

```python
class TestPickTargetResetRelax:
    def test_7d_over_threshold_but_reset_imminent_not_excluded(self):
        """Account over 7d threshold but resetting in 5 min -> still a candidate."""
        from datetime import datetime, timezone, timedelta
        resets_soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        accounts = [
            _acct(1, usage_5h=90),  # current account
            _acct(2, usage_5h=10, usage_7d=90, resets_7d=resets_soon),  # over 7d but reset imminent
        ]
        # Need to add resets_7d to the account dict
        accounts[1]["cached_7d_resets_at"] = resets_soon
        result = pick_best_target(accounts, current_id=1)
        assert result is not None
        assert result["id"] == 2

    def test_7d_over_threshold_reset_far_still_excluded(self):
        """Account over 7d threshold with reset far out -> still excluded."""
        from datetime import datetime, timezone, timedelta
        resets_far = (datetime.now(timezone.utc) + timedelta(days=5)).isoformat()
        accounts = [
            _acct(1, usage_5h=90),
            _acct(2, usage_5h=10, usage_7d=90, resets_7d=resets_far),
        ]
        accounts[1]["cached_7d_resets_at"] = resets_far
        result = pick_best_target(accounts, current_id=1)
        assert result is None
```

- [ ] **Step 2: Run tests to verify they fail**

- [ ] **Step 3: Modify the 7d filter in `pick_best_target`**

Change the filter line (currently line 185):

```python
        and (a.get("cached_usage_7d") or 0) < threshold_7d
```

To:

```python
        and (
            (a.get("cached_usage_7d") or 0) < threshold_7d
            or _resets_within(a.get("cached_7d_resets_at"), RESET_SUPPRESS_MINUTES)
        )
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v --tb=short`

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: pick_best_target relaxes 7d filter for imminent resets"
```

---

### Task 5: Update caller in usage_monitor.py — pass new params to should_swap

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Pass reset timestamps and usage_cached_at to should_swap**

In the active poll loop (around line 237), find the `should_swap` call:

```python
            if should_swap(
                usage_5h=usage_5h,
                usage_7d=usage_7d,
                critical_5h=effective_critical,
                warning_5h=warning_5h,
                threshold_7d=threshold_7d,
                burn_rate=br,
                check_interval_min=check_interval / 60,
            ):
```

Change to:

```python
            if should_swap(
                usage_5h=usage_5h,
                usage_7d=usage_7d,
                critical_5h=effective_critical,
                warning_5h=warning_5h,
                threshold_7d=threshold_7d,
                burn_rate=br,
                check_interval_min=check_interval / 60,
                resets_5h_at=active_acct.get("cached_5h_resets_at"),
                resets_7d_at=active_acct.get("cached_7d_resets_at"),
                usage_cached_at=active_acct.get("usage_cached_at"),
            ):
```

- [ ] **Step 2: Add escape hatch — check best candidate score before suppression override**

After the `should_swap` call returns False (i.e., swap was suppressed), we need to check the escape hatch. But `should_swap` returns a bool — we can't tell if it was suppressed or genuinely not needed.

The simpler approach: in the caller, after `should_swap` returns False, check if suppression was the reason and if so, check the escape hatch. Actually, even simpler — add the escape hatch logic to the caller, not to `should_swap`:

After the `if should_swap(...)` block, add an else clause that checks for suppression override:

```python
            else:
                # Escape hatch: if should_swap returned False due to reset
                # suppression, check if a clearly better candidate exists.
                # If so, swap anyway — don't keep the user on a degraded
                # account just to save a window reset.
                from jacked.web.auto_swap import RESET_SUPPRESS_MINUTES, SUPPRESS_OVERRIDE_SCORE, _resets_within
                if (
                    usage_5h is not None
                    and usage_5h >= warning_5h
                    and _resets_within(active_acct.get("cached_5h_resets_at"), RESET_SUPPRESS_MINUTES)
                ):
                    override_target = pick_best_target(
                        accounts, current_id=active_acct_id,
                        threshold_7d=threshold_7d,
                    )
                    if override_target and score_candidate(override_target) > SUPPRESS_OVERRIDE_SCORE:
                        # Treat as should_swap=True, target already found
                        target = override_target
                        # ... execute swap (same code as the normal swap path)
```

Actually, this duplicates the swap execution code. The cleaner approach: make the escape hatch part of the existing swap block. Restructure the if/else so the swap execution code is shared. This is getting complex — let the implementer read both paths and refactor cleanly. The key contract:

1. `should_swap` returns True → proceed with swap as before
2. `should_swap` returns False BUT usage is in warning+ zone AND 5h reset is imminent → check `pick_best_target` score > `SUPPRESS_OVERRIDE_SCORE` → if yes, swap anyway

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 4: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: pass reset timestamps to should_swap, add escape hatch override"
```

---

### Task 6: Run full test suite

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

Expected: All tests pass.
