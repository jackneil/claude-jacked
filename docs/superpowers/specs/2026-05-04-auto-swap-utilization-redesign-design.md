# Auto-Swap Algorithm Redesign — Deadline-Aware 7d Utilization

**Date:** 2026-05-04
**Status:** Approved (post-brainstorm) — amended 2026-06-10
**Replaces decisioning portion of:** `2026-04-03-7d-capacity-scheduler-design.md`, `2026-04-03-unified-swap-decision-design.md`

## Changelog

- **2026-06-10** — Amendments from the deadlock/utilization pass (see plan
  `docs/superpowers/plans/2026-06-10-usage-refresh-deadlock-and-autoswap-fixes.html`
  and the updated `docs/architecture/auto-swap-system.md`, which is the
  authoritative current-state description):
  - **Active-hours executor guard REMOVED** (§ "Active hours guard" below
    is dead). Swaps execute 24/7: a credential swap costs nothing while
    idle and pre-positions the fleet for morning — 5h windows open on the
    first API call, not on credential placement. The dead
    `active_start`/`active_end` params were dropped from `_execute_swap`.
  - **`RESET_SUPPRESS_MINUTES` raised 10 → 30** and shared with
    selection's imminent-reset lookahead (`_FIVE_H_HEADROOM_RESET_MIN =
    RESET_SUPPRESS_MINUTES`). Invariant: lookahead <= suppression,
    otherwise an account admitted on an imminent reset is immediately
    ejected by the 5h-critical rule (deterministic ping-pong).
  - **Min-residency gate:** proactive departures (higher-tier emerged /
    intra-tier / burn-rate) blocked until the active account has held the
    slot 900s (`_MIN_RESIDENCY_SECONDS`); drained and 5h-critical are
    forced departures and exempt. Manual switches arm the clock
    (`note_external_swap`) plus a 15-min sweep pause.
  - **New departure rule 1b — intra-T0 preemption:** an earlier-expiring
    T0 candidate with deficit >= 5% and loss-rate
    (`deficit / hours_to_expiry`) >= 1.5x the active T0's preempts it.
    Relaxes this spec's "same-tier never overrides" for the T0-vs-T0
    case, which was the largest observed stranding mechanism.
  - **Deadline-aware T1 target:** `max(90, 100 - min(10,
    achievable_final_day_burn))` (`_t1_deadline_target`); a fixed 90
    strands the buffer when expiry lands outside working hours. Floor 90.
  - **T0 viable-headroom bypass:** T0 candidates with deficit >= 1 skip
    the `has_viable_headroom` burn-floor check — drain-to-100 must not
    strand the final slice below the per-window floor (~4.2%).
  - **Symmetric tier hysteresis:** single-step less-urgent flips (e.g.
    T0→T1) are now also damped within `_TIER_HYSTERESIS_MIN` of the
    boundary (refetch noise fires a premature `drained` + pull-back);
    multi-step jumps flip immediately (genuine window reset).
  - **Trigger taxonomy extended** with stay-cause triggers:
    `cooldown_blocked`, `residency_blocked`, `no_target`, `swap_aborted`,
    `emergence_pending`, plus swap trigger `intra_tier_preempted`. `tick`
    is now strictly "plain stay".
  - **Swap audit:** `swap_log.status` (`pending`/`committed`/`failed`) +
    `residency_seconds`; `/swap-log` returns `swaps_last_24h`
    (committed-only). Failed credential writes are paced by exponential
    backoff (60s base, x2, cap 600s) instead of retry-next-tick.
  - **Drain advisor:** `expiring_with_stranded_capacity` WS event for T0
    accounts projected to strand >2% (`stranding_estimate`). Advisor
    ONLY — explicitly no auto-burn worker; routing real work beats
    burning quota on no-ops.
  - **Stall pattern (d) demoted** to `same_tier_deficit_advisory` (INFO +
    WS, 30-min cooldown, T0/T1 candidates only) — same-tier-stay is
    intended behavior per this spec and must not page at ERROR.
  - **Burn-rate decay ungated:** decays after 5+ unchanged ticks at ANY
    usage level (the below-warning gate froze rates exactly when the
    user went idle at >=80%, firing spurious burn-rate swaps).
  - **Emergence persistence keyed by tier** (`_emerged_tier_streak`),
    not account id — near-tied same-tier candidates alternating as best
    reset each other's id-keyed streak forever. Fast path: a >=2-tier
    gap (T0 best vs T2+ active) skips persistence.

## Problem

The current swap algorithm doesn't align with real-world utilization goals.
Observed misbehaviors:

1. **Wrong target picked.** Switching to a far-from-expiry account when a
   soonest-expiring account still had 5h capacity left.
   `score_candidate` weights `usage_7d × (1 - days_factor)` — penalizing
   *less* when more days remain, i.e. **preferring** far-from-expiry
   accounts. This is the opposite of the goal.
2. **Over-switching.** A 5-minute time cooldown allows mid-5h-window
   flapping between accounts. Each switch reloads context cache (lost
   prompt-cache hits ⇒ new input tokens), so mid-window switches are
   net-negative.
3. **Underutilization at week-end.** Accounts with <24h to expiry are
   not aggressively prioritized. Capacity that doesn't burn before its
   7d resets is lost forever.

The current model expresses some of this (deficit, urgency tiers) but
the formulas pull in different directions and the scoring layer
contradicts the urgency layer.

## Goals (user voice)

- Maximize 7d window utilization across all accounts before each
  account's 7d resets. The 7d window is the scarce resource.
- Soonest-expiring 7d window with usage left = top priority. Within
  the last 24h, drain it. Within 24-48h, drive to ~90%.
- Far-from-expiry accounts (4-7 days out): don't push usage past the
  current week-elapsed line ("white bar"). Buffer is fine — we can
  always burn it later.
- Don't over-switch. Each switch costs prompt-cache. Default behavior:
  ride out the current account's 5h window once we land on it.
- Real-world fit: work happens in bursts and skipped days, so the
  algorithm must route bursts toward the most expiry-risky capacity.

## Decision Model

### Tier definitions (per-account, wall-clock to 7d expiry)

| Tier | Time to 7d expiry | Target 7d usage | Selection priority |
|------|-------------------|-----------------|-------------------|
| **T0** | < 24h | 100% (drain) | highest |
| **T1** | 24–48h | 90% (10% buffer for last-day 5h windows) | high |
| **T2** | 48h – 4d (96h) | white_bar + 5% lead | medium |
| **T3** | 4d – 7d (96h–168h) | white_bar (behind is fine) | lowest |

### White bar (per-account, wall-clock)

Matches the UI exactly:

```
white_bar(account) = (now - (resets_at - 7d)) / 7d
```

No active-hours adjustment. Linear. The scheduler's view of "expected
usage" mirrors what the user sees on the dashboard, so visual and
algorithmic state never diverge.

### Deficit vs tier target

```
target_7d(account) = case tier(account):
    T0 -> 100.0
    T1 ->  90.0
    T2 -> min(100.0, white_bar * 100 + 5.0)
    T3 -> white_bar * 100

deficit(account) = target_7d(account) - cached_usage_7d
```

`deficit > 0` means behind the tier target — eligible for selection.
`deficit <= 0` means at or beyond tier target — not eligible (skip).

### Selection rule (single source of truth)

When the algorithm needs to pick a target (defensive swap or proactive
swap), it ranks **all** non-active candidates by:

```python
candidates = [a for a in accounts
              if eligible(a)                    # gates below
              and deficit_vs_tier_target(a) > 0
              and has_5h_headroom(a)            # can use it now or soon
              and has_viable_7d_headroom(a)]    # >= one 5h-window's burn left

target = min(candidates, key=lambda a: (
    tier_index(a),                       # 0=T0 .. 3=T3 (lower = more urgent)
    a.cached_7d_resets_at,               # earliest expiry within tier
    -deficit_vs_tier_target(a),          # larger deficit within tier
))
```

`eligible(a)`:
- `is_active != 0`, `is_deleted != 1`
- `consecutive_failures < 3`
- `validation_status != "invalid"`
- `cc_access_token is not None`
- `auto_swap_enabled != 0`

**Why strict tier priority is mathematically correct:** Capacity not
burned before a 7d reset is lost forever. Per-tier expected hourly
loss-rate is `deficit / hours_to_expiry`. T0's denominator is always
smallest, so T0 (with room) always has highest loss-rate per hour.
Discrete tiers and a continuous loss-rate score yield the same answer;
discrete is debuggable and matches user mental model.

### Active-account departure rule (when do we *leave* the current account?)

The algorithm decides "should we swap?" each tick on the active account.

Swap if **any** of:

1. **Active is forced out:**
   - `usage_5h >= effective_critical_5h` (tier-aware ceiling), AND 5h
     reset is NOT imminent (within `RESET_SUPPRESS_MINUTES`)
   - `usage_7d >= target_7d(active)` — active hit its tier target. We
     drained it; move on.

2. **Higher-tier candidate emerges with room:**
   - A candidate with strictly lower `tier_index` than the active
     account is eligible. T0 emerging while active is T2/T3 = swap.
     T1 emerging while active is T2/T3 = swap. **Same-tier or
     lower-tier candidate never overrides** the active account
     mid-window.

3. **Burn-rate projection:**
   - `usage_5h >= warning_5h` AND projected to cross critical within
     `2 × check_interval`. Same as today.

If none of the above and the active 5h window has not reset, **stay**.
This is the anti-flap rule: ride out the 5h window.

**On "drained" semantics across tiers:**
- T0/T1 targets are drain-to goals (100%, 90%): hitting target means
  we got everything we wanted from this account.
- T2/T3 targets are floors ("don't burn past this"): hitting target
  means "no more burn here right now". Algorithm reaction is the
  same — depart if a candidate with deficit exists; otherwise stay
  (no candidate = no productive swap).
- As the white bar advances, T2/T3 targets rise. An account "at
  target" now may be "below target" later — the next tick re-evaluates.
  Cooldown + anti-flap prevent rapid ping-pong across these
  re-evaluations.

### Active hours guard (when do we *act*?)

> **REMOVED 2026-06-10.** This section no longer reflects the design.
> The premise was wrong: 5h windows open on the *first API call*, not on
> credential placement, so a sleep-time swap wastes nothing — it costs
> nothing while idle and pre-positions the fleet for morning. The
> executor now runs 24/7; the dead `active_start`/`active_end` params
> were dropped from `_execute_swap`. Active hours still feed the
> burn-floor math (`has_viable_headroom`), the deadline-aware T1 target,
> `achievable_burn`/`stranding_estimate`, and the window keeper's ping
> schedule — they just no longer gate execution.

Original (superseded) text: outside active hours (before
`window_keeper_active_start` or after `window_keeper_active_end`), the
swap *executor* does not run, even if the rules above say swap.
Rationale was: a swap during sleep wastes a 5h window opening.

### Cooldown (residual)

Keep `_SWAP_COOLDOWN_SECONDS = 300` as a safety floor against pathological
swap loops (e.g. flapping data, race with manual intervention). The
new departure rule should make hitting cooldown rare; if it triggers,
log it as a warning.

### Anti-Jitter Hardening (added 2026-05-04 post-DCR)

Cooldown alone is not sufficient when the *data* itself jitters. Anthropic's
API can return `cached_7d_resets_at` values that drift by ±30s tick-to-tick
(clock-skew, backend re-anchoring). At a tier boundary (24h or 48h), this
drift flips an account between adjacent tiers each tick, which would cause
the higher-tier-emerged rule to fire repeatedly. Three layers defend
against this:

1. **Tier hysteresis (`tier_for(account, prev_tier=...)`).** When transitioning
   *toward* a more-urgent tier (T1→T0, T2→T1, etc.), require the account
   to be at least `_TIER_HYSTERESIS_MIN = 5` minutes past the boundary
   before flipping. Movement *away* from urgency (T0→T1, T1→T2) is not
   damped. State held in module-level
   `_last_observed_tiers: dict[int, int]` in `usage_monitor.py`, refreshed
   each tick from observations and pruned of dead account ids.
   *(Amended 2026-06-10: damping is now symmetric for single-step
   less-urgent flips — see Changelog.)*

2. **Emergence persistence (`_apply_emergence_persistence`).** A
   "higher tier emerged" reason from `should_swap_now` does not act
   immediately. The candidate must remain the best target for
   `_EMERGENCE_PERSISTENCE_TICKS = 2` consecutive ticks first.
   State held in `_emerged_target_streak: dict[int, int]`. Other
   reasons (`drained`, `5h critical`, `burn-rate`) fire immediately —
   only the emergence path is gated, because only that path is
   susceptible to single-tick boundary jitter.
   *(Amended 2026-06-10: streak is now keyed by TIER
   (`_emerged_tier_streak`), also gates intra-tier preemption, and a
   >=2-tier gap skips persistence — see Changelog.)*

3. **Silent-stall watchdog.** A counter `_consecutive_no_best_ticks`
   tracks ticks where the loop is stuck (three patterns: multi-account
   stale, single-account forced-out, drained-no-candidate). At 10
   consecutive stuck ticks, escalate to `logger.error` and broadcast an
   `auto_swap_stall` WS event so the dashboard can surface the stuck
   state. Threshold cooldown: `_STALL_WARNING_COOLDOWN_SECONDS = 1800`
   (30 min) between repeat warnings.

All three pieces of state are cleared by `reset_locks` on lifespan
restart so a tray restart starts with a fresh observation.

### Trigger Taxonomy (extended 2026-06-10)

The decision-log `trigger` field uses one of:
- `tier_drained` — active hit T0/T1 drain-to target
- `higher_tier_emerged` — candidate with strictly lower tier index
  emerged AND emergence persistence streak met
- `intra_tier_preempted` — rule 1b intra-T0 preemption fired
  (added 2026-06-10)
- `forced_critical` — active 5h ≥ critical (and 5h reset NOT imminent)
- `burn_rate` — burn-rate projection crosses critical within window
- `tier_aware` — catch-all (rare)
- `tick` — plain stay, nothing fired
- `emergence_pending` — emerge reason suppressed by the persistence
  streak this tick (added 2026-06-10)
- `residency_blocked` — proactive reason blocked by min-residency
  (added 2026-06-10)
- `cooldown_blocked` — reason blocked by post-swap cooldown
  (added 2026-06-10)
- `no_target` — reason fired but no eligible target (added 2026-06-10)
- `swap_aborted` — failure backoff active, or the swap attempt failed
  (TOCTOU / credential write) (added 2026-06-10)

Swap triggers are computed in `_trigger_for_reason(reason)` from the
prefix of `should_swap_now`'s reason string (constants:
`REASON_PREFIX_HIGHER_TIER`, `REASON_PREFIX_INTRA_TIER`,
`REASON_PREFIX_DRAINED`, `REASON_PREFIX_FIVE_H`,
`REASON_PREFIX_BURN_RATE`); stay-cause triggers are set explicitly by
the gate that blocked the swap. Reason-string prefixes are part of the
public contract — do not change without updating both the helper and
consumers.

## Functions to add / replace in `jacked/web/auto_swap.py`

### Replace

- `score_candidate(account, ...)` — **delete**. Replaced by the explicit
  tier/deficit/expiry sort key. Remove all callers.
- `pick_best_target(accounts, ...)` — **rewrite** to use the new
  selection rule (no more `score_candidate`).
- `should_swap(...)` — **rename** to `should_swap_now(...)` and rewrite.
  Returns `None` (stay) or a reason string (swap). The reason string
  feeds the decision-log trigger taxonomy and the user-visible reason
  text. All callers update.
- `compute_urgency_threshold(...)` — **delete**. Tier targets subsume it.

### Add

```python
def tier_for(account: dict, now: datetime | None = None) -> int:
    """Return 0=T0, 1=T1, 2=T2, 3=T3 based on hours to 7d expiry.
    Returns 4 (out-of-tiers / no data) when no resets_at or expired."""

def white_bar(account: dict, now: datetime | None = None) -> float | None:
    """Wall-clock elapsed fraction (0.0-1.0) of the 7d window.
    Matches the UI's computeElapsedFraction7d. None if no resets_at."""

def target_7d(account: dict, now: datetime | None = None) -> float | None:
    """Tier-based 7d usage target as a percentage (0-100).
    None if account has no 7d data."""

def deficit_vs_target(account: dict, now: datetime | None = None) -> float | None:
    """target_7d - cached_usage_7d. Positive = behind target.
    None if account has no 7d data."""
```

### Keep (no behavior change)

- `BurnRate`, `update_burn_rate`, `tier_label`, `format_account_label`,
  `tier_critical_threshold`, `_resets_within`, `RESET_SUPPRESS_MINUTES`,
  `compute_burn_per_window`, `has_viable_headroom`,
  `compute_effective_working_hours` (kept available — not used by the
  selection rule but useful for analytics/UI elsewhere).
- `compute_7d_deficit` — **keep** as a public diagnostic that returns
  `{deficit_vs_white_bar, deficit_vs_tier_target, tier, white_bar,
  hours_to_expiry, has_viable_headroom}`. Used by the decision-log
  candidate dump (so the UI can show why an account was/wasn't picked).

## Algorithm flow in `jacked/api/usage_monitor.py::active_account_poll_loop`

Replace the current "should_swap → escape_hatch → proactive scanner"
sequence with one unified pass:

```python
# (Existing settings/active-account fetch code unchanged)

active_target = pick_best_target(
    accounts,
    current_id=active_acct_id,
    now=now,
)

# active_target is the BEST candidate across the whole pool.
# Decide whether to leave the current account based on departure rules.

reason = should_swap_now(
    active=active_acct,
    best=active_target,
    burn_rate=br,
    settings=...,
    now=now,
)

if reason is None:
    record_decision(action="stay", ...)
elif within_cooldown():
    record_decision(action="stay", reason=f"cooldown ({reason})", ...)
elif outside_active_hours():
    record_decision(action="stay", reason=f"quiet hours ({reason})", ...)
else:
    execute_swap(active -> active_target, reason=reason)
    record_decision(action="swap", ...)
```

`should_swap_now` returns either `None` (stay) or a string describing
the reason. Reason strings drive the decision-log UI.

The proactive vs defensive distinction collapses: there's just one
question per tick — "should we swap?" — answered by the departure
rules. The decision log still tags trigger as `defensive`, `tier_drained`,
`higher_tier_emerged`, `burn_rate`, `forced_critical` for analytics.

## Edge cases

| Case | Handling |
|------|----------|
| All accounts at or past tier target | No candidate. `target = None` → stay on active. Log "all accounts at tier target". |
| Active is T3 (4-7d), no higher-tier room | Stay (no eligible higher-tier candidate). Ride out 5h window. |
| Higher-tier emerges mid-window | Override: swap immediately. Cache cost is justified — T0 capacity decays at the highest rate. |
| Two accounts tie on tier + expiry | Larger deficit wins (deterministic). |
| Account has no 7d data (`cached_7d_resets_at` or `cached_usage_7d` is None) | Excluded from candidate pool. `tier_for` returns 4 (out-of-tiers). |
| 7d already expired (`resets_at < now`) | Excluded. The reset itself will repopulate data via the next sweep. |
| 5h reset imminent on active (within `RESET_SUPPRESS_MINUTES`) | Suppress the `forced_critical` trigger only. Higher-tier emergence still fires (a fresh T0 deserves the swap). |
| Decision changes between ticks (data refresh) | OK. Each tick re-evaluates. Cooldown protects against pathological flapping. |
| Active account hit T0 mid-week (24h to expiry, e.g. on Wednesday) | Tier rule still applies — drain it. Switch off only when 5h cap or 7d at 100%. |
| User work pattern: skipped 2 days, now Friday | T1/T0 accounts will have large deficits → top of candidate list. Bursts route there first. |

## Test scenarios (TDD — must pass before merge)

Each scenario uses synthetic accounts and a mocked `now`. Test framework
is already pytest with fakes; reuse the existing patterns in
`tests/unit/test_auto_swap.py`.

### A. Tier classification

1. Account with `resets_at = now + 12h` → tier 0.
2. Account with `resets_at = now + 36h` → tier 1.
3. Account with `resets_at = now + 3d` → tier 2.
4. Account with `resets_at = now + 6d` → tier 3.
5. Account with `resets_at = now - 1h` → tier 4 (excluded).
6. Account with `resets_at = None` → tier 4 (excluded).

### B. Tier targets

7. T0 target = 100.
8. T1 target = 90.
9. T2 target = white_bar*100 + 5, capped at 100.
10. T3 target = white_bar*100.

### C. Selection (the user's core complaint)

11. **The headline test:** T0 candidate at 80%/12h-to-expiry beats
    T3 candidate at 10%/6d-to-expiry. Pick T0. (This is the bug
    the user observed.)
12. Two T0 candidates: pick the one with earlier `resets_at`.
13. Two T0 candidates same expiry: pick the one with larger deficit.
14. T0 at 100% (no deficit) vs T1 at 50%: pick T1.
15. T0 with no 5h headroom (5h at 95%, no reset for hours): excluded;
    pick next-tier candidate.
16. All accounts at or past tier target: no candidate.

### D. Active-departure rule

17. Active T2 at 50%, no higher-tier candidate: stay (no swap).
18. Active T2 at 50%, T1 candidate at 60% deficit: swap (higher tier
    emerged).
19. Active T2 at 50%, another T2 with bigger deficit: stay
    (same-tier override forbidden).
20. Active T0 at 100%: swap (drained).
21. Active 5h at 95%, candidate exists: swap (forced critical).
22. Active 5h at 95% but 5h resets in 8 min: stay (suppress critical).
    But if a T0 just emerged, swap (higher-tier override beats
    suppression).
23. Active in T3, current 5h not yet reset, no T0/T1/T2 with deficit:
    stay until 5h resets (anti-flap on T3 — user's "ride it out"
    requirement).

### E. White bar matches UI

24. `white_bar(resets_at = now + 1d)` ≈ 6/7 ≈ 0.857.
25. White bar uses wall-clock — overnight tick advances the bar
    (compare `now=Mon 22:00` vs `now=Tue 06:00` for same `resets_at`,
    fraction increases by 8h/168h).

### F. Anti-flap

26. Two consecutive ticks with same data: only one swap, then stay.
27. Cooldown active (last_swap < 5min ago): no swap even if rule
    fires. Log decision.

### G. Real-life burst pattern

28. Scenario: Mon-Tue no usage, Wed afternoon burst. Accounts:
    - A1 resets Wed 23:59 (T0), at 30% — should be picked first.
    - A2 resets Thu 23:59 (T1), at 30% — picked next when A1 hits 100%.
    - A3 resets Sat 23:59 (T3), at 30% — only picked when A1, A2 done.
    Expected sequence: A1 → A1 (until 100% or 5h cap) → A2 → A3.

29. Scenario: Friday evening, A1 just rolled into T0 at 50%. Active
    was A2 (T2) mid-5h window. Expected: swap to A1 immediately
    (higher-tier emerged override).

### H. Regression: existing behavior preserved

30. Burn-rate projection still triggers (existing test
    `test_burn_rate_triggers_swap` ports over).
31. `consecutive_failures >= 3` excludes account from candidates.
32. `auto_swap_enabled = 0` excludes account from candidates.
33. Active-hours guard suppresses execution outside 06:00–23:00.
34. Decision log records every tick's action and reason.

## Migration notes

- The new selection rule is a behavior change — tests will need
  updating. Old tests of `score_candidate` are deleted with the function.
- Decision-log entries change schema slightly (new `tier`, `target_7d`,
  `deficit_vs_target` fields per candidate). Backwards-compatible:
  add fields, don't remove.
- No database migration required (no schema change).
- No UI change required for white-bar consistency (UI already uses
  wall-clock; backend now matches).
- Future work for UI (out of scope here): show tier badge per account,
  show "behind by X% vs tier target" inline. Not required for
  algorithm correctness.

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | Rewrite `should_swap`, `pick_best_target`. Delete `score_candidate`, `compute_urgency_threshold`. Add `tier_for`, `white_bar`, `target_7d`, `deficit_vs_target`. Refactor `compute_7d_deficit` to expose tier diagnostics. |
| `jacked/api/usage_monitor.py` | Collapse defensive/proactive into a single `should_swap_now` decision. Update decision-log triggers. |
| `tests/unit/test_auto_swap.py` | Replace `score_candidate` tests with the 34 scenarios above. TDD: write all tests first, watch them fail, then implement. |
| `tests/unit/test_usage_monitor.py` | Update integration tests to assert new trigger names and stay-vs-swap outcomes. |
| `docs/superpowers/specs/2026-04-03-7d-capacity-scheduler-design.md` | Add header note: "Decisioning portion superseded by 2026-05-04-auto-swap-utilization-redesign-design.md." |

## Out of scope

- UI changes (tier badge, target line). Tracked separately if desired.
- Tier-aware 5h headroom (high-tier accounts can burn more per 5h).
  The current `compute_burn_per_window` ignores tier multipliers; this
  redesign keeps that as-is. Add later if it proves necessary.
- Multi-account simultaneous use (still one active account at a time).
- Predictive scheduling using historical burst patterns. Pure
  reactive: route work to most-at-risk capacity *now*.
