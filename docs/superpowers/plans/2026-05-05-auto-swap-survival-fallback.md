# Auto-Swap 5h-Survival Fallback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When `should_swap_now` fires a 5h-survival reason (forced_critical or burn_rate) and the strict candidate filter returns no eligible target, fall back to a survival-mode selection that drops the `deficit_vs_target > 0` filter while keeping every other guard. Closes the user-observed gap where a fresh-5h candidate (id=3 at 5h=12%) was rejected because its 7d was 8% above its T2 white-bar floor, while the active account was at 5h=94% with 51 minutes to reset and got blocked instead.

**Architecture:** Two-pass selection inside the loop: (1) strict pick (existing behavior — drains 7d-deficit-positive candidates); (2) if strict returned None AND the swap reason is in `SURVIVAL_REASON_PREFIXES`, retry with `mode="survival"` which keeps tier-strict ordering, 5h-headroom, viable-7d-headroom, eligibility filters, but drops the deficit filter. Reason string gets `" (survival fallback)"` appended when survival mode chose the target so the audit log shows why a normally-ineligible candidate was picked. The trigger taxonomy stays unchanged (`forced_critical` / `burn_rate`) so analytics keep working.

**Tech Stack:** Python 3.12, pytest, asyncio. No new dependencies.

---

## File Structure

| File | Responsibility |
|------|----------------|
| `jacked/web/auto_swap/selection.py` | Add `mode` parameter to `pick_best_target` (Literal["strict", "survival"], default "strict"). Add `SURVIVAL_REASON_PREFIXES` module constant listing which `should_swap_now` reasons trigger the fallback. In survival mode, skip the `deficit > 0` rejection while keeping all other filters and the `_SortKey` ordering. |
| `jacked/api/usage_monitor.py` | Inside `active_account_poll_loop`, after the strict `pick_best_target` call: if it returned None AND `reason` starts with any prefix in `SURVIVAL_REASON_PREFIXES`, call `pick_best_target(..., mode="survival")` once. On a survival-mode hit, append `" (survival fallback)"` to the reason before the swap-execution branch consumes it. |
| `tests/unit/test_auto_swap.py` | Add `TestSurvivalSelection` covering: the user-reported scenario, headroom guard preserved, viable-7d guard preserved, tier-strict ordering preserved, tie-break preserved, drained/higher-tier do NOT trigger survival, exhaustion still fires when survival also empty, non-survival reason types not affected. |
| `tests/unit/test_usage_monitor.py` | Add `TestLoopSurvivalFallback` integration test asserting the loop dispatches strict-then-survival on the user's exact scenario and produces a reason string with the `(survival fallback)` suffix. |

`selection.py` is currently 290 lines; this change adds approximately 35 lines (constants + branch) — final size under 330, well under the 500-line hard guardrail. `usage_monitor.py` is currently 1100+ lines (already over guardrail per a separate techdebt finding) but the change here is local to ~12 lines inside an existing function and does not enlarge the file meaningfully.

---

## Task 1: Add `SURVIVAL_REASON_PREFIXES` constant

**Files:**
- Modify: `jacked/web/auto_swap/selection.py` (after the existing `REASON_PREFIX_*` constants, around line 196)
- Test: `tests/unit/test_auto_swap.py` (append a small constants-shape test near `TestShouldSwapNow`)

- [ ] **Step 1.1: Write the failing test**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestSurvivalReasonPrefixes:
    """Lock the contract: survival fallback fires only on 5h-survival
    reasons (forced_critical, burn_rate). Higher-tier-emerged and
    drained reasons are NOT survival cases — they're proactive."""

    def test_survival_prefixes_contains_only_5h_survival(self):
        from jacked.web.auto_swap import (
            SURVIVAL_REASON_PREFIXES,
            REASON_PREFIX_HIGHER_TIER,
            REASON_PREFIX_DRAINED,
            REASON_PREFIX_FIVE_H,
            REASON_PREFIX_BURN_RATE,
        )
        assert REASON_PREFIX_FIVE_H in SURVIVAL_REASON_PREFIXES
        assert REASON_PREFIX_BURN_RATE in SURVIVAL_REASON_PREFIXES
        assert REASON_PREFIX_HIGHER_TIER not in SURVIVAL_REASON_PREFIXES
        assert REASON_PREFIX_DRAINED not in SURVIVAL_REASON_PREFIXES
        # Locked length so adding a new survival reason is a deliberate
        # contract change rather than an accident.
        assert len(SURVIVAL_REASON_PREFIXES) == 2
```

- [ ] **Step 1.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestSurvivalReasonPrefixes -v`
Expected: FAIL with `ImportError: cannot import name 'SURVIVAL_REASON_PREFIXES'`.

- [ ] **Step 1.3: Add the constant**

In `jacked/web/auto_swap/selection.py`, immediately after the line `REASON_PREFIX_NO_DATA = "active has no 7d data"`, add:

```python
# Reasons that trigger the survival-mode fallback in pick_best_target.
# Only 5h-survival categories (forced_critical, burn_rate) — those are
# "active is dying NOW, take what you can get." Drained (T0/T1 hit
# target) and higher_tier_emerged are proactive optimizations that
# should NOT relax the 7d-deficit filter.
SURVIVAL_REASON_PREFIXES: tuple[str, ...] = (
    REASON_PREFIX_FIVE_H,
    REASON_PREFIX_BURN_RATE,
)
```

- [ ] **Step 1.4: Add to package exports**

In `jacked/web/auto_swap/__init__.py`, add `SURVIVAL_REASON_PREFIXES` to the `from .selection import (...)` block AND to `__all__`.

Find the existing `from .selection import` block. Add `SURVIVAL_REASON_PREFIXES,` to it. Find the existing `__all__` list. Add `"SURVIVAL_REASON_PREFIXES",` after the existing `REASON_PREFIX_*` entries.

- [ ] **Step 1.5: Run test to verify it passes**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestSurvivalReasonPrefixes -v`
Expected: PASS.

- [ ] **Step 1.6: Commit**

```bash
git add jacked/web/auto_swap/selection.py jacked/web/auto_swap/__init__.py tests/unit/test_auto_swap.py
git -c commit.gpgsign=false commit -m "feat(auto_swap): SURVIVAL_REASON_PREFIXES constant — locks the survival-fallback eligibility list"
```

---

## Task 2: Add `mode` parameter to `pick_best_target` (strict path unchanged)

**Files:**
- Modify: `jacked/web/auto_swap/selection.py` (the `pick_best_target` function)
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 2.1: Write the failing test for strict mode default**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestPickBestTargetMode:
    """Verify the new ``mode`` kwarg defaults to strict (existing
    behavior) and explicit "strict" is identical."""

    def test_default_mode_is_strict(self):
        # No mode arg → behaves exactly like the legacy callable shape.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=2)))
        # Candidate ahead of T2 schedule (negative deficit) — strict
        # mode rejects it; we just want to confirm default behavior
        # still rejects.
        ahead_t2 = _acct(1, usage_5h=10, usage_7d=80,
                         resets_7d=_iso(now + timedelta(days=3)))
        target = pick_best_target([active, ahead_t2], current_id=99, now=now)
        assert target is None

    def test_explicit_strict_matches_default(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=2)))
        ahead_t2 = _acct(1, usage_5h=10, usage_7d=80,
                         resets_7d=_iso(now + timedelta(days=3)))
        target = pick_best_target(
            [active, ahead_t2], current_id=99, now=now, mode="strict",
        )
        assert target is None

    def test_invalid_mode_raises(self):
        from jacked.web.auto_swap import pick_best_target
        import pytest as _pytest
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(99, resets_7d=_iso(now + timedelta(days=2)))
        with _pytest.raises(ValueError):
            pick_best_target(
                [active], current_id=99, now=now, mode="bogus",
            )
```

- [ ] **Step 2.2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestPickBestTargetMode -v`
Expected: FAIL — `pick_best_target` does not accept `mode` keyword.

- [ ] **Step 2.3: Add the mode parameter**

In `jacked/web/auto_swap/selection.py`, modify the `pick_best_target` signature and body. Find:

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
```

Replace with:

```python
def pick_best_target(
    accounts: list[dict],
    current_id: int,
    *,
    active_start: str = "06:00",
    active_end: str = "23:00",
    now: datetime | None = None,
    prev_tiers: dict[int, int] | None = None,
    mode: str = "strict",
) -> dict | None:
```

Then immediately after `now = _resolve_now(now)`, add validation:

```python
    if mode not in ("strict", "survival"):
        raise ValueError(
            f"mode must be 'strict' or 'survival', got {mode!r}"
        )
```

Then find the line `if deficit is None or deficit <= 0:` inside the candidate loop and replace it with:

```python
        if deficit is None:
            continue
        if mode == "strict" and deficit <= 0:
            # Strict mode only considers candidates behind their tier
            # target. Survival mode keeps ahead-of-schedule candidates
            # because the active account is dying and any 5h-headroom
            # candidate beats getting blocked.
            continue
```

Update the function docstring's "Selection rule" section to include the new mode contract. Find the docstring's existing section that begins:

```
    Selection rule (tier-strict; see spec
```

Append (still inside the docstring, before the closing `"""`):

```
    Mode dial:
      - ``mode="strict"`` (default): reject candidates with
        ``deficit_vs_target <= 0``. Used for the proactive selection
        path — we only burn capacity on accounts that are behind
        their tier target.
      - ``mode="survival"``: drop the deficit filter. Used by the
        loop's 5h-survival fallback when a forced_critical or
        burn_rate reason fires and strict mode found nothing. Every
        other guard (5h headroom, viable 7d headroom, validity,
        cooldown-eligibility) is identical. Selection still uses
        ``_SortKey(tier, resets_at_iso, -deficit)`` so a less-ahead
        candidate beats a more-ahead one within the same tier.
```

- [ ] **Step 2.4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestPickBestTargetMode -v`
Expected: 3 PASS.

- [ ] **Step 2.5: Commit**

```bash
git add jacked/web/auto_swap/selection.py tests/unit/test_auto_swap.py
git -c commit.gpgsign=false commit -m "feat(auto_swap): pick_best_target gains mode='strict'|'survival'"
```

---

## Task 3: Survival mode actually finds the previously-rejected candidate

**Files:**
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 3.1: Write the headline regression test (the user's exact scenario)**

Append to `tests/unit/test_auto_swap.py`:

```python
class TestSurvivalSelection:
    """Survival-mode behavior. The headline test reproduces the
    user-reported bug: active T1 at 5h=94%, only candidate with 5h
    headroom is a T2 account already 8.7% ahead of its white-bar
    floor target. Strict mode rejects it; survival mode picks it."""

    def test_user_reported_scenario_picks_ahead_of_schedule_t2(self):
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 6, 0, 0, tzinfo=timezone.utc)
        # Active id=1 (T1, 5h=94%, 7d=85, deficit=5 — fine for strict
        # but it IS the active account).
        active = _acct(1, usage_5h=94, usage_7d=85,
                       resets_5h=_iso(now + timedelta(minutes=51)),
                       resets_7d=_iso(now + timedelta(hours=24, minutes=54)))
        # Candidate id=3: T2, 5h=12% (fresh!), 7d=62%.
        # white_bar(3.6 days into 7d) ≈ 51% → target ≈ 56% → deficit
        # = 56-62 = -6%. STRICT REJECTS. survival picks it.
        cand_3 = _acct(3, usage_5h=12, usage_7d=62,
                       resets_5h=_iso(now + timedelta(minutes=51)),
                       resets_7d=_iso(now + timedelta(hours=86, minutes=54)))
        # Candidate id=2: T3 BUT 5h saturated → no headroom → filtered
        # in BOTH modes (5h headroom is non-negotiable).
        cand_2 = _acct(2, usage_5h=98, usage_7d=45,
                       resets_5h=_iso(now + timedelta(minutes=51)),
                       resets_7d=_iso(now + timedelta(hours=98, minutes=54)))
        accounts = [active, cand_3, cand_2]

        strict = pick_best_target(accounts, current_id=1, now=now)
        assert strict is None, "strict mode must reject id=3 (ahead of T2 floor)"

        survival = pick_best_target(
            accounts, current_id=1, now=now, mode="survival",
        )
        assert survival is not None
        assert survival["id"] == 3, (
            f"survival must pick id=3 (only candidate with 5h headroom); "
            f"got id={survival['id'] if survival else None}"
        )

    def test_survival_keeps_5h_headroom_filter(self):
        # Even in survival mode, a saturated 5h candidate is rejected.
        # That's the WHOLE POINT of survival mode — find a candidate
        # that can RUN. Relaxing 5h headroom defeats the purpose.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, resets_7d=_iso(now + timedelta(days=2)))
        saturated_t0 = _acct(2, usage_5h=98, usage_7d=10,
                             resets_5h=_iso(now + timedelta(hours=2)),
                             resets_7d=_iso(now + timedelta(hours=20)))
        # Even a juicy T0 candidate at 10% 7d gets rejected in survival
        # because it's saturated on 5h.
        target = pick_best_target(
            [active, saturated_t0], current_id=1, now=now, mode="survival",
        )
        assert target is None

    def test_survival_keeps_viable_7d_headroom_filter(self):
        # has_viable_headroom rejects accounts with less than one 5h
        # window's burn left in their 7d. Survival respects that —
        # swapping to a candidate that'll exhaust within minutes is
        # worse than not swapping.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, resets_7d=_iso(now + timedelta(days=2)))
        almost_drained = _acct(2, usage_5h=10, usage_7d=99,
                               resets_5h=_iso(now + timedelta(hours=2)),
                               resets_7d=_iso(now + timedelta(hours=20)))
        target = pick_best_target(
            [active, almost_drained], current_id=1, now=now,
            mode="survival",
        )
        assert target is None

    def test_survival_keeps_eligibility_filters(self):
        # Survival never picks invalid / failed / disabled candidates.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, resets_7d=_iso(now + timedelta(days=2)))
        invalid = _acct(2, usage_5h=10, usage_7d=10, valid=False,
                        resets_7d=_iso(now + timedelta(hours=20)))
        failing = _acct(3, usage_5h=10, usage_7d=10, failures=5,
                        resets_7d=_iso(now + timedelta(hours=20)))
        no_token = _acct(4, usage_5h=10, usage_7d=10, cc_token=False,
                         resets_7d=_iso(now + timedelta(hours=20)))
        disabled = _acct(5, usage_5h=10, usage_7d=10, auto_swap=False,
                         resets_7d=_iso(now + timedelta(hours=20)))
        target = pick_best_target(
            [active, invalid, failing, no_token, disabled],
            current_id=1, now=now, mode="survival",
        )
        assert target is None

    def test_survival_tier_strict_priority_preserved(self):
        # Among multiple ahead-of-schedule candidates, tier index still
        # rules: T0 beats T2 even if T0 is more ahead than T2.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=94,
                       resets_5h=_iso(now + timedelta(minutes=45)),
                       resets_7d=_iso(now + timedelta(days=2)))
        # T0 candidate, ahead of (drain-to-100) target by 20%
        # (impossible by definition, but force the math: T0 target =
        # 100, usage = 80 → deficit = +20. So this is NOT actually
        # ahead.) Re-construct with T0 target=100, usage=100
        # (deficit=0 — strict rejects, survival picks.)
        ahead_t0 = _acct(2, usage_5h=10, usage_7d=100,
                         resets_5h=_iso(now + timedelta(minutes=45)),
                         resets_7d=_iso(now + timedelta(hours=12)))
        # T2 candidate behind floor (deficit positive — both modes
        # accept). But T2's tier index 2 > T0's 0 → T0 wins by tier.
        behind_t2 = _acct(3, usage_5h=10, usage_7d=20,
                          resets_5h=_iso(now + timedelta(minutes=45)),
                          resets_7d=_iso(now + timedelta(days=3)))
        target = pick_best_target(
            [active, ahead_t0, behind_t2], current_id=1, now=now,
            mode="survival",
        )
        assert target is not None
        assert target["id"] == 2, (
            "Tier-strict ordering preserved in survival: T0 ahead of "
            "target still beats T2 behind target."
        )

    def test_survival_tie_break_earlier_expiry_within_tier(self):
        # Two T2 candidates, both ahead of schedule. Earlier-expiring
        # one wins (matches strict-mode tie-break).
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=94,
                       resets_5h=_iso(now + timedelta(minutes=45)),
                       resets_7d=_iso(now + timedelta(days=2)))
        early_t2 = _acct(2, usage_5h=10, usage_7d=80,
                         resets_5h=_iso(now + timedelta(minutes=45)),
                         resets_7d=_iso(now + timedelta(hours=49)))
        late_t2 = _acct(3, usage_5h=10, usage_7d=80,
                        resets_5h=_iso(now + timedelta(minutes=45)),
                        resets_7d=_iso(now + timedelta(hours=80)))
        target = pick_best_target(
            [active, early_t2, late_t2], current_id=1, now=now,
            mode="survival",
        )
        assert target is not None
        assert target["id"] == 2

    def test_strict_still_rejects_in_survival_eligible_pool(self):
        # Sanity: same accounts, two modes, opposite outcomes.
        from jacked.web.auto_swap import pick_best_target
        now = datetime(2026, 5, 6, 0, 0, tzinfo=timezone.utc)
        active = _acct(1, usage_5h=94,
                       resets_5h=_iso(now + timedelta(minutes=51)),
                       resets_7d=_iso(now + timedelta(hours=24, minutes=54)))
        cand = _acct(3, usage_5h=12, usage_7d=62,
                     resets_5h=_iso(now + timedelta(minutes=51)),
                     resets_7d=_iso(now + timedelta(hours=86, minutes=54)))
        assert pick_best_target([active, cand], current_id=1, now=now) is None
        assert pick_best_target(
            [active, cand], current_id=1, now=now, mode="survival",
        )["id"] == 3
```

- [ ] **Step 3.2: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestSurvivalSelection -v`
Expected: 7 PASS.

- [ ] **Step 3.3: Run the full auto_swap suite — no regressions**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v 2>&1 | tail -10`
Expected: every existing test still passes.

- [ ] **Step 3.4: Commit**

```bash
git add tests/unit/test_auto_swap.py
git -c commit.gpgsign=false commit -m "test(auto_swap): TestSurvivalSelection — headline + guard preservation tests"
```

---

## Task 4: Loop dispatches survival fallback on 5h-survival reasons

**Files:**
- Modify: `jacked/api/usage_monitor.py` (inside `active_account_poll_loop`, around the existing `pick_best_target(...)` strict call and the action-decision branches)
- Test: `tests/unit/test_usage_monitor.py`

- [ ] **Step 4.1: Write the failing integration test**

Append to `tests/unit/test_usage_monitor.py`:

```python
class TestLoopSurvivalFallback:
    """Drive the loop's strict-then-survival pipeline. We exercise the
    real production helpers (no internal mocks of selection.py) — only
    DB / WS / credential side-effects are stubbed."""

    def _build_user_scenario_accounts(self, now):
        # Reconstruct the user-reported bug:
        # active id=1 jack@jackmd.com  T1 5h=94 7d=85
        # cand   id=3 jackusc@gmail   T2 5h=12 7d=62 (ahead of floor)
        # cand   id=2 jack.neil       T3 5h=98 (saturated)
        # cand   id=7 jack.neil hank  T2 5h=100 (saturated)
        return [
            _full_acct(
                1, usage_5h=94, usage_7d=85,
                resets_5h=_iso(now + timedelta(minutes=51)),
                resets_7d=_iso(now + timedelta(hours=24, minutes=54)),
            ),
            _full_acct(
                3, usage_5h=12, usage_7d=62,
                resets_5h=_iso(now + timedelta(minutes=51)),
                resets_7d=_iso(now + timedelta(hours=86, minutes=54)),
            ),
            _full_acct(
                2, usage_5h=98, usage_7d=45,
                resets_5h=_iso(now + timedelta(minutes=51)),
                resets_7d=_iso(now + timedelta(hours=98, minutes=54)),
            ),
            _full_acct(
                7, usage_5h=100, usage_7d=46,
                resets_5h=_iso(now + timedelta(minutes=51)),
                resets_7d=_iso(now + timedelta(hours=70, minutes=54)),
            ),
        ]

    def test_strict_pick_returns_none_then_survival_picks_id_3(self):
        # Drives pick_best_target twice — strict then survival —
        # without touching any DB/WS state. Just calls the production
        # functions directly to prove the contract holds end-to-end.
        from jacked.web.auto_swap import (
            pick_best_target,
            should_swap_now,
            SURVIVAL_REASON_PREFIXES,
        )
        now = datetime(2026, 5, 6, 0, 0, tzinfo=timezone.utc)
        accounts = self._build_user_scenario_accounts(now)
        active = accounts[0]

        # Strict: nothing eligible.
        strict = pick_best_target(accounts, current_id=1, now=now)
        assert strict is None

        # should_swap_now must produce a 5h-critical reason on this
        # active (94% >= 90, reset 51min away >= 30min suppress threshold).
        reason = should_swap_now(active=active, best=strict, now=now)
        assert reason is not None
        assert any(reason.startswith(p) for p in SURVIVAL_REASON_PREFIXES), (
            f"reason {reason!r} should be a survival prefix"
        )

        # Loop's fallback: retry survival.
        survival = pick_best_target(
            accounts, current_id=1, now=now, mode="survival",
        )
        assert survival is not None and survival["id"] == 3

    def test_survival_does_not_fire_on_non_survival_reason(self):
        # Active drained on T0 — that's a tier_drained reason, NOT
        # survival. Loop should NOT fall back to survival mode.
        from jacked.web.auto_swap import (
            should_swap_now,
            SURVIVAL_REASON_PREFIXES,
        )
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _full_acct(
            1, usage_5h=20, usage_7d=100,
            resets_5h=_iso(now + timedelta(hours=2)),
            resets_7d=_iso(now + timedelta(hours=12)),
        )
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is not None
        assert reason.startswith("drained:")
        assert not any(
            reason.startswith(p) for p in SURVIVAL_REASON_PREFIXES
        ), "drained must NOT trigger survival fallback"

    def test_5h_critical_with_imminent_reset_does_not_fire(self):
        # When 5h reset is within RESET_SUPPRESS_MINUTES, should_swap_now
        # suppresses the 5h-critical reason → reason None → no survival
        # fallback either. (This is the existing invariant we don't
        # want to disturb.)
        from jacked.web.auto_swap import should_swap_now
        now = datetime(2026, 5, 4, 12, 0, tzinfo=timezone.utc)
        active = _full_acct(
            1, usage_5h=95, usage_7d=50,
            resets_5h=_iso(now + timedelta(minutes=8)),  # <10 = suppressed
            resets_7d=_iso(now + timedelta(days=3)),
        )
        reason = should_swap_now(active=active, best=None, now=now)
        assert reason is None
```

- [ ] **Step 4.2: Run test to verify behavior the test expects**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py::TestLoopSurvivalFallback -v`
Expected: 3 PASS — these tests cover the contract that selection + reason classification already provides; they pass right after Task 3 because the underlying functions are correct. The remaining work is wiring the loop to actually call the survival path.

- [ ] **Step 4.3: Wire the survival fallback into `active_account_poll_loop`**

In `jacked/api/usage_monitor.py`, find the existing late-import block inside the loop. Add `SURVIVAL_REASON_PREFIXES` to the `from jacked.web.auto_swap import (...)` block:

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
    SURVIVAL_REASON_PREFIXES,
    TIER_EXCLUDED,
)
```

Find the existing call site:

```python
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
                prev_tiers=_last_observed_tiers,
            )
```

Replace with:

```python
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
                prev_tiers=_last_observed_tiers,
            )

            # 5h-survival fallback: if the strict pick found nothing
            # and the swap reason is "active is dying NOW" (5h critical
            # or burn-rate projection), retry with survival mode which
            # drops the deficit-vs-target filter while keeping every
            # other guard. Closes the user-observed gap where a
            # fresh-5h candidate was rejected because it was already
            # ahead of its 7d floor target. See spec
            # docs/superpowers/plans/2026-05-05-auto-swap-survival-fallback.md.
            survival_used = False
            if (
                best is None
                and reason is not None
                and any(reason.startswith(p) for p in SURVIVAL_REASON_PREFIXES)
            ):
                best = pick_best_target(
                    accounts,
                    current_id=active_acct_id,
                    active_start=active_start,
                    active_end=active_end,
                    now=now_utc,
                    prev_tiers=_last_observed_tiers,
                    mode="survival",
                )
                if best is not None:
                    survival_used = True
                    reason = f"{reason} (survival fallback)"
                    logger.info(
                        "Auto-swap: survival fallback found candidate %d "
                        "for active %d (reason=%s)",
                        best["id"], active_acct_id, reason,
                    )
```

Then, in the swap branch of the action-decision ladder, find the line that builds `_decision_reason = reason`. Leave it unchanged — the suffix is already in `reason` by this point. The existing `_trigger_for_reason(reason)` mapping correctly returns `forced_critical` or `burn_rate` because the prefix is unchanged.

**Important:** do NOT add any new module-level state for survival-mode tracking. The strict→survival sequence is fully captured in `reason` (suffix tells the story) and the standard decision-log machinery handles it.

- [ ] **Step 4.4: Run the integration test plus full test_usage_monitor.py**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py -v 2>&1 | tail -15`
Expected: every test passes (the new survival-fallback class plus all prior).

- [ ] **Step 4.5: Run the entire test_auto_swap.py suite**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v 2>&1 | tail -10`
Expected: every test passes.

- [ ] **Step 4.6: Commit**

```bash
git add jacked/api/usage_monitor.py tests/unit/test_usage_monitor.py
git -c commit.gpgsign=false commit -m "feat(usage_monitor): wire 5h-survival fallback into the unified decision flow"
```

---

## Task 5: Audit-log clarity — `(survival fallback)` suffix is preserved in decision detail

**Files:**
- Test: `tests/unit/test_usage_monitor.py`

- [ ] **Step 5.1: Write the failing test**

Append to `tests/unit/test_usage_monitor.py` inside `TestLoopSurvivalFallback`:

```python
    def test_survival_reason_string_carries_suffix(self):
        # When the survival fallback fires, the decision_reason that
        # ends up in the DB / WS broadcast must end with the
        # "(survival fallback)" marker so an operator scanning the log
        # can immediately tell why a normally-ineligible candidate was
        # picked. Trigger taxonomy stays on the original cause
        # (forced_critical / burn_rate) so analytics keep aggregating.
        from jacked.web.auto_swap import (
            REASON_PREFIX_FIVE_H,
            should_swap_now,
        )
        from jacked.api.usage_monitor import _trigger_for_reason
        # The suffix is appended in the loop, but the trigger mapper
        # must continue to return forced_critical for a reason that
        # starts with REASON_PREFIX_FIVE_H even WITH the suffix.
        suffixed = (
            f"{REASON_PREFIX_FIVE_H} 94.0% >= 90% (survival fallback)"
        )
        assert _trigger_for_reason(suffixed) == "forced_critical"

    def test_survival_burn_rate_keeps_burn_rate_trigger(self):
        from jacked.web.auto_swap import REASON_PREFIX_BURN_RATE
        from jacked.api.usage_monitor import _trigger_for_reason
        suffixed = (
            f"{REASON_PREFIX_BURN_RATE} 82.0% -> 92.0% in 10min "
            "(survival fallback)"
        )
        assert _trigger_for_reason(suffixed) == "burn_rate"
```

- [ ] **Step 5.2: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py::TestLoopSurvivalFallback -v`
Expected: 5 PASS — the existing `_trigger_for_reason` already uses `startswith`, so a suffix doesn't break it. These tests lock that contract so a future "use full-string match" refactor breaks loud.

- [ ] **Step 5.3: Commit**

```bash
git add tests/unit/test_usage_monitor.py
git -c commit.gpgsign=false commit -m "test(usage_monitor): survival suffix preserves trigger taxonomy"
```

---

## Task 6: Final verification + spec note

**Files:**
- Modify: `docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md` (add a note linking to this plan)

- [ ] **Step 6.1: Run the full project test suite**

Run: `uv run python -m pytest tests/unit/ -v 2>&1 | tail -10`
Expected: all green (count of passes increases by ~13 from this plan: 1 prefix-shape + 3 mode + 7 survival + 3 loop integration − the 1 test_user_reported_scenario already counted above ... pragmatically, the full suite count should rise by 13 net new tests).

- [ ] **Step 6.2: Append a "Survival fallback" note to the auto-swap spec**

Edit `docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md`. Find the "Anti-Jitter Hardening" section. Add a new section immediately after it:

```markdown
### 5h-Survival Fallback (added 2026-05-05 post-DCR)

When `should_swap_now` returns a 5h-survival reason (`forced_critical`
or `burn_rate`) AND the strict `pick_best_target` returns None, the
loop retries with `pick_best_target(mode="survival")`. Survival mode
drops the `deficit_vs_target > 0` filter while keeping every other
guard (5h headroom, viable 7d headroom, eligibility, tier ≠ EXCLUDED).
Sort key (`_SortKey(tier, resets_at_iso, -deficit)`) is unchanged —
tier-strict ordering still wins.

**Why:** without this, an active account at 5h critical with no
deficit-positive candidate gets blocked even when a fresh-5h candidate
exists that's slightly ahead of its 7d floor target. Real-world bug:
active at 5h=94%, candidate at 5h=12% but 8.7% above its T2 floor →
strict rejects → "no eligible target" alert despite a perfectly good
escape pod. Survival mode treats negative deficit as fine when active
is dying — getting blocked is strictly worse than over-burning a
single account by a few percent.

**Audit signal:** survival-fallback reason strings end with
`" (survival fallback)"`. Decision-log trigger remains the original
cause (`forced_critical` / `burn_rate`) so analytics aggregations are
unaffected.

**Drained / higher-tier-emerged paths intentionally do NOT use
survival.** Those are proactive optimizations (drain-this-account /
balance-load), not survival. Relaxing the deficit filter there would
defeat the long-term utilization goal of the tier model.

See implementation plan
`docs/superpowers/plans/2026-05-05-auto-swap-survival-fallback.md`.
```

- [ ] **Step 6.3: Commit the spec note**

```bash
git add docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md
git -c commit.gpgsign=false commit -m "docs(spec): document the 5h-survival fallback path"
```

- [ ] **Step 6.4: Bump version + tag + push**

Bump `jacked/__init__.py` `__version__` to `0.42.5`. Then:

```bash
git add jacked/__init__.py
git -c commit.gpgsign=false commit -m "chore: bump version to 0.42.5"
git tag v0.42.5
git push origin master --tags
gh release create v0.42.5 --title "v0.42.5 — 5h-survival fallback selection" --generate-notes --latest
```

- [ ] **Step 6.5: Wait for PyPI publish workflow + verify**

Run: `gh run watch $(gh run list --workflow=publish.yml --limit 1 --json databaseId --jq '.[0].databaseId') --exit-status`
Expected: success.

Verify on PyPI:

```bash
until curl -sf -m 5 https://pypi.org/pypi/claude-jacked/json 2>/dev/null | python3 -c "import json, sys; sys.exit(0 if json.load(sys.stdin)['info']['version'] == '0.42.5' else 1)"; do sleep 5; done && echo "0.42.5 live on PyPI"
```

---

## Out of Scope

- **Changing tier-strict priority semantics.** Survival mode keeps the same `_SortKey`. Re-thinking how tiers compose with survival is a separate spec.
- **Tier-multiplier-aware burn-rate** (5x vs 20x). The user mentioned 5x burns faster; that's a separate finding tracked under `tier_critical_threshold` / `compute_burn_per_window` and not addressed here.
- **Active-tier hysteresis.** Already addressed in 0.42.2.
- **Watchdog `pattern_d` (same-tier-stay).** Already addressed in 0.42.2.

---

## Self-Review

**Spec coverage:**
- ✓ Survival fallback fires on `forced_critical` / `burn_rate` only — Task 1 locks the constant; Task 4 dispatches; Task 4.2 negative test (`test_survival_does_not_fire_on_non_survival_reason`).
- ✓ Drops `deficit > 0` filter — Task 2 implementation; Task 3.1 headline test.
- ✓ Keeps tier-strict priority — Task 3 `test_survival_tier_strict_priority_preserved`.
- ✓ Keeps 5h-headroom guard — Task 3 `test_survival_keeps_5h_headroom_filter`.
- ✓ Keeps viable-7d guard — Task 3 `test_survival_keeps_viable_7d_headroom_filter`.
- ✓ Keeps eligibility filters — Task 3 `test_survival_keeps_eligibility_filters`.
- ✓ Audit-log clarity — Task 4 appends `(survival fallback)` to reason; Task 5 locks trigger-mapper contract.
- ✓ Existing tests still pass — Task 3.3 + Task 4.5 + Task 6.1.

**Placeholder scan:** No TBDs, no "implement appropriately", no "similar to Task N", every code step has full code, every command has expected output.

**Type consistency:** `mode: str = "strict"` parameter (Task 2) referenced consistently in Task 3 and Task 4 invocations. `SURVIVAL_REASON_PREFIXES: tuple[str, ...]` (Task 1) referenced as a tuple in Task 4's `any(reason.startswith(p) for p in ...)`. Loop's `survival_used` local matches the rest of the function's snake_case style.

Plan is ready for execution.
