# On-Demand Usage Fetch + 7-Day Capacity Scheduler Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop wasting API calls on background usage fetches for non-active accounts. Instead, fetch on-demand at swap time. Add proactive 7-day capacity scheduling that rotates accounts to maximize utilization of expiring 7-day windows.

**Architecture:** Remove the bulk usage fetch from the sweep loop (window-keeper-only). Add `compute_effective_working_hours` and `compute_7d_deficit` pure functions to `auto_swap.py`. Add on-demand candidate fetch + proactive deficit-based swap trigger to the active poll loop. Add staleness penalty to `score_candidate`.

**Tech Stack:** Python 3.12+ (datetime, asyncio)

**Design specs:**
- `docs/superpowers/specs/2026-04-03-on-demand-usage-fetch-design.md`
- `docs/superpowers/specs/2026-04-03-7d-capacity-scheduler-design.md`

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/web/auto_swap.py` | Swap decision engine | Add `compute_effective_working_hours`, `compute_7d_deficit`, staleness penalty in `score_candidate` |
| `jacked/api/usage_monitor.py` | Poll + sweep loops | Remove bulk fetch from sweep. Add on-demand fetch + proactive scheduler to active poll. Add prime-the-pump. |
| `tests/unit/test_auto_swap.py` | Tests | Working hours, deficit calculation, staleness penalty |

---

### Task 1: Add `compute_effective_working_hours` pure function

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

A pure function that counts working hours between two LOCAL datetimes, excluding overnight.

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_auto_swap.py`:

```python
from jacked.web.auto_swap import compute_effective_working_hours


class TestEffectiveWorkingHours:
    def test_same_day_within_active_hours(self):
        """4 PM to 9 PM with active 7:00-22:00 = 5 hours."""
        from datetime import datetime
        start = datetime(2026, 4, 3, 16, 0)  # 4 PM
        end = datetime(2026, 4, 3, 21, 0)    # 9 PM
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 5.0) < 0.01

    def test_overnight_skips_sleep(self):
        """4 PM today to 10 AM tomorrow, active 07:00-22:00.
        Today: 4 PM - 10 PM = 6h. Tomorrow: 7 AM - 10 AM = 3h. Total = 9h."""
        from datetime import datetime
        start = datetime(2026, 4, 3, 16, 0)
        end = datetime(2026, 4, 4, 10, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 9.0) < 0.01

    def test_multiple_days(self):
        """3 full days, active 07:00-22:00 = 15h/day = 45h."""
        from datetime import datetime
        start = datetime(2026, 4, 1, 7, 0)
        end = datetime(2026, 4, 4, 7, 0)  # exactly 3 days later
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 45.0) < 0.01

    def test_start_before_active_hours(self):
        """Start at 5 AM, end at 10 AM. Active 07:00-22:00.
        Only 7 AM - 10 AM = 3h counts."""
        from datetime import datetime
        start = datetime(2026, 4, 3, 5, 0)
        end = datetime(2026, 4, 3, 10, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 3.0) < 0.01

    def test_end_after_active_hours(self):
        """Start at 8 PM, end at 11 PM. Active 07:00-22:00.
        Only 8 PM - 10 PM = 2h counts."""
        from datetime import datetime
        start = datetime(2026, 4, 3, 20, 0)
        end = datetime(2026, 4, 3, 23, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert abs(result - 2.0) < 0.01

    def test_zero_when_entirely_outside_active(self):
        """11 PM to 5 AM — entirely outside active hours."""
        from datetime import datetime
        start = datetime(2026, 4, 3, 23, 0)
        end = datetime(2026, 4, 4, 5, 0)
        result = compute_effective_working_hours(start, end, "07:00", "22:00")
        assert result == 0.0

    def test_start_equals_end(self):
        from datetime import datetime
        start = datetime(2026, 4, 3, 12, 0)
        result = compute_effective_working_hours(start, start, "07:00", "22:00")
        assert result == 0.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestEffectiveWorkingHours -v --tb=short`

- [ ] **Step 3: Implement**

Add to `jacked/web/auto_swap.py`, after the `_resets_within` function and before `should_swap`:

```python
def compute_effective_working_hours(
    start_dt: datetime,
    end_dt: datetime,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> float:
    """Count working hours between two LOCAL datetimes, excluding overnight.

    Only counts hours within [active_start, active_end) each day.
    Both start_dt and end_dt must be in local time (caller converts from UTC).

    >>> from datetime import datetime
    >>> compute_effective_working_hours(
    ...     datetime(2026, 1, 1, 16, 0), datetime(2026, 1, 1, 21, 0),
    ...     "07:00", "22:00")
    5.0
    """
    if end_dt <= start_dt:
        return 0.0

    s_h, s_m = map(int, active_start.split(":"))
    e_h, e_m = map(int, active_end.split(":"))
    active_start_mins = s_h * 60 + s_m
    active_end_mins = e_h * 60 + e_m
    active_hours_per_day = (active_end_mins - active_start_mins) / 60.0

    if active_hours_per_day <= 0:
        return 0.0

    from datetime import timedelta

    total = 0.0
    current_day = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)

    while current_day < end_day:
        day_active_start = current_day.replace(hour=s_h, minute=s_m)
        day_active_end = current_day.replace(hour=e_h, minute=e_m)

        # Clamp to the overall range
        effective_start = max(start_dt, day_active_start)
        effective_end = min(end_dt, day_active_end)

        if effective_end > effective_start:
            total += (effective_end - effective_start).total_seconds() / 3600.0

        current_day += timedelta(days=1)

    return total
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestEffectiveWorkingHours -v --tb=short`

- [ ] **Step 5: Run all auto_swap tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v --tb=short`

- [ ] **Step 6: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: compute_effective_working_hours for 7d capacity scheduling"
```

---

### Task 2: Add `compute_7d_deficit` pure function

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write failing tests**

Add to `tests/unit/test_auto_swap.py`:

```python
from jacked.web.auto_swap import compute_7d_deficit


class TestCompute7dDeficit:
    def test_account_behind_schedule(self):
        """Account at 20% usage, 57% through the window = 37% deficit."""
        from datetime import datetime, timedelta
        # 7d window: started 4 days ago, ends in 3 days
        resets_at = (datetime.now() + timedelta(days=3)).isoformat()
        acct = {
            "cached_usage_7d": 20.0,
            "cached_7d_resets_at": resets_at,
            "usage_cached_at": int(time.time()) - 60,
        }
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is not None
        assert result["deficit"] > 25  # should be around 37%

    def test_account_ahead_of_schedule(self):
        """Account at 80% usage, 29% through the window = negative deficit."""
        from datetime import datetime, timedelta
        resets_at = (datetime.now() + timedelta(days=5)).isoformat()
        acct = {
            "cached_usage_7d": 80.0,
            "cached_7d_resets_at": resets_at,
            "usage_cached_at": int(time.time()) - 60,
        }
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is not None
        assert result["deficit"] < 0

    def test_none_when_no_resets_at(self):
        """No 7d reset data -> return None."""
        acct = {"cached_usage_7d": 50.0, "cached_7d_resets_at": None}
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is None

    def test_none_when_no_usage(self):
        """No 7d usage data -> return None."""
        from datetime import datetime, timedelta
        resets_at = (datetime.now() + timedelta(days=3)).isoformat()
        acct = {"cached_usage_7d": None, "cached_7d_resets_at": resets_at}
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is None

    def test_expired_window_returns_none(self):
        """7d window already expired -> None."""
        from datetime import datetime, timedelta
        resets_at = (datetime.now() - timedelta(days=1)).isoformat()
        acct = {"cached_usage_7d": 50.0, "cached_7d_resets_at": resets_at}
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is None

    def test_includes_effective_hours_and_windows(self):
        """Result includes effective_hours_remaining and effective_windows_remaining."""
        from datetime import datetime, timedelta
        resets_at = (datetime.now() + timedelta(days=2)).isoformat()
        acct = {
            "cached_usage_7d": 30.0,
            "cached_7d_resets_at": resets_at,
            "usage_cached_at": int(time.time()) - 60,
        }
        result = compute_7d_deficit(acct, "07:00", "22:00")
        assert result is not None
        assert "effective_hours_remaining" in result
        assert "effective_windows_remaining" in result
        assert "unused_7d" in result
        assert result["effective_hours_remaining"] > 0
        assert result["unused_7d"] == 70.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestCompute7dDeficit -v --tb=short`

- [ ] **Step 3: Implement**

Add to `jacked/web/auto_swap.py`, after `compute_effective_working_hours`:

```python
# Proactive 7d capacity scheduling constants
PROACTIVE_SWAP_THRESHOLD = 15.0  # minimum deficit (%) to trigger proactive swap


def compute_7d_deficit(
    account: dict,
    active_start: str = "07:00",
    active_end: str = "22:00",
) -> dict | None:
    """Compute 7-day utilization deficit for an account.

    Returns dict with deficit, effective_hours_remaining,
    effective_windows_remaining, unused_7d. Or None if insufficient data.

    Deficit > 0 means the account is behind schedule (underutilized).
    The caller should prioritize accounts with the highest deficit.

    Both cached_7d_resets_at and time calculations use local time
    (caller ensures UTC conversion before this point if needed;
    for this function, we parse the ISO string and convert).
    """
    resets_at_str = account.get("cached_7d_resets_at")
    usage_7d = account.get("cached_usage_7d")

    if resets_at_str is None or usage_7d is None:
        return None

    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None

    now_utc = datetime.now(timezone.utc)
    if resets_at <= now_utc:
        return None  # window already expired

    # Convert to local time for working-hours calculation
    now_local = datetime.now()
    resets_local = now_local + (resets_at - now_utc)  # offset-naive local
    window_start_local = resets_local - __import__("datetime").timedelta(days=7)

    # Elapsed and total working hours
    elapsed_hours = compute_effective_working_hours(
        window_start_local, now_local, active_start, active_end,
    )
    total_hours = compute_effective_working_hours(
        window_start_local, resets_local, active_start, active_end,
    )

    if total_hours <= 0:
        return None

    elapsed_fraction = min(elapsed_hours / total_hours, 1.0)
    expected_usage = elapsed_fraction * 100.0
    deficit = expected_usage - usage_7d

    # Remaining capacity
    remaining_hours = compute_effective_working_hours(
        now_local, resets_local, active_start, active_end,
    )
    remaining_windows = remaining_hours / 5.0  # each 5h window is a "slot"

    return {
        "deficit": deficit,
        "effective_hours_remaining": remaining_hours,
        "effective_windows_remaining": remaining_windows,
        "unused_7d": 100.0 - usage_7d,
    }
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v --tb=short`

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: compute_7d_deficit for proactive capacity scheduling"
```

---

### Task 3: Add staleness penalty to `score_candidate`

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write failing test**

Add to `tests/unit/test_auto_swap.py`:

```python
class TestScoreStaleness:
    def test_stale_data_reduces_score(self):
        """Account with usage_cached_at > 30 min old should score lower."""
        import time as _time
        fresh = _acct(1, usage_5h=20)
        fresh["usage_cached_at"] = int(_time.time()) - 60  # 1 min ago
        stale = _acct(2, usage_5h=20)
        stale["usage_cached_at"] = int(_time.time()) - 3600  # 1 hour ago
        assert score_candidate(fresh) > score_candidate(stale)

    def test_stale_data_kills_reset_bonus(self):
        """Imminent reset bonus should be 0 when data is stale."""
        import time as _time
        from datetime import datetime, timezone, timedelta
        resets_soon = (datetime.now(timezone.utc) + timedelta(minutes=3)).isoformat()
        acct = _acct(1, usage_5h=80, resets_5h=resets_soon)
        acct["usage_cached_at"] = int(_time.time()) - 3600  # stale
        # The +30 bonus should be killed, score should be close to acct without bonus
        no_reset = _acct(2, usage_5h=80)
        no_reset["usage_cached_at"] = int(_time.time()) - 3600
        assert abs(score_candidate(acct) - score_candidate(no_reset)) < 5
```

- [ ] **Step 2: Implement staleness penalty**

In `score_candidate` in `jacked/web/auto_swap.py`, add at the very end before `return score`:

```python
    # Staleness penalty: reduce score when usage data is old.
    # Prevents stale reset-proximity bonuses from selecting wrong targets.
    _STALENESS_THRESHOLD = 1800  # 30 minutes
    cached_at = account.get("usage_cached_at")
    if cached_at:
        try:
            age = int(time.time()) - int(cached_at)
            if age > _STALENESS_THRESHOLD:
                score -= 10  # flat penalty for stale data
                # Also kill any reset bonus that was added above — the "imminent
                # reset" data is unreliable when the fetch is old.
        except (ValueError, TypeError):
            pass
```

Also, wrap the imminent 5h reset bonus (lines 216-223) in a staleness check. Change:

```python
    if resets_5h:
        try:
            r = datetime.fromisoformat(resets_5h.replace("Z", "+00:00"))
            remaining_min = (r - datetime.now(timezone.utc)).total_seconds() / 60.0
            if 0 < remaining_min <= 15:
                score += 30 * (1 - remaining_min / 15)
        except (ValueError, TypeError):
            pass
```

To:

```python
    # Bonus for imminent 5h reset — only when data is fresh enough to trust.
    _STALENESS_THRESHOLD = 1800
    _is_stale = False
    cached_at = account.get("usage_cached_at")
    if cached_at:
        try:
            _is_stale = (int(time.time()) - int(cached_at)) > _STALENESS_THRESHOLD
        except (ValueError, TypeError):
            pass

    if resets_5h and not _is_stale:
        try:
            r = datetime.fromisoformat(resets_5h.replace("Z", "+00:00"))
            remaining_min = (r - datetime.now(timezone.utc)).total_seconds() / 60.0
            if 0 < remaining_min <= 15:
                score += 30 * (1 - remaining_min / 15)
        except (ValueError, TypeError):
            pass

    # Flat staleness penalty
    if _is_stale:
        score -= 10
```

Move the `_STALENESS_THRESHOLD` and `_is_stale` computation to before the reset bonus block so it's computed once and used for both the bonus gating and the flat penalty.

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v --tb=short`

- [ ] **Step 4: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: staleness penalty in score_candidate for stale usage data"
```

---

### Task 4: Remove bulk usage fetch from sweep loop

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Remove the bulk fetch block**

In `full_sweep_loop`, remove the entire block from the `# -- Fetch usage for ALL non-active accounts` comment (line 510) through the end of the for loop (line 526). Keep the `active_acct_id` and `accounts` reads since window keeper still needs them.

Change lines 510-534 from:

```python
            # -- Fetch usage for ALL non-active accounts -----------------
            active_acct_id = _read_active_account_id()
            accounts = db.list_accounts(include_inactive=False)
            sweep_checked = 0
            sweep_pinged = 0

            for acct in accounts:
                acct_id = acct["id"]
                if acct_id == active_acct_id:
                    continue  # active account handled by poll loop
                result = await fetch_usage(acct_id, db)
                if result and not result.get("_cached"):
                    logger.debug(
                        "Usage fetched for account %d in full sweep", acct_id,
                    )
                await asyncio.sleep(1)  # pacing
                sweep_checked += 1

            # -- Window keeper -------------------------------------------
            wk_start = _setting_str(db, "window_keeper_active_start", "06:00")
            wk_end = _setting_str(db, "window_keeper_active_end", "23:00")
            wk_prewake = _setting_str(db, "window_keeper_prewake", "04:00")

            # Re-read accounts after usage fetch for fresh data
            accounts = db.list_accounts(include_inactive=False)
```

To:

```python
            # -- Window keeper -------------------------------------------
            active_acct_id = _read_active_account_id()
            accounts = db.list_accounts(include_inactive=False)
            sweep_pinged = 0

            wk_start = _setting_str(db, "window_keeper_active_start", "06:00")
            wk_end = _setting_str(db, "window_keeper_active_end", "23:00")
            wk_prewake = _setting_str(db, "window_keeper_prewake", "04:00")
```

Also update the heartbeat log from:
```python
            logger.info(
                "Full sweep complete: checked %d accounts, pinged %d windows",
                sweep_checked, sweep_pinged,
            )
```
To:
```python
            logger.info(
                "Full sweep complete: pinged %d windows",
                sweep_pinged,
            )
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: remove bulk usage fetch from sweep — window keeper only now"
```

---

### Task 5: Add on-demand candidate fetch at swap time + exhaustion path

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Add on-demand fetch helper**

Add a helper function before `active_account_poll_loop`:

```python
async def _fetch_candidate_usage(accounts: list, active_acct_id: int, db) -> list:
    """Fetch fresh usage for all non-active candidate accounts.

    Called on-demand when a swap decision needs current data.
    Returns the refreshed accounts list from DB.
    """
    from jacked.web.auth import fetch_usage

    for acct in accounts:
        if acct["id"] == active_acct_id:
            continue
        await fetch_usage(acct["id"], db)
        await asyncio.sleep(1)  # pacing

    # Re-read with fresh data
    return db.list_accounts(include_inactive=False)
```

- [ ] **Step 2: Call it before pick_best_target**

In the active poll loop, find where `pick_best_target` is called (around line 299). Add the on-demand fetch just before it:

```python
            if want_swap or escape_override:
                # Fetch fresh usage for candidates before scoring
                accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)

                target = pick_best_target(
```

- [ ] **Step 3: Call it in the exhaustion path**

In the exhaustion path (around line 428, the `next_recovery_at` computation), add on-demand fetch before the loop:

```python
                else:
                    # No eligible target — fetch fresh data for recovery estimate
                    accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 5: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: on-demand candidate usage fetch at swap time and exhaustion path"
```

---

### Task 6: Add prime-the-pump initial fetch

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Add module-level flag**

Add near the top of the file with the other module-level state (after `_burn_rate_unchanged_ticks`):

```python
# One-time initial fetch flag — prime the pump when auto-swap first enables
_initial_fetch_done = False
```

- [ ] **Step 2: Add prime-the-pump in active poll loop**

In the active poll loop, after the settings checks and before the fetch_usage call for the active account (around line 188), add:

```python
            # Prime the pump: one-time bulk fetch to establish baseline data
            # for all accounts when auto-swap first enables. Prevents "never
            # fetched" in the UI and blind first swap.
            global _initial_fetch_done
            if not _initial_fetch_done:
                _initial_fetch_done = True
                logger.info("Auto-swap: priming usage data for all accounts")
                all_accounts = db.list_accounts(include_inactive=False)
                for acct in all_accounts:
                    if acct["id"] != active_acct_id:
                        await fetch_usage(acct["id"], db)
                        await asyncio.sleep(1)
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 4: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: prime-the-pump initial fetch on first auto-swap tick"
```

---

### Task 7: Add proactive 7d swap trigger

**Files:**
- Modify: `jacked/api/usage_monitor.py`

This is the core scheduler. After defensive checks (should_swap, escape hatch), check all accounts' 7d deficits and trigger a proactive swap if any is significantly behind schedule.

- [ ] **Step 1: Add proactive check after the defensive/escape hatch block**

In the active poll loop, after the entire `if want_swap or escape_override:` block closes (after the exhaustion broadcast), add:

```python
            # -- Proactive 7d capacity scheduler ---------------------------
            # If no defensive swap triggered, check if any account is behind
            # on its 7d utilization schedule. Swap proactively to maximize
            # total capacity across all accounts.
            if not want_swap and not escape_override:
                from jacked.web.auto_swap import compute_7d_deficit, PROACTIVE_SWAP_THRESHOLD
                wk_start = _setting_str(db, "window_keeper_active_start", "07:00")
                wk_end = _setting_str(db, "window_keeper_active_end", "22:00")

                # Only proactive swap when active account is comfortable
                if usage_5h is not None and usage_5h < warning_5h:
                    best_deficit_acct = None
                    best_deficit = 0.0

                    for acct in accounts:
                        if acct["id"] == active_acct_id:
                            continue
                        result = compute_7d_deficit(acct, wk_start, wk_end)
                        if result and result["deficit"] > best_deficit:
                            best_deficit = result["deficit"]
                            best_deficit_acct = acct
                            best_deficit_result = result

                    if best_deficit_acct and best_deficit > PROACTIVE_SWAP_THRESHOLD:
                        # Check swap cooldown
                        if (time.time() - _last_swap_time) >= _SWAP_COOLDOWN_SECONDS:
                            # Fetch fresh usage for the target before swapping
                            await fetch_usage(best_deficit_acct["id"], db)
                            best_deficit_acct = db.get_account(best_deficit_acct["id"])

                            if best_deficit_acct:
                                reason = (
                                    f"proactive: burning {best_deficit_result['unused_7d']:.0f}% "
                                    f"unused 7d on {best_deficit_acct.get('email', '?')} — "
                                    f"{best_deficit_result['effective_hours_remaining']:.0f} "
                                    f"effective hours left "
                                    f"({best_deficit_result['effective_windows_remaining']:.1f} windows)"
                                )
                                logger.info(
                                    "Proactive swap: account %d is %.0f%% behind 7d schedule",
                                    best_deficit_acct["id"], best_deficit,
                                )

                                # Reconcile outgoing credentials
                                from jacked.api.credential_helpers import (
                                    reconcile_outgoing_credentials,
                                    sync_credential_to_all_stores,
                                )
                                reconcile_outgoing_credentials(active_acct_id, db)

                                _last_swap_time = time.time()
                                db.record_swap(
                                    from_account_id=active_acct_id,
                                    to_account_id=best_deficit_acct["id"],
                                    reason=reason,
                                    trigger="proactive_7d",
                                    from_5h=usage_5h,
                                    from_7d=usage_7d,
                                    to_5h=best_deficit_acct.get("cached_usage_5h"),
                                    to_7d=best_deficit_acct.get("cached_usage_7d"),
                                )
                                sync_credential_to_all_stores(
                                    best_deficit_acct["id"], best_deficit_acct,
                                    email=best_deficit_acct.get("email"),
                                )

                                # Clean up burn rate
                                _burn_rates.pop(active_acct_id, None)
                                _burn_rate_unchanged_ticks.pop(active_acct_id, None)
                                _burn_rates.pop(best_deficit_acct["id"], None)
                                _burn_rate_unchanged_ticks.pop(best_deficit_acct["id"], None)

                                ws_registry = getattr(app.state, "ws_registry", None)
                                if ws_registry:
                                    await ws_registry.broadcast(
                                        "auto_swap_triggered",
                                        {
                                            "from_account_id": active_acct_id,
                                            "to_account_id": best_deficit_acct["id"],
                                            "to_email": best_deficit_acct.get("email", ""),
                                            "reason": reason,
                                        },
                                    )
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: proactive 7d capacity scheduler — deficit-based account rotation"
```

---

### Task 8: Run full test suite and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

Expected: All tests pass.
