# Auto-Swap System Architecture

**Last updated:** 2026-04-03
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
- **5h-to-7d burn rate cap:** Each 5h window can only burn a fraction of 7d capacity (~1/15th per week of working 5h windows).
- **Active hours:** User works during configured hours (e.g., 7 AM - 10 PM). Overnight hours don't count.
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

## Decision Flow (Per Tick)

```
1. Fetch active account usage (adaptive interval)
2. Push fresh data to dashboard via WebSocket
3. Compute: should I consider switching?
   a. Defensive triggers: 5h critical, 7d threshold, burn-rate projection
   b. Window-aware suppression: don't swap away if reset is imminent
   c. Stale-data guard: don't trust usage if older than the reset timestamp
4. Score ALL candidate accounts (unified scoring):
   a. Base score from 5h/7d usage (lower usage = higher score)
   b. Tier-aware headroom bonus
   c. Inactive/expired 5h window bonus
   d. Imminent 5h reset bonus (up to +30)
   e. 7d deficit bonus (behind schedule = higher score)
   f. Staleness penalty (old data = lower score)
5. Compare best candidate against staying:
   a. If defensive trigger fired → swap to best candidate
   b. If no defensive trigger but best candidate has high 7d deficit
      AND active account is comfortable → proactive swap
   c. Otherwise → stay
6. Execute swap if decided:
   a. Fetch fresh usage for target (on-demand)
   b. Reconcile outgoing credentials
   c. Record swap with reason
   d. Sync credentials to all stores
   e. Broadcast via WebSocket
```

## Swap Triggers

### Defensive (active account in trouble)
- **5h critical:** `usage_5h >= tier_critical_threshold` (80-95% depending on tier)
- **7d threshold:** `usage_7d >= 85%`
- **Burn-rate projection:** Warning zone + projected to hit critical within 2× check interval

### Proactive (capacity optimization)
- **7d deficit:** Any non-active account has `deficit > PROACTIVE_SWAP_THRESHOLD` (15%) AND active account is comfortable (`usage_5h < warning_5h`)

### Suppression
- **Window-aware:** Don't swap away if `cached_5h_resets_at` or `cached_7d_resets_at` is within `RESET_SUPPRESS_MINUTES` (10 min) — the reset will fix it for free
- **Escape hatch:** Override suppression if a candidate scores > `SUPPRESS_OVERRIDE_SCORE` (80)
- **Stale-data guard:** If reset happened but usage data is older than the reset → suppress (data is unreliable)

## Scoring Model (`score_candidate`)

```
score = 100
score -= cached_usage_5h                          # lower 5h = better
score -= cached_usage_7d × time_weight            # 7d weighted by days remaining
score += tier_headroom × 0.3                      # room before tier limit
score += 15 if 5h window inactive/expired         # encourage opening windows
score += reset_proximity_bonus (up to +30)        # imminent 5h reset
score += 7d_deficit × weight                      # behind-schedule accounts
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

### 429 Recovery
1. **Token refresh:** Rate limits are per-access-token. Exchange refresh token for fresh access token (clears the rate limit). Uses cross-process lock compatible with Claude Code's `proper-lockfile`.
2. **Escalating backoff:** 65s → 130s → 260s → cap 900s on consecutive 429s
3. **Tier override:** After 3+ consecutive 429s, force idle tier

### Cross-Process Locking
- Lock: `os.mkdir(~/.claude.lock)` (atomic, same protocol as `proper-lockfile`)
- PID file inside for stale detection
- 5 retries with 1-2s jittered delay
- Claude Code detects `.credentials.json` mtime change and re-reads

## Credential Management

### Before Every Swap
1. **Reconcile outgoing:** Read live credentials from Keychain/file, import rotated tokens into DB
2. **Write incoming:** `sync_credential_to_all_stores` writes to DB, `.credentials.json`, Keychain

### Token Priority
- CC tokens preferred (`cc_access_token`, `cc_refresh_token`)
- Primary tokens as fallback (`access_token`, `refresh_token`)
- If CC expired + no CC refresh → fall through to primary (sets `refreshToken: null` to prevent Claude Code from consuming primary refresh)

### `invalid_grant` Recovery
- Before clearing `cc_refresh_token`, check live credential store
- If Claude Code refreshed successfully, import the fresh token
- Only clear if no live recovery available

## Window Keeper

Runs on the sweep loop timer (`usage_check_interval`). Only pings — does NOT fetch usage.

- Checks `needs_ping` for each account (uses `cached_5h_resets_at` from DB)
- Pings via direct `httpx.POST` to messages API (haiku, max_tokens=1)
- After successful ping, fetches usage to update `cached_5h_resets_at`
- Only during active hours or pre-wake window

## Dashboard Integration

### WebSocket Events
- `usage_poll_updated` — after each active poll, push fresh account data
- `auto_swap_triggered` — persistent banner with reason (5 min, dismissible)
- `all_accounts_exhausted` — exhaustion banner with recovery estimate

### Countdown Timer
- Shows "auto-check in Xs (tier)" on the active account card
- Uses tier-appropriate interval (not hardcoded 60s)
- Reads `usage_cached_at` from live state (updated by WebSocket)

### Swap History
- Always-visible section at bottom of accounts page
- Shows timestamp, from→to emails, reason
- Backend JOINs account emails into swap_log query

## Files

| File | Responsibility |
|------|---------------|
| `jacked/web/auto_swap.py` | Pure decision functions: should_swap, score_candidate, pick_best_target, compute_7d_deficit, compute_effective_working_hours |
| `jacked/api/usage_monitor.py` | Background loops: active poll (adaptive), full sweep (window keeper). Swap execution. |
| `jacked/web/auth.py` | Usage coordinator: fetch_usage, rate limiting, 429 recovery, token refresh. Urgency tiers. |
| `jacked/api/credential_helpers.py` | Credential I/O: reconcile, sync, cross-process lock, keychain access |
| `jacked/web/window_keeper.py` | Ping logic, schedule helpers |
| `jacked/api/routes/settings_swap.py` | Settings API with validation |
| `jacked/data/web/js/websocket.js` | WebSocket event handlers |
| `jacked/data/web/js/components/accounts.js` | Account cards, countdown timer |
| `jacked/data/web/js/components/auto-swap.js` | Settings panel, swap log table |
| `jacked/data/web/js/components/account-actions.js` | Refresh, countdown tick |

## Known Limitations

- **Tier weighting:** The deficit model treats all tiers equally. Higher-tier accounts can burn more per 5h window.
- **Clock skew:** `_resets_within` assumes NTP-synchronized system clock within ~1 minute.
- **Multi-instance:** No coordination between multiple jacked instances managing the same accounts.
- **Activity log:** No structured log of why the system decided NOT to swap (future enhancement).
