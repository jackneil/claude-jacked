# Token Resilience, Poll Accuracy, and Decision Log Live Updates

**Date:** 2026-04-06
**Status:** Draft
**Scope:** auth.py refactor, UI countdown fix, credential reconciliation, decision log WebSocket

## Problem Statement

Six issues discovered during checkpoint review and live observation:

1. **CC token falsely shows "needs re-auth"** — Claude Code has valid tokens in Keychain/`.credentials.json`, but jacked's DB has stale CC token data. The dashboard shows "CC Token: re-auth" for accounts that are actively working. Only clears after a swap triggers `reconcile_outgoing_credentials`.

2. **Active account countdown stuck on "checking..."** — The frontend computes its own poll interval from raw usage thresholds. The backend uses a different adaptive tier system with jitter, burn-rate projection, 7d escalation, and a 65s rate ceiling. When they disagree, the frontend shows "checking..." for extended periods because `usage_cached_at` doesn't update on cache hits.

3. **Token exchange code duplication** — `refresh_cc_token` and `_try_refresh_on_429` have inline POST logic identical to `_exchange_refresh_token`. The duplicated code has divergent error handling and makes the circuit breaker bug harder to fix uniformly.

4. **Circuit breaker is a permanent death sentence** — `_primary_refresh_state["dead"]=True` is in-memory, never cleared except on process restart. The heal loop can't recover because it doesn't clear the circuit breaker before trying, and skips refresh for non-expired tokens.

5. **Active hours defaults inconsistent** — `compute_effective_working_hours` and `compute_7d_deficit` default to 07:00-22:00, but `compute_burn_per_window` and `compute_urgency_threshold` default to 06:00-23:00. Silent bug.

6. **Decision log has no real-time updates** — New decisions require manually toggling the filter to reload. No WebSocket push.

## Design

### 1. Token Exchange Deep Unification

#### 1a. `RefreshConfig` dataclass

New dataclass configuring how `_refresh_token_flow` behaves:

```python
@dataclass
class RefreshConfig:
    token_set: str              # "primary" | "cc" | "cc_or_primary"
    timeout: float = 15.0
    lock_type: str = "async"    # "async" | "cross_process" | "none"
    circuit_breaker: bool = False
    cooldown_seconds: int = 600
    db_retry: bool = False
    db_retry_attempts: int = 3
    write_credential_stores: bool = False
    recovery_from_live: bool = False
    fetch_profile_after: bool = False
```

#### 1b. `_refresh_token_flow` function

New mid-level orchestrator between the low-level `_exchange_refresh_token` (unchanged) and the four caller functions. Steps:

1. **Resolve refresh token** — read from DB based on `token_set` ("primary" → `refresh_token`, "cc" → `cc_refresh_token`, "cc_or_primary" → CC first, fallback to primary)
2. **Acquire lock** — per-account asyncio lock (`lock_type="async"`) or cross-process Claude lock (`lock_type="cross_process"`)
3. **Check circuit breaker** — if enabled, read `refresh_last_failed_at` and `refresh_failure_type` from DB. Skip if within cooldown. No permanent "dead" state.
4. **Check live credentials** — if `recovery_from_live=True` or `write_credential_stores=True`, read Keychain/`.credentials.json`. If live store has a newer token for this account, import it and skip the exchange.
5. **Check stale token** — if another process/coroutine already refreshed (DB token differs from caller's copy), return the fresh token.
6. **Call `_exchange_refresh_token`** — the existing POST helper, unchanged.
7. **On success:**
   - Update DB columns (cc_* or primary, based on `token_set`)
   - If `db_retry=True`, retry DB writes with exponential backoff
   - If `write_credential_stores=True` and account is active, call `sync_credential_to_all_stores`
   - If `fetch_profile_after=True`, call `fetch_profile`
   - Clear circuit breaker state in DB
8. **On `invalid_grant`:**
   - If `recovery_from_live=True`, attempt live credential import (for active account, skip the `_jackedAccountId` gate — trust that the active account owns the live credentials)
   - If recovery succeeds, return recovered tokens
   - If recovery fails and `token_set="cc"`, clear `cc_refresh_token` (consumed, unrecoverable)
   - Set circuit breaker cooldown in DB (NOT permanent death)
9. **On other errors:** Set circuit breaker cooldown in DB, log appropriately.
10. **Return `TokenExchangeResult`** — extended with `fresh_access_token` field for callers that need the token string.

#### 1c. Caller refactoring

Each caller becomes a thin wrapper:

| Caller | Config |
|--------|--------|
| `refresh_account_token` | `token_set="primary", timeout=30, db_retry=True, fetch_profile_after=True` + caller-specific error policy (401/403 → mark invalid, but only after 2 consecutive failures) |
| `refresh_cc_token` | `token_set="cc", timeout=30, lock_type="async", recovery_from_live=True` |
| `_try_refresh_on_429` | `token_set="cc_or_primary", lock_type="cross_process", write_credential_stores=True` |
| `_try_refresh_primary_token` | `token_set="primary", circuit_breaker=True` |

#### 1d. Less aggressive invalid-marking

`refresh_account_token`: change 401/403 handling from immediate `validation_status="invalid"` to:
- First failure: record error + set circuit breaker cooldown, do NOT mark invalid
- Second consecutive failure (after cooldown expires and retry fails): mark invalid

`fetch_usage` 401 path: before marking invalid, attempt live credential import for the active account. Only mark invalid after both refresh AND live import fail.

### 2. Circuit Breaker to DB

#### 2a. New DB columns on `accounts`

```sql
ALTER TABLE accounts ADD COLUMN refresh_last_failed_at INTEGER;
ALTER TABLE accounts ADD COLUMN refresh_failure_type TEXT;
```

`refresh_failure_type` stores the error string ("invalid_grant", "network_error", "http_429", etc.). `refresh_last_failed_at` stores epoch seconds.

#### 2b. Delete in-memory state

Remove `_primary_refresh_state` dict and `_get_primary_refresh_state()`. All circuit breaker reads/writes go through DB columns. This means:
- State survives process restarts
- Heal loop can clear it before attempting recovery
- Dashboard can display why an account is stuck

#### 2c. Cooldown logic

All errors get a TTL-based cooldown (default 600s). No permanent "dead" state. After cooldown expires, the next refresh attempt runs normally. If it fails again, cooldown resets.

### 3. Live Credential Reconciliation

#### 3a. Periodic reconciliation

`refresh_all_expiring_tokens` (background loop, every 30 min) gains a step: for the active account, call `reconcile_from_live_credentials(account_id, db)` before attempting token refresh. This catches cases where Claude Code rotated tokens during normal operation.

#### 3b. On-demand reconciliation

In `_row_to_account` (routes/auth.py, the function that computes `cc_needs_auth`): if the account is the active one AND any of these are true:
- `cc_refresh_token` is NULL
- `cc_expires_at` has passed
- `cc_access_token` differs from what's in live credentials

...then synchronously read live credentials and import fresh CC tokens before computing `cc_needs_auth`. This is a fast local read (Keychain or file), not a network call.

#### 3c. Rename and generalize

Rename `reconcile_outgoing_credentials` → `reconcile_credentials_from_live_store`. Same logic, but callable anytime, not just during swaps. The `_jackedAccountId` gate stays for non-active accounts but is skipped for the known active account.

#### 3d. Heal loop fix

In `heal_invalid_accounts`:
1. Clear circuit breaker state (`refresh_last_failed_at=None, refresh_failure_type=None`) before attempting recovery
2. Always attempt refresh if `refresh_token` exists — drop the `should_refresh()` gate (healing is recovery mode, not normal operation)
3. Before calling `validate_account`, try `reconcile_credentials_from_live_store` to import any fresh tokens Claude Code may have

### 4. Poll Countdown Fix

#### 4a. Backend: include poll metadata in WebSocket payload

After computing `_poll_interval` and `_poll_tier`, include them in the `usage_poll_updated` broadcast:

```python
safe_acct["_poll_interval"] = int(_poll_interval)
safe_acct["_poll_tier"] = _poll_tier
safe_acct["_last_poll_at"] = int(time.time())
```

Move `_compute_poll_interval` call to BEFORE the broadcast (currently it's after, at line 943). This way the frontend knows the actual interval for this tick.

`_last_poll_at` is a new field that always updates every tick, regardless of whether `fetch_usage` hit the rate ceiling. This is NOT stored in DB — it's computed in the broadcast payload from `time.time()`.

#### 4b. Frontend: use backend-provided interval

Replace the hardcoded threshold table in `_startCheckCountdown` (lines 24-28) with the backend-provided values:

```javascript
var pollInterval = activeAcct._poll_interval || 300;
var lastPollAt = activeAcct._last_poll_at || cachedAt;
var rem = Math.max(0, pollInterval - (now - lastPollAt));
```

This means:
- Frontend counts down from the backend's actual interval, not its own guess
- `_last_poll_at` updates every tick even on cache hits, so the countdown always resets
- Tier name can be displayed: "Next check in 45s (warning)" or just "45s"

#### 4c. Stale guard

If `_last_poll_at` is more than 2× `_poll_interval` ago and no WebSocket event has arrived, show "delayed" instead of "checking...". This handles the case where the WebSocket connection dropped or the backend loop crashed.

### 5. Active Hours Default Normalization

All functions in `auto_swap.py` that accept `active_start`/`active_end` parameters will use the same defaults: `"06:00"` and `"23:00"`. These match the settings defaults in `usage_monitor.py` (line 340-341: `window_keeper_active_start: "06:00"`, `window_keeper_active_end: "23:00"`).

Functions to update (currently defaulting to 07:00/22:00):
- `compute_effective_working_hours`: change `active_start="07:00"` → `"06:00"`, `active_end="22:00"` → `"23:00"`
- `compute_7d_deficit`: change `active_start="07:00"` → `"06:00"`, `active_end="22:00"` → `"23:00"`
- `has_viable_headroom`: change `active_start="07:00"` → `"06:00"`, `active_end="22:00"` → `"23:00"`
- `pick_best_target`: change `active_start="07:00"` → `"06:00"`, `active_end="22:00"` → `"23:00"`

Already correct (06:00/23:00) — no change needed:
- `compute_burn_per_window`
- `compute_urgency_threshold`
- `score_candidate` (uses 06:00/23:00)

Tests that hardcode `active_start="07:00"` will be updated to match.

### 6. Decision Log WebSocket Push

#### 6a. New WebSocket event: `decision_log_entry`

When `db.record_decision()` is called, broadcast a `decision_log_entry` event with the full decision data. Two recording points:

1. **Auto-swap tick** (usage_monitor.py:913) — after `db.record_decision()`, broadcast via `ws_registry`
2. **Manual switch** (routes/auth.py:888) — after `db.record_decision()`, broadcast via `ws_registry`

Payload:
```python
{
    "id": <decision_id>,  # from DB insert
    "account_id": ...,
    "action": "swap" | "stay" | "manual_switch",
    "trigger": "auto_swap" | "proactive_7d" | "tick" | "manual",
    "reason": "...",
    "timestamp": "...",
    "detail": { ... },  # full tick detail (candidates, flags, etc.)
}
```

#### 6b. `record_decision` returns the inserted ID

Currently `record_decision` is a void method. Change it to return the row ID so the caller can include it in the broadcast.

#### 6c. Frontend handler

In `websocket.js`, add handler for `decision_log_entry`:

```javascript
jackedWS.on('decision_log_entry', (msg) => {
    const d = msg.payload || msg;
    // If decision log is currently visible, prepend the new entry
    const container = document.getElementById('decision-log-container');
    if (container) {
        renderDecisionLog('decision-log-container');  // re-render with fresh data
    }
});
```

Simple approach: re-render the whole table on new entry. The table is small (100-200 rows) and re-render is fast. No need for incremental DOM insertion.

### 7. Decision Log Frontend QA

Browser-test the decision log UI with Playwright/Chrome MCP:
- Verify expandable rows toggle correctly
- Verify badge colors (teal=swap, blue=manual, gray=check)
- Verify filter toggle (show all ↔ show swaps only)
- Verify candidate table renders inside detail rows
- Verify decision flags display
- Verify WebSocket live updates (after implementing section 6)
- Check for XSS in `escapeHtml` calls
- Test empty state ("No decisions recorded yet")

### 8. Architecture Doc Update

Update `docs/architecture/auto-swap-system.md` to reflect:
- **401 auto-refresh system** — `_try_refresh_primary_token`, circuit breaker with DB persistence, integration with `fetch_usage` and `validate_account`
- **Circuit breaker** — DB-persisted with TTL cooldown, no permanent death
- **Live credential reconciliation** — periodic + on-demand, not just during swaps
- **Decision log recording** — all tick decisions recorded, WebSocket push
- **`auto_swap_enabled` flag** — per-account toggle, checked in `pick_best_target` and proactive scanner
- **Skip reason values** — document `near_exhaustion`, `recoverable_too_low`, `ahead_of_schedule`, `below_threshold`
- **Urgency score formula** — promote to first-class definition in capacity waste model section
- **429 recovery backoff sequence** — exact values: 65s → 130s → 260s → 520s → cap 900s
- **Active hours** — document normalized defaults (06:00-23:00) and that settings override these
- **Window keeper execution context** — runs in `full_sweep_loop`, not standalone
- **WebSocket whitelist maintenance** — note that new DB columns must be added to `_WS_SAFE_FIELDS`
- **Poll interval metadata** — `_poll_interval`, `_poll_tier`, `_last_poll_at` in WS payload

## Files Modified

| File | Changes |
|------|---------|
| `jacked/web/auth.py` | `RefreshConfig`, `_refresh_token_flow`, refactor 4 callers, circuit breaker to DB, heal loop fixes, poll interval computation moved |
| `jacked/web/database.py` | Migration: `refresh_last_failed_at`, `refresh_failure_type` columns. `record_decision` returns row ID |
| `jacked/web/auto_swap.py` | Normalize active hours defaults to 06:00/23:00 across all functions |
| `jacked/api/usage_monitor.py` | Include `_poll_interval`/`_poll_tier`/`_last_poll_at` in WS broadcast, decision log WS push |
| `jacked/api/credential_helpers.py` | Rename `reconcile_outgoing_credentials` → `reconcile_credentials_from_live_store`, generalize |
| `jacked/api/routes/auth.py` | On-demand credential reconciliation in `_row_to_account` for active account, decision log WS push for manual switch |
| `jacked/data/web/js/components/account-actions.js` | Use backend-provided `_poll_interval` and `_last_poll_at` instead of hardcoded thresholds, stale guard |
| `jacked/data/web/js/websocket.js` | Handle `decision_log_entry` event |
| `jacked/data/web/js/components/auto-swap.js` | Re-render on WS push |
| `tests/unit/test_auto_swap.py` | Update active hours defaults in tests |
| `tests/unit/test_auth.py` or new test file | Tests for `_refresh_token_flow`, circuit breaker DB persistence, live credential recovery |
| `docs/architecture/auto-swap-system.md` | Full update per section 8 |

## Non-Goals

- WebSocket pagination or virtual scrolling for decision log (table is small)
- Persisting poll tier/interval to DB (only needed for WS broadcast)
- Changing the adaptive tier thresholds or burn-rate projection logic
- Adding new UI for circuit breaker state (can show in decision log detail if needed later)
