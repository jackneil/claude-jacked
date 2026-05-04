# Auto-Swap System Architecture

**Last updated:** 2026-05-04
**Status:** Living document — update when the system changes

## Changelog

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
- **Active hours:** Default 06:00–23:00. Active hours feed two things:
  (a) the swap *executor* refuses to run outside them (sleep-time swaps
  waste a fresh 5h window opening); (b) `has_viable_headroom` /
  `compute_burn_per_window` use them to compute the 5h-burn floor that
  candidates must clear. Tier classification and white-bar both use
  wall-clock time and are NOT affected by active hours.
- **Per-account opt-out:** `auto_swap_enabled = 0` excludes an account from
  the candidate pool. The flag does not prevent the account from being
  active — it only prevents the algorithm from swapping TO it.
- **One account active at a time:** All Claude Code sessions share the
  same active account.

### Tier Definitions (per-account, wall-clock to 7d expiry)

| Tier | Time to 7d expiry | Target 7d usage | Selection priority |
|------|-------------------|-----------------|--------------------|
| **T0** | < 24h | 100% (drain) | highest |
| **T1** | 24–48h | 90% (10% buffer) | high |
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
target_7d(account) = case tier(account):
    T0 -> 100.0
    T1 ->  90.0
    T2 -> min(100.0, white_bar * 100 + 5.0)
    T3 -> white_bar * 100

deficit(account) = target_7d(account) - cached_usage_7d
```

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
1. Adaptive interval lapses; fetch active-account usage (with 401/429
   recovery — see Rate Limit Management).
2. Push fresh data to dashboard via WebSocket (`usage_poll_updated`).
3. Refresh non-active candidate usage if its `usage_cached_at` is older
   than `_CANDIDATE_STALENESS_SECONDS` (600s). Stable accounts are skipped
   to avoid burning API quota on every tick.
4. `pick_best_target(accounts, current_id, prev_tiers=_last_observed_tiers)`
   - filters: not active; is_active=1; is_deleted=0;
     consecutive_failures<3; validation_status!="invalid";
     cc_access_token not None; auto_swap_enabled!=0; has_viable_headroom;
     `_has_5h_headroom`; tier!=T4; deficit_vs_target>0
   - sort key (`_SortKey`): `(tier_index, resets_at_iso, -deficit)`
   - returns the unique min, or None.
5. `should_swap_now(active, best, burn_rate, ...)` returns either None
   ("stay") or a reason string from a fixed prefix vocabulary.
6. Anti-jitter: `_apply_emergence_persistence(...)` may force the reason
   back to None this tick if it's a `higher tier emerged` reason and the
   candidate hasn't held the spot for `_EMERGENCE_PERSISTENCE_TICKS`
   (=2) consecutive ticks. Other reasons fire immediately.
7. Cooldown check: if `(now - _last_swap_time) < _SWAP_COOLDOWN_SECONDS`
   (=300s), the action is downgraded to "stay" with `cooldown (...)`
   suffix on the reason. The decision log still records.
8. Branch:
   - `reason is None` → record `stay` (with detail about why best
     wasn't actionable).
   - cooldown active → record `stay` with cooldown reason.
   - `best is None` but reason fired → record `stay`, log warning,
     broadcast `all_accounts_exhausted`.
   - else → `_execute_swap(active -> best)`, record `swap`.
9. Silent-stall watchdog: `_evaluate_stall(...)` increments
   `_consecutive_no_best_ticks` if any of three stall patterns matched
   (see § Anti-Jitter Hardening). At >= `_STALL_TICK_THRESHOLD` (=10)
   consecutive stuck ticks, escalate to `logger.error` and broadcast
   `auto_swap_stall`. Cooldown between repeat warnings:
   `_STALL_WARNING_COOLDOWN_SECONDS` (1800s).
10. `db.record_decision(...)` returns the inserted row id;
    `decision_log_entry` WS event broadcast with id, action, trigger
    (from `_trigger_for_reason(reason)`), reason, detail, candidate
    summaries.
11. Outside active hours, the *executor* refuses to write credentials —
    the decision is still recorded so the log shows "would swap, but
    quiet hours". (Suppression at this layer; the algorithm above is
    unaware of clock-time.)
```

## Swap Triggers (decision-log taxonomy)

The decision-log `trigger` field is one of these values, computed by
`_trigger_for_reason(reason)` from the prefix of `should_swap_now`'s
reason string. Reason-string prefixes (`REASON_PREFIX_*` in
`jacked/web/auto_swap/selection.py`) are part of the public contract — do
not change without updating both ends.

| Trigger | Fired by | Meaning |
|---------|----------|---------|
| `tier_drained` | `REASON_PREFIX_DRAINED` | Active hit its T0/T1 drain-to target. Move on. |
| `higher_tier_emerged` | `REASON_PREFIX_HIGHER_TIER` | A strictly-higher-tier candidate appeared AND the emergence-persistence streak met. |
| `forced_critical` | `REASON_PREFIX_FIVE_H` | Active 5h ≥ critical (and 5h reset NOT imminent — see Suppression). |
| `burn_rate` | `REASON_PREFIX_BURN_RATE` | Burn-rate projection: usage_5h ≥ warning AND projected to cross critical within `2 × check_interval`. |
| `tier_aware` | (catch-all) | Reason string didn't match any prefix. Rare; indicates a code drift between selection.py and usage_monitor.py. |
| `tick` | reason is None | `_decision_action == "stay"`. Recorded every tick so the log isn't silent. |

### Departure rules (`should_swap_now`)

In order; first match wins.

1. **Higher-tier candidate emerged.** `tier_for(best) < tier_for(active)`
   AND `tier_for(best) != TIER_EXCLUDED`. Active treated as `T3+1` when
   its own tier is excluded, so any real-tier candidate beats an unclassified
   active account. **Same-tier or lower-tier candidate never overrides
   mid-window.** This is the only rule that can override 5h-reset
   suppression — a fresh T0 deserves the swap.
2. **Drained.** Active tier is T0 or T1 AND `usage_7d >= target_7d(active)`.
   Only fires for T0/T1 because their targets are drain-to goals; T2/T3
   targets are floors and being above them is the desired state.
3. **5h critical.** `usage_5h >= effective_critical_5h` (max of the
   user-configured `auto_swap_5h_critical` and `tier_critical_threshold`)
   AND 5h reset is NOT within `RESET_SUPPRESS_MINUTES` (10 min).
4. **Burn-rate projection.** `usage_5h >= warning_5h` AND projected to
   cross critical within `2 × check_interval_min` AND 5h reset not
   imminent.

If none fire and the active 5h has not reset, **stay**. This is the
anti-flap rule — riding out the 5h window saves prompt-cache and avoids
opening a fresh window mid-burst.

### Suppression
- **5h reset imminent:** Suppresses `forced_critical` and `burn_rate` only.
  `higher_tier_emerged` and `tier_drained` still fire — a T0 emerging is
  worth eating the cache cost.
- **Cooldown:** `_SWAP_COOLDOWN_SECONDS` (300s) is the safety floor against
  pathological flapping (data jitter, race with manual intervention).
  With the new departure rules + anti-jitter, hitting cooldown should be
  rare — when it triggers, it's logged.
- **Active-hours executor guard:** Outside `window_keeper_active_start..end`,
  the swap executor refuses to write. Decision still recorded.

## Anti-Jitter Hardening

Three layers defend against single-tick noise (Anthropic timestamp drift
of ±30s near a tier boundary, transient API errors, etc.). All three
pieces of state are cleared by `reset_locks()` on lifespan restart so a
tray restart starts with a fresh observation.

### 1. Tier hysteresis (`tier_for(account, prev_tier=...)`)

When transitioning *toward* a more-urgent tier (T1→T0, T2→T1, T3→T2),
require the account to be at least `_TIER_HYSTERESIS_MIN` (5 minutes)
past the boundary before flipping. Movement *away* from urgency
(T0→T1, T1→T2) flips immediately — only the dangerous "becoming more
urgent" direction is dampened. State held in module-level
`_last_observed_tiers: dict[int, int]` in `usage_monitor.py`, refreshed
each tick from observations and pruned of dead account ids.

### 2. Emergence persistence (`_apply_emergence_persistence`)

A `higher tier emerged` reason from `should_swap_now` does not act
immediately. The candidate must remain the best target for
`_EMERGENCE_PERSISTENCE_TICKS = 2` consecutive ticks first. State held
in `_emerged_target_streak: dict[int, int]`. Other reasons (`drained`,
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

**Non-active accounts:** NOT polled in the background. Usage is fetched
on-demand:
- Every tick if `usage_cached_at` is older than
  `_CANDIDATE_STALENESS_SECONDS` (600s); stable rows are skipped.
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

Runs every 5 minutes. Processes accounts with `validation_status` in
(`"invalid"`, `"unknown"`).

1. Clear circuit breaker under per-account lock
   (`refresh_last_failed_at=NULL`, `refresh_failure_type=NULL`).
2. Attempt token refresh via `refresh_account_token` — no
   `should_refresh()` gate. Healing is recovery mode.
3. If refresh fails, try `reconcile_credentials_from_live_store`.
4. Validate via profile fetch (`validate_account`) — works for API key
   accounts too.
5. Mark `validation_status` accordingly (heal must explicitly set
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
| `all_accounts_exhausted` | Recovery estimate | No viable accounts |
| `auto_swap_stall` | `active_account_id`, `consecutive_ticks`, `last_fetch_age_seconds` | Stall watchdog tripped |
| `usage_refresh_started` / `usage_refresh_progress` | progress | Bulk refresh |
| `decision_log_entry` | `id`, `account_id`, `email`, `label`, `action`, `trigger`, `reason`, `timestamp`, `detail` | Real-time per-tick decision |

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
- Cooldown-blocked swaps ARE recorded (so blocked attempts aren't
  invisible).

## File Responsibilities

| File | Responsibility |
|------|----------------|
| `jacked/web/auto_swap/tiers.py` | `tier_for`, `white_bar`, `target_7d`, `deficit_vs_target`, `_resolve_now`. Constants: `TIER_T0..TIER_EXCLUDED`, `T1_TARGET`, `T2_LEAD`, `_TIER_BOUNDARIES_HOURS`, `_TIER_HYSTERESIS_MIN`. |
| `jacked/web/auto_swap/selection.py` | `pick_best_target`, `should_swap_now`, `_has_5h_headroom`, `_SortKey`. Reason-prefix constants: `REASON_PREFIX_HIGHER_TIER`, `REASON_PREFIX_DRAINED`, `REASON_PREFIX_FIVE_H`, `REASON_PREFIX_BURN_RATE`, `REASON_PREFIX_NO_DATA`. |
| `jacked/web/auto_swap/burn.py` | `BurnRate`, `update_burn_rate`, `_resets_within`, `RESET_SUPPRESS_MINUTES`, `has_viable_headroom`, `compute_effective_working_hours`, `compute_burn_per_window`. |
| `jacked/web/auto_swap/diagnostics.py` | `compute_7d_deficit` (diagnostic dict for decision-log candidate dump), `format_account_label`, `tier_label`, `tier_critical_threshold`. |
| `jacked/api/usage_monitor.py` | `active_account_poll_loop` + helpers `_apply_emergence_persistence`, `_evaluate_stall`, `_trigger_for_reason`. Module-level state: `_last_observed_tiers`, `_emerged_target_streak`, `_consecutive_no_best_ticks`, `_last_stall_warning`. Constants: `_EMERGENCE_PERSISTENCE_TICKS=2`, `_STALL_TICK_THRESHOLD=10`, `_STALL_USAGE_STALENESS_SECONDS=1800`, `_STALL_WARNING_COOLDOWN_SECONDS=1800`, `_SWAP_COOLDOWN_SECONDS=300`, `_CANDIDATE_STALENESS_SECONDS=600`. `_execute_swap` helper with TOCTOU guard + cross-process lock + partial-swap recovery. |
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
  coding. The active-hours executor guard approximates "don't swap
  while the user sleeps".
- **DST transitions:** `compute_7d_deficit` uses a rough UTC offset
  (`datetime.now()` minus `datetime.now(timezone.utc)`). Can be off by
  1 hour during the ~1-second DST transition. The selection rule itself
  is unaffected — it uses tz-aware UTC throughout.
