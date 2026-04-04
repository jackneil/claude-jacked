# Capacity Waste Proactive Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the broken proactive scheduler (fixed 15% deficit threshold, picks best-scored target instead of most-urgent) with a capacity waste model that uses remaining 5h windows to determine urgency and scales the threshold down as time runs out.

**Architecture:** Two changes: (1) Add `compute_urgency_threshold` pure function to `auto_swap.py` that returns the deficit threshold based on remaining 5h windows. (2) Rewrite the proactive scanner in `usage_monitor.py` to scan ALL candidates by urgency (not use `pick_best_target`), apply the scaled threshold, and pick the most urgent candidate.

**Tech Stack:** Python, pytest

**Spec:** `docs/architecture/auto-swap-system.md` — "Capacity Waste Model" section

---

## File Structure

| File | Responsibility |
|------|---------------|
| `jacked/web/auto_swap.py` | Add `compute_urgency_threshold()` pure function |
| `jacked/api/usage_monitor.py` | Rewrite proactive scheduler to use urgency scan |
| `tests/unit/test_auto_swap.py` | Tests for urgency threshold tiers |

---

### Task 1: Add `compute_urgency_threshold` + `compute_burn_per_window` to `auto_swap.py`

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_auto_swap.py`. First add `compute_urgency_threshold` to the import list at the top:

```python
from jacked.web.auto_swap import (
    BurnRate,
    _resets_within,
    compute_7d_deficit,
    compute_effective_working_hours,
    compute_urgency_threshold,
    format_account_label,
    pick_best_target,
    score_candidate,
    should_swap,
    tier_critical_threshold,
    update_burn_rate,
)
```

Then add at the end of the file before `TestFormatAccountLabel`:

```python
# ---------------------------------------------------------------------------
# compute_urgency_threshold
# ---------------------------------------------------------------------------

class TestComputeUrgencyThreshold:
    def test_critical_last_partial_window(self):
        """< 1 window remaining: any deficit triggers (threshold = 0)."""
        threshold = compute_urgency_threshold(
            effective_windows_remaining=0.5,
            active_start="06:00", active_end="23:00",
        )
        assert threshold == 0.0

    def test_high_one_to_two_windows(self):
        """1-2 windows: threshold = burn_per_window (~4.2%)."""
        threshold = compute_urgency_threshold(
            effective_windows_remaining=1.5,
            active_start="06:00", active_end="23:00",
        )
        # burn_per_window = 100 / (7 * 17/5) = 100/23.8 ≈ 4.2
        assert 3.5 < threshold < 5.0

    def test_medium_three_to_four_windows(self):
        """3-4 windows: threshold = 2 * burn_per_window (~8.4%)."""
        threshold = compute_urgency_threshold(
            effective_windows_remaining=3.5,
            active_start="06:00", active_end="23:00",
        )
        assert 7.0 < threshold < 10.0

    def test_normal_five_plus_windows(self):
        """5+ windows: full PROACTIVE_SWAP_THRESHOLD (15%)."""
        threshold = compute_urgency_threshold(
            effective_windows_remaining=6.0,
            active_start="06:00", active_end="23:00",
        )
        assert threshold == 15.0

    def test_zero_windows(self):
        """0 windows: threshold = 0 (too late but still try)."""
        threshold = compute_urgency_threshold(
            effective_windows_remaining=0.0,
            active_start="06:00", active_end="23:00",
        )
        assert threshold == 0.0

    def test_boundary_exactly_one_window(self):
        """Exactly 1.0 windows: HIGH tier (1-2 range)."""
        threshold = compute_urgency_threshold(
            effective_windows_remaining=1.0,
            active_start="06:00", active_end="23:00",
        )
        assert 3.5 < threshold < 5.0

    def test_boundary_exactly_five_windows(self):
        """Exactly 5.0 windows: NORMAL tier."""
        threshold = compute_urgency_threshold(
            effective_windows_remaining=5.0,
            active_start="06:00", active_end="23:00",
        )
        assert threshold == 15.0

    def test_narrow_active_hours(self):
        """Narrow active hours (09:00-17:00 = 8h/day) changes burn_per_window."""
        # 8h/day → 7*8/5 = 11.2 windows/week → burn = 100/11.2 ≈ 8.9%/window
        threshold = compute_urgency_threshold(
            effective_windows_remaining=1.5,
            active_start="09:00", active_end="17:00",
        )
        # HIGH tier: threshold = burn_per_window ≈ 8.9%
        assert 8.0 < threshold < 10.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestComputeUrgencyThreshold -v`
Expected: FAIL — `compute_urgency_threshold` not found

- [ ] **Step 3: Implement `compute_urgency_threshold`**

In `jacked/web/auto_swap.py`, after the `URGENCY_HOURS` constant (line ~175), add:

```python
def compute_burn_per_window(active_start: str = "06:00", active_end: str = "23:00") -> float:
    """Max 7d capacity (%) that can be burned in one 5h window.

    Depends on active hours: more working hours/day = more windows/week
    = less burn per window.
    """
    s_h, s_m = map(int, active_start.split(":"))
    e_h, e_m = map(int, active_end.split(":"))
    working_hours_per_day = (e_h * 60 + e_m - s_h * 60 - s_m) / 60.0
    if working_hours_per_day <= 0:
        return 0.0
    windows_per_week = 7.0 * working_hours_per_day / 5.0
    return 100.0 / windows_per_week


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
        return 0.0  # last chance — any deficit triggers
    if effective_windows_remaining < 3.0:
        return burn  # 1-2 windows — one window's worth
    if effective_windows_remaining < 5.0:
        return burn * 2.0  # 3-4 windows — two windows' worth
    return PROACTIVE_SWAP_THRESHOLD  # 5+ windows — normal threshold
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: compute_urgency_threshold — tiered thresholds based on remaining 5h windows"
```

---

### Task 2: Rewrite proactive scheduler to use urgency scan

**Files:**
- Modify: `jacked/api/usage_monitor.py:586-644`

- [ ] **Step 1: Replace the proactive scheduler block**

Find the entire proactive scheduler (lines 586-644, from `# -- Proactive 7d capacity scheduler` to the end of the block before `except asyncio.CancelledError`). Replace with:

```python
            # -- Proactive 7d capacity scheduler ---------------------------
            # Scan for accounts with EXPIRING capacity that must be burned.
            # Uses remaining 5h windows to determine urgency — the closer
            # to expiry, the lower the threshold for triggering a swap.
            if not want_swap and not escape_override:
                from jacked.web.auto_swap import (
                    compute_7d_deficit,
                    compute_urgency_threshold,
                )

                if usage_5h is not None and usage_5h < warning_5h:
                    # Fetch fresh candidate data
                    accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)

                    # Scan ALL candidates for urgency — not pick_best_target,
                    # because the most-urgent account (expiring capacity) may
                    # not be the highest-scored account overall.
                    best_urgent = None
                    best_urgency = 0.0
                    best_deficit_result = None

                    for acct in accounts:
                        if acct["id"] == active_acct_id:
                            continue
                        if not acct.get("cc_access_token"):
                            continue
                        if acct.get("auto_swap_enabled") == 0:
                            continue
                        if acct.get("is_active") == 0 or acct.get("is_deleted") == 1:
                            continue
                        if (acct.get("consecutive_failures") or 0) >= 3:
                            continue

                        dr = compute_7d_deficit(acct, active_start, active_end)
                        if not dr or dr["deficit"] <= 0:
                            continue

                        # Urgency threshold scales with remaining windows
                        threshold = compute_urgency_threshold(
                            dr["effective_windows_remaining"],
                            active_start, active_end,
                        )
                        if dr["deficit"] <= threshold:
                            continue

                        # Urgency = recoverable capacity per hour of inaction
                        from jacked.web.auto_swap import compute_burn_per_window
                        burn = compute_burn_per_window(active_start, active_end)
                        recoverable = min(
                            dr["unused_7d"],
                            dr["effective_windows_remaining"] * burn,
                        )
                        urgency = recoverable / max(dr["effective_hours_remaining"], 0.5)

                        if urgency > best_urgency:
                            best_urgency = urgency
                            best_urgent = acct
                            best_deficit_result = dr

                    if not best_urgent:
                        logger.debug("Proactive: no urgent candidate found")
                    elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                        logger.debug(
                            "Proactive: urgent target %d found but cooldown active",
                            best_urgent["id"],
                        )
                    else:
                        # Re-fetch fresh data for the target
                        await fetch_usage(best_urgent["id"], db)
                        target = db.get_account(best_urgent["id"])

                        if target:
                            deficit_result = compute_7d_deficit(target, active_start, active_end)
                            if not deficit_result or deficit_result["deficit"] <= 0:
                                logger.debug(
                                    "Proactive: target %d deficit gone after re-fetch",
                                    target["id"],
                                )
                            else:
                                # Re-check threshold with fresh data
                                threshold = compute_urgency_threshold(
                                    deficit_result["effective_windows_remaining"],
                                    active_start, active_end,
                                )
                                if deficit_result["deficit"] <= threshold:
                                    logger.debug(
                                        "Proactive: target %d deficit %.1f%% below threshold %.1f%% after re-fetch",
                                        target["id"], deficit_result["deficit"], threshold,
                                    )
                                else:
                                    reason = (
                                        f"proactive: burning {deficit_result['unused_7d']:.0f}% "
                                        f"unused 7d on {format_account_label(target)} — "
                                        f"{deficit_result['effective_hours_remaining']:.0f}h left "
                                        f"({deficit_result['effective_windows_remaining']:.1f} windows), "
                                        f"deficit={deficit_result['deficit']:.0f}%"
                                    )
                                    logger.info(
                                        "Proactive swap: account %d has %.0f%% deficit, "
                                        "%.1f windows remaining, urgency=%.2f",
                                        target["id"], deficit_result["deficit"],
                                        deficit_result["effective_windows_remaining"],
                                        best_urgency,
                                    )

                                    ws_registry = getattr(app.state, "ws_registry", None)
                                    await _execute_swap(
                                        db, active_acct_id, active_acct, target,
                                        reason=reason, trigger="proactive_7d",
                                        usage_5h=usage_5h, usage_7d=usage_7d,
                                        active_start=active_start, active_end=active_end,
                                        ws_registry=ws_registry,
                                    )
```

- [ ] **Step 2: Clean up unused import**

The old proactive block imported `PROACTIVE_SWAP_THRESHOLD` at line 588. The new block imports `compute_urgency_threshold` instead. Remove `PROACTIVE_SWAP_THRESHOLD` from that import if it's no longer used in this block (it's still used in the `_passes_7d_filter` function in `auto_swap.py`, so the constant itself stays).

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: proactive scheduler uses urgency scan with tiered thresholds"
```

---

### Task 3: Add end-to-end urgency test

**Files:**
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write the test validating the Account 1 scenario**

Add to `tests/unit/test_auto_swap.py` in the `TestComputeUrgencyThreshold` class:

```python
    def test_account1_scenario_triggers(self):
        """Real scenario: 86% 7d, 5.7h remaining = 1.14 windows.

        Deficit ~9.2%, urgency threshold for 1.14 windows (HIGH tier) ~4.2%.
        9.2% > 4.2% → should trigger.
        """
        from datetime import datetime, timezone, timedelta
        import time as _time

        acct = {
            "email": "jack@jackmd.com",
            "cached_usage_7d": 86.0,
            "cached_7d_resets_at": (
                datetime.now(timezone.utc) + timedelta(hours=5, minutes=42)
            ).isoformat(),
            "usage_cached_at": int(_time.time()) - 60,
        }
        deficit = compute_7d_deficit(acct, "06:00", "23:00")
        assert deficit is not None
        assert deficit["deficit"] > 0
        assert deficit["effective_windows_remaining"] < 3.0  # HIGH or CRITICAL

        threshold = compute_urgency_threshold(
            deficit["effective_windows_remaining"],
            "06:00", "23:00",
        )
        # Deficit should exceed the urgency threshold
        assert deficit["deficit"] > threshold, (
            f"deficit {deficit['deficit']:.1f}% should exceed threshold "
            f"{threshold:.1f}% at {deficit['effective_windows_remaining']:.1f} windows"
        )
```

- [ ] **Step 2: Run test**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestComputeUrgencyThreshold::test_account1_scenario_triggers -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_auto_swap.py
git commit -m "test: end-to-end urgency threshold test for Account 1 scenario"
```
