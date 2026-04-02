# Adaptive Usage Polling with Centralized Coordinator

**Date:** 2026-04-02
**Status:** Approved

## Problem

The active account usage poll runs every 60 seconds with a fixed interval. The Anthropic usage API rate limits at ~1 request per 60 seconds per account. Multiple callers (active poll loop, UI auto-refresh, manual button, post-ping fetch) independently hit the same account, causing persistent 429s that blind the auto-swap system — it can't see usage data when it needs it most.

## Solution

### 1. Centralized Usage Coordinator

Replace the passive `min_age` cache guard in `fetch_usage` with an active per-account coordinator that:

- Tracks `last_fetched_at` and `next_eligible_at` per account
- Enforces a hard ceiling of 1 request per 65 seconds per account (rate limit + 5s safety margin)
- All callers go through the same entry point — no caller can bypass the ceiling
- Returns cached data immediately when the account isn't eligible yet
- Only one caller can be "in flight" per account at a time (prevents concurrent fetches)

**Caller behavior changes:**
- Active poll loop: calls `fetch_usage(account_id, db, urgency="auto")` — the coordinator decides when to actually fetch based on the adaptive tier
- Full sweep loop: calls `fetch_usage(account_id, db)` — same coordinator, same ceiling
- UI auto-refresh: skips the active account when auto-swap is enabled (server owns it). Non-active accounts go through the coordinator normally
- Manual "Refresh" button: calls `fetch_usage(account_id, db, force=True)` — still respects the 65s hard ceiling but jumps the queue (returns 429 backoff info to the UI if rate limited)
- Post-ping fetch: calls `fetch_usage(account_id, db, force=True)` — same as manual

**The `force` parameter:** Means "I really want fresh data" — it resets the adaptive timer but still respects the hard rate limit ceiling. If the account was fetched 30 seconds ago, force=True returns cached + a note that fresh data will be available in 35 seconds.

### 2. Adaptive Urgency Tiers

The active poll loop doesn't use a fixed interval. After each successful fetch, the coordinator computes the next poll time based on the account's current state:

| Tier | Condition | Interval | Rationale |
|------|-----------|----------|-----------|
| **Idle** | 5h usage < 50% AND burn rate < 0.01 | 5 min (300s) | Nothing happening |
| **Normal** | 5h usage < 70% OR burn rate low (< 0.5%/min) | 2.5 min (150s) | Moderate activity |
| **Warning** | 5h usage 70-85% OR burn rate projects critical within 15 min | 90s | Getting close |
| **Critical** | 5h usage > 85% OR burn rate projects critical within 5 min | 65s | About to hit the wall |

**7-day escalation:** If 7d usage > 80%, bump up one urgency tier regardless of 5h state. An account at 30% 5h but 85% 7d should be in Warning, not Idle.

**Burn-rate projection:** Uses the existing `BurnRate` data. "Projects critical within N minutes" means: `current_5h + (rate_5h_per_min * N) >= critical_threshold`.

**Jitter:** All intervals get ±15% random jitter to prevent sync patterns. So "Idle" is 255-345s, "Critical" is 55-75s.

**Tier transitions:** Immediate on each fetch. If usage jumps from 40% to 82% in one tick, the next interval immediately drops from 5 min to 90s.

### 3. UI Auto-Refresh Skips Active Account

When `swapSettings.auto_swap_enabled` is true, the client-side "Auto: 2min/5min" refresh loop skips the active account (`window.jackedState.activeCredentialAccountId`). The server's adaptive poll loop owns the active account's usage data.

Non-active accounts continue to refresh on the UI timer as before.

### 4. Countdown Timer Adaptation

The active account countdown timer currently assumes a fixed 60s interval. It needs to display the actual adaptive interval:

- After each usage update, the coordinator broadcasts the next poll time via state (stored as `_next_poll_at` epoch timestamp)
- The countdown reads this from `window.jackedState` and counts down to it
- Tier name shown alongside: "auto-check in 45s (warning)" so the user understands why it's fast/slow

## Architecture

```
                    ┌─────────────────────────┐
                    │   Usage Coordinator      │
                    │   (fetch_usage in        │
                    │    auth.py)              │
                    │                          │
                    │  Per-account state:      │
                    │  - last_fetched_at       │
                    │  - next_eligible_at      │
                    │  - in_flight lock        │
                    │  - 429 backoff           │
                    │  - adaptive tier/interval│
                    └──────────┬───────────────┘
                               │
            ┌──────────────────┼──────────────────┐
            │                  │                   │
     Active Poll Loop    Full Sweep Loop    UI / Manual
     (server, adaptive)  (server, fixed)    (client-side)
            │                  │                   │
            └──────────────────┼──────────────────┘
                               │
                    Anthropic Usage API
                    (1 req/60s/account)
```

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auth.py` | Refactor `fetch_usage` into coordinator with per-account state, adaptive tier computation, hard ceiling enforcement. Replace `_usage_backoff` dict with richer per-account state. |
| `jacked/api/usage_monitor.py` | Active poll loop uses adaptive sleep instead of fixed 60s. Passes urgency context. Stores `_next_poll_at` for countdown. |
| `jacked/data/web/js/components/account-actions.js` | UI auto-refresh skips active account when auto-swap enabled. Countdown reads adaptive interval. |
| `jacked/data/web/js/components/accounts.js` | Countdown shows tier name + adaptive time. |
| `tests/unit/test_usage_refresh.py` | Tests for coordinator: ceiling enforcement, tier computation, force behavior, concurrent fetch prevention. |

## What This Does NOT Do

- Does not manage window keeper pings (different API endpoint, different rate limit)
- Does not change the full sweep interval for non-active accounts (they use `usage_check_interval` setting)
- Does not add server-push of usage updates via WebSocket (future enhancement — currently the UI re-fetches from DB)
- Does not persist coordinator state across server restarts (ephemeral, rebuilds from DB on restart)
