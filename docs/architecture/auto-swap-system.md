# Auto-Swap System Architecture

**Last updated:** 2026-06-10
**Status:** Living document — update when the system changes

## Changelog

- **2026-06-10** — Refresh-deadlock fix + auto-swap utilization/churn/observability
  pass. (a) Per-account refresh locking now lives SOLELY in `_refresh_token_flow`
  with a bounded 60s acquire; callers must NEVER hold the lock around
  `refresh_account_token` (see § Token Refresh Architecture → Locking invariant);
  the post-refresh profile fetch moved outside the lock (its 401 path re-enters
  the flow — self-deadlock); `main.py` sweep loops self-heal via
  `asyncio.wait_for(..., 600)`. (b) Quiet-hours executor guard formally REMOVED —
  swaps execute 24/7 (dead `active_start`/`active_end` params dropped from
  `_execute_swap`). (c) `RESET_SUPPRESS_MINUTES` = 30, shared with selection's
  `_FIVE_H_HEADROOM_RESET_MIN`; invariant: lookahead <= suppression.
  (d) Min-residency 900s for proactive departures; manual switches arm it too
  (`note_external_swap` + 15-min pause). (e) New departure rule 1b: intra-T0
  preemption. (f) Deadline-aware T1 target (floor 90). (g) T0 candidates bypass
  the `has_viable_headroom` floor when deficit >= 1. (h) Symmetric tier
  hysteresis. (i) Trigger taxonomy extended (`cooldown_blocked`,
  `residency_blocked`, `no_target`, `swap_aborted`, `emergence_pending`,
  `intra_tier_preempted`). (j) `swap_log` gains `status` + `residency_seconds`;
  `/swap-log` returns `swaps_last_24h`. (k) Drain advisor WS event
  (`expiring_with_stranded_capacity`) — advisor only, no auto-burn worker.
  (l) Stall pattern (d) demoted to `same_tier_deficit_advisory`. (m) Burn-rate
  decay ungated. (n) Emergence persistence keyed by tier. Plus: swap-failure
  exponential backoff, credential writes via `asyncio.to_thread`, T0 poll-interval
  clamp, candidate staleness override, numeric sort key, bounded route/fetch
  timeouts. See plan
  `docs/superpowers/plans/2026-06-10-usage-refresh-deadlock-and-autoswap-fixes.html`.
- **2026-05-04** — Replaced score-based selection with tier-strict deadline-aware
  selection. Added anti-jitter hardening (hysteresis + emergence persistence +
  stall watchdog). See spec
  `docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md`.
- **2026-04-06** — Prior unified-decision-engine design (deficit-aware weighted
  scoring via `score_candidate`). Superseded by the 2026-05-04 redesign.

## Purpose

Automatically manage multiple Claude AI accounts to maximize total usable
capacity. The system:
1. Keeps the user on the best account at all times
2. Never wastes expiring capacity (especially 7-day windows)
3. Respects API rate limits
4. Coordinates safely with Claude Code's credential system

## Core Principle: Tier-strict, deadline-aware selection

**Among accounts with usable headroom and behind their tier target, prefer
the one closest to its 7d deadline.**

There is one decision per tick, answered in two parts:

1. `pick_best_target(...)` — scan all non-active accounts, return the one with
   (a) the most-urgent tier, (b) the earliest 7d expiry within that tier,
   (c) the largest deficit-vs-target as final tiebreak. No weighting, no
   scoring; just sort.
2. `should_swap_now(...)` — given the active account and the candidate from
   (1), decide whether to leave the active account at all this tick. The
   default is "stay" (ride out the current 5h window). Departure rules are
   spelled out below.

The proactive-vs-defensive split is gone. There is no separate scanner, no
`score_candidate`, no urgency thresholds. Tiers and tier targets do all the
work.

## Account Utilization Model

### Windows
- **5-hour window:** Resets to 0% instantly at `cached_5h_resets_at`. Opens
  when an API call is made. Short-lived — not the scarce resource.
- **7-day window:** Resets to 0% instantly at `cached_7d_resets_at`. The
  SCARCE resource. Takes a full week to reset. Unused capacity is
  permanently lost.

### Constraints
- **5h-to-7d burn rate cap:** Each 5h window can only burn a fraction of 7d
  capacity. With the default 17 working hours/day (06:00-23:00) that's
  ~3.4 windows/day, ~23.8 windows/week — each window burns ~4.2% of 7d
  capacity at maximum (`compute_burn_per_window`).
- **Active hours:** Default 06:00–23:00. Active hours feed math, not
  gating: (a) `has_viable_headroom` / `compute_burn_per_window` use them
  to compute the 5h-burn floor that candidates must clear; (b) the
  deadline-aware T1 target and `achievable_burn` / `stranding_estimate`
  use them to project what's still burnable; (c) the window keeper only
  pings during them. **The swap executor itself runs 24/7** — the
  quiet-hours executor guard was removed 2026-06-10: a credential swap
  costs nothing while the user is idle and pre-positions the fleet for
  morning. 5h windows open on the first API call, not on credential
  placement, so a sleep-time swap wastes nothing. Tier classification
  and white-bar both use wall-clock time and are NOT affected by active
  hours.
- **Per-account opt-out:** `auto_swap_enabled = 0` excludes an account from
  the candidate pool. The flag does not prevent the account from being
  active — it only prevents the algorithm from swapping TO it.
- **One account active at a time:** All Claude Code sessions share the
  same active account.

### Tier Definitions (per-account, wall-clock to 7d expiry)

| Tier | Time to 7d expiry | Target 7d usage | Selection priority |
|------|-------------------|-----------------|--------------------|
| **T0** | < 24h | 100% (drain) | highest |
| **T1** | 24–48h | deadline-aware, floor 90% (see below) | high |
| **T2** | 48h–4d (96h) | white_bar + 5% lead | medium |
| **T3** | 4d–7d (96–168h) | white_bar (floor) | lowest |
| **T4** | no data / expired | — | excluded |

`tier_for(account, prev_tier=...)` returns the tier index (0..4). Boundaries
belong to the higher-numbered tier (exactly 24h is T1, exactly 48h is T2).
T4 is the sentinel for "no usable 7d data" or "already expired" — those
accounts are removed from the candidate pool.

### White Bar (per-account, wall-clock)

```
white_bar(account) = clamp01((now - (resets_at - 7d)) / 7d)
```

No active-hours adjustment. Linear. The scheduler's view of "expected 7d
usage so far" mirrors what the user sees on the dashboard, so visual and
algorithmic state never diverge. (See spec § "White bar" for derivation.)

### Tier Target & Deficit

```
target_for_tier(tier, account) = case tier:
    T0 -> 100.0
    T1 -> max(90.0, 100.0 - min(10.0, achievable_final_day_burn))
    T2 -> min(100.0, white_bar * 100 + 5.0)
    T3 -> white_bar * 100

deficit(account) = target_for_tier(damped_tier, account) - cached_usage_7d
```

`target_for_tier(tier, account, now, active_start, active_end)` takes the
(possibly hysteresis-damped) tier from the caller instead of recomputing
it, so selection's admit math stays consistent with the tier the account
was ranked at. `target_7d` is the classify-then-delegate convenience
wrapper (no hysteresis).

**Deadline-aware T1 target (2026-06-10):** `achievable_final_day_burn` is
the effective working hours between local midnight of the expiry day and
the expiry, in 5h windows, times `compute_burn_per_window`. A fixed 90
strands the 10% buffer whenever expiry lands outside working hours (e.g.
03:00 local: nothing is burnable on the expiry day, so T1 must drain
toward 100 directly). Never returns below the `T1_TARGET` (90) floor.

Positive deficit → account is behind tier target → eligible for selection.
Non-positive deficit → at/above tier target → skip.

T0/T1 targets are **drain-to** goals (we want everything we can get from
this account before its window resets). T2/T3 targets are **floors** — the
algorithm's job is to keep usage at-or-above the white bar, but extra is
welcome.

## Why tier-strict selection (vs continuous loss-rate score)

Capacity not burned before a 7d reset is lost forever. Per-tier expected
hourly loss-rate is `deficit / hours_to_expiry`. T0's denominator is always
the smallest, so a T0 candidate with deficit always has the highest
loss-rate per hour. Discrete tiers and a continuous loss-rate score yield
the same answer when there's room everywhere — discrete is debuggable, has
a stable user mental model, and matches the dashboard's tier badge.
A weighted score collapses this contrast and historically (pre-2026-05-04)
preferred far-from-expiry accounts. We deleted the weighted score.

## Decision Flow (Per Tick)

```
1. Adaptive interval lapses; fetch active-account usage, bounded by a 50s
   timeout (tick continues on cached data on timeout), with 401/429
   recovery — see Rate Limit Management. The interval is then clamped by
   `_clamp_poll_interval`: <=90s whenever an otherwise-eligible non-active
   T0 with positive deficit exists; <=60s when such a 5h-saturated T0's
   reset lands within the next interval (be on time for the reset).
2. Push fresh data to dashboard via WebSocket (`usage_poll_updated`).
3. Refresh non-active candidate usage if its `usage_cached_at` is older
   than `_CANDIDATE_STALENESS_SECONDS` (600s) OR
   `_candidate_staleness_override` fires (cached 5h >= 90 but the 5h
   reset is already past, or the 7d window reset behind the cache —
   a window reset means the staleness clock lies). Each fetch is bounded
   by `_CANDIDATE_FETCH_TIMEOUT_SECONDS` (30s) + catch-all so one bad
   candidate can't stall the pass.
4. `pick_best_target(accounts, current_id, prev_tiers=_last_observed_tiers)`
   - filters: not active; is_active=1; is_deleted=0;
     consecutive_failures<3; validation_status!="invalid";
     cc_access_token not None; auto_swap_enabled!=0; tier!=T4;
     deficit>0 (computed against the DAMPED tier via target_for_tier);
     has_viable_headroom — BYPASSED for T0 candidates with deficit>=1
     (drain-to-100 must not strand the final slice below the
     burn-per-window floor); `_has_5h_headroom` (a PAST 5h reset counts
     as headroom — the >=90 cached usage is provably stale).
   - sort key (`_SortKey`): `(tier_index, resets_at_epoch, -deficit)`.
     resets_at is parsed to UTC epoch seconds (inf on failure) — lex
     string sort diverges from chronological for mixed ISO formats.
   - returns the unique min, or None.
5. `should_swap_now(active, best, burn_rate, ...)` returns either None
   ("stay") or a reason string from a fixed prefix vocabulary.
6. Anti-jitter: `_apply_emergence_persistence(...)` may force an emerge
   reason (`higher tier emerged` / `intra-tier preemption:`) back to None
   this tick if the emerged TIER hasn't held the best spot for
   `_EMERGENCE_PERSISTENCE_TICKS` (=2) consecutive ticks. Fast path: a
   >=2-tier gap (damped-T0 best vs damped-T2+ active) skips persistence —
   that gap cannot be boundary jitter. Other reasons fire immediately.
7. Gate chain (first match wins; every gate records a distinct trigger):
   - Min-residency: proactive reasons (emerged / intra-tier / burn-rate)
     are blocked until the active account has held the slot for
     `_MIN_RESIDENCY_SECONDS` (900s) since the last committed swap or
     manual switch. Drained / 5h-critical are forced departures — exempt.
     Trigger: `residency_blocked`.
   - Cooldown: `(now - _last_swap_time) < _SWAP_COOLDOWN_SECONDS` (300s).
     Trigger: `cooldown_blocked`.
   - No target: reason fired but `best is None` → log warning, broadcast
     `all_accounts_exhausted`. Trigger: `no_target`.
   - Failure backoff: after a failed credential write, retries are gated
     by exponential backoff (60s * 2^(n-1), cap 600s) instead of
     retry-next-tick. Trigger: `swap_aborted`.
8. Else → `_execute_swap(active -> best)`:
   - TOCTOU guard; record swap_log row as 'pending' with
     residency_seconds; credential write OFF the event loop via
     `asyncio.to_thread` (lock acquisition sleeps + keychain subprocesses,
     ~7.5s worst case); resolve row to 'committed' (reset backoff, clean
     burn-rate state, sync active_account_id, broadcast
     `auto_swap_triggered`) or 'failed' (arm backoff, broadcast
     `auto_swap_failed`). Trigger on success: from `_trigger_for_reason`;
     on failure: `swap_aborted`.
9. Silent-stall watchdog: `_evaluate_stall(...)` increments
   `_consecutive_no_best_ticks` if any of three stall patterns matched
   (see § Anti-Jitter Hardening). At >= `_STALL_TICK_THRESHOLD` (=10)
   consecutive stuck ticks, escalate to `logger.error` and broadcast
   `auto_swap_stall`. Cooldown between repeat warnings:
   `_STALL_WARNING_COOLDOWN_SECONDS` (1800s). Separately, the same-tier
   deficit advisory (INFO + WS, 30-min cooldown) covers intended-behavior
   same-tier stays — see § Anti-Jitter Hardening.
10. Drain advisor (`_drain_advisor_tick`): for every T0 account projected
    to strand >2% of 7d capacity (`stranding_estimate`), broadcast
    `expiring_with_stranded_capacity` (per-account 30-min cooldown).
    Purely informational — never affects the decision; failures are
    swallowed. Advisor only BY DESIGN: no auto-burn worker. Routing real
    work beats burning quota on no-ops.
11. `db.record_decision(...)` returns the inserted row id;
    `decision_log_entry` WS event broadcast with id, action, trigger
    (the explicit `_decision_trigger` set by the branch taken — no longer
    re-derived by parsing the reason string), reason, detail, candidate
    summaries (now including `stranding`, with target/deficit computed
    against the damped tier).
```

There is no quiet-hours step: swaps execute 24/7 (see § Constraints →
Active hours for the rationale).

## Swap Triggers (decision-log taxonomy)

The decision-log `trigger` field is one of these values. Swap triggers
are computed by `_trigger_for_reason(reason)` from the prefix of
`should_swap_now`'s reason string; stay triggers are set explicitly by
the gate that blocked the swap (`_decision_trigger`), so blocked attempts
are queryable by cause instead of by reason-string grep. Reason-string
prefixes (`REASON_PREFIX_*` in `jacked/web/auto_swap/selection.py`) are
part of the public contract — do not change without updating both ends.

| Trigger | Fired by | Meaning |
|---------|----------|---------|
| `tier_drained` | `REASON_PREFIX_DRAINED` | Active hit its T0/T1 drain-to target. Move on. |
| `higher_tier_emerged` | `REASON_PREFIX_HIGHER_TIER` | A strictly-higher-tier candidate appeared AND the emergence-persistence streak met. |
| `intra_tier_preempted` | `REASON_PREFIX_INTRA_TIER` | Intra-T0 preemption: an earlier-expiring T0 candidate with materially higher loss-rate preempted the active T0 (rule 1b). |
| `forced_critical` | `REASON_PREFIX_FIVE_H` | Active 5h ≥ critical (and 5h reset NOT imminent — see Suppression). |
| `burn_rate` | `REASON_PREFIX_BURN_RATE` | Burn-rate projection: usage_5h ≥ warning AND projected to cross critical within `2 × check_interval`. |
| `tier_aware` | (catch-all) | Reason string didn't match any prefix. Rare; indicates a code drift between selection.py and usage_monitor.py. |
| `tick` | reason is None | Plain stay — nothing fired. Recorded every tick so the log isn't silent. |
| `emergence_pending` | stay branch | An emerge reason was suppressed this tick by the persistence streak (count < 2). |
| `residency_blocked` | stay branch | Proactive reason fired but min-residency (900s) hasn't elapsed. |
| `cooldown_blocked` | stay branch | Reason fired but the 300s post-swap cooldown is active. |
| `no_target` | stay branch | Reason fired but no eligible target exists (exhaustion path). |
| `swap_aborted` | stay branch | Failure backoff active, or `_execute_swap` failed (TOCTOU / credential write). |

### Departure rules (`should_swap_now`)

In order; first match wins.

1. **Higher-tier candidate emerged.** `tier_for(best) < tier_for(active)`
   AND `tier_for(best) != TIER_EXCLUDED`. Active treated as `T3+1` when
   its own tier is excluded, so any real-tier candidate beats an unclassified
   active account. **Same-tier (except rule 1b) or lower-tier candidate
   never overrides mid-window.** Rules 1/1b are the only rules that can
   override 5h-reset suppression — a fresh T0 deserves the swap.
   - **Rule 1b — Intra-T0 preemption** (added 2026-06-10). Strict tier
     inequality alone lets an active T0 with a long runway block a T0
     candidate expiring in hours — the largest stranding mechanism
     observed. Preempt when `best` expires strictly earlier than the
     active, its deficit is >= 5%, and its loss-rate
     (`deficit / hours_to_expiry`) is >= 1.5x the active's. The margin
     gates prevent ping-pong between near-equal T0s.
2. **Drained.** Active tier is T0 or T1 AND `usage_7d >=
   target_for_tier(active_tier, active)` — the deadline-aware T1 target,
   computed against the hysteresis-damped active tier. Only fires for
   T0/T1 because their targets are drain-to goals; T2/T3 targets are
   floors and being above them is the desired state.
3. **5h critical.** `usage_5h >= effective_critical_5h` (max of the
   user-configured `auto_swap_5h_critical` and `tier_critical_threshold`)
   AND 5h reset is NOT within `RESET_SUPPRESS_MINUTES` (30 min). Also
   evaluated when the active has no 7d data (TIER_EXCLUDED) — exhaustion
   alerting must not depend on 7d cache presence.
4. **Burn-rate projection.** `usage_5h >= warning_5h` AND projected to
   cross critical within `2 × check_interval_min` AND 5h reset not
   imminent.

If none fire and the active 5h has not reset, **stay**. This is the
anti-flap rule — riding out the 5h window saves prompt-cache and avoids
opening a fresh window mid-burst.

### Suppression
- **5h reset imminent:** Suppresses `forced_critical` and `burn_rate` only.
  `higher_tier_emerged`, `intra_tier_preempted` and `tier_drained` still
  fire — a T0 emerging is worth eating the cache cost.
  `RESET_SUPPRESS_MINUTES` = 30 and is SHARED with selection's
  imminent-reset lookahead (`_FIVE_H_HEADROOM_RESET_MIN =
  RESET_SUPPRESS_MINUTES`). **Invariant: lookahead <= suppression.** An
  account admitted because its 5h reset is N minutes out must keep
  5h-critical suppressed for those N minutes after the swap, else
  `should_swap_now`'s 5h-critical rule ejects it immediately —
  deterministic ping-pong.
- **Min-residency:** `_MIN_RESIDENCY_SECONDS` (900s) since the last
  COMMITTED swap or manual switch blocks proactive departures
  (emerged / intra-tier / burn-rate). Drained and 5h-critical are forced
  departures and bypass it. Manual switches arm the clock via
  `note_external_swap()` (called from `use_account` in
  `routes/auth.py`, which also pauses the sweep loop for 15 minutes) so
  auto-swap can't silently revert a user-chosen account within minutes.
- **Cooldown:** `_SWAP_COOLDOWN_SECONDS` (300s) is the safety floor against
  pathological flapping (data jitter, race with manual intervention).
  Governs COMMITTED swaps only — failed credential writes are paced by
  the failure backoff instead.
- **Failure backoff:** failed swap attempts (credential write failures)
  arm an exponential backoff: `60s * 2^(count-1)`, capped at 600s. A
  committed swap resets the count. Replaces the old retry-next-tick
  behavior.

There is no active-hours executor guard (removed 2026-06-10) — swaps
execute 24/7. A credential swap while idle costs nothing and pre-positions
the fleet for morning; 5h windows open on the first API call, not on
credential placement.

## Anti-Jitter Hardening

Three layers defend against single-tick noise (Anthropic timestamp drift
of ±30s near a tier boundary, transient API errors, etc.). All three
pieces of state are cleared by `reset_locks()` on lifespan restart so a
tray restart starts with a fresh observation.

### 1. Tier hysteresis (`tier_for(account, prev_tier=...)`) — symmetric since 2026-06-10

When transitioning *toward* a more-urgent tier (T1→T0, T2→T1, T3→T2),
require the account to be at least `_TIER_HYSTERESIS_MIN` (5 minutes)
past the boundary before flipping. **Symmetric damping** also applies to
single-step *less-urgent* flips (e.g. prev=T0, now=T1 within 5 min past
the 24h boundary): real time only moves accounts MORE urgent, so a
single-step less-urgent flip is refetch noise on `cached_7d_resets_at`.
Un-damped, it fires a premature rule-2 `drained` (target jumps 100→90)
followed by a pull-back swap. Jumps of more than one step flip
immediately — that's a genuine window reset, not jitter. State held in
module-level `_last_observed_tiers: dict[int, int]` in
`usage_monitor.py`, refreshed each tick from observations and pruned of
dead account ids.

### 2. Emergence persistence (`_apply_emergence_persistence`) — tier-keyed since 2026-06-10

An emerge reason (`higher tier emerged` or `intra-tier preemption:`)
from `should_swap_now` does not act immediately. The emerged TIER must
remain the best candidate's tier for `_EMERGENCE_PERSISTENCE_TICKS = 2`
consecutive ticks first. The streak is keyed by tier (state:
`_emerged_tier_streak: {"tier": int | None, "count": int}`), NOT by
account id — two near-tied candidates in the same tier alternating as
best would reset each other's id-keyed streak forever. Fast path: a
>=2-tier gap (damped-T0 best vs damped-T2+ active) skips persistence
entirely — that gap cannot be boundary jitter. Other reasons (`drained`,
`5h critical`, `burn_rate`) fire immediately — only the emergence path
is gated, because only that path is susceptible to single-tick boundary
jitter.

### 3. Silent-stall watchdog (`_evaluate_stall`)

Detects three patterns where the loop would be productively stuck:
- **Multi-account stale:** stay + no best + active data is stale + at
  least one other account exists (we're not picking up new candidate
  data).
- **Single-account forced-out:** only one account total, departure reason
  fired but no target (literally nowhere to go).
- **Drained-no-candidate:** any reason fired but `best is None` (active
  is exhausted, no eligible candidate).

When any pattern matches the counter `_consecutive_no_best_ticks`
increments; otherwise it resets to 0. At
`_STALL_TICK_THRESHOLD = 10` consecutive stuck ticks, escalate to
`logger.error` and broadcast `auto_swap_stall` over WebSocket.
`_STALL_USAGE_STALENESS_SECONDS = 1800` defines what "stale" means
for active-account data; `_STALL_WARNING_COOLDOWN_SECONDS = 1800`
throttles repeat warnings.

**Demoted (2026-06-10):** the former stall pattern (d) —
same-tier-stay-with-meaningful-deficit — is NOT a stall. Per spec,
same-tier-never-overrides is intended behavior; flagging it at ERROR
trains the operator to ignore the watchdog. It is now the **same-tier
deficit advisory** (`_same_tier_advisory_applies`): when the decision is
`stay`, no reason fired, and the best candidate is >=
`_SAME_TIER_DEFICIT_THRESHOLD` (15%) behind its tier target AND its tier
is T0/T1 (harvestable drain-to targets; T2/T3 targets are floors, so
sitting behind them is not actionable), log at INFO and broadcast
`same_tier_deficit_advisory` with an independent 30-min cooldown
(`_SAME_TIER_ADVISORY_COOLDOWN_SECONDS`). With intra-T0 preemption
implemented, T0-vs-T0 cases may legitimately swap; the advisory covers
the remaining same-tier-stay cases.

## Adaptive Polling

The active account is polled at an interval determined by urgency.
Helpers live in `jacked/web/auth.py::compute_urgency_tier`.

| Tier | Usage State | Interval |
|------|------------|----------|
| Idle | <50% 5h, burn rate ~0 | 5 min |
| Normal | <70% or low burn | 2.5 min |
| Warning | 70-85% or projects critical in 15 min | 90s |
| Critical | >85% or projects critical in 5 min | 65s |

- 7d > 80% bumps up one tier
- ±15% jitter on each tick
- After 3+ consecutive 429s, force idle (stale data makes urgency
  unreliable)
- **T0 clamp (`_clamp_poll_interval`, 2026-06-10):** <=90s whenever any
  otherwise-eligible non-active T0 with positive deficit exists (a
  draining deadline must not wait out a 5-min idle interval); <=60s when
  such a 5h-saturated T0's reset lands within the next interval
  (event-driven re-entry approximation — be on time for the reset).

**Burn-rate decay (ungated 2026-06-10):** after 5+ consecutive unchanged
ticks the tracked rates decay by 0.8x per tick at ANY usage level —
unchanged usage is direct evidence burn stopped. The old gate (decay only
below the 5h warning threshold) froze the rate exactly when the user went
idle at >=80%, firing spurious burn-rate swaps.

**Non-active accounts:** NOT polled in the background. Usage is fetched
on-demand:
- Every tick if `usage_cached_at` is older than
  `_CANDIDATE_STALENESS_SECONDS` (600s); stable rows are skipped — unless
  `_candidate_staleness_override` forces a refetch: cached 5h >= 90 but
  the 5h reset is already past (the saturation is stale; the account may
  now be the most usable candidate — removes the 5-10 min blind spot
  right after a window reset), or the 7d window reset behind the cache
  (`needs_7d_ping` — fresh window means fresh tier classification).
- At swap time (`_execute_swap` re-reads target).
- At exhaustion time (for recovery estimates).
- When user clicks manual refresh.

## Rate Limit Management

### Coordinator (`fetch_usage`)
- Hard ceiling: max 1 request per 65 seconds per account
- `manual=True` bypasses ceiling (user clicked Refresh)
- All callers go through the same entry point

### 401 Auto-Refresh
On HTTP 401/403 from the usage API, `fetch_usage` runs a recovery chain
(single retry depth):
1. **Primary token refresh:** `_try_refresh_primary_token` via
   `_refresh_token_flow(PRIMARY_CIRCUIT_BREAKER)`. Uses the DB circuit
   breaker — will not attempt refresh if active.
2. **Live credential import:** If refresh fails, call
   `reconcile_credentials_from_live_store` to import tokens that Claude
   Code may have refreshed. If a fresh `access_token` is found, retry
   the usage fetch.
3. **Mark invalid:** If both fail, mark the account
   `validation_status="invalid"`.

Less aggressive invalid-marking: `refresh_account_token` requires 2
consecutive 401/403 failures before marking invalid. First failure
records the error and sets the circuit breaker cooldown but does NOT
mark invalid.

### 429 Recovery
1. **Token refresh:** Rate limits are per-access-token. Exchange refresh
   token for fresh access token (clears the rate limit) via
   `_try_refresh_on_429` → `_refresh_token_flow(CC_OR_PRIMARY_429)`.
2. **Escalating backoff:** 65s → 130s → 260s → 520s → cap 900s on
   consecutive 429s.
3. **Tier override:** After 3+ consecutive 429s, force idle tier.
4. **Active-only credential write:** `sync_credential_to_all_stores` is
   only called when the refreshed account IS the currently active
   account. Writing for a non-active account would silently switch
   Claude Code to the wrong account.

### Cross-Process Locking
- Lock: `os.mkdir(~/.claude.lock)` (atomic, same protocol as
  `proper-lockfile`).
- PID file inside for stale detection.
- 5 retries with 1-2s jittered delay.
- Claude Code detects `.credentials.json` mtime change and re-reads.

## Credential Management

### Before Every Swap
1. **Reconcile outgoing:** `reconcile_credentials_from_live_store` reads
   live credentials from Keychain/file, imports rotated tokens into DB.
2. **Write incoming:** `sync_credential_to_all_stores` writes to DB,
   `.credentials.json`, Keychain.

### Token Priority
- CC tokens preferred (`cc_access_token`, `cc_refresh_token`)
- Primary tokens as fallback (`access_token`, `refresh_token`)
- If CC expired + no CC refresh → fall through to primary (sets
  `refreshToken: null` to prevent Claude Code from consuming primary
  refresh).

### `invalid_grant` Recovery
- Before clearing `cc_refresh_token`, check live credential store.
- If Claude Code refreshed successfully, import the fresh token.
- Only clear if no live recovery available.

## Token Refresh Architecture

All token refresh paths funnel through a single orchestrator:
`_refresh_token_flow(account_id, db, mode)`.

### `RefreshMode` Enum

| Mode | Token Set | Lock | Timeout | Circuit Breaker | Cred Stores | Caller |
|------|-----------|------|---------|-----------------|-------------|--------|
| `PRIMARY` | primary | async per-account | 30s | No | No | `refresh_account_token` |
| `CC` | cc | async per-account CC | 30s | No | No | `refresh_cc_token` |
| `CC_OR_PRIMARY_429` | cc → primary | cross-process | 15s | No | If active | `_try_refresh_on_429` |
| `PRIMARY_CIRCUIT_BREAKER` | primary | async per-account | 15s | Yes | No | `_try_refresh_primary_token` |

### Lock Sharing
- **CC modes** share the CC lock (`_get_cc_refresh_lock(account_id)`).
- **PRIMARY modes** share the primary lock (`_get_refresh_lock(account_id)`).
- **Lock nesting for `CC_OR_PRIMARY_429`:** acquire async CC lock first,
  then (if active account) the cross-process Claude lock for credential
  store writes. This order prevents deadlocks.

### Locking invariant (2026-06-10 deadlock fix)

**Per-account refresh locking lives SOLELY inside `_refresh_token_flow`.
Callers must NEVER hold the per-account refresh lock around
`refresh_account_token` (or any path into `_refresh_token_flow`).** The
locks are non-reentrant `asyncio.Lock`s — a caller holding the lock
around the call self-deadlocks the moment the flow tries to acquire the
same lock internally. This is exactly the bug that froze the server:
`refresh_all_expiring_tokens` and `heal_invalid_accounts` wrapped the
call in `async with lock:`. Callers may *peek* (`lock.locked()`) for a
fast-skip, but never hold.

Defense in depth, in layers:

1. **Bounded acquire:** `_refresh_token_flow` acquires the lock via
   `asyncio.wait_for(lock.acquire(), timeout=60)`
   (`_REFRESH_LOCK_ACQUIRE_TIMEOUT`). On timeout it aborts the attempt
   with `TokenExchangeResult(success=False, error="lock_timeout")` and
   logs at ERROR — a wedged holder can no longer suspend every later
   refresh/401-recovery path for that account until restart.
2. **Profile fetch outside the lock:** the PRIMARY-mode post-refresh
   `fetch_profile` runs AFTER lock release (step 4l). Its 401 path
   re-enters `_refresh_token_flow` on the same primary lock — running it
   while holding the lock self-deadlocks (this was the root cause of the
   original wedge).
3. **Bounded sweep callers:** `refresh_all_expiring_tokens` bounds each
   per-account refresh with `wait_for(..., 60)` and counts timeouts as
   failed; `heal_invalid_accounts` calls `_refresh_token_flow` directly
   (the flow owns locking) and bounds heal validation with
   `wait_for(..., 60)`.
4. **Self-healing loops:** `main.py`'s `_token_refresh_loop` and
   `_heal_sweep_loop` wrap each whole pass in
   `asyncio.wait_for(..., SWEEP_PASS_TIMEOUT=600)`. `wait_for` cancels
   the inner task on timeout, which releases any held per-account lock —
   the loop self-heals from a wedged pass instead of dying until server
   restart.
5. **Bounded HTTP routes:** the single-account routes
   (`/refresh-token` 45s, `/refresh-usage` 60s, `/validate` 60s) return
   a deterministic 504 (`REFRESH_TIMEOUT` / `VALIDATE_TIMEOUT`) instead
   of holding the request open; the usage route also resets
   `validation_status` to "unknown" so the row doesn't sit at
   "checking".

### DB Retry
All modes use 3x exponential backoff for the DB write after a successful
token exchange. Prevents a transient SQLite lock from wasting a
successfully-exchanged token.

## Circuit Breaker

Prevents repeated refresh attempts against tokens that are known-bad.
DB-persisted via two columns on the `accounts` table:

- `refresh_last_failed_at` — Unix timestamp of last failure (or NULL).
- `refresh_failure_type` — Error classification string (or NULL).

### Scaled Cooldowns

| Error Type | Cooldown | Rationale |
|-----------|----------|-----------|
| `invalid_grant` | 600s (10 min) | Token revoked — retrying wastes quota |
| `network_error` | 60s (1 min) | Transient — retry quickly |
| `http_429` | 120s (2 min) | Rate limited — give upstream time |
| `http_5xx` | 120s (2 min) | Server error — moderate wait |
| (default) | 300s (5 min) | Unknown error — conservative fallback |

The circuit breaker always expires. There is no permanent block — even
`invalid_grant` retries after its cooldown. The heal loop clears CB
state before recovery attempts, so accounts are never permanently stuck.

## Live Credential Reconciliation

`reconcile_credentials_from_live_store` imports tokens that Claude Code
may have refreshed independently.

### When It Runs
- During swaps, before writing new credentials.
- Periodically in `refresh_all_expiring_tokens` (every 30 min) for the
  active account.
- On-demand in the account list API (with 30s cache).

### Safety Rules
- **`invalid_grant` guard:** Never imports `cc_refresh_token` when CB
  shows `invalid_grant` — that token is Claude Code's active session.
- **`_jackedAccountId` gate:** Always enforced. Live credentials must
  carry a `_jackedAccountId` matching the account being reconciled.

## Heal Loop

Runs every 5 minutes (each pass bounded by `SWEEP_PASS_TIMEOUT=600` in
`main.py`). Processes accounts with `validation_status` in
(`"invalid"`, `"unknown"`).

1. Fast-skip if the per-account lock is held (`lock.locked()` peek —
   never acquired here; see § Token Refresh Architecture → Locking
   invariant).
2. Clear circuit breaker (`refresh_last_failed_at=NULL`,
   `refresh_failure_type=NULL`).
3. Attempt token exchange via `_refresh_token_flow(PRIMARY)` directly —
   NOT `refresh_account_token`, whose `should_refresh()` gate reports
   success without exchanging when the token isn't near expiry, marking
   accounts healed without proving the credentials work (phantom heal).
   The flow owns locking, so no lock is held by the caller.
4. If refresh fails, try `reconcile_credentials_from_live_store`.
5. Validate via profile fetch (`validate_account`, bounded 60s) — works
   for API key accounts too. Timeout counts as not healed.
6. Mark `validation_status` accordingly (heal must explicitly set
   "valid" — see `lessons.md`).

## Window Keeper

Runs on the sweep loop timer (`usage_check_interval`). Only pings; does
NOT fetch usage.

- Checks `needs_ping` (5h expired) AND `needs_7d_ping` (7d reset with
  stale data) for each account — either triggers a ping.
- Pings via direct `httpx.POST` to messages API (haiku, max_tokens=1).
- After ping, fetches usage to update cached reset timestamps.
- Only during active hours or pre-wake window.

## Dashboard Integration

### WebSocket Events

| Event | Payload | Purpose |
|-------|---------|---------|
| `usage_poll_updated` | Account data (whitelisted), `_poll_interval`, `_poll_tier`, `_last_poll_at` | Active account data refresh |
| `auto_swap_triggered` | `from_label`, `to_label`, reason | Swap occurred — persistent banner |
| `auto_swap_failed` | `from_account_id`, `to_account_id`, `reason`, `failure` | Credential write failed — failure backoff armed |
| `all_accounts_exhausted` | Recovery estimate | No viable accounts |
| `auto_swap_stall` | `active_account_id`, `consecutive_ticks`, `last_fetch_age_seconds` | Stall watchdog tripped |
| `expiring_with_stranded_capacity` | `account_id`, `email`, `label`, `deficit`, `achievable`, `stranding`, `resets_at` | Drain advisor: T0 account projected to strand >2% of 7d capacity. Advisor only — see below. |
| `same_tier_deficit_advisory` | `active_account_id`, `best_account_id`, `best_tier`, `best_deficit` | Intended-behavior same-tier stay with a T0/T1 candidate >=15% behind target (demoted stall pattern d) |
| `usage_refresh_started` / `usage_refresh_progress` | progress | Bulk refresh |
| `decision_log_entry` | `id`, `account_id`, `email`, `label`, `action`, `trigger`, `reason`, `timestamp`, `detail` | Real-time per-tick decision |

**Drain advisor is advisor-only by design.** There is deliberately NO
auto-burn worker that spends stranded capacity on synthetic requests:
routing real work toward the expiring account (which the advisory enables
the user to do) beats burning quota on no-ops. Per-account 30-min
cooldown (`_DRAIN_ADVISOR_COOLDOWN_SECONDS`); stranding threshold 2%
(`_DRAIN_ADVISOR_STRANDING_THRESHOLD`); `stranding_estimate = max(0,
deficit_vs_target - achievable_burn)`.

`_WS_SAFE_FIELDS` controls which account columns are broadcast in
`usage_poll_updated`. New DB columns must be explicitly whitelisted.

### Decision Log
- Recorded every poll tick in `decision_log` (queryable "why" history).
- `record_decision` returns the inserted row id, used in the WebSocket
  push (`decision_log_entry`).
- Records: active state, departure reason, ALL candidates evaluated
  (with tier/target_7d/deficit/is_best), final decision.
- Three actions: `stay`, `swap`, `manual_switch`.
- 7-day retention with deterministic prune (every 500 ticks or 1%
  random).
- API: `GET /api/settings/decision-log?limit=N&action=swap&action=manual_switch`.
- Frontend: expandable table, color-coded action badges, default filter
  shows only swaps + manual, toggle reveals all ticks.
- Blocked swaps ARE recorded with a cause-specific trigger
  (`residency_blocked`, `cooldown_blocked`, `no_target`, `swap_aborted`,
  `emergence_pending`) so blocked attempts aren't invisible.

### Swap Log (audit trail)

- `swap_log` rows carry `status` (`pending` → `committed`/`failed`) and
  `residency_seconds` (how long the outgoing account held the active
  slot, measured from the previous committed swap or manual switch; NULL
  when unknown). The row is written as `pending` BEFORE the credential
  write so the audit trail survives a write failure, then resolved.
  Legacy rows migrate with `status='committed'`.
- API: `GET /api/settings/swap-log` returns
  `{"swaps": [...], "swaps_last_24h": N}` — the trailing-24h count is
  committed-only and is the headline churn metric (watch it after
  deploys).

## File Responsibilities

| File | Responsibility |
|------|----------------|
| `jacked/web/auto_swap/tiers.py` | `tier_for` (symmetric hysteresis), `white_bar`, `target_for_tier`, `_t1_deadline_target`, `target_7d`, `deficit_vs_target`, `_resolve_now`. Constants: `TIER_T0..TIER_EXCLUDED`, `T1_TARGET` (floor), `T2_LEAD`, `_TIER_BOUNDARIES_HOURS`, `_TIER_HYSTERESIS_MIN`. |
| `jacked/web/auto_swap/selection.py` | `pick_best_target`, `should_swap_now` (incl. rule 1b intra-T0 preemption), `_has_5h_headroom`, `_SortKey`, `_epoch_or_inf`, `_hours_to_expiry`, `_FIVE_H_HEADROOM_RESET_MIN` (= `RESET_SUPPRESS_MINUTES`). Reason-prefix constants: `REASON_PREFIX_HIGHER_TIER`, `REASON_PREFIX_INTRA_TIER`, `REASON_PREFIX_DRAINED`, `REASON_PREFIX_FIVE_H`, `REASON_PREFIX_BURN_RATE`, `REASON_PREFIX_NO_DATA`. |
| `jacked/web/auto_swap/burn.py` | `BurnRate`, `update_burn_rate`, `_resets_within`, `RESET_SUPPRESS_MINUTES=30` (invariant comment lives here), `has_viable_headroom`, `compute_effective_working_hours`, `compute_burn_per_window`. |
| `jacked/web/auto_swap/diagnostics.py` | `compute_7d_deficit` (diagnostic dict for decision-log candidate dump), `achievable_burn`, `stranding_estimate`, `format_account_label`, `tier_label`, `tier_critical_threshold`. |
| `jacked/api/usage_monitor.py` | `active_account_poll_loop` + helpers `_apply_emergence_persistence`, `_evaluate_stall`, `_same_tier_advisory_applies`, `_trigger_for_reason`, `_clamp_poll_interval`, `_candidate_staleness_override`, `_drain_advisor_tick`, `_swap_backoff_remaining`, `note_external_swap`. Module-level state: `_last_observed_tiers`, `_emerged_tier_streak`, `_consecutive_no_best_ticks`, `_last_stall_warning`, `_last_committed_swap_time`, `_swap_failure_count`, `_drain_advisor_last_sent`, `_same_tier_advisory_last`. Constants: `_EMERGENCE_PERSISTENCE_TICKS=2`, `_STALL_TICK_THRESHOLD=10`, `_STALL_USAGE_STALENESS_SECONDS=1800`, `_STALL_WARNING_COOLDOWN_SECONDS=1800`, `_SWAP_COOLDOWN_SECONDS=300`, `_MIN_RESIDENCY_SECONDS=900`, `_SWAP_FAILURE_BACKOFF_BASE_SECONDS=60`, `_SWAP_FAILURE_BACKOFF_CAP_SECONDS=600`, `_CANDIDATE_STALENESS_SECONDS=600`, `_CANDIDATE_FETCH_TIMEOUT_SECONDS=30`, `_DRAIN_ADVISOR_COOLDOWN_SECONDS=1800`, `_DRAIN_ADVISOR_STRANDING_THRESHOLD=2.0`, `_SAME_TIER_ADVISORY_COOLDOWN_SECONDS=1800`, `_SAME_TIER_DEFICIT_THRESHOLD=15.0`. `_execute_swap` helper with TOCTOU guard + pending/committed/failed swap_log status + credential write via `asyncio.to_thread` (`_write_swap_credentials`) + failure backoff. |
| `jacked/web/auth.py` | Usage coordinator: `fetch_usage`, rate limiting, 429 recovery, token refresh, `compute_urgency_tier`. |
| `jacked/api/credential_helpers.py` | Credential I/O: reconcile, sync, cross-process lock, keychain. |
| `jacked/web/window_keeper.py` | Ping logic, schedule helpers. |
| `jacked/api/routes/settings_swap.py` | Settings API with validation. |
| `jacked/data/web/js/websocket.js` | WebSocket event handlers. |
| `jacked/data/web/js/components/accounts.js` | Account cards, countdown timer. |
| `jacked/data/web/js/components/auto-swap.js` | Settings panel, swap log table. |
| `jacked/data/web/js/components/account-actions.js` | Refresh, countdown tick. |

## Observability

All state transitions produce structured log lines. This is the
observability contract — automation can rely on these existing.

### Decision Loop
- **Stall warning:** `"Auto-swap stalled: %d consecutive ticks with no candidate and stale active-account data (active=%d, last_fetch=%ss ago)"` — logged at `error` level when `_consecutive_no_best_ticks >= _STALL_TICK_THRESHOLD`.
- **Swap fired:** `"Auto-swap: switching from account %d (5h=%.1f%%) to account %d (5h=%.1f%%) — %s [%s]"` — `info` level; trailing tag is the trigger taxonomy value.
- **No-target warning:** `"Auto-swap needed but no eligible target (active account %d at 5h=%.1f%%)"` — `warning`, throttled by `_EXHAUSTION_COOLDOWN_SECONDS`.
- **Swap write failed:** `"Swap recorded but credential write failed — retry gated by failure backoff, attempt %d (account %d -> %d)"` — `warning`.
- **Drain advisor:** `"Drain advisor: account %d (%s) expires with ~%.1f%% stranded capacity (deficit=%.1f%%, achievable=%.1f%%, resets_at=%s)"` — `info`.
- **Same-tier advisory:** `"Same-tier deficit advisory: best id=%s (tier=%s) is %.1f%% behind its tier target but same-tier-never-overrides keeps the loop on stay (active=%d)"` — `info` (deliberately not ERROR; intended behavior).

### Refresh / Heal Self-Healing
- **Lock acquire timeout:** `"Account %d: could not acquire %s refresh lock within %ds — possible wedged holder; aborting this refresh attempt"` — `error`.
- **Sweep pass cancelled:** `"Token refresh pass exceeded %ds and was cancelled"` / `"Heal sweep exceeded %ds and was cancelled"` — `error`; the loop continues next interval.
- **Per-account refresh timeout:** `"Account %d: refresh timed out after 60s — counting as failed"` — `error`.

### Circuit Breaker
- **Activating:** `"Account %d: circuit breaker active (%s, %ds remaining)"`.
- **Expiring:** `"Account %d: circuit breaker cooldown expired, re-attempting refresh"`.

### Token Refresh
- **Stale token short-circuit:** `"Account %d: token already refreshed by another path"` — another coroutine refreshed while we waited for the lock.

### Heal Loop
- **Clearing CB:** `"Account %d: clearing circuit breaker for heal attempt"`.

### Poll Loop Watchdog
- **Overdue tick:** `"Active poll loop delayed — last tick %ds ago, expected interval %ds"`.

## Known Limitations

- **5h tier weighting:** `compute_burn_per_window` ignores the
  account's tier multiplier (e.g. 20x can burn more per 5h than 5x).
  Headroom math is therefore conservative for high-tier accounts.
- **Clock skew:** `_resets_within` and `tier_for` assume an
  NTP-synchronized system clock within ~1 minute. Tier hysteresis
  absorbs the typical Anthropic API jitter (±30s) but won't help with
  larger drift.
- **Multi-instance:** No coordination between multiple jacked instances
  managing the same account set.
- **User activity signal:** No concept of whether the user is actively
  coding. Swaps execute 24/7 by design (a swap while idle costs nothing
  and pre-positions the fleet); min-residency + cooldown bound churn
  while the user IS working.
- **DST transitions:** `compute_7d_deficit` uses a rough UTC offset
  (`datetime.now()` minus `datetime.now(timezone.utc)`). Can be off by
  1 hour during the ~1-second DST transition. The selection rule itself
  is unaffected — it uses tz-aware UTC throughout.
