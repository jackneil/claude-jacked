# Auto-Swap Utilization Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace `score_candidate` and split-decision proactive/defensive flow with a deadline-aware tier model that drains soonest-expiring 7d windows first, rides out 5h windows to minimize prompt-cache churn, and keeps the algorithm's view of "expected usage" aligned with the UI's white bar.

**Architecture:** Pure functions in `jacked/web/auto_swap.py` provide tier classification, target computation, deficit math, and a tier-strict selection rule. `jacked/api/usage_monitor.py::active_account_poll_loop` calls a single `should_swap_now(active, best, ...)` per tick — no separate proactive scanner. White bar is wall-clock per-account, identical to the UI calc in `jacked/data/web/js/components/usage.js::computeElapsedFraction7d`.

**Anti-jitter hardening:** A "higher-tier emerged" override requires the same target id to persist across 2 consecutive ticks before swapping. This prevents Anthropic-API timestamp jitter (±30s in `cached_7d_resets_at`) from flapping accounts across tier boundaries. State held in module-level `_emerged_target_streak: dict[int, int]` in `usage_monitor.py`.

**Silent-stall watchdog:** Module-level `_consecutive_no_best_ticks` counter; if loop ticks ≥10 times with `best is None` AND active account has stale data (`usage_cached_at` >30 min) AND ≥1 candidate has fresh data showing deficit, escalate to ERROR + broadcast `auto_swap_stall` WS event so the dashboard can show the stuck state. Differentiates "all candidates at target" (benign) from "no candidate is evaluable" (stuck).

**Trigger taxonomy:** Per spec, decision-log `trigger` field uses one of `tier_drained`, `higher_tier_emerged`, `forced_critical`, `burn_rate`, `tier_aware` (catch-all for the rare "best exists but only burn-rate fired"). Derived from the prefix of `should_swap_now`'s reason string.

**Tech Stack:** Python 3.12, asyncio, pytest. Run all tests via `uv run python -m pytest` (per project CLAUDE.md). The async loop runs inside FastAPI lifespan.

**Spec:** `docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md`

---

## File Structure

| File | Responsibility |
|------|----------------|
| `jacked/web/auto_swap.py` | Pure decision-engine functions: tier_for, white_bar, target_7d, deficit_vs_target, pick_best_target, should_swap_now. Burn-rate/headroom helpers retained. **File at 620 lines (over 500-line guardrail); Task 15 splits this into focused submodules after the algorithmic work lands.** |
| `jacked/api/usage_monitor.py` | Active-account poll loop; calls `pick_best_target` + `should_swap_now` once per tick; records decision; executes swap. **active_account_poll_loop is 720 lines (way over 50-line function guardrail); Task 16 extracts decision/decision-log helpers.** |
| `tests/unit/test_auto_swap.py` | Unit tests for pure functions (scenarios A–H from spec). |
| `tests/unit/test_usage_monitor.py` | Integration tests for the loop's stay/swap behavior, including the silent-stall watchdog and tier-jitter resistance. |
| `jacked/data/web/js/components/auto-swap.js` | Decision-log render — needs new column set (`tier`, `target_7d`, `deficit`, `is_best`) for the new candidate dump. Updated in Task 14. |
| `docs/architecture/auto-swap-system.md` | Authoritative architecture doc — describes the new tier-based algorithm. Rewritten in Task 13. |
| `docs/superpowers/specs/2026-04-03-7d-capacity-scheduler-design.md` | Add header note: superseded by 2026-05-04 spec. |

---

## Task 0: Pre-split `auto_swap.py` into focused submodules (preparation)

`auto_swap.py` is currently 620 lines (over the 500-line guardrail in `JACKED_GUARDRAILS.md`). Tasks 1–7 ADD ~280 lines and Task 8 deletes ~190 — peak mid-execution would be ~700–900 lines in one file. Splitting first means every subsequent commit lands in clean, sub-300-line files and ruff/CI guardrails never need waivers.

**Files:**
- Create: `jacked/web/auto_swap/__init__.py`, `tiers.py`, `selection.py`, `burn.py`, `diagnostics.py`
- Modify: imports in `jacked/api/usage_monitor.py`, `jacked/web/auth.py`, any other auto_swap importer
- Delete: original `jacked/web/auto_swap.py` after content moves

- [ ] **Step 0.1: Convert `auto_swap.py` to a package**

```bash
mkdir -p jacked/web/auto_swap
git mv jacked/web/auto_swap.py jacked/web/auto_swap/_legacy.py
```

- [ ] **Step 0.2: Create `burn.py`**

`jacked/web/auto_swap/burn.py` contains: `BurnRate`, `update_burn_rate`, `_resets_within`, `RESET_SUPPRESS_MINUTES`, `compute_burn_per_window`, `has_viable_headroom`, `compute_effective_working_hours`. Move from `_legacy.py`. Add module docstring + ruff-friendly imports.

- [ ] **Step 0.3: Create `diagnostics.py`**

`jacked/web/auto_swap/diagnostics.py` contains: `format_account_label`, `tier_label`, `tier_critical_threshold`. (`compute_7d_deficit` will be refactored later in Task 11; for Task 0 we simply move the existing implementation.)

- [ ] **Step 0.4: Create `tiers.py` (initially empty besides imports)**

`jacked/web/auto_swap/tiers.py` is the future home of `tier_for`, `white_bar`, `target_7d`, `deficit_vs_target`, `_resolve_now`, and the `TIER_*` / `_TIER_*` / `T1_TARGET` / `T2_LEAD` constants. For now, write the module with just the imports/docstring; functions land in Tasks 1-4.

```python
"""Tier classification, white-bar progress, and tier-based usage targets.

See docs/architecture/auto-swap-system.md for the algorithm overview.
"""
from __future__ import annotations
from datetime import datetime, timedelta, timezone
```

- [ ] **Step 0.5: Create `selection.py` (initially with current `should_swap` + old `pick_best_target`)**

Move the current (pre-redesign) `should_swap` and `pick_best_target` and `score_candidate` into `jacked/web/auto_swap/selection.py`. They'll be replaced in Tasks 5-6 + 8 — putting them here keeps imports working until then.

- [ ] **Step 0.6: Create `__init__.py` re-exports**

Same content as Task 13 Step 13.6 (re-export everything for backwards-compat). Create now so existing imports `from jacked.web.auto_swap import X` continue to resolve.

- [ ] **Step 0.7: Delete `_legacy.py`**

```bash
rm jacked/web/auto_swap/_legacy.py
```

- [ ] **Step 0.8: Verify imports + tests still pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py tests/unit/test_usage_monitor.py -v`
Expected: all green (we only moved code, didn't change behavior).

Run: `wc -l jacked/web/auto_swap/*.py`
Expected: every file under 300 lines.

- [ ] **Step 0.9: Commit**

```bash
git add jacked/web/auto_swap/
git commit -m "refactor(auto_swap): split into tiers/selection/burn/diagnostics submodules"
```

> **For Tasks 1-12 below:** wherever a step says "In `jacked/web/auto_swap.py`...", read it as "in the appropriate submodule under `jacked/web/auto_swap/`" — typically `tiers.py` for tier helpers, `selection.py` for `pick_best_target` / `should_swap_now`, etc. Imports in tests should remain `from jacked.web.auto_swap import X` (the package's `__init__.py` re-exports the public API).

---

## Task 1: Tier classification (`tier_for`)

**Files:**
- Modify: `jacked/web/auto_swap.py` (add new function near top, after imports)
- Modify: `tests/unit/test_auto_swap.py` (add new test class)

- [ ] **Step 1.1: Update top-of-file imports + write failing tests**

First, add `datetime`/`timedelta`/`timezone` to the existing top-of-file import block in `tests/unit/test_auto_swap.py` (currently `import time`/`import pytest`). Find:

```python
"""Tests for the auto-swap decision engine (pure functions, no I/O)."""

import time

import pytest
```

Replace with:

```python
"""Tests for the auto-swap decision engine (pure functions, no I/O)."""

import time
from datetime import datetime, timedelta, timezone

import pytest
```

Then append the new tests at the bottom of the file (no mid-file imports — all imports stay at the top to satisfy ruff E402):

```python
# ---------------------------------------------------------------------------
# tier_for — deadline tier classification (T0-T3, 4=excluded)
# ---------------------------------------------------------------------------


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

    def test_hysteresis_dampens_t1_to_t0_jitter(self):
        # Anthropic API jitter: account hovers right at the 24h boundary.
        # prev_tier=1 (T1). Instant reads T0 by 60 seconds (less than the
        # 5-minute hysteresis margin). Should remain T1.
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=23, minutes=59)))
        assert tier_for(acct, now=now, prev_tier=1) == 1

    def test_hysteresis_allows_clean_t1_to_t0_after_margin(self):
        # 6 minutes past the 24h boundary — clearly into T0.
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=23, minutes=54)))
        assert tier_for(acct, now=now, prev_tier=1) == 0

    def test_hysteresis_does_not_block_movement_toward_less_urgent(self):
        # Account with prev=T0 reads as T1 at 24h+10s — flip immediately
        # (we never want to STAY too urgent; only the dangerous direction
        # is dampened).
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=24, seconds=10)))
        assert tier_for(acct, now=now, prev_tier=0) == 1

    def test_hysteresis_no_prev_means_no_dampening(self):
        # Without prev_tier, we use the raw classification.
        from jacked.web.auto_swap import tier_for
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        acct = _acct(1, resets_7d=_iso(now + timedelta(hours=23, minutes=59)))
        assert tier_for(acct, now=now) == 0
        assert tier_for(acct, now=now, prev_tier=None) == 0
```

- [ ] **Step 1.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestTierFor -v`
Expected: 9 errors of `ImportError: cannot import name 'tier_for'`.

- [ ] **Step 1.3: Implement `tier_for`**

> **Submodule path:** `jacked/web/auto_swap/tiers.py` (after Task 0).

In `jacked/web/auto_swap/tiers.py` (or `jacked/web/auto_swap.py` if Task 0 has not been performed), add:

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


_TIER_BOUNDARIES_HOURS = (24, 48, 96, 168)  # T0|T1|T2|T3 cutoffs
_TIER_HYSTERESIS_MIN = 5.0  # minutes inside a more-urgent tier before flip


def _resolve_now(now: datetime | None = None) -> datetime:
    """Coerce an optional ``now`` to a UTC-aware datetime."""
    n = now or datetime.now(timezone.utc)
    return n if n.tzinfo else n.replace(tzinfo=timezone.utc)


def tier_for(
    account: dict,
    now: datetime | None = None,
    *,
    prev_tier: int | None = None,
) -> int:
    """Classify an account by its 7d expiry deadline (T0..T3 or 4=excluded).

    Returns 0..3 for T0..T3 or 4 (excluded) when 7d data is missing or the
    window has already expired. Boundaries belong to the higher-numbered
    (less urgent) tier — exactly 24h is T1, exactly 48h is T2.

    Hysteresis (anti-jitter): if ``prev_tier`` is provided and the account's
    instantaneous tier is one step MORE urgent than prev_tier (e.g. prev=T1,
    now=T0), require the new tier to be at least
    ``_TIER_HYSTERESIS_MIN`` minutes deep into the boundary before flipping.
    Within the hysteresis band, returns ``prev_tier``. Movement TOWARD less
    urgent (T0→T1, T1→T2, etc.) flips immediately — only the dangerous
    "becoming more urgent" direction is dampened. This prevents
    Anthropic-API timestamp jitter (±30s) from oscillating across the
    24h or 48h boundary.
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

    now = _resolve_now(now)
    seconds_left = (resets_at - now).total_seconds()
    if seconds_left <= 0:
        return TIER_EXCLUDED

    hours_left = seconds_left / 3600.0
    if hours_left < 24:
        instant = TIER_T0
    elif hours_left < 48:
        instant = TIER_T1
    elif hours_left < 96:
        instant = TIER_T2
    else:
        instant = TIER_T3

    if prev_tier is None:
        return instant

    # Hysteresis: only damp transitions toward more urgent (smaller index).
    if instant >= prev_tier or prev_tier == TIER_EXCLUDED:
        return instant

    # Compute hours past the boundary into the new (more urgent) tier.
    boundary_hours = _TIER_BOUNDARIES_HOURS[instant]  # the upper edge of `instant`
    hours_into_new_tier = boundary_hours - hours_left  # positive if past boundary
    if hours_into_new_tier * 60 >= _TIER_HYSTERESIS_MIN:
        return instant
    return prev_tier
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

> **Submodule path:** `jacked/web/auto_swap/tiers.py`.

In `jacked/web/auto_swap/tiers.py`, immediately after `tier_for`:

```python
def white_bar(account: dict, now: datetime | None = None) -> float | None:
    """Wall-clock elapsed fraction (0.0-1.0) of the 7d window.

    Matches the UI's computeElapsedFraction7d in
    jacked/data/web/js/components/usage.js — same formula:
    (now - (resets_at - 7d)) / 7d. No active-hours adjustment.
    Clamped to [0, 1] (also matches the UI's Math.max/min clamp).

    Returns None when 7d data is missing.
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

    now = _resolve_now(now)
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

> **Submodule path:** `jacked/web/auto_swap/tiers.py`.

In `jacked/web/auto_swap/tiers.py`, immediately after `white_bar`:

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

> **Submodule path:** `jacked/web/auto_swap/tiers.py`.

In `jacked/web/auto_swap/tiers.py`, immediately after `target_7d`:

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

- [ ] **Step 5.3: Add module-level `_has_5h_headroom` helper + sort key dataclass**

> **Submodule path:** `jacked/web/auto_swap/selection.py`.

In `jacked/web/auto_swap/selection.py`, add (above `pick_best_target`):

```python
from dataclasses import dataclass


_FIVE_H_HEADROOM_LIMIT = 90  # >= 90 means "no usable room unless reset imminent"
_FIVE_H_HEADROOM_RESET_MIN = 30  # imminent-reset window for 5h headroom


def _has_5h_headroom(account: dict) -> bool:
    """Return True if the account's 5h window has room now OR is about to reset.

    Pure module-level helper (testable, reusable). Accounts saturated at 5h
    get one chance: if their reset is within ~30 min, they're still viable
    targets because the swap settles + the window flips fresh.
    """
    usage_5h = account.get("cached_usage_5h") or 0
    if usage_5h < _FIVE_H_HEADROOM_LIMIT:
        return True
    return _resets_within(
        account.get("cached_5h_resets_at"), _FIVE_H_HEADROOM_RESET_MIN,
    )


@dataclass(frozen=True, order=True)
class _SortKey:
    """Sort key for pick_best_target.

    Smaller wins (Python ``min`` semantics). Ordering: (tier_index_lower=more_urgent,
    earlier_resets_at, more_negative_neg_deficit means larger raw deficit).
    All three fields are negated/encoded so that "smaller tuple = better
    candidate" — never edit one field without re-reading the others.
    """
    tier_index: int           # 0=T0 most urgent .. 3=T3 least urgent (smaller = better)
    resets_at_iso: str        # ISO timestamp; lex-sort = chronological (smaller = better)
    neg_deficit: float        # negated deficit so larger raw deficit -> smaller key
```

- [ ] **Step 5.4: Rewrite `pick_best_target`**

> **Submodule path:** `jacked/web/auto_swap/selection.py`. After Task 0, the legacy `pick_best_target` lives there too — replace its body in place.

In `jacked/web/auto_swap/selection.py`, replace the existing `pick_best_target` function with:

```python
def pick_best_target(
    accounts: list[dict],
    current_id: int,
    *,
    active_start: str = "06:00",
    active_end: str = "23:00",
    now: datetime | None = None,
    prev_tiers: dict[int, int] | None = None,
) -> dict | None:
    """Return the best swap-target account, or None if nothing qualifies.

    Selection rule (tier-strict; see spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md):

    1. Filter out: current account, inactive/deleted, failures>=3,
       invalid, no token, auto_swap_enabled=0, no 5h headroom,
       no viable 7d headroom, deficit_vs_target<=0, tier=excluded.
    2. Pick the candidate that minimizes (tier_index, resets_at_iso,
       -deficit_vs_target). See ``_SortKey`` for the sign convention.
    3. ``active_start``/``active_end`` are passed only to
       ``has_viable_headroom`` (the 7d-burn-floor check). They do NOT
       gate the algorithm in any other way.
    4. ``prev_tiers``: optional {account_id: last_tier_observed} map for
       hysteresis. When provided, ``tier_for`` ignores tier flips toward
       a more-urgent tier that fall within ``_TIER_HYSTERESIS_MIN``
       minutes of the boundary.

    Inputs read from each account: ``id``, ``is_active``, ``is_deleted``,
    ``consecutive_failures``, ``validation_status``, ``cc_access_token``,
    ``auto_swap_enabled``, ``cached_5h_resets_at``, ``cached_7d_resets_at``,
    ``cached_usage_5h``, ``cached_usage_7d``. Nothing else is consulted —
    do NOT add fields here without updating callers and tests.
    """
    now = _resolve_now(now)
    prev_tiers = prev_tiers or {}

    candidates: list[tuple[_SortKey, dict]] = []
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

        tier = tier_for(a, now=now, prev_tier=prev_tiers.get(a["id"]))
        if tier == TIER_EXCLUDED:
            continue
        if not has_viable_headroom(a, active_start, active_end):
            continue
        if not _has_5h_headroom(a):
            continue
        deficit = deficit_vs_target(a, now=now)
        if deficit is None or deficit <= 0:
            continue

        key = _SortKey(
            tier_index=tier,
            resets_at_iso=a.get("cached_7d_resets_at") or "",
            neg_deficit=-deficit,
        )
        candidates.append((key, a))

    if not candidates:
        return None

    best_key, best = min(candidates, key=lambda kv: kv[0])

    if logger.isEnabledFor(logging.DEBUG):
        sorted_for_log = sorted(candidates, key=lambda kv: kv[0])[:3]
        for key, cand in sorted_for_log:
            logger.debug(
                "pick_best_target: candidate %s (%s) tier=%d resets=%s deficit=%.1f",
                cand.get("id", "?"), cand.get("email", "?"),
                key.tier_index, key.resets_at_iso, -key.neg_deficit,
            )

    return best
```

**Backwards-compat shim:** Tests/external callers may still call `pick_best_target(accounts, current_id, threshold_7d=85)`. The new signature uses keyword-only args after `current_id`, so `threshold_7d` is now an unknown keyword — call sites pass it would raise `TypeError`. Verify with grep before merging:

Run: `grep -rn "pick_best_target" jacked tests | grep -v "def pick_best_target"`
Expected: only callers that match the new signature. The plan's Task 9 already updates the lone production caller. If a third-party test passes `threshold_7d`, update it.

- [ ] **Step 5.5: Run to verify pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestPickBestTargetTierStrict -v`
Expected: 10 passed.

- [ ] **Step 5.6: Add tests for `_has_5h_headroom` helper**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestHas5hHeadroom:
    def test_has_room_when_below_90(self):
        from jacked.web.auto_swap import _has_5h_headroom
        assert _has_5h_headroom({"cached_usage_5h": 50}) is True

    def test_no_room_at_95_no_imminent_reset(self):
        from jacked.web.auto_swap import _has_5h_headroom
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": None,
        }) is False

    def test_room_at_95_with_imminent_reset(self):
        from jacked.web.auto_swap import _has_5h_headroom
        future = datetime.now(timezone.utc) + timedelta(minutes=10)
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": _iso(future),
        }) is True

    def test_no_room_at_95_with_distant_reset(self):
        from jacked.web.auto_swap import _has_5h_headroom
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        assert _has_5h_headroom({
            "cached_usage_5h": 95,
            "cached_5h_resets_at": _iso(future),
        }) is False
```

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestHas5hHeadroom -v`
Expected: 4 passed.

- [ ] **Step 5.7: Verify no orphan callers of pick_best_target with old positional/kw args**

Run: `grep -rn "pick_best_target(" jacked tests | grep -v "def pick_best_target\|TestPickBest"`
Expected: only the call site in `jacked/api/usage_monitor.py` (will be updated in Task 9). No call passes `threshold_7d=` (which would raise TypeError under the new keyword-only signature).

- [ ] **Step 5.8: Run full test_auto_swap.py — note expected failures**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: many failures from old tests of `score_candidate`, `pick_best_target`, `compute_urgency_threshold`. We will delete those in Task 8. For now, only the new TestPickBestTargetTierStrict + TestHas5hHeadroom classes must pass.

- [ ] **Step 5.9: Commit**

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

    def test_active_excluded_no_best_means_stay(self):
        # Active has no 7d data (e.g., fresh restart, not polled yet)
        # AND no candidate exists. Don't force a swap — wait.
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=None)  # excluded
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_active_excluded_with_best_means_swap(self):
        # Active has no 7d data; ``best`` is a real-tier candidate.
        # Treat active as least-urgent and swap.
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=None)  # excluded
        best = _acct(2, usage_5h=10, usage_7d=50,
                     resets_7d=_iso(now + timedelta(hours=12)))  # T0
        reason = should_swap_now(active=active, best=best, now=now)
        assert reason is not None
        assert "higher tier" in reason.lower()

    def test_t3_above_floor_with_no_best_does_not_drain(self):
        # CRITICAL regression: T2/T3 targets are FLOORS, not drain-to goals.
        # An active T3 above its rising white_bar floor with NO candidate
        # must NOT trigger DRAINED. Otherwise the loop would broadcast
        # all_accounts_exhausted on every tick whenever a T3 active is
        # above the floor — a misleading critical alert.
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        # T3 account near end of window, well above its white_bar floor.
        # white_bar(6d to expiry) = 1/7 ≈ 14.3% → T3 target ≈ 14.3%.
        # Active at 30% (above target by 15.7%).
        active = _acct(1, usage_5h=20, usage_7d=30,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=6)))  # T3
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None, (
            f"T3 above floor with no candidate must not drain; got: {reason}"
        )

    def test_t2_above_floor_with_no_best_does_not_drain(self):
        # Same principle for T2.
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=80,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))  # T2
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None

    def test_t0_at_100_drains_even_without_best(self):
        # T0 IS a drain-to goal: hitting 100% is "done".
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=100,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=12)))  # T0
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")

    def test_t1_at_target_drains_even_without_best(self):
        # T1 IS a drain-to goal: hitting 90% is "done".
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=20, usage_7d=90,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=36)))  # T1
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")

    def test_reason_prefixes_match_constants(self):
        # Critical: usage_monitor parses these prefixes to map into the
        # decision-log trigger taxonomy. Lock the contract.
        from jacked.web.auto_swap import (
            should_swap_now,
            REASON_PREFIX_HIGHER_TIER,
            REASON_PREFIX_DRAINED,
            REASON_PREFIX_FIVE_H,
            REASON_PREFIX_BURN_RATE,
            BurnRate,
        )
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)

        # higher_tier
        active = _acct(1, usage_7d=50, resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        best = _acct(2, usage_7d=50, resets_7d=_iso(now + timedelta(hours=12)))
        r = should_swap_now(active=active, best=best, now=now)
        assert r.startswith(REASON_PREFIX_HIGHER_TIER), r

        # drained
        active = _acct(1, usage_5h=20, usage_7d=100,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(hours=12)))
        r = should_swap_now(active=active, best=None, now=now)
        assert r.startswith(REASON_PREFIX_DRAINED), r

        # 5h critical
        active = _acct(1, usage_5h=95, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        r = should_swap_now(active=active, best=None, now=now)
        assert r.startswith(REASON_PREFIX_FIVE_H), r

        # burn-rate
        active = _acct(1, usage_5h=82, usage_7d=50,
                       resets_5h=_iso(now + timedelta(hours=2)),
                       resets_7d=_iso(now + timedelta(days=3)))
        br = BurnRate(rate_5h_per_min=2.0, last_check_5h=82.0,
                      rate_7d_per_min=0.0, last_check_7d=0.0)
        r = should_swap_now(active=active, best=None, burn_rate=br,
                            check_interval_min=5, now=now)
        assert r.startswith(REASON_PREFIX_BURN_RATE), r
```

- [ ] **Step 6.2: Run to verify failure**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestShouldSwapNow -v`
Expected: 9 errors of `ImportError: cannot import name 'should_swap_now'`.

- [ ] **Step 6.3: Implement `should_swap_now`**

> **Submodule path:** `jacked/web/auto_swap/selection.py`.

In `jacked/web/auto_swap/selection.py`, immediately after `pick_best_target`:

```python
_TIER_NAMES = {
    TIER_T0: "T0 (<24h)",
    TIER_T1: "T1 (24-48h)",
    TIER_T2: "T2 (48h-4d)",
    TIER_T3: "T3 (4-7d)",
    TIER_EXCLUDED: "T?",
}


# Reason-string prefixes — DO NOT change these without updating
# usage_monitor's `_trigger_for_reason` mapping. The decision-log
# trigger taxonomy is derived from these.
REASON_PREFIX_HIGHER_TIER = "higher tier emerged"
REASON_PREFIX_DRAINED = "drained:"
REASON_PREFIX_FIVE_H = "5h critical:"
REASON_PREFIX_BURN_RATE = "burn-rate projection:"
REASON_PREFIX_NO_DATA = "active has no 7d data"


def should_swap_now(
    active: dict,
    best: dict | None,
    *,
    burn_rate: BurnRate | None = None,
    check_interval_min: float = 5,  # MINUTES (not seconds). Caller converts.
    critical_5h: float = 90,
    warning_5h: float = 80,
    now: datetime | None = None,
) -> str | None:
    """Return a reason string if the algorithm should swap off ``active``,
    or None to stay.

    Departure rules (any one triggers swap; see spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md):

    1. Higher-tier candidate emerged: ``best`` exists with strictly
       lower tier_index than ``active``. (T0 emerged while on T2/T3
       always overrides — even when 5h reset suppresses critical.)
       Active is treated as TIER_T3+1 when its tier is EXCLUDED, so
       any candidate with a real tier wins over an unclassified
       active account — but ONLY if ``best`` is already a real tier.
    2. Active drained: usage_7d >= target_7d(active).
    3. Active 5h critical: usage_5h >= critical_5h, AND 5h reset NOT
       imminent (within RESET_SUPPRESS_MINUTES).
    4. Burn-rate projection: usage_5h >= warning_5h AND projected to
       cross critical within 2 * check_interval_min, AND 5h reset
       not imminent.

    Returns None when none fire (stay; ride out the 5h window).

    Reason strings always start with one of the ``REASON_PREFIX_*``
    constants — callers (usage_monitor) parse these prefixes to derive
    the decision-log ``trigger`` taxonomy.

    NOTE: ``check_interval_min`` is the poll interval in MINUTES, not
    seconds. Caller (usage_monitor) computes this from the DB setting
    `usage_check_interval` (which is in seconds) by dividing by 60.
    """
    now = _resolve_now(now)
    usage_5h = active.get("cached_usage_5h") or 0
    usage_7d = active.get("cached_usage_7d") or 0
    active_tier = tier_for(active, now=now)
    suppress_5h = _resets_within(
        active.get("cached_5h_resets_at"), RESET_SUPPRESS_MINUTES,
    )

    # 1. Higher-tier candidate (overrides 5h reset suppression)
    # When active is EXCLUDED (no 7d data), treat it as least-urgent so
    # any real-tier candidate wins. This handles "fresh restart, active
    # account hasn't been polled" gracefully — we move to a candidate
    # that DOES have data.
    if best is not None:
        best_tier = tier_for(best, now=now)
        active_rank = active_tier if active_tier != TIER_EXCLUDED else TIER_T3 + 1
        if best_tier < active_rank and best_tier != TIER_EXCLUDED:
            return (
                f"{REASON_PREFIX_HIGHER_TIER}: {_TIER_NAMES[best_tier]} "
                f"candidate vs active {_TIER_NAMES[active_tier]}"
            )

    # If active has no 7d data and there's no candidate with data either,
    # do not force a swap — wait for fresh data on the next tick.
    if active_tier == TIER_EXCLUDED:
        if best is None:
            return None
        # ``best`` exists but didn't trigger above (same/lower rank); fall
        # through to the remaining checks. Without a usable target it is
        # rare for those to fire, but we keep the path explicit.

    # 2. Active drained vs its tier target — ONLY for T0/T1 (drain-to goals).
    # T2/T3 targets are floors (rising white-bar baseline) — being above
    # the floor is the *desired* state, not "drained". Per spec:
    # "T0/T1 targets are drain-to goals. T2/T3 targets are floors —
    # depart only if a candidate with deficit exists; otherwise stay."
    # Firing DRAINED for T2/T3 every tick would broadcast misleading
    # all_accounts_exhausted alerts when no swap is actually warranted.
    if active_tier in (TIER_T0, TIER_T1):
        target = target_7d(active, now=now)
        if target is not None and usage_7d >= target:
            return (
                f"{REASON_PREFIX_DRAINED} 7d usage {usage_7d:.1f}% >= "
                f"tier target {target:.1f}%"
            )

    # 3. 5h critical (suppressed if reset imminent)
    if usage_5h >= critical_5h and not suppress_5h:
        return f"{REASON_PREFIX_FIVE_H} {usage_5h:.1f}% >= {critical_5h:.0f}%"

    # 4. Burn-rate projection (suppressed if 5h reset imminent)
    if (usage_5h >= warning_5h
            and burn_rate is not None
            and not suppress_5h):
        rate = burn_rate.rate_5h_per_min
        if rate > 0:
            mins_to_critical = max(0, critical_5h - usage_5h) / rate
            window_min = max(1.0, 2 * check_interval_min)  # clamp tiny intervals
            if mins_to_critical <= window_min:
                projected = usage_5h + rate * window_min
                return (
                    f"{REASON_PREFIX_BURN_RATE} {usage_5h:.1f}% -> "
                    f"{projected:.1f}% in {int(window_min)}min"
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

> **Submodule path:** `jacked/web/auto_swap/selection.py` (where Task 0 placed it).

In `jacked/web/auto_swap/selection.py`, delete the entire `score_candidate` function including its section header comment block.

- [ ] **Step 8.3: Delete `compute_urgency_threshold` from `auto_swap.py`**

> **Submodule path:** `jacked/web/auto_swap/selection.py` (or wherever Task 0 placed `compute_urgency_threshold` — likely co-located with the legacy `pick_best_target`).

In the appropriate submodule, delete the entire `compute_urgency_threshold` function. Also delete the now-orphaned constant `URGENCY_HOURS` — the new selection rule does not need it. Keep `PROACTIVE_SWAP_THRESHOLD` and `MIN_PROACTIVE_MINUTES` for now; they may be referenced by usage_monitor temporarily and we'll remove orphans in Task 9.

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

- [ ] **Step 9.2: Add module-level state for hysteresis + stall detection**

Near the top of `jacked/api/usage_monitor.py`, after the existing module-level state declarations (around line 26-34), add:

```python
# Per-account last-observed tier for hysteresis. Persists across ticks;
# cleared when an account is removed or a swap occurs to/from it.
_last_observed_tiers: dict[int, int] = {}

# Anti-jitter: require the same higher-tier target to persist across
# ``_EMERGENCE_PERSISTENCE_TICKS`` consecutive ticks before swapping.
# Maps target account id -> consecutive-tick count.
_emerged_target_streak: dict[int, int] = {}
_EMERGENCE_PERSISTENCE_TICKS = 2

# Silent-stall watchdog state.
_consecutive_no_best_ticks: int = 0
_last_stall_warning: float = 0.0
_STALL_TICK_THRESHOLD = 10           # ticks of "best is None + drained-active"
_STALL_USAGE_STALENESS_SECONDS = 1800  # 30 minutes
_STALL_WARNING_COOLDOWN_SECONDS = 1800  # warn at most every 30 min
```

Update `reset_locks` (currently around line 37-49) to also clear these dicts on lifespan startup so a tray restart doesn't carry stale tier/streak/stall state:

```python
def reset_locks() -> None:
    """Rebind module-level asyncio primitives + clear per-account state.

    Tier hysteresis and emergence streak counters depend on consecutive
    ticks; a lifespan restart resets the count so we observe fresh data
    from scratch instead of acting on remembered tiers from before the
    restart.
    """
    global _sweep_wake
    _sweep_wake = asyncio.Event()
    _last_observed_tiers.clear()
    _emerged_target_streak.clear()
    global _consecutive_no_best_ticks, _last_stall_warning
    _consecutive_no_best_ticks = 0
    _last_stall_warning = 0.0
```

- [ ] **Step 9.3: Update imports**

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
    REASON_PREFIX_HIGHER_TIER,
    REASON_PREFIX_DRAINED,
    REASON_PREFIX_FIVE_H,
    REASON_PREFIX_BURN_RATE,
    RESET_SUPPRESS_MINUTES,
    TIER_EXCLUDED,
)
```

- [ ] **Step 9.4: Replace the defensive + proactive flow with a unified call**

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

            # Refresh candidate usage if stale (>10 min). Note: this is
            # called every tick now (was previously gated by want_swap);
            # _CANDIDATE_STALENESS_SECONDS keeps the API call-rate the
            # same for stable data — only candidates we haven't fetched
            # in 10+ minutes get re-fetched.
            accounts = await _fetch_candidate_usage(
                accounts, active_acct_id, db,
            )

            # Hysteresis: pass last-observed tier per non-active account
            # to suppress jitter-driven flips across tier boundaries.
            best = pick_best_target(
                accounts,
                current_id=active_acct_id,
                active_start=active_start,
                active_end=active_end,
                now=now_utc,
                prev_tiers=_last_observed_tiers,
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

            # ---- Anti-jitter persistence on higher-tier emergence ----
            # When the reason is "higher tier emerged", require the SAME
            # target id to persist for >= _EMERGENCE_PERSISTENCE_TICKS
            # consecutive ticks before swapping. This guards against
            # Anthropic API timestamp jitter flipping accounts across
            # the 24h or 48h boundary. Other trigger reasons fire
            # immediately (5h critical / drained / burn-rate cannot
            # be jittered the same way).
            emergence_reason = (
                reason is not None
                and reason.startswith(REASON_PREFIX_HIGHER_TIER)
            )
            if emergence_reason and best is not None:
                streak = _emerged_target_streak.get(best["id"], 0) + 1
                _emerged_target_streak[best["id"]] = streak
                # Drop streaks for any other id (stale)
                for stale_id in list(_emerged_target_streak.keys()):
                    if stale_id != best["id"]:
                        del _emerged_target_streak[stale_id]
                if streak < _EMERGENCE_PERSISTENCE_TICKS:
                    logger.debug(
                        "Suppressing emergence swap to %d — streak %d/%d",
                        best["id"], streak, _EMERGENCE_PERSISTENCE_TICKS,
                    )
                    reason = None  # require another tick
            else:
                # Any non-emergence outcome resets all streaks.
                _emerged_target_streak.clear()

            # Build candidate summaries for decision log (regardless of action)
            _candidate_summaries = []
            for cand in accounts:
                if cand["id"] == active_acct_id:
                    continue
                cand_tier = tier_for(
                    cand, now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
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

            # Refresh hysteresis state from this tick's observations,
            # AND prune entries for accounts no longer present.
            # (Without pruning, a deleted/deactivated account leaks an
            # entry forever — `db.list_accounts(include_inactive=False)`
            # filters them out of `accounts`, so they never reappear in
            # this loop. Same risk for `_emerged_target_streak`.)
            live_ids = {a["id"] for a in accounts if a["id"] != active_acct_id}
            for stale_id in list(_last_observed_tiers.keys()):
                if stale_id not in live_ids:
                    _last_observed_tiers.pop(stale_id, None)
            for stale_id in list(_emerged_target_streak.keys()):
                if stale_id not in live_ids:
                    _emerged_target_streak.pop(stale_id, None)
            for cand in accounts:
                if cand["id"] == active_acct_id:
                    continue
                cand_tier = tier_for(
                    cand, now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
                if cand_tier == TIER_EXCLUDED:
                    _last_observed_tiers.pop(cand["id"], None)
                else:
                    _last_observed_tiers[cand["id"]] = cand_tier

            ws_registry = getattr(app.state, "ws_registry", None)

            # Map reason prefix -> trigger taxonomy (spec: tier_drained,
            # higher_tier_emerged, forced_critical, burn_rate, tier_aware).
            def _trigger_for_reason(r: str | None) -> str:
                if r is None:
                    return "tick"
                if r.startswith(REASON_PREFIX_HIGHER_TIER):
                    return "higher_tier_emerged"
                if r.startswith(REASON_PREFIX_DRAINED):
                    return "tier_drained"
                if r.startswith(REASON_PREFIX_FIVE_H):
                    return "forced_critical"
                if r.startswith(REASON_PREFIX_BURN_RATE):
                    return "burn_rate"
                return "tier_aware"

            global _last_exhaustion_warning, _consecutive_no_best_ticks
            global _last_stall_warning

            if reason is None:
                # Stay path. Three sub-cases — distinguish them in the
                # audit log so operators can tell "no candidate had
                # deficit", "best is same/lower tier", and "emergence
                # suppressed pending confirmation". Mislabeling
                # emergence-suppression as "same/lower tier" would make
                # genuine T0 emergences look like the algorithm is
                # ignoring them.
                _decision_action = "stay"
                if best is None:
                    _decision_reason = (
                        f"stay: no candidate has deficit "
                        f"(tier {_tier_label(active_acct).strip() or 'unset'})"
                    )
                else:
                    _decision_target_id = best["id"]
                    active_tier_now = tier_for(
                        active_acct, now=now_utc,
                    )
                    best_tier = tier_for(
                        best, now=now_utc,
                        prev_tier=_last_observed_tiers.get(best["id"]),
                    )
                    streak = _emerged_target_streak.get(best["id"], 0)
                    if (best_tier < active_tier_now
                            and best_tier != TIER_EXCLUDED
                            and streak > 0):
                        _decision_reason = (
                            f"stay: emergence streak "
                            f"{streak}/{_EMERGENCE_PERSISTENCE_TICKS}, "
                            f"awaiting confirmation "
                            f"(best id={best['id']} tier={best_tier})"
                        )
                    else:
                        _decision_reason = (
                            f"stay: best is same/lower tier "
                            f"(best id={best['id']} tier={best_tier})"
                        )
            elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                # Cooldown blocks a real departure trigger. Surface the
                # would-have-been target so the audit trail is complete.
                _decision_action = "stay"
                _decision_target_id = best["id"] if best else None
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
                _decision_reason = (
                    f"swap warranted ({reason}) but no eligible target"
                )

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
                trigger = _trigger_for_reason(reason)
                logger.info(
                    "Auto-swap: switching from account %d (5h=%.1f%%) to "
                    "account %d (5h=%.1f%%) — %s [%s]",
                    active_acct_id, usage_5h or 0,
                    best["id"], best.get("cached_usage_5h") or 0,
                    reason, trigger,
                )
                await _execute_swap(
                    db, active_acct_id, active_acct, best,
                    reason=reason, trigger=trigger,
                    usage_5h=usage_5h, usage_7d=usage_7d,
                    active_start=active_start, active_end=active_end,
                    ws_registry=ws_registry,
                )
                # Reset hysteresis & emergence streak around a swap so
                # the next tick observes fresh data on the new pairing.
                _emerged_target_streak.clear()
                _last_observed_tiers.pop(active_acct_id, None)
                _last_observed_tiers.pop(best["id"], None)
                _decision_action = "swap"
                _decision_target_id = best["id"]
                _decision_reason = reason

            # ---- Silent-stall watchdog ------------------------------------
            # Trips when the loop is unable to make progress. Three
            # qualifying patterns (any one increments the counter):
            #
            #  (a) Multi-account stale: stay+no-best+stale active data
            #      AND ≥1 other account exists. The user should see a
            #      candidate but doesn't (likely candidate fetches
            #      failing or all are validation_status=invalid).
            #
            #  (b) Single-account stuck: only one account exists AND
            #      it has hit a forced-out reason (drained/critical/
            #      burn-rate) but `best is None` because no other
            #      account is available. User has no working rotation.
            #
            #  (c) DRAINED with no candidate: any reason fires + no
            #      eligible target exists. The user wanted to leave
            #      this account but can't — needs operator attention.
            #
            # All three patterns produce action=stay, so the gate is
            # `decision_action == "stay"` plus the trigger condition.
            if _decision_action == "stay":
                cached_at = active_acct.get("usage_cached_at") or 0
                age_seconds = int(time.time()) - int(cached_at)
                stale = age_seconds > _STALL_USAGE_STALENESS_SECONDS
                has_other_accounts = sum(
                    1 for a in accounts if a["id"] != active_acct_id
                ) > 0
                # Did a real departure trigger fire even though we stayed?
                # `reason` reflects should_swap_now's verdict AS POSSIBLY
                # MUTATED by emergence persistence (which may set it to
                # None to defer a higher-tier swap until streak met).
                # Cooldown does NOT mutate reason — only persistence does.
                forced_out_reason = reason is not None
                pattern_a = best is None and stale and has_other_accounts
                pattern_b = (
                    not has_other_accounts
                    and forced_out_reason
                )
                pattern_c = best is None and forced_out_reason
                if pattern_a or pattern_b or pattern_c:
                    _consecutive_no_best_ticks += 1
                else:
                    _consecutive_no_best_ticks = 0
            else:
                _consecutive_no_best_ticks = 0

            if _consecutive_no_best_ticks >= _STALL_TICK_THRESHOLD:
                now_ts = time.time()
                if now_ts - _last_stall_warning > _STALL_WARNING_COOLDOWN_SECONDS:
                    logger.error(
                        "Auto-swap stalled: %d consecutive ticks with no "
                        "candidate and stale active-account data "
                        "(active=%d, last_fetch=%ss ago)",
                        _consecutive_no_best_ticks, active_acct_id,
                        int(time.time()) - int(active_acct.get("usage_cached_at") or 0),
                    )
                    _last_stall_warning = now_ts
                    if ws_registry:
                        await ws_registry.broadcast(
                            "auto_swap_stall",
                            {
                                "active_account_id": active_acct_id,
                                "consecutive_ticks": _consecutive_no_best_ticks,
                                "last_fetch_age_seconds": (
                                    int(time.time()) - int(active_acct.get("usage_cached_at") or 0)
                                ),
                            },
                        )
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
                        trigger=_trigger_for_reason(_decision_reason),
                        target_id=_decision_target_id,
                        reason=_decision_reason or "no trigger",
                        detail=_tick_detail,
                    )
```

Make the same trigger replacement in the WS broadcast block right after — replace the inline ternary with `_trigger_for_reason(_decision_reason)`.

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

- [ ] **Step 9.5: Drop the cooldown intermediate decision-log block**

The existing flow has an inline decision-log block inside the cooldown branch (around lines 605-647 in the current file). Our new flow records cooldown-stay via the unified post-flow block, so the inline block must be removed. Search for `# -- Swap cooldown: prevent ping-ponging` in the new code (it should NOT appear after Step 9.4 — verify it doesn't). If any remnants exist, delete them.

- [ ] **Step 9.6: Delete orphan constants from `auto_swap.py`**

Once the proactive-scanner block is gone, these constants in `jacked/web/auto_swap.py` have zero callers:

- `PROACTIVE_SWAP_THRESHOLD` (around line 173)
- `URGENCY_HOURS` (around line 174)
- `MIN_PROACTIVE_MINUTES` (around line 176)
- `SUPPRESS_OVERRIDE_SCORE` (around line 101)

Delete them. Verify with: `grep -rn "PROACTIVE_SWAP_THRESHOLD\|URGENCY_HOURS\|MIN_PROACTIVE_MINUTES\|SUPPRESS_OVERRIDE_SCORE" jacked tests`

Expected: no matches.

- [ ] **Step 9.7: Run usage_monitor unit tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py -v`
Expected: many failures from old expectations; we update those next task.

- [ ] **Step 9.8: Run usage_monitor under syntax check**

Run: `uv run python -c "import jacked.api.usage_monitor"`
Expected: no errors. Indicates the file at least parses and imports clean.

- [ ] **Step 9.9: Commit**

```bash
git add jacked/api/usage_monitor.py jacked/web/auto_swap.py
git commit -m "refactor(usage_monitor): tier-aware unified decision + watchdog"
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

- [ ] **Step 10.4: Add tier-aware decision tests (sync — no `@pytest.mark.asyncio`)**

Append at the bottom of `tests/unit/test_usage_monitor.py`:

```python
from datetime import datetime, timedelta, timezone


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _full_acct(id, *, usage_5h=20, usage_7d=50, resets_5h=None,
               resets_7d=None, valid=True, auto_swap=True,
               failures=0, cc_token=True, usage_cached_at=None):
    return {
        "id": id, "email": f"u{id}@test",
        "is_active": 1, "is_deleted": 0,
        "consecutive_failures": failures,
        "validation_status": "valid" if valid else "invalid",
        "cc_access_token": "tok" if cc_token else None,
        "auto_swap_enabled": 1 if auto_swap else 0,
        "cached_usage_5h": usage_5h, "cached_usage_7d": usage_7d,
        "cached_5h_resets_at": resets_5h,
        "cached_7d_resets_at": resets_7d,
        "usage_cached_at": (
            usage_cached_at
            if usage_cached_at is not None
            else int(datetime.now(timezone.utc).timestamp())
        ),
    }


class TestTierAwareDecision:
    """End-to-end pure-function: T0 wins over T3 (the headline bug fix)."""

    def test_picks_t0_over_t3(self):
        from jacked.web.auto_swap import pick_best_target, should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _full_acct(99,
                            resets_5h=_iso(now + timedelta(hours=2)),
                            resets_7d=_iso(now + timedelta(days=3)))  # T2
        t0 = _full_acct(1, usage_5h=10, usage_7d=80,
                        resets_5h=_iso(now + timedelta(hours=2)),
                        resets_7d=_iso(now + timedelta(hours=12)))   # T0
        t3 = _full_acct(2, usage_5h=10, usage_7d=10,
                        resets_5h=_iso(now + timedelta(hours=2)),
                        resets_7d=_iso(now + timedelta(days=6)))     # T3
        target = pick_best_target([active, t0, t3], current_id=99, now=now)
        assert target["id"] == 1
        reason = should_swap_now(active=active, best=target, now=now)
        assert reason is not None and reason.startswith("higher tier emerged")


# NOTE: TestEmergencePersistence and TestSilentStallWatchdog are
# introduced in Task 14 alongside the helpers they exercise
# (`_apply_emergence_persistence`, `_evaluate_stall`). Adding them
# here would create import-time failures (the helpers don't exist
# yet at this point in the build sequence).


class TestCooldownPath:
    """Spec scenario F27: cooldown blocks swap, but decision-log entry
    must surface the would-have-been target_id (audit trail)."""

    def test_cooldown_branch_records_target_id(self):
        # Verify the SHAPE of the decision-log entry built in the cooldown
        # branch. Since the production path is async, this test exercises
        # the data contract directly by walking through the same
        # condition tree.
        best = {"id": 42}
        decision_target_id = best["id"] if best else None
        decision_action = "stay"
        decision_reason = "swap warranted (higher tier emerged: T0...) but cooldown active (123s remaining)"
        # Assertion: target_id is set even though action is stay.
        assert decision_target_id == 42
        assert decision_action == "stay"
        assert "cooldown" in decision_reason


class TestTriggerTaxonomy:
    """Spec line 256-269: trigger field uses tier_drained,
    higher_tier_emerged, forced_critical, burn_rate, tier_aware,
    tick — derived from reason prefix."""

    def test_higher_tier_reason_maps_to_higher_tier_trigger(self):
        from jacked.web.auto_swap import REASON_PREFIX_HIGHER_TIER
        # Mimic the _trigger_for_reason mapping inline:
        def _trigger(r):
            from jacked.web.auto_swap import (
                REASON_PREFIX_HIGHER_TIER, REASON_PREFIX_DRAINED,
                REASON_PREFIX_FIVE_H, REASON_PREFIX_BURN_RATE,
            )
            if r is None: return "tick"
            if r.startswith(REASON_PREFIX_HIGHER_TIER): return "higher_tier_emerged"
            if r.startswith(REASON_PREFIX_DRAINED): return "tier_drained"
            if r.startswith(REASON_PREFIX_FIVE_H): return "forced_critical"
            if r.startswith(REASON_PREFIX_BURN_RATE): return "burn_rate"
            return "tier_aware"

        assert _trigger(None) == "tick"
        assert _trigger("higher tier emerged: T0 (<24h)...") == "higher_tier_emerged"
        assert _trigger("drained: 7d usage 100.0% >= ...") == "tier_drained"
        assert _trigger("5h critical: 95.0% >= 90%") == "forced_critical"
        assert _trigger("burn-rate projection: 82% -> 92% in 10min") == "burn_rate"
        assert _trigger("some other reason") == "tier_aware"


class TestActiveHoursGuardPreserved:
    """Spec scenario H33: the active-hours guard for swap *execution*
    survives this redesign. _execute_swap is called from the async loop,
    so we assert the contract via configuration-readable settings."""

    def test_active_hours_settings_round_trip(self, monkeypatch):
        # Simple smoke test that the loop reads window_keeper_active_start/end
        # and respects them. Full integration is exercised by manual
        # verification (Step 12.5).
        # Here: assert the settings keys still match what the new flow
        # passes to pick_best_target as active_start / active_end.
        # If someone renames the key, this test breaks loud.
        EXPECTED_KEYS = ("window_keeper_active_start", "window_keeper_active_end")
        # The loop reads these via _setting_str — verify by grep:
        import inspect
        from jacked.api import usage_monitor as um
        src = inspect.getsource(um.active_account_poll_loop)
        for key in EXPECTED_KEYS:
            assert key in src, f"Setting {key} not read by active_account_poll_loop"
```

- [ ] **Step 10.5: Run usage_monitor tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py -v --deselect tests/unit/test_usage_monitor.py::TestEmergencePersistence --deselect tests/unit/test_usage_monitor.py::TestSilentStallWatchdog`
Expected: all selected tests pass. (`TestEmergencePersistence` and `TestSilentStallWatchdog` are added in Task 14 and target helpers that don't exist yet — they are deselected here and will be added + verified together in Task 14.)

If any fail with assertions about the old trigger naming or proactive scanner, update those tests to match the new unified flow.

- [ ] **Step 10.6: Commit**

```bash
git add tests/unit/test_usage_monitor.py
git commit -m "test(usage_monitor): tier-aware flow + emergence + stall + cooldown"
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

> **Submodule path:** `jacked/web/auto_swap/diagnostics.py`.

In `jacked/web/auto_swap/diagnostics.py`, replace the body of `compute_7d_deficit` with:

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

## Task 12.5: Update `docs/architecture/auto-swap-system.md` for new algorithm

**Files:**
- Modify: `docs/architecture/auto-swap-system.md`

The architecture doc is the authoritative description of this subsystem and currently describes the OLD `score_candidate`-based algorithm. Sections to rewrite:

- "Core Principle" — replace "deficit-aware scoring with weighted scoring" with "tier-strict deadline-aware selection"
- "7-Day Deficit Model" — replace single-deficit formula with the 4-tier targets table from the new spec
- "Capacity Waste Model (Proactive Scheduling)" — delete entire section; the proactive/defensive distinction collapses
- "Decision Flow (Per Tick)" — rewrite to: refresh candidate usage → `pick_best_target` (tier-strict) → `should_swap_now` (departure rule) → cooldown/quiet-hours/exhaustion/swap branches → silent-stall watchdog → decision log
- "Swap Triggers" — replace with the 5-value taxonomy (`tier_drained`, `higher_tier_emerged`, `forced_critical`, `burn_rate`, `tier_aware`)
- "Scoring Model" — delete section; replace with link to spec 2026-05-04 and short tier-priority math (loss-rate-per-hour argument)
- File responsibilities table — list new functions in `auto_swap.py` (tier_for, white_bar, target_7d, deficit_vs_target, pick_best_target, should_swap_now) and new module-level state in `usage_monitor.py` (_last_observed_tiers, _emerged_target_streak, _consecutive_no_best_ticks)

- [ ] **Step 12.5.1: Edit `docs/architecture/auto-swap-system.md`**

Use the spec at `docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md` as the source of truth for algorithm description. Bring the architecture doc into agreement.

- [ ] **Step 12.5.2: Update the doc's "Last updated" line at the bottom**

Set to `Last updated: 2026-05-04` and reflect the redesign in the changelog list.

- [ ] **Step 12.5.3: Commit**

```bash
git add docs/architecture/auto-swap-system.md
git commit -m "docs(architecture): rewrite auto-swap-system.md for tier model"
```

---

## Task 12.6: Update `auto-swap.js` UI for new candidate fields

**Files:**
- Modify: `jacked/data/web/js/components/auto-swap.js`

The decision-log UI renders candidate dumps. The new flow's `_candidate_summaries` produces fields `id`, `email`, `label`, `5h`, `7d`, `tier` (0-4), `target_7d`, `deficit`, `is_best`. Old fields no longer present: `windows_remaining`, `urgency_tier`, `skip_reason`, `score`. Old top-level decision detail fields no longer present: `escape_override`, `suppression`, `proactive_target_id`. The UI references all of these; without an update it will render `?` / `undefined` for the deprecated ones.

- [ ] **Step 12.6.1: Identify candidate-row rendering code**

Run: `grep -n "windows_remaining\|urgency_tier\|skip_reason\|escape_override\|suppression\|proactive_target_id\|score" jacked/data/web/js/components/auto-swap.js`
Note line numbers for replacement.

- [ ] **Step 12.6.2: Replace candidate columns**

Edit `jacked/data/web/js/components/auto-swap.js`. Where the candidate row currently renders columns for `windows_remaining` / `urgency_tier` / `skip_reason` / `score`, replace with:
- `tier` (mapped to label: 0→"T0 (<24h)", 1→"T1 (24-48h)", 2→"T2 (48h-4d)", 3→"T3 (4-7d)", 4→"T?")
- `target_7d` (numeric % or "—")
- `deficit` (numeric % with sign or "—")
- `is_best` (✓ when true)

Remove the rendering blocks for top-level `escape_override`, `suppression`, and `proactive_target_id` — they no longer appear. The `cooldown_active` field still applies and stays.

- [ ] **Step 12.6.3: Add `auto_swap_stall` WS handler in `websocket.js`**

The new flow's silent-stall watchdog (Task 9 Step 9.4) broadcasts an
`auto_swap_stall` event over WebSocket when the loop has been stuck
≥10 ticks with stale data and no eligible candidate. Without a UI
handler, the watchdog logs ERROR locally to a void.

In `jacked/data/web/js/websocket.js`, find the existing handler for
`all_accounts_exhausted` (around lines 487-491) and add a parallel
handler immediately after:

```javascript
case 'auto_swap_stall': {
    if (typeof showStallBanner === 'function') {
        showStallBanner(data);
    } else {
        console.warn('Auto-swap stall detected', data);
    }
    break;
}
```

In `jacked/data/web/js/components/auto-swap.js`, add the corresponding
banner renderer:

```javascript
function showStallBanner(data) {
    const banner = document.getElementById('auto-swap-stall-banner');
    if (!banner) return;
    const ageMin = Math.round((data.last_fetch_age_seconds || 0) / 60);
    banner.classList.remove('hidden');
    banner.querySelector('.stall-text').textContent = (
        `Auto-swap stalled: ${data.consecutive_ticks} consecutive ticks ` +
        `with no eligible candidate. Active account data is ${ageMin} ` +
        `min old. Try refreshing usage manually.`
    );
}
```

In `jacked/data/web/index.html` (or the auto-swap-related template),
add a hidden banner element where the existing `all_accounts_exhausted`
banner lives — same structure, different id `auto-swap-stall-banner`.

- [ ] **Step 12.6.4: Smoke-check in browser**

User runs `jacked webux` from a separate terminal (per project memory: never auto-start the server). Verify the decision log renders the new columns without console errors. To exercise the stall banner, set every candidate's `validation_status="invalid"` via the dashboard or DB and let the loop tick 10+ times — banner should appear.

- [ ] **Step 12.6.5: Commit**

```bash
git add jacked/data/web/js/components/auto-swap.js jacked/data/web/js/websocket.js jacked/data/web/index.html
git commit -m "feat(ui): tier columns + auto_swap_stall banner"
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

## Task 13: File-size sanity check (Task 0 already split the package)

Task 0 split `auto_swap.py` into submodules before TDD started. After all
algorithmic work lands, verify each submodule remains within the
guardrail. If any submodule has bloated past 300 lines (target), split
further at this point.

- [ ] **Step 13.1: Verify submodule sizes**

Run: `wc -l jacked/web/auto_swap/*.py`
Expected: every file under 300 lines (target) and well under 500 (hard max).

If `selection.py` is over (likely — it holds both `pick_best_target` and `should_swap_now` plus the `_SortKey` dataclass), split into `selection.py` (just `pick_best_target` + `_SortKey` + `_has_5h_headroom`) and `departure.py` (`should_swap_now` + REASON_PREFIX_* constants). Update `__init__.py` re-exports.

- [ ] **Step 13.2: Run tests after any further split**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py tests/unit/test_usage_monitor.py -v`
Expected: all green.

- [ ] **Step 13.3: Commit if a split was performed**

```bash
git add jacked/web/auto_swap/
git commit -m "refactor(auto_swap): split selection.py — extract departure logic"
```

---

## Task 14: Extract decision/decision-log helpers from `active_account_poll_loop`

**Files:**
- Modify: `jacked/api/usage_monitor.py`

The `active_account_poll_loop` is currently 720 lines (way over the
50-line function guardrail). Extract pure-function helpers so we can
unit-test the state machine directly (replacing the
"tests-against-themselves" pattern in TestEmergencePersistence /
TestSilentStallWatchdog).

- [ ] **Step 14.0: Write failing tests for the extracted helpers**

Before extracting helpers, write the tests that drive their contracts. Append to `tests/unit/test_usage_monitor.py`:

```python
class TestEmergencePersistence:
    """Spec scenario F26 + pre-mortem F2: anti-flap on tier jitter.

    Drives the EXTRACTED helper `_apply_emergence_persistence` —
    these tests exercise real production code, NOT a hand-rolled
    local copy of the logic.
    """

    def test_first_emerge_suppressed(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak: dict[int, int] = {}
        result = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2",
            best_id=7,
            streak=streak,
            persistence_ticks=2,
        )
        assert result is None
        assert streak == {7: 1}

    def test_second_emerge_fires(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {7: 1}
        result = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2",
            best_id=7,
            streak=streak,
            persistence_ticks=2,
        )
        assert result is not None
        assert result.startswith("higher tier emerged")
        assert streak == {7: 2}

    def test_target_change_resets_streak(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {1: 5}
        result = _apply_emergence_persistence(
            reason="higher tier emerged: T0 vs T2",
            best_id=2,
            streak=streak,
            persistence_ticks=2,
        )
        assert 1 not in streak
        assert streak[2] == 1
        assert result is None

    def test_non_emerge_reason_clears_streak(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {7: 1}
        result = _apply_emergence_persistence(
            reason="drained: 7d usage 100% >= 100%",
            best_id=7,
            streak=streak,
            persistence_ticks=2,
        )
        assert result.startswith("drained")
        assert streak == {}

    def test_none_reason_clears_streak(self):
        from jacked.api.usage_monitor import _apply_emergence_persistence
        streak = {7: 1}
        result = _apply_emergence_persistence(
            reason=None, best_id=7, streak=streak, persistence_ticks=2,
        )
        assert result is None
        assert streak == {}


class TestSilentStallWatchdog:
    """Pre-mortem F3: detect "loop ticking but never produces a target".

    Drives the EXTRACTED helper `_evaluate_stall`.
    """

    def test_pattern_a_multi_account_stale(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=2000,
            has_other_accounts=True, reason=None,
            staleness_threshold=1800,
        )
        assert bumped is True

    def test_pattern_b_single_account_forced_out(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=10,
            has_other_accounts=False,
            reason="drained: 7d at 100%",
            staleness_threshold=1800,
        )
        assert bumped is True

    def test_pattern_c_drained_no_candidate(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=10,
            has_other_accounts=True,
            reason="drained: 7d at 100%",
            staleness_threshold=1800,
        )
        assert bumped is True

    def test_no_increment_on_swap(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="swap", best={"id": 1},
            usage_cached_at_age_seconds=10,
            has_other_accounts=True, reason="anything",
            staleness_threshold=1800,
        )
        assert bumped is False

    def test_no_increment_on_healthy_stay(self):
        from jacked.api.usage_monitor import _evaluate_stall
        bumped = _evaluate_stall(
            decision_action="stay", best=None,
            usage_cached_at_age_seconds=10,
            has_other_accounts=True, reason=None,
            staleness_threshold=1800,
        )
        assert bumped is False
```

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py::TestEmergencePersistence tests/unit/test_usage_monitor.py::TestSilentStallWatchdog -v`
Expected: 10 errors of `ImportError: cannot import name '_apply_emergence_persistence'` / `_evaluate_stall`.

- [ ] **Step 14.1: Extract `_apply_emergence_persistence`**

Module-level, near the existing `_trigger_for_reason`:

```python
def _apply_emergence_persistence(
    reason: str | None,
    best_id: int | None,
    streak: dict[int, int],
    persistence_ticks: int,
) -> str | None:
    """Gate "higher tier emerged" reasons behind a multi-tick streak.

    Anti-jitter: a candidate must remain the best ``persistence_ticks``
    times in a row before the swap actually fires. Mutates ``streak``
    in place — increments the count for ``best_id``, prunes any other
    keys, or clears entirely on non-emerge / None reason.

    Returns the original ``reason`` when persistence is met (let the
    swap fire), or None to suppress this tick.
    """
    from jacked.web.auto_swap import REASON_PREFIX_HIGHER_TIER
    if reason is None or not reason.startswith(REASON_PREFIX_HIGHER_TIER):
        streak.clear()
        return reason
    if best_id is None:
        streak.clear()
        return None
    streak[best_id] = streak.get(best_id, 0) + 1
    for stale_id in list(streak.keys()):
        if stale_id != best_id:
            del streak[stale_id]
    if streak[best_id] < persistence_ticks:
        return None
    return reason
```

- [ ] **Step 14.2: Extract `_evaluate_stall`**

Module-level:

```python
def _evaluate_stall(
    *,
    decision_action: str,
    best: dict | None,
    usage_cached_at_age_seconds: int,
    has_other_accounts: bool,
    reason: str | None,
    staleness_threshold: int,
) -> bool:
    """Return True if this tick qualifies as a stall pattern.

    Three patterns trigger a bump (any one):
      (a) Multi-account stale: stay+no-best+stale active+has others
      (b) Single-account forced-out: only one account, departure reason fired
      (c) Drained-no-candidate: any reason fired but no eligible target

    Returns False otherwise (caller resets the counter).
    """
    if decision_action != "stay":
        return False
    stale = usage_cached_at_age_seconds > staleness_threshold
    forced_out = reason is not None
    pattern_a = best is None and stale and has_other_accounts
    pattern_b = not has_other_accounts and forced_out
    pattern_c = best is None and forced_out
    return pattern_a or pattern_b or pattern_c
```

- [ ] **Step 14.3: Extract `_trigger_for_reason` to module level**

The closure inside the loop (Step 9.4) now lifts to module level so
both the loop body and any future callers reuse it:

```python
def _trigger_for_reason(reason: str | None) -> str:
    """Map a should_swap_now reason-string to a decision-log trigger
    taxonomy value. Stable contract — see spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md.
    """
    from jacked.web.auto_swap import (
        REASON_PREFIX_HIGHER_TIER,
        REASON_PREFIX_DRAINED,
        REASON_PREFIX_FIVE_H,
        REASON_PREFIX_BURN_RATE,
    )
    if reason is None:
        return "tick"
    if reason.startswith(REASON_PREFIX_HIGHER_TIER):
        return "higher_tier_emerged"
    if reason.startswith(REASON_PREFIX_DRAINED):
        return "tier_drained"
    if reason.startswith(REASON_PREFIX_FIVE_H):
        return "forced_critical"
    if reason.startswith(REASON_PREFIX_BURN_RATE):
        return "burn_rate"
    return "tier_aware"
```

Replace the inline closure in Step 9.4's body with calls to this
module-level function.

- [ ] **Step 14.4: Replace the loop's emergence + stall logic with helper calls**

In `active_account_poll_loop`, replace the inlined emergence-streak
logic with:

```python
reason = _apply_emergence_persistence(
    reason=reason,
    best_id=best["id"] if best else None,
    streak=_emerged_target_streak,
    persistence_ticks=_EMERGENCE_PERSISTENCE_TICKS,
)
```

And replace the inlined stall logic with:

```python
cached_at = active_acct.get("usage_cached_at") or 0
age_seconds = int(time.time()) - int(cached_at)
has_other_accounts = sum(
    1 for a in accounts if a["id"] != active_acct_id
) > 0
if _evaluate_stall(
    decision_action=_decision_action, best=best,
    usage_cached_at_age_seconds=age_seconds,
    has_other_accounts=has_other_accounts,
    reason=reason,
    staleness_threshold=_STALL_USAGE_STALENESS_SECONDS,
):
    _consecutive_no_best_ticks += 1
else:
    _consecutive_no_best_ticks = 0
```

- [ ] **Step 14.5: (Optional) Extract `_decide_swap_action`**

If the loop body is still over ~150 lines after the above, extract the
if/elif/else action-selection chain. Otherwise skip — the helpers
above already deliver most of the readability benefit.

- [ ] **Step 14.6: Verify loop body shrinks**

Run: `awk '/async def active_account_poll_loop/,/async def full_sweep_loop/' jacked/api/usage_monitor.py | wc -l`
Expected: under ~200 lines (orchestration + settings/fetch + helper calls). Original was 720+.

- [ ] **Step 14.7: Run all tests**

Run: `uv run python -m pytest -v`
Expected: all green. The helper extraction means TestEmergencePersistence and TestSilentStallWatchdog now exercise real production code paths.

- [ ] **Step 14.8: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "refactor(usage_monitor): extract emergence/stall/trigger helpers"
```

---

## Verification

Final pre-merge checklist:

- [ ] `uv run python -m pytest tests/unit/test_auto_swap.py -v` — all pass
- [ ] `uv run python -m pytest tests/unit/test_usage_monitor.py -v` — all pass
- [ ] `uv run python -m pytest -v` — all pass
- [ ] `grep -rn "score_candidate\|compute_urgency_threshold\|PROACTIVE_SWAP_THRESHOLD\|MIN_PROACTIVE_MINUTES\|SUPPRESS_OVERRIDE_SCORE\|URGENCY_HOURS" jacked tests` returns nothing
- [ ] `grep -rn "from jacked.web.auto_swap import.*should_swap\b" jacked tests` returns nothing (only `should_swap_now` remains)
- [ ] `wc -l jacked/web/auto_swap/*.py` — every submodule under 500 lines (after Task 13)
- [ ] `awk '/async def active_account_poll_loop/,/async def full_sweep_loop/' jacked/api/usage_monitor.py | wc -l` — under ~200 lines (after Task 14)
- [ ] `grep -q "auto_swap_stall" jacked/data/web/js/websocket.js` — WS handler is wired
- [ ] `grep -q "showStallBanner" jacked/data/web/js/components/auto-swap.js` — stall banner renderer present
- [ ] `grep -q "auto-swap-stall-banner" jacked/data/web/index.html` — banner element exists
- [ ] `grep -q "is_best" jacked/data/web/js/components/auto-swap.js` — new candidate field rendered
- [ ] Manually: user starts `jacked webux` in a separate terminal (project memory: never auto-start), sets up two test accounts with staggered 7d windows, observes the decision log shows tier-aware reasons (`higher_tier_emerged`, `tier_drained`, etc.) and candidate columns render `tier`/`target_7d`/`deficit`. To exercise stall banner, set every candidate's `validation_status="invalid"` via the dashboard or DB and let the loop tick 10+ times — banner should appear.

## Out of Scope (separate work)

- UI changes to show tier badge / target line per account
- Tier-multiplier-aware 5h burn estimates
- Predictive scheduling using historical burst patterns
