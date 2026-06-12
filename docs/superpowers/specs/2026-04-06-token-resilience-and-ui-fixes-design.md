# Token Resilience, Poll Accuracy, and Decision Log Live Updates

**Date:** 2026-04-06
**Status:** Draft (DCR Wave 1 fixes applied)
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

#### 1a. Mode-based `RefreshMode` enum

Replace the open-ended `RefreshConfig` dataclass with a closed enum. There are exactly 4 valid configurations — an enum makes that explicit and prevents invalid combinations:

```python
class RefreshMode(str, Enum):
    PRIMARY = "primary"             # refresh_account_token
    CC = "cc"                       # refresh_cc_token
    CC_OR_PRIMARY_429 = "cc_429"    # _try_refresh_on_429
    PRIMARY_CIRCUIT_BREAKER = "primary_cb"  # _try_refresh_primary_token
```

Each mode's behavior is hardcoded inside `_refresh_token_flow`:

| Mode | Token Set | Lock | Timeout | CB | DB Retry | Cred Stores | Live Recovery | Profile |
|------|-----------|------|---------|----|----------|-------------|---------------|---------|
| `PRIMARY` | primary | async (per-account) | 30s | No | Yes (3x) | No | No | Yes |
| `CC` | cc | async (per-account CC) | 30s | No | Yes (3x) | No | Access token only | No |
| `CC_OR_PRIMARY_429` | cc→primary | cross-process | 15s | No | Yes (3x) | If active | Access token only | No |
| `PRIMARY_CIRCUIT_BREAKER` | primary | async (per-account) | 15s | Yes | Yes (3x) | No | No | No |

**Key constraint:** All modes that refresh the same token set (`cc` tokens: CC and CC_429) share the **same per-account lock**. This prevents two callers from simultaneously consuming the same refresh token. Specifically:
- CC and CC_OR_PRIMARY_429 both acquire `_get_cc_refresh_lock(account_id)` when refreshing cc tokens
- CC_OR_PRIMARY_429 additionally acquires the cross-process Claude lock for credential store writes
- PRIMARY and PRIMARY_CIRCUIT_BREAKER both acquire `_get_refresh_lock(account_id)`

**Lock nesting order for CC_OR_PRIMARY_429:** Acquire the async CC lock FIRST, then the cross-process Claude lock inside it. This matches the existing pattern where the async lock protects the token read/write and the cross-process lock protects the credential file. Never reverse this order — cross-process lock acquisition uses blocking `time.sleep()` inside `acquire_claude_lock`, so holding it while waiting for an async lock would deadlock the event loop. Known limitation: the blocking `time.sleep()` in `acquire_claude_lock` blocks the event loop for up to ~10s. Future improvement: convert to `asyncio.to_thread`.

**DB retry is always on.** A successful token exchange with a lost DB write is unrecoverable — the old refresh token was consumed and the new one exists only in dead memory. All modes retry DB writes 3x with exponential backoff.

#### 1b. `_refresh_token_flow` function

New mid-level orchestrator. All token updates + circuit breaker state changes happen in a **single `update_account()` call** to ensure atomicity.

Steps:

1. **Resolve refresh token** — read from DB based on mode's token set
2. **Acquire lock** — per mode's lock type. All callers sharing a token set share the same lock.
3. **Re-read DB under lock** — prevent stale reads. If DB token differs from caller's copy, another coroutine already refreshed → return the fresh token. **Log:** `"Account %d: token already refreshed by another path"`
4. **Check circuit breaker** (PRIMARY_CIRCUIT_BREAKER mode only) — read `refresh_last_failed_at` and `refresh_failure_type` from DB. **Log:** `"Account %d: circuit breaker active (%s, %ds remaining)"` with failure type and cooldown remaining. Skip if within cooldown.
5. **Call `_exchange_refresh_token`** — the existing POST helper, unchanged.
6. **On success — single atomic DB write:**
   - Token columns (cc_* or primary, based on mode)
   - `refresh_last_failed_at=None, refresh_failure_type=None` (clear circuit breaker)
   - Retry 3x with exponential backoff on DB error
   - **After DB write:** if mode writes credential stores and account is active, call `sync_credential_to_all_stores` (non-atomic side effect — partial failure documented below)
   - **After DB write:** if PRIMARY mode, call `fetch_profile`
7. **On `invalid_grant`:**
   - If CC or CC_429 mode: attempt live credential recovery — import `cc_access_token` and `cc_expires_at` only (NOT `cc_refresh_token` — see Safety Rule below)
   - If recovery found a fresh access token, update DB and return it
   - If no recovery, clear `cc_refresh_token=None` in DB
   - Set circuit breaker in same DB write: `refresh_last_failed_at=now, refresh_failure_type="invalid_grant"`
   - **Log:** `"Account %d: invalid_grant — recovered from live credentials"` or `"Account %d: invalid_grant — clearing cc_refresh_token (no recovery)"`
8. **On other errors:** Single atomic DB write setting circuit breaker cooldown. **Log:** `"Account %d: refresh failed (%s) — cooldown %ds"` with error type and cooldown duration.
9. **Return `TokenExchangeResult`** — extended with `fresh_access_token` field.

**Safety Rule: Never import `cc_refresh_token` from live credentials during `invalid_grant` recovery.**

CC refresh tokens are single-use. If jacked imports Claude Code's active refresh token and exchanges it, Claude Code loses its session. Live credential import is safe for:
- `cc_access_token` — read-only, shareable, non-destructive
- `cc_expires_at` — metadata, non-destructive

It is NOT safe for:
- `cc_refresh_token` — single-use, competitive, importing and exchanging it destroys Claude Code's ability to refresh

The `_jackedAccountId` gate is **never skipped**. It is the ground truth for which account owns the credential file. During a swap, the credential file transitions between accounts — skipping the gate would import the wrong account's tokens.

**CC_OR_PRIMARY_429 primary fallback failure:** When this mode falls back to the primary refresh token and the exchange fails with `invalid_grant`, the primary token is consumed but the circuit breaker is NOT enabled for this mode. The consumed primary token will be detected by `_try_refresh_primary_token` on the next 401, which DOES use the circuit breaker. No special handling needed — the existing circuit breaker path covers this.

**Credential store write partial failure:** If `sync_credential_to_all_stores` fails after a successful DB write, the DB has new tokens but the credential file is stale. Recovery: the next poll tick will detect the mismatch and re-sync. This is an existing behavior that the refactor preserves, not introduces.

**Cross-reference: cc_refresh_token import safety.** The rule "never import cc_refresh_token from live credentials during invalid_grant" applies to THREE code paths — all must be updated:
1. `_refresh_token_flow` step 7 (section 1b) — CC/CC_429 mode invalid_grant recovery
2. `reconcile_credentials_from_live_store` (section 3a) — periodic reconciliation
3. On-demand reconciliation in account list API (section 3b)
The existing `refresh_cc_token` (auth.py:340-356) currently imports cc_refresh_token — this must be replaced.

#### 1c. Caller refactoring

Each caller becomes a one-liner:

```python
async def refresh_cc_token(account_id: int, db: Database) -> bool:
    result = await _refresh_token_flow(account_id, db, RefreshMode.CC)
    return result.success
```

Plus caller-specific error policy for `refresh_account_token` (401/403 → mark invalid after 2 consecutive failures, not 1).

#### 1d. Less aggressive invalid-marking

`refresh_account_token`: change 401/403 handling from immediate `validation_status="invalid"` to:
- First failure: record error + set circuit breaker cooldown, do NOT mark invalid
- Second consecutive failure (after cooldown expires and retry fails): mark invalid

`fetch_usage` 401 path: before marking invalid, attempt live credential import for the active account (access token only). Only mark invalid after both refresh AND live import fail.

### 2. Circuit Breaker to DB

#### 2a. New DB columns on `accounts`

```sql
ALTER TABLE accounts ADD COLUMN refresh_last_failed_at INTEGER;
ALTER TABLE accounts ADD COLUMN refresh_failure_type TEXT;
```

`refresh_failure_type` stores the error string ("invalid_grant", "network_error", "http_429", etc.). `refresh_last_failed_at` stores epoch seconds.

**Required updates for new columns:**
- Add to `_ACCOUNT_UPDATE_COLS` whitelist in `database.py` (line ~916) — without this, `update_account` raises `ValueError`
- Add to Pydantic `Account` model in `database.py` (line ~38) as `Optional[int]` / `Optional[str]`
- Do NOT add to `_WS_SAFE_FIELDS` yet — only add when the dashboard displays circuit breaker state
- Migration uses existing `ALTER TABLE ADD COLUMN` with `try/except OperationalError: pass` pattern (idempotent, forward-only, rollback-safe because old code ignores unknown columns)

#### 2b. Delete in-memory state

Remove `_primary_refresh_state` dict and `_get_primary_refresh_state()`. All circuit breaker reads/writes go through DB columns.

#### 2c. Cooldown logic — scaled by error type

| Error Type | Cooldown | Rationale |
|-----------|----------|-----------|
| `invalid_grant` | 600s (10 min) | Token consumed, need time for external recovery |
| `network_error` | 60s | Transient, usually resolves quickly |
| `http_429` | 120s | Rate limit, moderate backoff |
| `http_5xx` | 120s | Server issue, moderate backoff |
| Other | 300s | Unknown, moderate default |

No permanent "dead" state. After cooldown expires, the next refresh attempt runs normally.

### 3. Live Credential Reconciliation

#### 3a. Periodic reconciliation

`refresh_all_expiring_tokens` (background loop, every 30 min) gains a step: for the active account, call `reconcile_credentials_from_live_store(account_id, db)` before attempting token refresh.

**What reconciliation imports:**
- `cc_access_token` — always (non-destructive)
- `cc_expires_at` — always (metadata)
- `cc_refresh_token` — **only if** `refresh_failure_type != "invalid_grant"` in DB. If the circuit breaker was set due to `invalid_grant`, the live refresh token is Claude Code's active token and importing+exchanging it would destroy Claude Code's session.

#### 3b. On-demand reconciliation — cached

In the account list API endpoint: for the active account, if `cc_refresh_token` is NULL or `cc_expires_at` has passed, read live credentials and import fresh CC access token + expiry before computing `cc_needs_auth`.

**Caching:** Cache the live credential read result for 30 seconds (in-memory, keyed by active account ID). This prevents thundering herd from multiple concurrent API calls. The cache is invalidated on swap.

**Read-only path preservation:** The import only writes to DB if the live credentials actually differ from what's stored. Most calls will be cache hits or no-ops.

Note: `_row_to_account` doesn't exist as a named function — the `cc_needs_auth` computation is inline in the `AccountResponse(...)` constructor at routes/auth.py:300. The implementation should extract this into a helper or add the reconciliation call before the constructor.

#### 3c. Rename and generalize

Rename `reconcile_outgoing_credentials` → `reconcile_credentials_from_live_store`. Same logic, callable anytime. The `_jackedAccountId` gate is **always enforced** — never skipped, even for the active account.

#### 3d. Heal loop fix

In `heal_invalid_accounts`:
1. Clear circuit breaker state (`refresh_last_failed_at=None, refresh_failure_type=None`) **under the per-account lock** before attempting recovery — prevents other coroutines from racing
2. Always attempt refresh if `refresh_token` exists — drop the `should_refresh()` gate (healing is recovery mode)
3. Before calling `validate_account`, try `reconcile_credentials_from_live_store` to import fresh access tokens (NOT refresh tokens if `invalid_grant`)

#### 3e. Non-active account CC token recovery

Non-active accounts whose CC refresh token is consumed have NO automatic recovery path — live credentials belong to a different account. The dashboard should show a clear "CC re-auth needed" indicator (the existing `cc_needs_auth` flag already handles this). This is not a new problem — the spec acknowledges it rather than pretending the circuit breaker provides recovery for non-active accounts.

### 4. Poll Countdown Fix

#### 4a. Backend: include poll metadata in WebSocket payload

Move `_compute_poll_interval` call to BEFORE the broadcast (currently after, at line 943). Note: the poll interval depends on burn rates updated later in the tick, so the broadcast will show the PREVIOUS tick's interval. This one-tick staleness is acceptable — the tier rarely changes between consecutive ticks. Include in `usage_poll_updated`:

```python
safe_acct["_poll_interval"] = int(_poll_interval)
safe_acct["_poll_tier"] = _poll_tier
safe_acct["_last_poll_at"] = int(time.time())
```

`_last_poll_at` always updates every tick regardless of cache hits. NOT stored in DB.

**Backend watchdog:** If the poll loop has not completed a tick in 2× the expected interval, log: `"WARN: active account poll loop delayed — last tick %ds ago, expected interval %ds"`. This gives on-call something to grep for.

#### 4b. Frontend: use backend-provided interval

Replace the hardcoded threshold table in `_startCheckCountdown` with backend values:

```javascript
var pollInterval = activeAcct._poll_interval || 300;
var lastPollAt = activeAcct._last_poll_at || cachedAt;
var rem = Math.max(0, pollInterval - (now - lastPollAt));
```

Display tier: "45s (warning)" or just "45s".

#### 4c. Stale guard and restart handling

- If `_last_poll_at` is more than 2× `_poll_interval` ago → show "delayed" instead of "checking..."
- If `_last_poll_at` is absent (process restart, WS reconnect) → show "starting..." until the first poll tick arrives
- On WS reconnect: the frontend should fetch current state via HTTP (`GET /api/accounts`) to get fresh data, then resume countdown from the next WS event

### 5. Active Hours Default Normalization

All functions in `auto_swap.py` that accept `active_start`/`active_end` parameters will use the same defaults: `"06:00"` and `"23:00"`.

Functions to update (currently defaulting to 07:00/22:00):
- `compute_effective_working_hours` (line 130-131)
- `compute_7d_deficit` (line 242-243)
- `should_swap` (line 328-329)
- `has_viable_headroom` (line 415-416)
- `score_candidate` (line 512-513)
- `pick_best_target` (line 512-513)

Already correct (06:00/23:00) — no change needed:
- `compute_burn_per_window` (line 181)
- `compute_urgency_threshold` (line 215-216)

Tests that hardcode `active_start="07:00"` will be updated to match.

### 6. Decision Log WebSocket Push

#### 6a. New WebSocket event: `decision_log_entry`

When `db.record_decision()` is called, broadcast a `decision_log_entry` event. Two recording points:

1. **Auto-swap tick** (usage_monitor.py:913) — after `db.record_decision()`, broadcast via `ws_registry`
2. **Manual switch** (routes/auth.py:888) — after `db.record_decision()`, broadcast via `ws_registry`

Payload includes account email/label for self-describing entries:
```python
{
    "id": <decision_id>,
    "account_id": ...,
    "email": ...,
    "label": ...,
    "action": "swap" | "stay" | "manual_switch",
    "trigger": "auto_swap" | "proactive_7d" | "tick" | "manual",
    "reason": "...",
    "timestamp": "...",
    "detail": { ... },  # full tick detail — already sanitized by _build_tick_detail
}
```

The `detail` field comes from `_build_tick_detail()` which constructs the JSON explicitly from safe fields — it does not include raw account rows with tokens. No additional sanitization needed.

#### 6b. `record_decision` returns the inserted ID

Use `cursor.lastrowid` (not `INSERT ... RETURNING` which requires SQLite 3.35+). The `lastrowid` is available on the cursor immediately after `execute()` within the `_writer()` context manager.

#### 6c. Frontend handler

```javascript
jackedWS.on('decision_log_entry', (msg) => {
    const container = document.getElementById('decision-log-container');
    if (container) {
        renderDecisionLog('decision-log-container');
    }
});
```

Re-render the whole table on new entry. The table is small (100-200 rows).

### 7. Decision Log Frontend QA

Browser-test with Playwright/Chrome MCP:
- Expandable rows toggle correctly
- Badge colors (teal=swap, blue=manual, gray=check)
- Filter toggle (show all ↔ show swaps only)
- Candidate table renders inside detail rows
- Decision flags display
- WebSocket live updates (after implementing section 6)
- XSS coverage in `escapeHtml` calls
- Empty state ("No decisions recorded yet")

### 8. Architecture Doc Update

Update `docs/architecture/auto-swap-system.md` to reflect all changes from sections 1-7.

## Observability Contract

Every state transition below MUST produce a log message at the specified level:

| Event | Level | Fields | Example |
|-------|-------|--------|---------|
| Circuit breaker activating | WARNING | account_id, failure_type, cooldown_seconds | `"Account 3: refresh failed (invalid_grant) — cooldown 600s"` |
| Circuit breaker blocking | DEBUG | account_id, failure_type, remaining_seconds | `"Account 3: circuit breaker active (invalid_grant, 420s remaining)"` |
| Circuit breaker expiring | INFO | account_id | `"Account 3: circuit breaker cooldown expired, re-attempting refresh"` |
| Stale token short-circuit | INFO | account_id, source | `"Account 3: token already refreshed by another path"` |
| Live credential import | INFO | account_id, fields_imported | `"Account 3: imported cc_access_token from live credentials"` |
| Live credential skip (invalid_grant) | DEBUG | account_id | `"Account 3: skipping cc_refresh_token import (invalid_grant active)"` |
| Heal loop clearing CB | INFO | account_id | `"Account 3: clearing circuit breaker for heal attempt"` |
| Poll loop watchdog | WARNING | last_tick_ago, expected_interval | `"Active poll loop delayed — last tick 340s ago, expected 150s"` |

## Files Modified

| File | Changes |
|------|---------|
| `jacked/web/auth.py` | `RefreshMode` enum, `_refresh_token_flow`, refactor 4 callers, circuit breaker to DB, heal loop fixes, poll interval computation moved, delete `_primary_refresh_state` |
| `jacked/web/database.py` | Migration: `refresh_last_failed_at`, `refresh_failure_type` columns + `_ACCOUNT_UPDATE_COLS` + Pydantic `Account` model. `record_decision` returns `lastrowid` |
| `jacked/web/auto_swap.py` | Normalize active hours defaults to 06:00/23:00 across all functions |
| `jacked/api/usage_monitor.py` | Include `_poll_interval`/`_poll_tier`/`_last_poll_at` in WS broadcast, move `_compute_poll_interval` before broadcast, decision log WS push, poll watchdog |
| `jacked/api/credential_helpers.py` | Rename `reconcile_outgoing_credentials` → `reconcile_credentials_from_live_store`, add 30s cache, never import cc_refresh_token during invalid_grant |
| `jacked/api/routes/auth.py` | Cached on-demand credential reconciliation for active account, decision log WS push for manual switch, extract cc_needs_auth helper |
| `jacked/data/web/js/components/account-actions.js` | Use backend `_poll_interval` + `_last_poll_at`, stale guard ("delayed"), restart handling ("starting..."), WS reconnect fetch |
| `jacked/data/web/js/websocket.js` | Handle `decision_log_entry` event, fetch state on reconnect |
| `jacked/data/web/js/components/auto-swap.js` | Re-render on WS push |
| `tests/unit/test_auto_swap.py` | Update active hours defaults |
| `tests/unit/test_auth.py` or new | Tests for `_refresh_token_flow` modes, circuit breaker DB persistence (cooldown expiry, scaled durations), live credential recovery (access token only, not refresh), lock sharing between modes, atomic DB writes |
| `docs/architecture/auto-swap-system.md` | Full update per section 8 |

## Non-Goals

- WebSocket pagination or virtual scrolling for decision log (table is small)
- Persisting poll tier/interval to DB (only needed for WS broadcast)
- Changing the adaptive tier thresholds or burn-rate projection logic
- Automatic recovery of non-active accounts' consumed CC refresh tokens (requires re-auth — acknowledged in section 3e)
