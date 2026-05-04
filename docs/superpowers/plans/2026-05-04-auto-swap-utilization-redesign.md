# Auto-Swap Utilization Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `score_candidate` and split-decision proactive/defensive flow with a deadline-aware tier model that drains soonest-expiring 7d windows first, rides out 5h windows to minimize prompt-cache churn, and keeps the algorithm's view of "expected usage" aligned with the UI's white bar.

**Architecture:** Pure functions in `jacked/web/auto_swap.py` provide tier classification, target computation, deficit math, and a tier-strict selection rule. `jacked/api/usage_monitor.py::active_account_poll_loop` calls a single `should_swap_now(active, best, ...)` per tick — no separate proactive scanner. White bar is wall-clock per-account, identical to the UI calc in `jacked/data/web/js/components/usage.js::computeElapsedFraction7d`.

**Tech Stack:** Python 3.12, asyncio, pytest. Run all tests via `uv run python -m pytest` (per project CLAUDE.md). The async loop runs inside FastAPI lifespan.

**Spec:** `docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `jacked/web/auto_swap.py` | Pure decision-engine functions: tier_for, white_bar, target_7d, deficit_vs_target, pick_best_target, should_swap_now. Burn-rate/headroom helpers retained. |
| `jacked/api/usage_monitor.py` | Active-account poll loop; calls `pick_best_target` + `should_swap_now` once per tick; records decision; executes swap. |
| `tests/unit/test_auto_swap.py` | Unit tests for pure functions (scenarios A–H from spec). |
| `tests/unit/test_usage_monitor.py` | Integration tests for the loop's stay/swap behavior. |
| `docs/superpowers/specs/2026-04-03-7d-capacity-scheduler-design.md` | Add header note: superseded by 2026-05-04 spec. |

---

## Task 1: Tier classification (`tier_for`)

**Files:**
- Modify: `jacked/web/auto_swap.py` (add new function near top, after imports)
- Modify: `tests/unit/test_auto_swap.py` (add new test class)

- [ ] **Step 1.1: Write failing tests**

Append at the bottom of `tests/unit/test_auto_swap.py`:

```python
# ---------------------------------------------------------------------------
# tier_for — deadline tier classification (T0-T3, 4=excluded)
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone


def _iso(dt: datetime) -> str:
    """Format datetime as ISO with Z suffix (matches Anthropic API)."""
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


class TestTierFor:
    def test_t0_under_24h(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=12)))
        assert tier_for(acct, now=now) == 0

    def test_t1_24_to_48h(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=36)))
        assert tier_for(acct, now=now) == 1

    def test_t2_48h_to_4d(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=3)))
        assert tier_for(acct, now=now) == 2

    def test_t3_4d_to_7d(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=6)))
        assert tier_for(acct, now=now) == 3

    def test_excluded_when_expired(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now - timedelta(hours=1)))
        assert tier_for(acct, now=now) == 4

    def test_excluded_when_resets_at_missing(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert tier_for(acct, now=now) == 4

    def test_boundary_exactly_24h(self):
        # At exactly 24h, account is in T1 (boundary belongs to higher tier)
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=24)))
        assert tier_for(acct, now=now) == 1

    def test_boundary_exactly_48h(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=48)))
        assert tier_for(acct, now=now) == 2

    def test_boundary_exactly_4d(self):
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=4)))
        assert tier_for(acct, now=now) == 3
```

- [ ] **Step 1.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestTierFor -v`
Expected: 9 errors of `ImportError: cannot import name 'tier_for'`.

- [ ] **Step 1.3: Implement `tier_for`**

In `jacked/web/auto_swap.py`, just below the existing `tier_critical_threshold` function (around line 94), add:

```python
# ---------------------------------------------------------------------------
# Tier classification (deadline-aware)
# ---------------------------------------------------------------------------

# Lower index = higher priority. Tier boundaries belong to the higher-numbered
# tier (e.g., exactly 24h → T1, not T0). T4 is the sentinel for "no usable
# 7d data" or "already expired".
TIER_T0 = 0  # < 24h to expiry
TIER_T1 = 1  # 24h - 48h
TIER_T2 = 2  # 48h - 96h (4d)
TIER_T3 = 3  # 96h - 168h (7d)
TIER_EXCLUDED = 4  # no data or already expired


def tier_for(account: dict, now: datetime | None = None) -> int:
    """Classify an account by its 7d expiry deadline.

    Returns 0..3 for T0..T3 or 4 (excluded) when 7d data is missing
    or the window has already expired. Boundaries belong to the
    higher-numbered tier (less urgent).
    """
    resets_at_str = account.get("cached_7d_resets_at")
    if resets_at_str is None:
        return TIER_EXCLUDED
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return TIER_EXCLUDED

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    seconds_left = (resets_at - now).total_seconds()
    if seconds_left <= 0:
        return TIER_EXCLUDED

    hours_left = seconds_left / 3600.0
    if hours_left < 24:
        return TIER_T0
    if hours_left < 48:
        return TIER_T1
    if hours_left < 96:
        return TIER_T2
    return TIER_T3
```

- [ ] **Step 1.4: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestTierFor -v`
Expected: 9 passed.

- [ ] **Step 1.5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat(auto_swap): tier_for — classify accounts by 7d expiry deadline"
```

---

## Task 2: White bar (`white_bar`)

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 2.1: Write failing tests**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestWhiteBar:
    def test_one_day_left(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        # 1 day left in a 7-day window = 6/7 elapsed
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=1)))
        assert abs(white_bar(acct, now=now) - 6 / 7) < 1e-6

    def test_just_started(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=7)))
        assert white_bar(acct, now=now) == 0.0

    def test_about_to_expire(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        # 1 hour left = 167/168 elapsed
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=1)))
        assert abs(white_bar(acct, now=now) - 167 / 168) < 1e-6

    def test_overnight_advances(self):
        # User's spec requirement: wall-clock means white bar advances
        # overnight even if the user is asleep.
        from jacked.web.auto_swap import white_bar
        resets_at = datetime(2026, 5, 8, 0, 0, tzinfo=timezone.utc)
        before = datetime(2026, 5, 7, 22, 0, tzinfo=timezone.utc)  # Mon 22:00
        after = datetime(2026, 5, 8, 6, 0, tzinfo=timezone.utc)   # Tue 06:00
        # Use real account (resets_7d field is in iso format)
        acct = _acct(1, resets_7d=_iso(resets_at))
        wb_before = white_bar(acct, now=before)
        wb_after = white_bar(acct, now=after)
        assert wb_after > wb_before
        # Difference should be 8h / 168h
        assert abs((wb_after - wb_before) - 8 / 168) < 1e-6

    def test_returns_none_when_no_data(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert white_bar(acct, now=now) is None

    def test_clamped_at_one_when_expired(self):
        from jacked.web.auto_swap import white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now - timedelta(hours=1)))
        # Past expiry returns 1.0 (saturated). The selection rule
        # uses tier_for to filter expired accounts; white_bar is
        # informational here.
        assert white_bar(acct, now=now) == 1.0
```

- [ ] **Step 2.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestWhiteBar -v`
Expected: 6 errors of `ImportError: cannot import name 'white_bar'`.

- [ ] **Step 2.3: Implement `white_bar`**

In `jacked/web/auto_swap.py`, immediately after `tier_for`:

```python
def white_bar(account: dict, now: datetime | None = None) -> float | None:
    """Wall-clock elapsed fraction (0.0-1.0) of the 7d window.

    Matches the UI's computeElapsedFraction7d in
    jacked/data/web/js/components/usage.js — same formula:
    (now - (resets_at - 7d)) / 7d. No active-hours adjustment.

    Returns None when 7d data is missing. Clamped to [0, 1].
    """
    resets_at_str = account.get("cached_7d_resets_at")
    if resets_at_str is None:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    window_seconds = 7 * 24 * 3600
    start = resets_at - timedelta(seconds=window_seconds)
    elapsed = (now - start).total_seconds() / window_seconds
    return max(0.0, min(1.0, elapsed))
```

Also add `timedelta` to the existing import line at the top of the file. Find:

```python
from datetime import datetime, timezone
```

Replace with:

```python
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 2.4: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestWhiteBar -v`
Expected: 6 passed.

- [ ] **Step 2.5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat(auto_swap): white_bar — wall-clock 7d progress matching UI"
```

---

## Task 3: Tier targets (`target_7d`)

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 3.1: Write failing tests**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestTarget7d:
    def test_t0_target_is_100(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=12)))
        assert target_7d(acct, now=now) == 100.0

    def test_t1_target_is_90(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=36)))
        assert target_7d(acct, now=now) == 90.0

    def test_t2_target_is_white_bar_plus_5(self):
        from jacked.web.auto_swap import target_7d, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=3)))
        wb = white_bar(acct, now=now) * 100
        assert abs(target_7d(acct, now=now) - (wb + 5.0)) < 1e-6

    def test_t2_target_capped_at_100(self):
        # Edge case: white_bar*100 + 5 could exceed 100 near expiry.
        # Force this with a fictional "T2 with 1h left" — although this
        # would actually be T0; test with a hand-crafted case where
        # (white_bar*100 + 5) > 100 with tier 2 by mocking the tier?
        # Easier: test with T2 just barely (white_bar at ~96%, would
        # give 101 without cap). Use a 7-day window with 4h left =
        # 164/168 = 97.6% white bar → target = 102.6 capped to 100.
        # But that's T0 (under 24h), not T2. Cap is unreachable in
        # T2; assert it nevertheless via direct math at the function:
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        # Construct a T2 account near the T1 boundary (48h+1s left):
        # white_bar = (168 - 48) / 168 = 0.714 → target = 71.4 + 5 = 76.4
        # Cap not exercised here; capping is a defensive guard.
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=48, seconds=1)))
        # Just verify it returns a value <= 100
        result = target_7d(acct, now=now)
        assert result <= 100.0

    def test_t3_target_is_white_bar_exact(self):
        from jacked.web.auto_swap import target_7d, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(days=6)))
        wb = white_bar(acct, now=now) * 100
        assert abs(target_7d(acct, now=now) - wb) < 1e-6

    def test_returns_none_when_no_data(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert target_7d(acct, now=now) is None

    def test_returns_none_when_expired(self):
        from jacked.web.auto_swap import target_7d
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now - timedelta(hours=1)))
        assert target_7d(acct, now=now) is None
```

- [ ] **Step 3.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestTarget7d -v`
Expected: 7 errors `ImportError: cannot import name 'target_7d'`.

- [ ] **Step 3.3: Implement `target_7d`**

In `jacked/web/auto_swap.py`, immediately after `white_bar`:

```python
# Tier targets — see spec 2026-05-04-auto-swap-utilization-redesign-design.md
T1_TARGET = 90.0  # 24-48h: 10% buffer for last-day 5h windows
T2_LEAD = 5.0     # 48h-4d: stay slightly ahead of white bar


def target_7d(account: dict, now: datetime | None = None) -> float | None:
    """Tier-based 7d usage target as a percentage (0-100).

    T0 → 100 (drain). T1 → 90 (buffer). T2 → white_bar*100 + 5 (lead).
    T3 → white_bar*100 (floor). Returns None when 7d data is missing
    or already expired.
    """
    tier = tier_for(account, now=now)
    if tier == TIER_EXCLUDED:
        return None
    if tier == TIER_T0:
        return 100.0
    if tier == TIER_T1:
        return T1_TARGET
    wb = white_bar(account, now=now)
    if wb is None:
        return None
    if tier == TIER_T2:
        return min(100.0, wb * 100.0 + T2_LEAD)
    # TIER_T3
    return wb * 100.0
```

- [ ] **Step 3.4: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestTarget7d -v`
Expected: 7 passed.

- [ ] **Step 3.5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat(auto_swap): target_7d — tier-based usage targets"
```

---

## Task 4: Deficit vs target (`deficit_vs_target`)

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 4.1: Write failing tests**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestDeficitVsTarget:
    def test_t0_at_80_has_20_deficit(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, usage_7d=80, resets_7d=_iso(now + timedelta(hours=12)))
        assert deficit_vs_target(acct, now=now) == 20.0

    def test_t1_at_70_has_20_deficit(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, usage_7d=70, resets_7d=_iso(now + timedelta(hours=36)))
        assert deficit_vs_target(acct, now=now) == 20.0  # 90 - 70

    def test_t2_at_white_bar_minus_3_has_8_deficit(self):
        # T2 target = white_bar + 5. Account at white_bar - 3 → deficit 8.
        from jacked.web.auto_swap import deficit_vs_target, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct_no_usage = _acct(1, resets_7d=_iso(now + timedelta(days=3)))
        wb_pct = white_bar(acct_no_usage, now=now) * 100
        acct = _acct(1, usage_7d=wb_pct - 3, resets_7d=_iso(now + timedelta(days=3)))
        assert abs(deficit_vs_target(acct, now=now) - 8.0) < 1e-6

    def test_t3_at_white_bar_has_zero_deficit(self):
        from jacked.web.auto_swap import deficit_vs_target, white_bar
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct_no_usage = _acct(1, resets_7d=_iso(now + timedelta(days=6)))
        wb_pct = white_bar(acct_no_usage, now=now) * 100
        acct = _acct(1, usage_7d=wb_pct, resets_7d=_iso(now + timedelta(days=6)))
        assert abs(deficit_vs_target(acct, now=now)) < 1e-6

    def test_negative_deficit_when_above_target(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, usage_7d=95, resets_7d=_iso(now + timedelta(hours=36)))
        # T1 target = 90, account at 95 → deficit -5
        assert deficit_vs_target(acct, now=now) == -5.0

    def test_returns_none_when_no_data(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=None)
        assert deficit_vs_target(acct, now=now) is None

    def test_returns_none_when_usage_missing(self):
        from jacked.web.auto_swap import deficit_vs_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=12)))
        acct["cached_usage_7d"] = None
        assert deficit_vs_target(acct, now=now) is None
```

- [ ] **Step 4.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestDeficitVsTarget -v`
Expected: 7 errors of `ImportError: cannot import name 'deficit_vs_target'`.

- [ ] **Step 4.3: Implement `deficit_vs_target`**

In `jacked/web/auto_swap.py`, immediately after `target_7d`:

```python
def deficit_vs_target(account: dict, now: datetime | None = None) -> float | None:
    """Difference between tier target and current 7d usage.

    Positive = behind tier target (eligible for selection).
    Negative = at/above tier target (not a candidate).
    None when 7d data missing, expired, or usage is None.
    """
    target = target_7d(account, now=now)
    if target is None:
        return None
    usage = account.get("cached_usage_7d")
    if usage is None:
        return None
    return target - usage
```

- [ ] **Step 4.4: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestDeficitVsTarget -v`
Expected: 7 passed.

- [ ] **Step 4.5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat(auto_swap): deficit_vs_target — gap to tier target"
```

---

## Task 5: Pick best target (tier-strict selection)

**Files:**
- Modify: `jacked/web/auto_swap.py` (rewrite `pick_best_target`)
- Modify: `tests/unit/test_auto_swap.py` (replace existing pick_best_target tests)

- [ ] **Step 5.1: Write failing tests for the new selection rule**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestPickBestTargetTierStrict:
    """Spec scenarios C11-C16 — the headline behavior change."""

    def test_t0_with_room_beats_t3_with_room(self):
        # Spec scenario C11: T0 at 80%/12h beats T3 at 10%/6d.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, usage_5h=50, usage_7d=50,
                       resets_7d=_iso(now + timedelta(days=2)))
        t0 = _acct(1, usage_5h=10, usage_7d=80,
                   resets_7d=_iso(now + timedelta(hours=12)))
        t3 = _acct(2, usage_5h=10, usage_7d=10,
                   resets_7d=_iso(now + timedelta(days=6)))
        target = pick_best_target([active, t0, t3], current_id=99, now=now)
        assert target is not None
        assert target["id"] == 1  # T0 wins

    def test_two_t0s_earlier_expiry_wins(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_early = _acct(1, usage_7d=50,
                         resets_7d=_iso(now + timedelta(hours=4)))
        t0_late = _acct(2, usage_7d=50,
                        resets_7d=_iso(now + timedelta(hours=20)))
        target = pick_best_target([active, t0_early, t0_late],
                                  current_id=99, now=now)
        assert target["id"] == 1

    def test_two_t0s_same_expiry_larger_deficit_wins(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        resets = _iso(now + timedelta(hours=12))
        small_deficit = _acct(1, usage_7d=90, resets_7d=resets)  # def 10
        big_deficit = _acct(2, usage_7d=50, resets_7d=resets)    # def 50
        target = pick_best_target([active, small_deficit, big_deficit],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_t0_at_target_skipped_in_favor_of_t1(self):
        # Spec scenario C14: T0 at 100% (no deficit) vs T1 at 50%: pick T1.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_done = _acct(1, usage_7d=100,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, t0_done, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_t0_without_5h_headroom_excluded(self):
        # Spec scenario C15: T0 with 5h at 95% and no reset → excluded;
        # next-tier candidate picked.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_no_5h = _acct(1, usage_5h=95, usage_7d=50,
                         resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_5h=10, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, t0_no_5h, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_no_candidate_when_all_at_target(self):
        # Spec scenario C16.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        t0_done = _acct(1, usage_7d=100,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1_done = _acct(2, usage_7d=90,
                        resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, t0_done, t1_done],
                                  current_id=99, now=now)
        assert target is None

    def test_excludes_disabled_account(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        disabled = _acct(1, usage_7d=50, auto_swap=False,
                         resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, disabled, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_excludes_invalid_account(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        invalid = _acct(1, usage_7d=50, valid=False,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, invalid, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_excludes_failures_above_threshold(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        failing = _acct(1, usage_7d=50, failures=5,
                        resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, failing, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2

    def test_excludes_no_token(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=3)))
        no_tok = _acct(1, usage_7d=50, cc_token=False,
                       resets_7d=_iso(now + timedelta(hours=12)))
        t1 = _acct(2, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=36)))
        target = pick_best_target([active, no_tok, t1],
                                  current_id=99, now=now)
        assert target["id"] == 2
```

- [ ] **Step 5.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestPickBestTargetTierStrict -v`
Expected: 10 failures — selection currently uses `score_candidate` weighting, picks wrong account. Some assertions about `target is None` may pass coincidentally; that's OK.

- [ ] **Step 5.3: Rewrite `pick_best_target`**

In `jacked/web/auto_swap.py`, replace the existing `pick_best_target` function (currently around lines 508-570) with:

```python
def pick_best_target(
    accounts: list[dict],
    current_id: int,
    threshold_7d: float = 85,  # kept for signature compat — unused
    active_start: str = "06:00",  # kept for signature compat
    active_end: str = "23:00",
    now: datetime | None = None,
) -> dict | None:
    """Return the best swap-target account, or None if nothing qualifies.

    Selection rule (tier-strict; see spec
    2026-05-04-auto-swap-utilization-redesign-design.md):

    1. Filter out: current account, inactive/deleted, failures>=3,
       invalid, no token, auto_swap_enabled=0, no 5h headroom,
       no viable 7d headroom, deficit_vs_target<=0, tier=excluded.
    2. Sort by (tier_index, cached_7d_resets_at, -deficit_vs_target).
    3. Return the first.
    """
    now = now or datetime.now(timezone.utc)

    def _has_5h_headroom(a: dict) -> bool:
        """5h has room now or resets soon enough to be useful."""
        usage_5h = a.get("cached_usage_5h") or 0
        if usage_5h < 90:
            return True
        # 5h at >=90: only viable if reset is imminent
        return _resets_within(a.get("cached_5h_resets_at"), 30)

    eligible: list[tuple[int, str, float, dict]] = []
    for a in accounts:
        if a["id"] == current_id:
            continue
        if a.get("is_active") == 0 or a.get("is_deleted") == 1:
            continue
        if (a.get("consecutive_failures") or 0) >= 3:
            continue
        if a.get("validation_status") == "invalid":
            continue
        if a.get("cc_access_token") is None:
            continue
        if a.get("auto_swap_enabled") == 0:
            continue

        tier = tier_for(a, now=now)
        if tier == TIER_EXCLUDED:
            continue
        if not has_viable_headroom(a, active_start, active_end):
            continue
        if not _has_5h_headroom(a):
            continue
        deficit = deficit_vs_target(a, now=now)
        if deficit is None or deficit <= 0:
            continue

        eligible.append((tier, a.get("cached_7d_resets_at") or "", -deficit, a))

    if not eligible:
        return None

    eligible.sort(key=lambda t: (t[0], t[1], t[2]))

    if logger.isEnabledFor(logging.DEBUG):
        for tier, resets_at, neg_deficit, cand in eligible[:3]:
            logger.debug(
                "pick_best_target: candidate %s (%s) tier=%d resets=%s deficit=%.1f",
                cand.get("id", "?"), cand.get("email", "?"),
                tier, resets_at, -neg_deficit,
            )

    return eligible[0][3]
```

- [ ] **Step 5.4: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestPickBestTargetTierStrict -v`
Expected: 10 passed.

- [ ] **Step 5.5: Run full test_auto_swap.py — note expected failures**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: many failures from old tests of `score_candidate`, `pick_best_target`, `compute_urgency_threshold`. We will delete those in Task 8. For now, only the new TestPickBestTargetTierStrict class must pass.

- [ ] **Step 5.6: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat(auto_swap): pick_best_target — tier-strict selection rule"
```

---

## Task 6: Departure rule (`should_swap_now`)

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 6.1: Write failing tests**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestShouldSwapNow:
    """Spec scenarios D17-D23 — departure rule."""

    def test_stay_when_no_higher_tier_candidate(self):
        # Active T2 at 50%, no higher-tier candidate: stay.
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        # No "best" candidate at all
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_swap_when_higher_tier_emerged(self):
        # Active T2, best is T1: swap (higher tier emerged).
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=30,
                     resets_7d=_iso(now + timedelta(hours=36)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert "higher tier" in reason.lower() or "tier" in reason.lower()

    def test_stay_when_same_tier_candidate(self):
        # Active T2 with bigger deficit candidate also T2: stay.
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=80,  # active at 80
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        same_tier = _acct(2, usage_5h=10, usage_7d=10,  # same tier, more deficit
                          resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=same_tier, now=now)
        assert reason is None

    def test_swap_when_active_drained(self):
        # Active T0 at 100%: depart (drained).
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=100,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=12)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert "drain" in reason.lower() or "target" in reason.lower()

    def test_swap_when_5h_critical(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert "5h" in reason.lower() or "critical" in reason.lower()

    def test_no_swap_when_5h_critical_but_reset_imminent(self):
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(minutes=8)),  # imminent
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is None  # suppressed (5h reset within 10 min)

    def test_swap_when_5h_imminent_but_higher_tier_emerged(self):
        # Even if 5h reset suppresses critical, a higher-tier candidate
        # still triggers the swap. (T0 emerged.)
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(minutes=8)),
                       resets_7d=_iso(now + timedelta(days=3)))  # T2
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(hours=12)))  # T0
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None

    def test_t3_active_rides_out_5h_window(self):
        # Spec scenario D23: active T3, no higher-tier candidate:
        # stay until 5h resets.
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=10,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=6)))
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_burn_rate_projection_triggers_swap(self):
        from jacked.web.auto_swap import should_swap_now, BurnRate
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=82, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(days=3)))
        # Will hit 90 in ~4 min at 2%/min
        br = BurnRate(rate_5h_per_min=2.0, last_check_5h=82.0,
                      rate_7d_per_min=0.0, last_check_7d=0.0)
        reason = should_swap_now(active=active, best=best, burn_rate=br,
                                 check_interval_min=5, now=now)
        assert reason is not None
        assert "burn" in reason.lower() or "project" in reason.lower()
```

- [ ] **Step 6.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestShouldSwapNow -v`
Expected: 9 errors of `ImportError: cannot import name 'should_swap_now'`.

- [ ] **Step 6.3: Implement `should_swap_now`**

In `jacked/web/auto_swap.py`, immediately after `pick_best_target`:

```python
def should_swap_now(
    active: dict,
    best: dict | None,
    *,
    burn_rate: BurnRate | None = None,
    check_interval_min: float = 5,
    critical_5h: float = 90,
    warning_5h: float = 80,
    now: datetime | None = None,
) -> str | None:
    """Return a reason string if the algorithm should swap off ``active``,
    or None to stay.

    Departure rules (any one triggers swap; see spec
    2026-05-04-auto-swap-utilization-redesign-design.md):

    1. Higher-tier candidate emerged: ``best`` exists with strictly
       lower tier_index than ``active``. (T0 emerged while on T2/T3
       always overrides — even when 5h reset suppresses critical.)
    2. Active drained: usage_7d >= target_7d(active).
    3. Active 5h critical: usage_5h >= critical_5h, AND 5h reset NOT
       imminent (within RESET_SUPPRESS_MINUTES).
    4. Burn-rate projection: usage_5h >= warning_5h AND projected to
       cross critical within 2 * check_interval_min, AND 5h reset
       not imminent.

    Returns None when none fire (stay; ride out the 5h window).
    """
    now = now or datetime.now(timezone.utc)
    usage_5h = active.get("cached_usage_5h") or 0
    usage_7d = active.get("cached_usage_7d") or 0
    active_tier = tier_for(active, now=now)
    suppress_5h = _resets_within(
        active.get("cached_5h_resets_at"), RESET_SUPPRESS_MINUTES,
    )

    # 1. Higher-tier candidate (overrides 5h reset suppression)
    if best is not None:
        best_tier = tier_for(best, now=now)
        if best_tier < active_tier and best_tier != TIER_EXCLUDED:
            tier_names = {0: "T0 (<24h)", 1: "T1 (24-48h)",
                          2: "T2 (48h-4d)", 3: "T3 (4-7d)", 4: "?"}
            return (
                f"higher tier emerged: {tier_names[best_tier]} candidate "
                f"vs active {tier_names[active_tier]}"
            )

    # 2. Active drained vs its tier target
    target = target_7d(active, now=now)
    if target is not None and usage_7d >= target:
        return f"drained: 7d usage {usage_7d:.1f}% >= tier target {target:.1f}%"

    # 3. 5h critical (suppressed if reset imminent)
    if usage_5h >= critical_5h and not suppress_5h:
        return f"5h critical: {usage_5h:.1f}% >= {critical_5h:.0f}%"

    # 4. Burn-rate projection (suppressed if 5h reset imminent)
    if (usage_5h >= warning_5h
            and burn_rate is not None
            and not suppress_5h):
        rate = burn_rate.rate_5h_per_min
        if rate > 0:
            mins_to_critical = max(0, critical_5h - usage_5h) / rate
            if mins_to_critical <= 2 * check_interval_min:
                projected = usage_5h + rate * (2 * check_interval_min)
                return (
                    f"burn-rate projection: {usage_5h:.1f}% -> "
                    f"{projected:.1f}% in {int(2 * check_interval_min)}min"
                )

    return None
```

- [ ] **Step 6.4: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestShouldSwapNow -v`
Expected: 9 passed.

- [ ] **Step 6.5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat(auto_swap): should_swap_now — tier-aware departure rule"
```

---

## Task 7: Burst pattern + emergence integration tests

**Files:**
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 7.1: Write the burst-pattern integration test**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestBurstPattern:
    """Spec scenarios G28-G29 — real-life patterns."""

    def test_burst_drains_t0_then_t1_then_t3(self):
        from jacked.web.auto_swap import pick_best_target
        # Friday afternoon. Three accounts:
        #  A1: T0 (resets in 11h on Saturday morning), at 30%
        #  A2: T1 (resets in 35h Sun morning), at 30%
        #  A3: T3 (resets in 6d), at 30%
        now = datetime(2026, 5, 8, 17, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=2)))  # T2
        a1 = _acct(1, usage_5h=10, usage_7d=30,
                   resets_7d=_iso(now + timedelta(hours=11)))
        a2 = _acct(2, usage_5h=10, usage_7d=30,
                   resets_7d=_iso(now + timedelta(hours=35)))
        a3 = _acct(3, usage_5h=10, usage_7d=30,
                   resets_7d=_iso(now + timedelta(days=6)))

        # Round 1: A1 picked (T0)
        target = pick_best_target([active, a1, a2, a3],
                                  current_id=99, now=now)
        assert target["id"] == 1

        # Simulate A1 hitting 100% — drained.
        a1["cached_usage_7d"] = 100
        target = pick_best_target([active, a1, a2, a3],
                                  current_id=99, now=now)
        assert target["id"] == 2  # A2 (T1) picked next

        # Simulate A2 hitting 90% — drained vs T1 target.
        a2["cached_usage_7d"] = 90
        target = pick_best_target([active, a1, a2, a3],
                                  current_id=99, now=now)
        # A3 is T3 — target = white_bar%. After 4 days elapsed,
        # white_bar ≈ 4/7 * 100 = 57.1%; A3 at 30 has deficit.
        assert target["id"] == 3

    def test_higher_tier_emergence_mid_window(self):
        # Spec scenario G29: active is A2 (T2), A1 just rolled into T0
        # at 50%. Expected: A1 wins.
        from jacked.web.auto_swap import pick_best_target, should_swap_now
        now = datetime(2026, 5, 8, 17, 0, tzinfo=timezone.utc)
        active = _acct(99, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))  # T2
        a1 = _acct(1, usage_5h=10, usage_7d=50,
                   resets_7d=_iso(now + timedelta(hours=20)))   # T0
        target = pick_best_target([active, a1], current_id=99, now=now)
        assert target["id"] == 1
        reason = should_swap_now(active=active, best=target, now=now)
        assert reason is not None
        assert "tier" in reason.lower() or "T0" in reason
```

- [ ] **Step 7.2: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestBurstPattern -v`
Expected: 2 passed (relies on Task 5 + 6 implementations already in place).

- [ ] **Step 7.3: Commit**

```bash
git add tests/unit/test_auto_swap.py
git commit -m "test(auto_swap): burst pattern + tier emergence integration"
```

---

## Task 8: Delete dead code (`score_candidate`, `compute_urgency_threshold`)

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 8.1: Identify all callers**

Run: `grep -rn "score_candidate\|compute_urgency_threshold" jacked tests`
Note the locations. Expected callers in:
- `jacked/web/auto_swap.py` (definitions)
- `jacked/api/usage_monitor.py` (proactive scanner block, ~line 740-940; defensive candidate-summary build, ~line 565-595)
- `tests/unit/test_auto_swap.py`
- `tests/unit/test_usage_monitor.py`

- [ ] **Step 8.2: Delete `score_candidate` from `auto_swap.py`**

In `jacked/web/auto_swap.py`, delete the entire `score_candidate` function (currently around lines 412-501) including its section header comment block.

- [ ] **Step 8.3: Delete `compute_urgency_threshold` from `auto_swap.py`**

In `jacked/web/auto_swap.py`, delete the entire `compute_urgency_threshold` function (currently around lines 213-237). Also delete the now-orphaned constant `URGENCY_HOURS` (line ~174) — the new selection rule does not need it. Keep `PROACTIVE_SWAP_THRESHOLD` and `MIN_PROACTIVE_MINUTES` for now; they may be referenced by usage_monitor temporarily and we'll remove orphans in Task 9.

- [ ] **Step 8.4: Strip dead-code references from existing tests**

In `tests/unit/test_auto_swap.py`:

1. In the top imports block, remove `score_candidate`, `compute_urgency_threshold`, and `should_swap` from `from jacked.web.auto_swap import (...)`. Replace `should_swap` with `should_swap_now` if not already added by Task 6.
2. Delete the entire `class TestScoreCandidate:` block.
3. Delete the entire `class TestScoreStaleness:` block.
4. Delete the entire `class TestScoreResetBonus:` block.
5. Delete the entire `class TestScoreDeficitBonus:` block.
6. Delete the entire `class TestComputeUrgencyThreshold:` block.
7. Delete the entire `class TestShouldSwap:` block (replaced by `TestShouldSwapNow` from Task 6).
8. Delete the entire `class TestShouldSwapWindowAware:` block (suppression logic verified by `TestShouldSwapNow::test_no_swap_when_5h_critical_but_reset_imminent`).
9. Delete the entire `class TestShouldSwapDeficitAware:` block (deficit-aware behavior is implicit in tier targets, verified by Task 6 tests).
10. Delete the entire `class TestPickBestTarget:` block (replaced by `TestPickBestTargetTierStrict` from Task 5).
11. Delete the entire `class TestPickTargetResetRelax:` block.
12. Delete the entire `class TestPickTargetUrgencyRelax:` block.
13. Delete any standalone module-level test functions that call `score_candidate` (they exist around lines 191-216 — search for `def test_` followed by `score_candidate` references and remove them).
14. **Keep:** `TestUpdateBurnRate`, `TestResetsWithin`, `TestEffectiveWorkingHours`, `TestHasViableHeadroom`, `TestFormatAccountLabel` — these test helpers we retained.

After deletion, run: `grep -n "score_candidate\|compute_urgency_threshold" tests/unit/test_auto_swap.py` — should return nothing.

- [ ] **Step 8.5: Run test_auto_swap.py — note remaining failures**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: All TestTierFor/TestWhiteBar/TestTarget7d/TestDeficitVsTarget/TestPickBestTargetTierStrict/TestShouldSwapNow/TestBurstPattern pass. Some legacy tests in TestShouldSwap, TestPickBestTarget, TestComputeSevenDayDeficit may still fail because they rely on old behavior — these are addressed in Tasks 9-10.

- [ ] **Step 8.6: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "refactor(auto_swap): drop score_candidate + compute_urgency_threshold"
```

---

## Task 9: Update `usage_monitor.py` — collapse defensive/proactive into one decision

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 9.1: Read the current loop end-to-end**

Run: `wc -l jacked/api/usage_monitor.py` (sanity check ~1180 lines).

Read carefully: `jacked/api/usage_monitor.py:309-1023` — the `active_account_poll_loop`. Note where the existing flow runs:
- `should_swap` call (~line 511-525)
- "Escape hatch" block (~537-559)
- Defensive swap branch (~561-695)
- Proactive scanner block (~740-944)

These four sections collapse into one new flow.

- [ ] **Step 9.2: Update imports**

In `jacked/api/usage_monitor.py`, find the late-import block inside `active_account_poll_loop` (currently around lines 374-388):

```python
from jacked.web.auto_swap import (
    should_swap,
    pick_best_target,
    update_burn_rate,
    tier_critical_threshold,
    tier_label as _tier_label,
    score_candidate,
    _resets_within,
    format_account_label,
    RESET_SUPPRESS_MINUTES,
    SUPPRESS_OVERRIDE_SCORE,
)
```

Replace with:

```python
from jacked.web.auto_swap import (
    should_swap_now,
    pick_best_target,
    update_burn_rate,
    tier_critical_threshold,
    tier_label as _tier_label,
    tier_for,
    target_7d,
    deficit_vs_target,
    _resets_within,
    format_account_label,
    RESET_SUPPRESS_MINUTES,
)
```

- [ ] **Step 9.3: Replace the defensive + proactive flow with a unified call**

In `jacked/api/usage_monitor.py::active_account_poll_loop`, find the block starting roughly at:

```python
            # -- Should swap? --------------------------------------------
            want_swap = should_swap(...)
```

…and ending after the proactive scanner block (the comment `# Record decision in the log`). Replace **everything from `# -- Should swap? --` through (but NOT including) `# Record decision in the log`** with the following unified flow:

```python
            # -- Tier-aware unified decision ----------------------------
            # Single decision per tick: pick the best candidate across
            # the whole pool, then ask should_swap_now whether to leave
            # the active account. Replaces the prior defensive +
            # proactive split. See spec
            # docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md
            now_utc = datetime.now(timezone.utc)

            # Refresh candidate usage if stale (>10 min)
            accounts = await _fetch_candidate_usage(
                accounts, active_acct_id, db,
            )

            best = pick_best_target(
                accounts, current_id=active_acct_id, now=now_utc,
            )

            reason = should_swap_now(
                active=active_acct,
                best=best,
                burn_rate=br,
                check_interval_min=check_interval / 60,
                critical_5h=effective_critical,
                warning_5h=warning_5h,
                now=now_utc,
            )

            # Build candidate summaries for decision log (regardless of action)
            _candidate_summaries = []
            for cand in accounts:
                if cand["id"] == active_acct_id:
                    continue
                cand_tier = tier_for(cand, now=now_utc)
                cand_target = target_7d(cand, now=now_utc)
                cand_deficit = deficit_vs_target(cand, now=now_utc)
                _candidate_summaries.append({
                    "id": cand["id"],
                    "email": cand.get("email", ""),
                    "label": format_account_label(cand),
                    "5h": cand.get("cached_usage_5h"),
                    "7d": cand.get("cached_usage_7d"),
                    "tier": cand_tier,
                    "target_7d": (
                        round(cand_target, 1)
                        if cand_target is not None else None
                    ),
                    "deficit": (
                        round(cand_deficit, 1)
                        if cand_deficit is not None else None
                    ),
                    "is_best": (best is not None and cand["id"] == best["id"]),
                })

            ws_registry = getattr(app.state, "ws_registry", None)

            if reason is None:
                # Stay
                _decision_action = "stay"
                _decision_reason = (
                    f"on track ({_tier_label(active_acct).strip() or 'no tier'})"
                    if best is None
                    else f"no higher-tier candidate (best is tier {tier_for(best, now=now_utc)})"
                )
            elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                _decision_action = "stay"
                _decision_reason = (
                    f"swap warranted ({reason}) but cooldown active "
                    f"({_SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time):.0f}s remaining)"
                )
                logger.debug("Active poll: %s", _decision_reason)
            elif best is None:
                # should_swap_now flagged a forced-out reason but no
                # eligible target exists. Log warning + broadcast
                # exhausted state.
                _decision_action = "stay"
                _decision_reason = f"swap warranted ({reason}) but no eligible target"

                global _last_exhaustion_warning
                now_ts = time.time()
                if now_ts - _last_exhaustion_warning > _EXHAUSTION_COOLDOWN_SECONDS:
                    logger.warning(
                        "Auto-swap needed but no eligible target "
                        "(active account %d at 5h=%.1f%%)",
                        active_acct_id, usage_5h or 0,
                    )
                    _last_exhaustion_warning = now_ts

                next_recovery_at = None
                for acct in accounts:
                    resets = acct.get("cached_5h_resets_at")
                    if not resets:
                        continue
                    try:
                        r = datetime.fromisoformat(resets.replace("Z", "+00:00"))
                        if r > now_utc and (
                            next_recovery_at is None or r < next_recovery_at
                        ):
                            next_recovery_at = r
                    except (ValueError, TypeError):
                        continue

                if ws_registry:
                    await ws_registry.broadcast(
                        "all_accounts_exhausted",
                        {
                            "active_account_id": active_acct_id,
                            "usage_5h": usage_5h,
                            "usage_7d": usage_7d,
                            "next_recovery_at": (
                                next_recovery_at.isoformat()
                                if next_recovery_at else None
                            ),
                        },
                    )
            else:
                # Execute swap.
                logger.info(
                    "Auto-swap: switching from account %d (5h=%.1f%%) to "
                    "account %d (5h=%.1f%%) — %s",
                    active_acct_id, usage_5h or 0,
                    best["id"], best.get("cached_usage_5h") or 0,
                    reason,
                )
                await _execute_swap(
                    db, active_acct_id, active_acct, best,
                    reason=reason, trigger="tier_aware",
                    usage_5h=usage_5h, usage_7d=usage_7d,
                    active_start=active_start, active_end=active_end,
                    ws_registry=ws_registry,
                )
                _decision_action = "swap"
                _decision_target_id = best["id"]
                _decision_reason = reason
```

Also update the existing initialization block at the top of the loop (replace `_proactive_target_id = None` lines etc. — they are no longer needed):

Find:

```python
            _decision_action = "stay"
            _decision_target_id = None
            _decision_reason = None
            _candidate_summaries = None
            _proactive_target_id = None
            _suppression = None
```

Replace with:

```python
            _decision_action = "stay"
            _decision_target_id = None
            _decision_reason = None
            _candidate_summaries = None
            _suppression = None  # kept for log-schema compat (always None in new flow)
```

And in the final decision-log block (currently around lines 947-996), find:

```python
                    decision_id = db.record_decision(
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger=(
                            ("proactive_7d" if _proactive_target_id else "auto_swap")
                            if _decision_action == "swap"
                            else "tick"
                        ),
                        target_id=_decision_target_id,
                        reason=_decision_reason or "no trigger",
                        detail=_tick_detail,
                    )
```

Replace with:

```python
                    decision_id = db.record_decision(
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger="tier_aware" if _decision_action == "swap" else "tick",
                        target_id=_decision_target_id,
                        reason=_decision_reason or "no trigger",
                        detail=_tick_detail,
                    )
```

Make the same trigger replacement in the WS broadcast block right after.

Also update the `_build_tick_detail` callsite — `_proactive_target_id` is no longer defined. Find:

```python
                    _tick_detail = _build_tick_detail(
                        active_acct=active_acct,
                        usage_5h=usage_5h,
                        usage_7d=usage_7d,
                        want_swap=want_swap,
                        suppression=_suppression,
                        escape_override=escape_override if 'escape_override' in dir() else False,
                        candidates=_candidate_summaries,
                        proactive_target_id=_proactive_target_id,
                        cooldown_active=(time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS,
                        decision=_decision_action,
                    )
```

Replace with:

```python
                    _tick_detail = _build_tick_detail(
                        active_acct=active_acct,
                        usage_5h=usage_5h,
                        usage_7d=usage_7d,
                        want_swap=(_decision_action == "swap"),
                        suppression=_suppression,
                        escape_override=False,
                        candidates=_candidate_summaries,
                        proactive_target_id=None,
                        cooldown_active=(time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS,
                        decision=_decision_action,
                    )
```

(Leave `_build_tick_detail`'s signature unchanged; the function tolerates the now-vestigial `proactive_target_id=None` and `escape_override=False` parameters per its current code.)

- [ ] **Step 9.4: Drop the cooldown intermediate decision-log block**

The existing flow has an inline decision-log block inside the cooldown branch (around lines 605-647 in the current file). Our new flow records cooldown-stay via the unified post-flow block, so the inline block must be removed. Search for `# -- Swap cooldown: prevent ping-ponging` in the new code (it should NOT appear after Step 9.3 — verify it doesn't). If any remnants exist, delete them.

- [ ] **Step 9.5: Run usage_monitor unit tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py -v`
Expected: many failures from old expectations; we update those next task.

- [ ] **Step 9.6: Run usage_monitor under syntax check**

Run: `uv run python -c "import jacked.api.usage_monitor"`
Expected: no errors. Indicates the file at least parses and imports clean.

- [ ] **Step 9.7: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "refactor(usage_monitor): single tier-aware decision per tick"
```

---

## Task 10: Update `test_usage_monitor.py` for new flow

**Files:**
- Modify: `tests/unit/test_usage_monitor.py`

- [ ] **Step 10.1: Inventory current tests**

Run: `grep -n "^class\|^def \|^    def test_" tests/unit/test_usage_monitor.py | head -60`
Note any tests that import `should_swap` (old name), `score_candidate`, or assert `proactive_7d`/`auto_swap` triggers.

- [ ] **Step 10.2: Update imports**

In `tests/unit/test_usage_monitor.py`, find any references to:
- `should_swap` → replace with `should_swap_now`
- `score_candidate` → remove (function deleted)
- `compute_urgency_threshold` → remove

- [ ] **Step 10.3: Update trigger assertions**

For tests that assert specific trigger names in the decision log:
- `"proactive_7d"` and `"auto_swap"` → replace with `"tier_aware"`

For tests that monkey-patch the helper functions (e.g., `monkeypatch.setattr("jacked.api.usage_monitor.should_swap", ...)`), update the target name to `should_swap_now`.

- [ ] **Step 10.4: Add a tier-aware integration test**

Append at the bottom of `tests/unit/test_usage_monitor.py`:

```python
class TestTierAwareDecision:
    """End-to-end: the loop picks T0 over T3 (the headline behavior change)."""

    @pytest.mark.asyncio
    async def test_picks_t0_over_t3(self, monkeypatch, tmp_path):
        # This test wires up real auto_swap pure functions but stubs DB/WS.
        # See existing test patterns for the loop fixture.
        from datetime import datetime, timedelta, timezone
        from jacked.web.auto_swap import pick_best_target, should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

        def _iso(dt):
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        active = {
            "id": 99, "email": "active@test", "is_active": 1, "is_deleted": 0,
            "consecutive_failures": 0, "validation_status": "valid",
            "auto_swap_enabled": 1, "cc_access_token": "tok",
            "cached_usage_5h": 50, "cached_usage_7d": 50,
            "cached_5h_resets_at": _iso(now + timedelta(hours=2)),
            "cached_7d_resets_at": _iso(now + timedelta(days=3)),  # T2
        }
        t0 = {
            **active, "id": 1, "email": "t0@test",
            "cached_usage_5h": 10, "cached_usage_7d": 80,
            "cached_5h_resets_at": _iso(now + timedelta(hours=2)),
            "cached_7d_resets_at": _iso(now + timedelta(hours=12)),  # T0
        }
        t3 = {
            **active, "id": 2, "email": "t3@test",
            "cached_usage_5h": 10, "cached_usage_7d": 10,
            "cached_5h_resets_at": _iso(now + timedelta(hours=2)),
            "cached_7d_resets_at": _iso(now + timedelta(days=6)),  # T3
        }

        target = pick_best_target([active, t0, t3], current_id=99, now=now)
        assert target["id"] == 1

        reason = should_swap_now(active=active, best=target, now=now)
        assert reason is not None
```

- [ ] **Step 10.5: Run usage_monitor tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py -v`
Expected: all pass. If any fail with assertions about the old trigger naming or proactive scanner, update those tests to match the new unified flow.

- [ ] **Step 10.6: Commit**

```bash
git add tests/unit/test_usage_monitor.py
git commit -m "test(usage_monitor): align with tier-aware unified flow"
```

---

## Task 11: Refactor `compute_7d_deficit` to expose tier diagnostics

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Modify: `tests/unit/test_auto_swap.py`

- [ ] **Step 11.1: Write failing tests for new shape**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestCompute7dDeficitNewShape:
    def test_returns_tier_and_target_fields(self):
        from jacked.web.auto_swap import compute_7d_deficit
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, usage_7d=70, resets_7d=_iso(now + timedelta(hours=36)))
        result = compute_7d_deficit(acct, now=now)
        assert result is not None
        assert "tier" in result
        assert result["tier"] == 1
        assert "target_7d" in result
        assert result["target_7d"] == 90.0
        assert "deficit_vs_tier_target" in result
        assert result["deficit_vs_tier_target"] == 20.0
        assert "white_bar" in result
        assert "hours_to_expiry" in result
        # Backwards-compat aliases retained:
        assert "deficit" in result  # old field — equals deficit_vs_white_bar
        assert "unused_7d" in result
```

- [ ] **Step 11.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestCompute7dDeficitNewShape -v`
Expected: failures (missing keys).

- [ ] **Step 11.3: Refactor `compute_7d_deficit`**

In `jacked/web/auto_swap.py`, replace the body of `compute_7d_deficit` with:

```python
def compute_7d_deficit(
    account: dict,
    active_start: str = "06:00",
    active_end: str = "23:00",
    now: datetime | None = None,
) -> dict | None:
    """Diagnostic dict for 7d utilization status of an account.

    Returns dict with: tier, target_7d, deficit_vs_tier_target,
    white_bar, hours_to_expiry, unused_7d, plus legacy fields
    (deficit, effective_hours_remaining, effective_windows_remaining)
    for callers that haven't migrated yet.

    None when 7d data missing or window expired.
    """
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    tier = tier_for(account, now=now)
    if tier == TIER_EXCLUDED:
        return None

    resets_at_str = account.get("cached_7d_resets_at")
    usage_7d = account.get("cached_usage_7d")
    if resets_at_str is None or usage_7d is None:
        return None
    try:
        resets_at = datetime.fromisoformat(resets_at_str.replace("Z", "+00:00"))
        if resets_at.tzinfo is None:
            resets_at = resets_at.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None

    hours_to_expiry = (resets_at - now).total_seconds() / 3600.0
    wb = white_bar(account, now=now)  # 0..1
    target = target_7d(account, now=now)
    deficit_vs_target_val = (target - usage_7d) if target is not None else 0.0
    deficit_vs_white_bar = (wb * 100.0 - usage_7d) if wb is not None else 0.0

    # Legacy (effective working hours) — kept for analytics/backcompat.
    from datetime import timedelta as _td
    now_local = datetime.now()
    now_utc_naive = now.replace(tzinfo=None)
    utc_offset_seconds = (now_utc_naive - now_local).total_seconds()
    resets_local = resets_at.replace(tzinfo=None) - _td(seconds=utc_offset_seconds)
    remaining_hours = compute_effective_working_hours(
        now_local, resets_local, active_start, active_end,
    )
    remaining_windows = remaining_hours / 5.0

    return {
        "tier": tier,
        "target_7d": target,
        "deficit_vs_tier_target": deficit_vs_target_val,
        "white_bar": wb,
        "hours_to_expiry": hours_to_expiry,
        "unused_7d": 100.0 - usage_7d,
        # Legacy fields (callers in flight migration)
        "deficit": deficit_vs_white_bar,
        "effective_hours_remaining": remaining_hours,
        "effective_windows_remaining": remaining_windows,
    }
```

- [ ] **Step 11.4: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestCompute7dDeficitNewShape -v`
Expected: pass.

- [ ] **Step 11.5: Audit and update `TestCompute7dDeficit`**

Open `tests/unit/test_auto_swap.py` and find `class TestCompute7dDeficit:`. The legacy tests reference `result["deficit"]` (which still works — we kept the alias as `deficit_vs_white_bar`), `result["effective_hours_remaining"]`, `result["effective_windows_remaining"]`, `result["unused_7d"]`. These all still exist in the new shape, so the legacy tests should still pass.

If any specific test expects a particular deficit value (e.g., "deficit equals positive number when behind schedule"), check the math — the legacy `deficit` field is now `deficit_vs_white_bar = white_bar*100 - usage`. This matches the old definition (where the spec for the old function said `expected_usage = elapsed_fraction * 100; deficit = expected_usage - actual_usage`), so values are unchanged.

- [ ] **Step 11.6: Run full auto_swap test file**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: all green.

- [ ] **Step 11.7: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "refactor(auto_swap): compute_7d_deficit exposes tier diagnostics"
```

---

## Task 12: Final integration sweep + spec note

**Files:**
- Modify: `docs/superpowers/specs/2026-04-03-7d-capacity-scheduler-design.md`
- Verify: every test passes

- [ ] **Step 12.1: Run the full test suite**

Run: `uv run python -m pytest -v`
Expected: 100% pass. Investigate any remaining failures and fix at the source.

- [ ] **Step 12.2: Annotate the superseded spec**

Edit `docs/superpowers/specs/2026-04-03-7d-capacity-scheduler-design.md`. Find:

```markdown
**Date:** 2026-04-03
**Status:** Approved (revised after DCR)
```

Replace with:

```markdown
**Date:** 2026-04-03
**Status:** SUPERSEDED — decisioning portion replaced by `2026-05-04-auto-swap-utilization-redesign-design.md`
```

- [ ] **Step 12.3: Run smoke import**

Run: `uv run python -c "from jacked.api import usage_monitor; from jacked.web import auto_swap; print('OK')"`
Expected: `OK`.

- [ ] **Step 12.4: Verify `score_candidate` and `compute_urgency_threshold` are gone**

Run: `grep -rn "score_candidate\|compute_urgency_threshold" jacked tests`
Expected: no matches in `jacked/` or `tests/`. Matches in `docs/` are fine (historical).

- [ ] **Step 12.5: Commit**

```bash
git add docs/superpowers/specs/2026-04-03-7d-capacity-scheduler-design.md
git commit -m "docs: mark 2026-04-03 7d-capacity spec as superseded"
```

---

## Verification

Final pre-merge checklist:

- [ ] `uv run python -m pytest tests/unit/test_auto_swap.py -v` — all pass
- [ ] `uv run python -m pytest tests/unit/test_usage_monitor.py -v` — all pass
- [ ] `uv run python -m pytest -v` — all pass
- [ ] `grep -rn "score_candidate\|compute_urgency_threshold" jacked tests` returns nothing
- [ ] Manually: start `jacked webux` (in a separate terminal — user runs this), set up two test accounts with staggered 7d windows, observe the decision log shows tier-aware reasons.

## Out of Scope (separate work)

- UI changes to show tier badge / target line per account
- Tier-multiplier-aware 5h burn estimates
- Predictive scheduling using historical burst patterns
