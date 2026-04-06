# Auto-Swap System Architecture

**Last updated:** 2026-04-06
**Status:** Living document — update when the system changes

## Purpose

Automatically manage multiple Claude AI accounts to maximize total usable capacity. The system:
1. Keeps the user on the best account at all times
2. Never wastes expiring capacity (especially 7-day windows)
3. Respects API rate limits
4. Coordinates safely with Claude Code's credential system

## Core Principle: Unified Decision Engine

**Every poll tick asks one question: "Am I on the best account right now?"**

This is NOT two separate systems (defensive + proactive). It is ONE evaluation that considers ALL factors simultaneously:

- **Defensive pressure:** Is the current account approaching its limits?
- **Proactive opportunity:** Is there another account with expiring capacity I should burn?
- **Target quality:** Which alternative account maximizes overall value?
- **Stay value:** Is the current account itself the best option (e.g., it has expiring capacity to burn)?

If the best account is the current one → stay.
If a different account is clearly better → swap.

## Account Utilization Model

### Windows
- **5-hour window:** Resets to 0% instantly at `cached_5h_resets_at`. Opens when an API call is made. Short-lived — not the scarce resource.
- **7-day window:** Resets to 0% instantly at `cached_7d_resets_at`. The SCARCE resource. Takes a full week to reset. Unused capacity is permanently lost.

### Constraints
- **5h-to-7d burn rate cap:** Each 5h window can only burn a fraction of 7d capacity. With 17 working hours/day (06:00-23:00), that's ~3.4 windows/day, ~23.8 windows/week, so each window burns ~4.2% of 7d capacity at maximum.
- **Active hours:** Normalized defaults are 06:00-23:00. The system uses `compute_effective_working_hours` which iterates day-by-day, counting only hours within [active_start, active_end). All pure-decision functions (`compute_burn_per_window`, `compute_7d_deficit`, `compute_urgency_threshold`, `has_viable_headroom`, `score_candidate`, `pick_best_target`) default their `active_start`/`active_end` params to `"06:00"`/`"23:00"`.
- **Per-account opt-out:** Each account has an `auto_swap_enabled` flag (default 1). Accounts with `auto_swap_enabled == 0` are excluded from candidate selection in `pick_best_target`. The flag does not prevent an account from being the active account — it only prevents the system from swapping TO it.
- **One account active at a time:** All Claude Code sessions share the same active account.

### 7-Day Deficit Model
For each account, track how far behind schedule its 7-day utilization is:

```
window_start = cached_7d_resets_at - 7 days
elapsed_fraction = working_hours_elapsed / total_working_hours_in_window
expected_usage = elapsed_fraction × 100%
deficit = expected_usage - actual_usage
```

Positive deficit = account is behind schedule (underutilized, wasting capacity).
This drives proactive rotation throughout the week, not just at the end.

### Capacity Waste Model (Proactive Scheduling)

The deficit model answers "how far behind schedule?" but the proactive scheduler needs to answer a different question: **"how much capacity will be permanently lost if we don't act?"**

Key derived values from `compute_7d_deficit`:
```
effective_hours_remaining  = working hours until 7d reset (respects active hours, skips overnight)
effective_windows_remaining = effective_hours_remaining / 5.0
unused_7d                  = 100% - actual_usage
burn_per_window            = 100% / (7 × working_hours_per_day / 5.0)  ≈ 4.2%
recoverable                = min(unused_7d, windows_remaining × burn_per_window)
```

`recoverable` is the maximum capacity we can save by swapping to this account NOW. The rest of `unused_7d` is already unrecoverable (not enough windows left to burn it). Even partial recovery is valuable — every percent recovered is real Claude usage.

**Urgency tiers** based on remaining 5-hour windows:

| Windows Remaining | Tier | Threshold | Rationale |
|---|---|---|---|
| < 1 | CRITICAL | deficit > 0 | Last chance. Any unused capacity is about to be lost forever. |
| 1 – 2 | HIGH | deficit > burn_per_window (~4%) | Only 1-2 shots. Swap if there's meaningful capacity to recover. |
| 3 – 4 | MEDIUM | deficit > 2 × burn_per_window (~8%) | Several windows left but time is running out. |
| 5+ | NORMAL | deficit > PROACTIVE_SWAP_THRESHOLD (15%) | Plenty of time. Only swap for significant deficits. |

**Urgency score** (first-class metric for proactive scheduling):
```
urgency_score = recoverable / max(effective_hours_remaining, 0.5)
```
When multiple accounts have expiring capacity, the system picks the one with the highest urgency score. This favors accounts where the most capacity is wasting per hour of inaction. The `0.5` floor prevents division-by-zero when an account is in its final minutes.

**Active hours matter:** A 7d window expiring at 3 AM with the user at 10 PM has only 1 effective hour remaining (10-11 PM), not 5 calendar hours. The overnight gap means there's only 0.2 windows left — CRITICAL tier. `compute_effective_working_hours` handles this automatically.

**Example:** Account at 86% 7d, resets in 5.7 effective hours (1.14 windows):
- Unused: 14%. Recoverable: min(14, 1.14 × 4.2) = 4.8%.
- Tier: HIGH (1-2 windows). Threshold: 4.2%.
- Deficit: 9.2% > 4.2% → **swap triggers**.
- Without this model: deficit 9.2% < old fixed threshold 15% → swap would NOT trigger, 4.8% of capacity permanently wasted.

**Viability floor:** Even if an account has positive deficit, it's filtered out when `recoverable < burn_per_window` — swapping to an account that can't fill even one full window is disruptive and risks immediate exhaustion. Also filtered: accounts with `unused_7d < burn_per_window` via `has_viable_headroom` (applied to both defensive AND proactive paths in `pick_best_target`).

## Decision Flow (Per Tick)

```
1. Fetch active account usage (adaptive interval)
2. Push fresh data to dashboard via WebSocket (usage_poll_updated)
3. Compute: should I consider switching?
   a. Defensive triggers: 5h critical, 7d threshold, burn-rate projection
   b. Window-aware suppression: don't swap away if reset is imminent
   c. Stale-data guard: don't trust usage if older than the reset timestamp
   d. Deficit-aware: 7d trigger suppressed if current account has positive deficit
      (we intentionally placed the user here to burn expiring capacity)
4. Score ALL candidate accounts (unified scoring):
   a. Filter: usage below 7d threshold, OR imminent 7d reset,
      OR urgency (deficit > 0 AND < 24 working hours remaining)
   b. Base score from 5h/7d usage (lower usage = higher score)
   c. Tier-aware headroom bonus
   d. Inactive/expired 5h window bonus
   e. Imminent 5h reset bonus (up to +30)
   f. 7d deficit bonus (behind schedule = higher score, deficit × 0.5)
   g. Staleness penalty (old data = lower score)
5. Compare best candidate against staying:
   a. If defensive trigger fired → swap to best candidate
   b. If no defensive trigger but best candidate has high 7d deficit
      AND active account is comfortable → proactive swap (uses same pick_best_target)
   c. Otherwise → stay
6. Record decision (ALL ticks — stay AND swap):
   a. `db.record_decision()` returns the inserted row ID
   b. Broadcast `decision_log_entry` via WebSocket with ID, action, reason, detail
   c. Non-passing candidates include `skip_reason` in detail:
      `near_exhaustion`, `recoverable_too_low`, `ahead_of_schedule`, `below_threshold`
7. Execute swap if decided (via `_execute_swap` helper):
   a. Fetch fresh usage for target (on-demand, done by caller before execute)
   b. TOCTOU guard — re-read active account ID, abort if changed
   c. Record swap with reason + arm cooldown (audit trail survives credential failure)
   d. Reconcile outgoing credentials (capture token rotation)
   e. Sync credentials to all stores under cross-process lock
   f. Clean up burn-rate state for both accounts
   g. Broadcast via WebSocket (includes org-aware `from_label`/`to_label`)
   h. If credential write failed, reset cooldown so next tick retries immediately
```

## Swap Triggers

### Defensive (active account in trouble)
- **5h critical:** `usage_5h >= tier_critical_threshold` (80-95% depending on tier)
- **7d threshold:** `usage_7d >= 85%`
- **Burn-rate projection:** Warning zone + projected to hit critical within 2× check interval

### Proactive (capacity optimization)
- **7d capacity waste:** Scans ALL non-active accounts for expiring capacity using the Capacity Waste Model. Threshold scales down with urgency — from 15% (5+ windows left) to 0% (last window). Active account must be comfortable (`usage_5h < warning_5h`). Picks the most urgent candidate (highest `recoverable / hours_remaining`).
- **Manual:** User clicks "Set Active" in the dashboard. Recorded in both `swap_log` (trigger=manual) and `decision_log` (action=manual_switch), and broadcast via WebSocket.

### Target Viability Guards (applied to ALL swap paths)
- **Viable headroom:** `has_viable_headroom` — reject targets whose unused 7d capacity is less than one 5h window's burn (~4.2%). Prevents swapping to near-exhausted accounts that would crash sessions.
- **Minimum recoverable (proactive only):** Reject candidates where `recoverable < burn_per_window`. Not worth swapping for scraps that won't fill even one window.
- **Time-of-day (proactive only):** Skip proactive swaps within `MIN_PROACTIVE_MINUTES` (30) of active hours end. Not worth opening a 5h window for a few minutes of use.

### Suppression
- **Window-aware:** Don't swap away if `cached_5h_resets_at` or `cached_7d_resets_at` is within `RESET_SUPPRESS_MINUTES` (10 min) — the reset will fix it for free
- **Deficit-aware:** Don't fire 7d defensive trigger on accounts with positive deficit (we intentionally placed the user here to burn expiring capacity — prevents ping-ponging)
- **Escape hatch:** Override suppression if a candidate scores > `SUPPRESS_OVERRIDE_SCORE` (100). Passes `account` to `should_swap` to preserve deficit suppression during escape evaluation.
- **Stale-data guard:** If reset happened but usage data is older than the reset → suppress (data is unreliable)

## Scoring Model (`score_candidate`)

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

When data is stale, the reset proximity bonus is killed (set to 0).

## Adaptive Polling

The active account is polled at an interval determined by urgency:

| Tier | Usage State | Interval |
|------|------------|----------|
| Idle | <50% 5h, burn rate ~0 | 5 min |
| Normal | <70% or low burn | 2.5 min |
| Warning | 70-85% or projects critical in 15 min | 90s |
| Critical | >85% or projects critical in 5 min | 65s |

- 7d > 80% bumps up one tier
- ±15% jitter on each tick
- After 3+ consecutive 429s, force idle (stale data makes urgency unreliable)

**Non-active accounts:** NOT polled in the background. Usage is fetched on-demand:
- At swap time (before scoring candidates)
- At exhaustion time (for recovery estimates)
- On first auto-swap tick (prime the pump)
- When user clicks manual refresh

## Rate Limit Management

### Coordinator (`fetch_usage`)
- Hard ceiling: max 1 request per 65 seconds per account
- `manual=True` bypasses ceiling (user clicked Refresh)
- All callers go through the same entry point

### 401 Auto-Refresh
On HTTP 401/403 from the usage API, `fetch_usage` runs a recovery chain (single retry depth):
1. **Primary token refresh:** `_try_refresh_primary_token` via `_refresh_token_flow(PRIMARY_CIRCUIT_BREAKER)`. Uses DB circuit breaker — will not attempt refresh if the circuit breaker is active (see Circuit Breaker section).
2. **Live credential import:** If refresh fails, call `reconcile_credentials_from_live_store` to import tokens that Claude Code may have refreshed. If a fresh `access_token` is found, retry the usage fetch.
3. **Mark invalid:** If both fail, mark the account `validation_status="invalid"`.

Less aggressive invalid-marking: `refresh_account_token` requires 2 consecutive 401/403 failures before marking invalid (checks `refresh_failure_type` for prior `http_401`/`http_403`). First failure records the error and sets the circuit breaker cooldown but does NOT mark invalid.

### 429 Recovery
1. **Token refresh:** Rate limits are per-access-token. Exchange refresh token for fresh access token (clears the rate limit) via `_try_refresh_on_429` → `_refresh_token_flow(CC_OR_PRIMARY_429)`.
2. **Escalating backoff:** 65s → 130s → 260s → 520s → cap 900s on consecutive 429s
3. **Tier override:** After 3+ consecutive 429s, force idle tier
4. **Active-only credential write:** `sync_credential_to_all_stores` is only called when the refreshed account IS the currently active account. Writing credentials for a non-active account would overwrite `.credentials.json` and silently switch Claude Code to the wrong account. Non-active accounts get DB-only updates.

### Cross-Process Locking
- Lock: `os.mkdir(~/.claude.lock)` (atomic, same protocol as `proper-lockfile`)
- PID file inside for stale detection
- 5 retries with 1-2s jittered delay
- Claude Code detects `.credentials.json` mtime change and re-reads

## Credential Management

### Before Every Swap
1. **Reconcile outgoing:** `reconcile_credentials_from_live_store` reads live credentials from Keychain/file, imports rotated tokens into DB (see Live Credential Reconciliation section)
2. **Write incoming:** `sync_credential_to_all_stores` writes to DB, `.credentials.json`, Keychain

### Token Priority
- CC tokens preferred (`cc_access_token`, `cc_refresh_token`)
- Primary tokens as fallback (`access_token`, `refresh_token`)
- If CC expired + no CC refresh → fall through to primary (sets `refreshToken: null` to prevent Claude Code from consuming primary refresh)

### `invalid_grant` Recovery
- Before clearing `cc_refresh_token`, check live credential store
- If Claude Code refreshed successfully, import the fresh token
- Only clear if no live recovery available

## Token Refresh Architecture

All token refresh paths funnel through a single orchestrator: `_refresh_token_flow(account_id, db, mode)`.

### `RefreshMode` Enum

| Mode | Token Set | Lock | Timeout | Circuit Breaker | Cred Stores | Caller |
|------|-----------|------|---------|-----------------|-------------|--------|
| `PRIMARY` | primary | async per-account | 30s | No | No | `refresh_account_token` |
| `CC` | cc | async per-account CC | 30s | No | No | `refresh_cc_token` |
| `CC_OR_PRIMARY_429` | cc → primary | cross-process | 15s | No | If active | `_try_refresh_on_429` |
| `PRIMARY_CIRCUIT_BREAKER` | primary | async per-account | 15s | Yes | No | `_try_refresh_primary_token` |

### Lock Sharing
- **CC modes** (`CC`, `CC_OR_PRIMARY_429`): share the CC lock (`_get_cc_refresh_lock(account_id)`)
- **PRIMARY modes** (`PRIMARY`, `PRIMARY_CIRCUIT_BREAKER`): share the primary lock (`_get_refresh_lock(account_id)`)
- **Lock nesting for `CC_OR_PRIMARY_429`:** Acquires the async CC lock first, then if the active account, acquires the cross-process Claude lock (`os.mkdir(~/.claude.lock)`) for credential store writes. This order prevents deadlocks.

### Flow Steps (inside `_refresh_token_flow`)
1. Read account from DB
2. Resolve which refresh token to use (based on mode)
3. Acquire the appropriate lock
4. Re-read account under lock (detect if another coroutine already refreshed)
5. Check circuit breaker (`PRIMARY_CIRCUIT_BREAKER` only)
6. Exchange via `_exchange_refresh_token` (low-level POST helper, unchanged)
7. On success: atomic DB write with retry (3x exponential backoff, always on)
8. On failure: record circuit breaker state in DB (for CB-enabled modes)

### DB Retry
All modes use 3x exponential backoff for the DB write after a successful token exchange. This prevents a transient SQLite lock from wasting a successfully-exchanged token.

### `_exchange_refresh_token`
The low-level POST helper. Sends `grant_type=refresh_token` to Anthropic's OAuth endpoint. Returns `TokenExchangeResult` with success/failure, new tokens, error type, and status code. Not modified by the refactor — all behavioral differences live in `_refresh_token_flow`.

## Circuit Breaker

Prevents repeated refresh attempts against tokens that are known-bad. DB-persisted (survives restarts) via two columns on the `accounts` table:

- `refresh_last_failed_at` — Unix timestamp of last failure (or NULL if healthy)
- `refresh_failure_type` — Error classification string (or NULL if healthy)

### Scaled Cooldowns by Error Type

| Error Type | Cooldown | Rationale |
|-----------|----------|-----------|
| `invalid_grant` | 600s (10 min) | Token is revoked — retrying wastes quota and risks rate limits |
| `network_error` | 60s (1 min) | Transient — retry quickly |
| `http_429` | 120s (2 min) | Rate limited — give upstream time to recover |
| `http_5xx` | 120s (2 min) | Server error — moderate wait |
| (default) | 300s (5 min) | Unknown error — conservative fallback |

### No Permanent "Dead" State
The circuit breaker always expires. There is no permanent block — even `invalid_grant` will be retried after its cooldown. The heal loop (see below) clears circuit breaker state before recovery attempts, so accounts are never permanently stuck.

### Circuit Breaker Lifecycle
1. **Activating:** `_refresh_token_flow` records `refresh_last_failed_at` + `refresh_failure_type` on exchange failure
2. **Blocking:** Subsequent `PRIMARY_CIRCUIT_BREAKER` calls check the cooldown and return `error="circuit_breaker"` without hitting the network
3. **Expiring:** Cooldown passes → next attempt proceeds normally
4. **Clearing:** Heal loop clears both columns under per-account lock before recovery

## Live Credential Reconciliation

`reconcile_credentials_from_live_store` (renamed from `reconcile_outgoing_credentials`) imports tokens that Claude Code may have refreshed independently.

### When It Runs
- **During swaps:** Before writing new credentials (captures outgoing account's rotated tokens)
- **Periodically:** In `refresh_all_expiring_tokens` (every 30 min) for the active account
- **On-demand:** In the account list API for the active account (with 30s cache to avoid Keychain subprocess spam)

### What It Imports
- `cc_access_token` — always (non-destructive, just updates our view)
- `cc_expires_at` — always (metadata)
- `cc_refresh_token` — **only if** `refresh_failure_type != "invalid_grant"` in DB

### Safety Rules
- **`invalid_grant` guard:** Never imports `cc_refresh_token` when the circuit breaker shows `invalid_grant`. The live refresh token is Claude Code's active session token — importing and exchanging it would destroy Claude Code's session.
- **`_jackedAccountId` gate:** Always enforced, never skipped. Live credentials must have a `_jackedAccountId` field matching the account being reconciled. This prevents importing tokens that belong to a different account (e.g., after a manual credential switch outside jacked).

## Heal Loop

Runs every 5 minutes. Processes accounts with `validation_status` in (`"invalid"`, `"unknown"`).

### Recovery Steps (per account)
1. **Clear circuit breaker** under per-account lock (`refresh_last_failed_at=NULL`, `refresh_failure_type=NULL`). This ensures the subsequent refresh attempt isn't blocked by stale CB state.
2. **Attempt token refresh** via `refresh_account_token` — no `should_refresh()` gate. Healing is recovery mode; always attempts regardless of token expiry.
3. **If refresh fails:** Try `reconcile_credentials_from_live_store` to import tokens Claude Code may have refreshed.
4. **Validate via profile fetch** (`validate_account`) — works for API key accounts too.
5. **Mark result:** healed (valid) or confirmed-invalid.

### Design Rationale
- Dropping the `should_refresh()` gate means accounts with non-expired but invalid tokens still get recovery attempts.
- Clearing the circuit breaker first is critical — without it, `_refresh_token_flow(PRIMARY)` would see the CB state and skip the exchange, making the heal loop unable to recover.

## Window Keeper

Runs on the sweep loop timer (`usage_check_interval`). Only pings — does NOT fetch usage.

- Checks `needs_ping` (5h expired) AND `needs_7d_ping` (7d reset with stale data) for each account — either triggers a ping
- The 5h and 7d windows don't always line up — the 7d can reset mid-5h-window, leaving it "floating" until the next API call. `needs_7d_ping` catches this by comparing `cached_7d_resets_at` against `usage_cached_at`.
- Pings via direct `httpx.POST` to messages API (haiku, max_tokens=1). The same ping starts both windows simultaneously.
- After successful ping, fetches usage to update cached reset timestamps
- Only during active hours or pre-wake window

## Dashboard Integration

### WebSocket Events

| Event | Payload | Purpose |
|-------|---------|---------|
| `usage_poll_updated` | Account data (whitelisted), `_poll_interval`, `_poll_tier`, `_last_poll_at` | Active account data refresh |
| `auto_swap_triggered` | `from_label`, `to_label`, reason | Swap occurred — persistent banner (5 min, dismissible) |
| `all_accounts_exhausted` | Recovery estimate | No viable accounts — exhaustion banner |
| `usage_refresh_started` | — | Bulk refresh begun |
| `usage_refresh_progress` | Per-account progress | Bulk refresh per-account status |
| `decision_log_entry` | `id`, `account_id`, `email`, `label`, `action`, `trigger`, `reason`, `timestamp`, `detail` | New decision recorded — real-time dashboard update |

**Field whitelisting:** `_WS_SAFE_FIELDS` controls which account columns are broadcast in `usage_poll_updated`. New DB columns don't leak by default — they must be explicitly added to the whitelist.

### Poll Countdown

The backend is the single source of truth for poll timing. The frontend does NOT compute its own interval.

- **Backend sends:** `_poll_interval`, `_poll_tier`, `_last_poll_at` in every `usage_poll_updated` WebSocket payload
- **Frontend uses:** These backend values to render "auto-check in Xs (tier)" on the active account card
- **`_last_poll_at`:** Updates every tick regardless of cache hits — ensures the countdown always reflects the most recent tick
- **Stale guard:** Frontend shows "delayed" when 2x the interval has passed since `_last_poll_at`
- **Restart handling:** Frontend shows "starting..." until the first poll tick arrives (no `_last_poll_at` yet)
- **Backend watchdog:** Logs a warning if the tick loop is 2x overdue (detects event loop stalls or scheduling issues)

### Swap History
- Always-visible section at bottom of accounts page
- Shows timestamp, from→to with org-aware labels, reason
- Backend JOINs account emails, org_name, and display_name into swap_log query
- `format_account_label` (Python + JS): shows `email (org)` or `Label — email (org)`. Personal orgs (`*'s Organization`) display as `(personal)`.
- Manual switches (via `use_account`) also write to `swap_log` with `trigger=manual`.

### Decision Log
- Full decision trace recorded every poll tick in `decision_log` table — queryable "why" history
- `record_decision` returns the inserted row ID, used for WebSocket broadcast inclusion
- Records: active account state, should_swap result, suppression reason, ALL candidates evaluated (with scores/deficit/urgency tier/pass-fail), final decision
- Three action types: `stay`, `swap`, `manual_switch`
- 7-day retention with deterministic prune (every 500 ticks or 1% random)
- API: `GET /api/settings/decision-log?limit=N&action=swap&action=manual_switch`
- Frontend: expandable table below Swap History, color-coded action badges (swap=teal, manual=blue, check=slate), default filter shows only swaps + manual, toggle reveals all ticks
- **Real-time push:** Every recorded decision is broadcast via `decision_log_entry` WebSocket event. The frontend appends new entries to the table without polling.
- Cooldown-blocked swaps ARE recorded (so blocked attempts aren't invisible)
- Non-passing candidates recorded too (with `skip_reason`: ahead_of_schedule, below_threshold, recoverable_too_low, near_exhaustion)

## Files

| File | Responsibility |
|------|---------------|
| `jacked/web/auto_swap.py` | Pure decision functions: should_swap, score_candidate, pick_best_target, compute_7d_deficit, compute_effective_working_hours, compute_urgency_threshold, compute_burn_per_window, has_viable_headroom, format_account_label |
| `jacked/api/usage_monitor.py` | Background loops: active poll (adaptive), full sweep (window keeper). `_execute_swap` helper with TOCTOU guard + cross-process lock + partial-swap recovery. Decision log recording every tick. |
| `jacked/web/auth.py` | Usage coordinator: fetch_usage, rate limiting, 429 recovery, token refresh. Urgency tiers. |
| `jacked/api/credential_helpers.py` | Credential I/O: reconcile, sync, cross-process lock, keychain access |
| `jacked/web/window_keeper.py` | Ping logic, schedule helpers |
| `jacked/api/routes/settings_swap.py` | Settings API with validation |
| `jacked/data/web/js/websocket.js` | WebSocket event handlers |
| `jacked/data/web/js/components/accounts.js` | Account cards, countdown timer |
| `jacked/data/web/js/components/auto-swap.js` | Settings panel, swap log table |
| `jacked/data/web/js/components/account-actions.js` | Refresh, countdown tick |

## Observability

All state transitions produce structured log messages. This is the observability contract — any automation or monitoring can rely on these log lines existing.

### Circuit Breaker
- **Activating:** `"Account %d: circuit breaker active (%s, %ds remaining)"` — logged when a `PRIMARY_CIRCUIT_BREAKER` refresh is blocked
- **Expiring:** `"Account %d: circuit breaker cooldown expired, re-attempting refresh"` — logged when the cooldown has passed and a fresh attempt proceeds

### Token Refresh
- **Stale token short-circuit:** `"Account %d: token already refreshed by another path"` — another coroutine refreshed while we waited for the lock

### Live Credential Reconciliation
- **Import:** Logs when tokens are imported from live credential stores into DB
- **Skip (invalid_grant):** Does not import `cc_refresh_token` when circuit breaker shows `invalid_grant` — logs the skip

### Heal Loop
- **Clearing CB:** `"Account %d: clearing circuit breaker for heal attempt"` — logged before every recovery attempt

### Poll Loop Watchdog
- **Overdue tick:** `"Active poll loop delayed — last tick %ds ago, expected interval %ds"` — logged when the tick is 2x overdue (detects event loop stalls)

## Known Limitations

- **Tier weighting:** The deficit model treats all tiers equally. Higher-tier accounts can burn more per 5h window.
- **Clock skew:** `_resets_within` assumes NTP-synchronized system clock within ~1 minute.
- **Multi-instance:** No coordination between multiple jacked instances managing the same accounts.
- **User activity signal:** No concept of whether the user is actively coding. The time-of-day guard (`MIN_PROACTIVE_MINUTES`) approximates this by skipping proactive swaps near `active_end`.
- **DST transitions:** `compute_7d_deficit` uses a rough UTC offset (`datetime.now()` minus `datetime.now(timezone.utc)`). Can be off by 1 hour during the ~1 second of DST transition.
