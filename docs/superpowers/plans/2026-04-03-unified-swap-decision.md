# Unified Swap Decision Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Unify defensive and proactive swap target selection into one decision engine with urgency-aware filter relaxation, deficit-based scoring, ping-pong prevention, and diagnosable logging.

**Architecture:** Six changes: (1) Add logging to `auto_swap.py`. (2) Add 7d deficit bonus to `score_candidate`. (3) Relax the 7d filter in `pick_best_target` using urgency criterion (deficit > 0 AND < 24 working hours remaining). (4) Suppress `should_swap`'s 7d defensive trigger on accounts with positive deficit (anti-ping-pong). (5) Raise escape hatch override threshold to 100. (6) Update all callers in `usage_monitor.py` and simplify proactive scheduler. All functions use default params so existing callers don't break.

**Tech Stack:** Python, pytest

**Spec:** `docs/superpowers/specs/2026-04-03-unified-swap-decision-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `jacked/web/auto_swap.py` | Pure decision functions. Add logger. `score_candidate` gets deficit bonus. `pick_best_target` gets urgency-based filter relaxation. `should_swap` gets deficit-aware 7d suppression. All get `active_start`/`active_end` params. Raise `SUPPRESS_OVERRIDE_SCORE` to 100. |
| `jacked/api/usage_monitor.py` | Background loops. Update all callers to pass active hours + account. Simplify proactive scheduler. |
| `tests/unit/test_auto_swap.py` | New tests for deficit bonus, urgency filter, ping-pong prevention, end-to-end original bug. |

---

### Task 1: Add logger to `auto_swap.py`

**Files:**
- Modify: `jacked/web/auto_swap.py:1-7`

- [ ] **Step 1: Add logger import**

At the top of `jacked/web/auto_swap.py`, after the existing imports (line 8, after `from datetime import datetime, timezone`), add:

```python
import logging

logger = logging.getLogger(__name__)
```

- [ ] **Step 2: Raise SUPPRESS_OVERRIDE_SCORE**

Change `SUPPRESS_OVERRIDE_SCORE = 80` to `SUPPRESS_OVERRIDE_SCORE = 100`. The deficit bonus adds up to ~25 points for realistic scenarios, inflating scores past the old threshold.

- [ ] **Step 3: Add URGENCY_HOURS constant**

After the `PROACTIVE_SWAP_THRESHOLD` constant, add:

```python
URGENCY_HOURS = 24.0  # accounts behind schedule with fewer effective hours remaining
                       # than this pass through the 7d filter for scoring
```

- [ ] **Step 4: Commit**

```bash
git add jacked/web/auto_swap.py
git commit -m "feat: add logger, URGENCY_HOURS constant, raise escape hatch threshold"
```

---

### Task 2: Add 7d deficit bonus to `score_candidate`

**Files:**
- Modify: `jacked/web/auto_swap.py:293-364`
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write the failing tests for deficit bonus**

Add to `tests/unit/test_auto_swap.py` after the `TestScoreResetBonus` class:

```python
# ---------------------------------------------------------------------------
# score_candidate — 7d deficit bonus
# ---------------------------------------------------------------------------

class TestScoreDeficitBonus:
    def test_behind_schedule_account_scores_higher(self):
        """Account behind on 7d schedule should score higher than one ahead."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # Account behind schedule: 20% usage, resets in 3 days (~57% through window)
        # Deficit: ~57% expected - 20% actual = ~37%
        behind = _acct(1, usage_5h=30, usage_7d=20)
        behind["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).isoformat()
        behind["usage_cached_at"] = int(_time.time()) - 60

        # Account ahead of schedule: 80% usage, resets in 5 days (~29% through)
        # Deficit: ~29% expected - 80% actual = ~-51% (negative, ahead)
        ahead = _acct(2, usage_5h=30, usage_7d=80)
        ahead["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=5)
        ).isoformat()
        ahead["usage_cached_at"] = int(_time.time()) - 60

        score_behind = score_candidate(behind, active_start="07:00", active_end="22:00")
        score_ahead = score_candidate(ahead, active_start="07:00", active_end="22:00")
        assert score_behind > score_ahead

    def test_deficit_bonus_is_proportional(self):
        """Larger deficit should give a larger bonus."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # Account A: 10% 7d, resets in 1 day (~86% through window)
        # Deficit: ~86% expected - 10% actual = ~76% -> bonus ~38
        a = _acct(1, usage_5h=20, usage_7d=10)
        a["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=1)
        ).isoformat()
        a["usage_cached_at"] = int(_time.time()) - 60

        # Account B: 10% 7d, resets in 5 days (~29% through window)
        # Deficit: ~29% expected - 10% actual = ~19% -> bonus ~9.5
        b = _acct(2, usage_5h=20, usage_7d=10)
        b["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=5)
        ).isoformat()
        b["usage_cached_at"] = int(_time.time()) - 60

        score_a = score_candidate(a, active_start="07:00", active_end="22:00")
        score_b = score_candidate(b, active_start="07:00", active_end="22:00")
        assert score_a > score_b

    def test_no_bonus_when_ahead_of_schedule(self):
        """Account ahead of schedule gets no deficit bonus (deficit <= 0)."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # 80% usage, 29% through window -> deficit = -51%
        acct = _acct(1, usage_5h=20, usage_7d=80)
        acct["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=5)
        ).isoformat()
        acct["usage_cached_at"] = int(_time.time()) - 60

        # Same but no 7d data -> no deficit calculation
        no_7d = _acct(2, usage_5h=20, usage_7d=80)
        no_7d["cached_7d_resets_at"] = None
        no_7d["usage_cached_at"] = int(_time.time()) - 60

        score_with = score_candidate(acct, active_start="07:00", active_end="22:00")
        score_without = score_candidate(no_7d, active_start="07:00", active_end="22:00")
        # Ahead of schedule = no bonus. Small diff from 7d time-weighted penalty OK.
        assert abs(score_with - score_without) < 5

    def test_default_active_hours_backward_compatible(self):
        """Calling score_candidate without active hours still works."""
        acct = _acct(1, usage_5h=20, usage_7d=10)
        score = score_candidate(acct)
        assert score > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestScoreDeficitBonus -v`
Expected: FAIL — `score_candidate()` does not accept `active_start`/`active_end` kwargs

- [ ] **Step 3: Implement deficit bonus in `score_candidate`**

In `jacked/web/auto_swap.py`, change the `score_candidate` function signature and add the deficit bonus at the end. The signature becomes:

```python
def score_candidate(
    account: dict,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> float:
    """Score an account for swap-to suitability. Higher is better.

    Considers:
    - 5h utilization (most weight)
    - 7d utilization weighted by remaining days in window
    - Tier-aware headroom (room before hitting tier's critical threshold)
    - Inactive 5h window bonus (encourages opening them)
    - 7d deficit bonus (behind-schedule accounts get priority)
    """
```

Keep the entire existing function body unchanged. After the `if _is_stale: score -= 10` block, before `return score`, add:

```python
    # 7d deficit bonus: accounts behind schedule on 7d utilization
    # get a bonus proportional to their deficit.
    # 0.5 weight: 30% deficit = +15 points, keeping it moderate.
    deficit_result = compute_7d_deficit(account, active_start, active_end)
    if deficit_result and deficit_result["deficit"] > 0:
        bonus = deficit_result["deficit"] * 0.5
        score += bonus
        logger.debug(
            "score_candidate: account %s deficit bonus +%.1f (deficit=%.1f%%)",
            account.get("id", "?"), bonus, deficit_result["deficit"],
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: add 7d deficit bonus to score_candidate"
```

---

### Task 3: Relax 7d filter in `pick_best_target` using urgency criterion

**Files:**
- Modify: `jacked/web/auto_swap.py:370-397`
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write the failing tests for urgency-based filter relaxation**

Add to `tests/unit/test_auto_swap.py` after the `TestPickTargetResetRelax` class:

```python
# ---------------------------------------------------------------------------
# pick_best_target — urgency-based 7d filter relaxation
# ---------------------------------------------------------------------------

class TestPickTargetUrgencyRelax:
    def test_behind_schedule_near_expiry_not_filtered(self):
        """Account at 85% 7d, behind schedule, <24h remaining -> passes filter."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90),  # current
            _acct(2, usage_5h=10, usage_7d=85),  # high 7d, expiring
        ]
        # 12 hours until reset, ~93% through window, deficit ~8%
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is not None
        assert result["id"] == 2

    def test_behind_schedule_far_from_expiry_still_filtered(self):
        """Account at 86% 7d, behind schedule, but 3 days left -> still filtered."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90),  # current
            _acct(2, usage_5h=10, usage_7d=86),  # 86% usage
        ]
        # Resets in 3 days (~57% through window). Deficit = ~57% - 86% = -29%
        # Deficit is negative (ahead of schedule). Even if it were positive,
        # 3 days = ~45 working hours >> 24h urgency threshold.
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=3)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is None

    def test_ahead_of_schedule_near_expiry_still_filtered(self):
        """Account at 95% 7d, AHEAD of schedule, <24h remaining -> filtered."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90),  # current
            _acct(2, usage_5h=10, usage_7d=95),
        ]
        # 12 hours until reset, ~93% through window, deficit = 93% - 95% = -2%
        # Negative deficit = ahead of schedule. No urgency relaxation.
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is None

    def test_end_to_end_original_bug_scenario(self):
        """THE original bug: Account 3 at 85% 7d with 12h left should beat Account 2 at 18% 7d."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        accounts = [
            _acct(1, usage_5h=90, usage_7d=85),  # current (active, triggers swap)
            _acct(2, usage_5h=74, usage_7d=18),   # low 7d, 6.8 days left
            _acct(3, usage_5h=5, usage_7d=85),     # high 7d, 0.5 days left
        ]
        # Account 2: resets in 6.8 days, ahead of schedule
        accounts[1]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=6, hours=19)
        ).isoformat()
        accounts[1]["usage_cached_at"] = int(_time.time()) - 60

        # Account 3: resets in 12 hours, behind schedule
        accounts[2]["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        accounts[2]["usage_cached_at"] = int(_time.time()) - 60

        result = pick_best_target(
            accounts, current_id=1,
            active_start="07:00", active_end="22:00",
        )
        assert result is not None
        # Account 3 should win: low 5h (5%), urgency-relaxed filter, deficit bonus
        assert result["id"] == 3

    def test_default_active_hours_backward_compatible(self):
        """Calling pick_best_target without active hours still works."""
        accounts = [_acct(1, usage_5h=90), _acct(2, usage_5h=10)]
        result = pick_best_target(accounts, current_id=1)
        assert result is not None
        assert result["id"] == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestPickTargetUrgencyRelax -v`
Expected: FAIL — `pick_best_target()` does not accept `active_start`/`active_end` kwargs, and the first test fails because account 2 is filtered by the 7d threshold.

- [ ] **Step 3: Implement urgency-based filter relaxation in `pick_best_target`**

In `jacked/web/auto_swap.py`, replace the `pick_best_target` function:

```python
def pick_best_target(
    accounts: list[dict],
    current_id: int,
    threshold_7d: float = 85,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> dict | None:
    """Return the best swap-target account, or None if nothing qualifies."""

    def _passes_7d_filter(a: dict) -> bool:
        """Check if account passes the 7d usage filter.

        Passes if: usage below threshold, OR 7d resets within
        RESET_SUPPRESS_MINUTES, OR account has expiring capacity
        (behind schedule with limited time remaining).
        """
        usage_7d = a.get("cached_usage_7d") or 0
        if usage_7d < threshold_7d:
            return True
        if _resets_within(a.get("cached_7d_resets_at"), RESET_SUPPRESS_MINUTES):
            return True
        # Urgency relaxation: behind schedule + limited time = expiring capacity
        deficit_result = compute_7d_deficit(a, active_start, active_end)
        if deficit_result:
            has_deficit = deficit_result["deficit"] > 0
            urgent = deficit_result["effective_hours_remaining"] < URGENCY_HOURS
            if has_deficit and urgent:
                logger.debug(
                    "pick_best_target: account %s passes 7d filter via urgency "
                    "(deficit=%.1f%%, hours_remaining=%.1f)",
                    a.get("id", "?"), deficit_result["deficit"],
                    deficit_result["effective_hours_remaining"],
                )
                return True
        return False

    candidates = [
        a for a in accounts
        if a["id"] != current_id
        and a.get("is_active") != 0
        and a.get("is_deleted") != 1
        and (a.get("consecutive_failures") or 0) < 3
        and a.get("validation_status") != "invalid"
        and a.get("cc_access_token") is not None
        and a.get("auto_swap_enabled") != 0
        and _passes_7d_filter(a)
    ]

    if not candidates:
        return None

    candidates.sort(key=lambda a: (-score_candidate(a, active_start, active_end), a.get("priority", 0)))

    if logger.isEnabledFor(logging.DEBUG):
        for c in candidates[:3]:
            logger.debug(
                "pick_best_target: candidate %s (%s) score=%.1f",
                c.get("id", "?"), c.get("email", "?"),
                score_candidate(c, active_start, active_end),
            )

    return candidates[0]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: urgency-based 7d filter relaxation in pick_best_target"
```

---

### Task 4: Suppress 7d defensive trigger on deficit accounts (anti-ping-pong)

**Files:**
- Modify: `jacked/web/auto_swap.py:215-271`
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write the failing test for ping-pong prevention**

Add to `tests/unit/test_auto_swap.py` after the `TestShouldSwapWindowAware` class:

```python
# ---------------------------------------------------------------------------
# should_swap — deficit-aware 7d suppression (anti-ping-pong)
# ---------------------------------------------------------------------------

class TestShouldSwapDeficitAware:
    def test_suppress_7d_trigger_when_account_has_deficit(self):
        """Account at 85% 7d but with positive deficit -> DON'T swap away."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # Account behind schedule: 85% usage, 93% through window, deficit ~8%
        acct = _acct(1, usage_5h=50, usage_7d=85)
        acct["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        acct["usage_cached_at"] = int(_time.time()) - 60

        assert should_swap(
            usage_5h=50, usage_7d=85,
            account=acct,
            active_start="07:00", active_end="22:00",
        ) is False

    def test_fire_7d_trigger_when_account_ahead_of_schedule(self):
        """Account at 90% 7d, ahead of schedule -> swap away (normal behavior)."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        # Ahead of schedule: 90% usage, 29% through window, deficit = -61%
        acct = _acct(1, usage_5h=50, usage_7d=90)
        acct["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(days=5)
        ).isoformat()
        acct["usage_cached_at"] = int(_time.time()) - 60

        assert should_swap(
            usage_5h=50, usage_7d=90,
            account=acct,
            active_start="07:00", active_end="22:00",
        ) is True

    def test_fire_7d_trigger_when_no_account_provided(self):
        """No account dict = backward compat, fire 7d trigger normally."""
        assert should_swap(usage_5h=50, usage_7d=90) is True

    def test_5h_critical_still_fires_regardless_of_deficit(self):
        """5h critical trigger fires even if account has deficit (5h is immediate risk)."""
        from datetime import datetime, timezone, timedelta
        import time as _time

        acct = _acct(1, usage_5h=95, usage_7d=85)
        acct["cached_7d_resets_at"] = (
            datetime.now(timezone.utc) + timedelta(hours=12)
        ).isoformat()
        acct["usage_cached_at"] = int(_time.time()) - 60

        assert should_swap(
            usage_5h=95, usage_7d=85,
            account=acct,
            active_start="07:00", active_end="22:00",
        ) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestShouldSwapDeficitAware -v`
Expected: FAIL — `should_swap()` does not accept `account`/`active_start`/`active_end` kwargs

- [ ] **Step 3: Implement deficit-aware 7d suppression in `should_swap`**

In `jacked/web/auto_swap.py`, update the `should_swap` signature and 7d trigger block:

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
    account: dict | None = None,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> bool:
    """Decide whether the current account should be swapped out.

    Returns False when *usage_5h* is None (no data yet — never swap on
    missing data).

    Suppresses all three swap triggers when the relevant window resets
    within ``RESET_SUPPRESS_MINUTES``.  Also suppresses when usage data
    is stale (predates a reset that already happened).

    Suppresses the 7d trigger when the account has a positive deficit
    (behind schedule) — we intentionally put the user here to burn
    expiring capacity.
    """
```

Then replace the 7d trigger block:

```python
    # 7-day saturation (unless 7d reset imminent OR we're burning deficit).
    if usage_7d is not None and usage_7d >= threshold_7d and not suppress_7d:
        # If account has a positive deficit, we're here to burn capacity — stay.
        if account is not None:
            deficit_result = compute_7d_deficit(account, active_start, active_end)
            if deficit_result and deficit_result["deficit"] > 0:
                logger.debug(
                    "should_swap: suppressing 7d trigger on deficit account "
                    "(usage_7d=%.1f%%, deficit=%.1f%%)",
                    usage_7d, deficit_result["deficit"],
                )
            else:
                return True
        else:
            return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: suppress 7d defensive trigger on deficit accounts (anti-ping-pong)"
```

---

### Task 5: Update `usage_monitor.py` callers and simplify proactive scheduler

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Add active hours settings and update should_swap caller**

In `jacked/api/usage_monitor.py`, add the active hours settings reads near line 183 (after `check_interval`):

```python
            active_start = _setting_str(db, "window_keeper_active_start", "07:00")
            active_end = _setting_str(db, "window_keeper_active_end", "22:00")
```

Update the `should_swap` call (around line 290) to pass the account and active hours:

```python
            want_swap = should_swap(
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
                account=active_acct,
                active_start=active_start,
                active_end=active_end,
            )
```

- [ ] **Step 2: Update pick_best_target and score_candidate callers**

Update the `pick_best_target` call (around line 331):

```python
                target = pick_best_target(
                    accounts, current_id=active_acct_id,
                    threshold_7d=threshold_7d,
                    active_start=active_start,
                    active_end=active_end,
                )
```

Update the escape hatch `score_candidate` call (around line 338):

```python
                    target_score = score_candidate(target, active_start, active_end)
```

Update the escape hatch reason string `score_candidate` call (around line 377):

```python
                        reason = (
                            f"escape hatch: suppressed swap overridden — "
                            f"target scores {score_candidate(target, active_start, active_end):.0f}"
                        )
```

- [ ] **Step 3: Replace the proactive scheduler with unified path**

Replace the entire proactive scheduler block (lines ~495-571) with the simplified version:

```python
            # -- Proactive 7d capacity scheduler ---------------------------
            if not want_swap and not escape_override:
                from jacked.web.auto_swap import compute_7d_deficit, PROACTIVE_SWAP_THRESHOLD

                if usage_5h is not None and usage_5h < warning_5h:
                    # Fetch fresh candidate data for proactive evaluation
                    accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)

                    target = pick_best_target(
                        accounts, current_id=active_acct_id,
                        threshold_7d=threshold_7d,
                        active_start=active_start,
                        active_end=active_end,
                    )

                    if target and (time.time() - _last_swap_time) >= _SWAP_COOLDOWN_SECONDS:
                        deficit_result = compute_7d_deficit(target, active_start, active_end)
                        if deficit_result and deficit_result["deficit"] > PROACTIVE_SWAP_THRESHOLD:
                            # Fetch one more time for the specific target
                            await fetch_usage(target["id"], db)
                            target = db.get_account(target["id"])

                            if target:
                                reason = (
                                    f"proactive: burning {deficit_result['unused_7d']:.0f}% "
                                    f"unused 7d on {target.get('email', '?')} — "
                                    f"{deficit_result['effective_hours_remaining']:.0f} "
                                    f"effective hours left "
                                    f"({deficit_result['effective_windows_remaining']:.1f} windows), "
                                    f"score={score_candidate(target, active_start, active_end):.0f}"
                                )
                                logger.info(
                                    "Proactive swap: account %d is %.0f%% behind 7d schedule "
                                    "(score=%.0f)",
                                    target["id"], deficit_result["deficit"],
                                    score_candidate(target, active_start, active_end),
                                )

                                from jacked.api.credential_helpers import (
                                    reconcile_outgoing_credentials,
                                    sync_credential_to_all_stores,
                                )
                                reconcile_outgoing_credentials(active_acct_id, db)

                                _last_swap_time = time.time()
                                db.record_swap(
                                    from_account_id=active_acct_id,
                                    to_account_id=target["id"],
                                    reason=reason,
                                    trigger="proactive_7d",
                                    from_5h=usage_5h,
                                    from_7d=usage_7d,
                                    to_5h=target.get("cached_usage_5h"),
                                    to_7d=target.get("cached_usage_7d"),
                                )
                                sync_credential_to_all_stores(
                                    target["id"], target,
                                    email=target.get("email"),
                                )

                                _burn_rates.pop(active_acct_id, None)
                                _burn_rate_unchanged_ticks.pop(active_acct_id, None)
                                _burn_rates.pop(target["id"], None)
                                _burn_rate_unchanged_ticks.pop(target["id"], None)

                                ws_registry = getattr(app.state, "ws_registry", None)
                                if ws_registry:
                                    await ws_registry.broadcast(
                                        "auto_swap_triggered",
                                        {
                                            "from_account_id": active_acct_id,
                                            "to_account_id": target["id"],
                                            "to_email": target.get("email", ""),
                                            "reason": reason,
                                        },
                                    )
```

- [ ] **Step 4: Run all tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: ALL PASS

Run: `uv run python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: unify defensive and proactive swap paths, add active hours to all callers"
```

---

### Task 6: Update architecture doc and spec

**Files:**
- Modify: `docs/architecture/auto-swap-system.md`

- [ ] **Step 1: Update the Scoring Model section**

Update the scoring model to include the deficit bonus:

```
score = 100
score -= cached_usage_5h                          # lower 5h = better
score -= cached_usage_7d × time_weight            # 7d weighted by days remaining
score += tier_headroom × 0.3                      # room before tier limit
score += 15 if 5h window inactive/expired         # encourage opening windows
score += reset_proximity_bonus (up to +30)        # imminent 5h reset
score += 7d_deficit × 0.5 (if deficit > 0)        # behind-schedule accounts
score -= 10 if data is stale (>30 min)            # don't trust old data
```

- [ ] **Step 2: Update the Decision Flow**

Update step 4 to mention urgency-based filter relaxation and deficit-aware 7d suppression:

```
4. Score ALL candidate accounts (unified scoring):
   a. Filter: usage below 7d threshold, OR imminent 7d reset,
      OR urgency (deficit > 0 AND < 24 working hours remaining)
   b-g. [scoring factors as before]
5. Compare best candidate against staying:
   a. If defensive trigger fired → swap to best candidate
      (7d trigger suppressed if current account has deficit — anti-ping-pong)
   b. If no defensive trigger but best candidate has high 7d deficit
      AND active account is comfortable → proactive swap
   c. Otherwise → stay
```

- [ ] **Step 3: Update Swap Triggers section**

Add under Suppression:
```
- **Deficit-aware:** Don't fire 7d defensive trigger on accounts with positive deficit
  (we intentionally placed the user here to burn expiring capacity)
```

- [ ] **Step 4: Commit**

```bash
git add docs/architecture/auto-swap-system.md docs/superpowers/specs/2026-04-03-unified-swap-decision-design.md
git commit -m "docs: update architecture and spec for unified swap decision engine"
```
