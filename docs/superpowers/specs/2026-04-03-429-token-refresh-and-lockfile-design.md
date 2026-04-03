# 429 Recovery via Token Refresh with Cross-Process Locking

**Date:** 2026-04-03
**Status:** Draft

## Problem

Three related issues with usage API rate limiting:

1. **429 cycle with no escape.** When the active account gets rate-limited (429), the adaptive tier stays at "critical" (65s) based on stale data, and the ceiling is also 65s. After backoff expires, the ceiling also expires, and the next poll immediately hits the API again → 429 → repeat forever. The account's `usage_cached_at` never updates because no fetch succeeds, so the UI shows stale data.

2. **Rate limits are per-access-token.** Empirically proved: old token → 429, fresh token (same account, obtained via refresh) → 200. Getting a fresh access token clears the rate limit. But refreshing requires the refresh token, which may rotate (invalidating the old one).

3. **Token refresh requires cross-process coordination.** Both jacked and Claude Code use the same credential stores (Keychain + `.credentials.json`). Claude Code uses `proper-lockfile` on `~/.claude/` to coordinate refreshes. Jacked must participate in this locking protocol to avoid breaking Claude Code sessions.

## Solution

### 1. On 429, attempt token refresh to clear the rate limit

When `fetch_usage` gets a 429 and the account has a refresh token (`cc_refresh_token` or `refresh_token`), attempt to exchange it for a fresh access token before falling back to backoff. The fresh token won't be rate-limited.

**Flow:**
```
On 429:
  1. If account has a refresh token:
     a. Acquire cross-process lock (~/.claude.lock)
     b. Re-read tokens from secure storage (another process may have refreshed)
     c. If tokens are fresh (access_token changed since our last read), use them
     d. Otherwise, exchange refresh_token → fresh access_token via OAuth endpoint
     e. Save ALL new tokens to: DB + credentials file + keychain
     f. Release lock
     g. Retry usage fetch with fresh access token
  2. If no refresh token available:
     a. Escalating backoff (see below)
```

### 2. Escalating backoff on consecutive 429s

When refresh isn't available (no refresh token) or refresh itself fails, escalate the backoff exponentially instead of staying at 65s:

- 1st 429: 65s backoff
- 2nd consecutive: 130s
- 3rd: 260s
- Cap at 900s (15 min)
- Reset to 65s on any successful fetch

Track consecutive 429 count in the per-account coordinator state.

### 3. Drop urgency tier on consecutive 429s

When usage fetches are failing, the tier should NOT stay at "critical" based on stale data. After 3 consecutive 429s, force the tier to "idle" (300s) regardless of cached usage values. This prevents the fast-poll → 429 → fast-poll cycle.

Reset the tier override on any successful fetch.

### 4. Cross-process lock implementation

Implement `proper-lockfile`-compatible locking in Python:

- **Lock:** `os.mkdir(~/.claude.lock)` (atomic — fails if exists)
- **Stale detection:** Write PID file inside the lock dir. Before failing on EEXIST, check if the PID is alive. If dead, remove the stale lock and retry.
- **Timeout:** Retry up to 5 times with 1-2s jittered delay (same as Claude Code's `MAX_RETRIES = 5`)
- **Release:** Remove the lock directory in a `finally` block
- **Claude Code compatibility:** After writing new tokens and releasing the lock, Claude Code detects `.credentials.json` mtime change and invalidates its cache automatically

### 5. Token storage after refresh

After a successful token exchange, update ALL stores atomically (same as `sync_credential_to_all_stores` pattern):

1. Update DB: `cc_access_token`, `cc_refresh_token` (if rotated), `cc_expires_at`
2. Write `~/.claude/.credentials.json` with new `claudeAiOauth` block
3. Write macOS Keychain with same data
4. Claude Code auto-detects via mtime and re-reads

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auth.py` | `fetch_usage`: on 429, attempt token refresh before backoff. Escalating backoff. Track consecutive 429 count. |
| `jacked/api/credential_helpers.py` | Add `acquire_claude_lock()` / `release_claude_lock()` for cross-process locking. Add `refresh_and_save_token()` that exchanges + writes all stores. |
| `jacked/api/usage_monitor.py` | `_compute_poll_interval`: force idle tier after N consecutive 429s |
| `jacked/web/auth.py` | `compute_urgency_tier`: accept optional `force_idle` override |

## What This Does NOT Change

- The normal (non-429) polling flow
- The window keeper ping mechanism
- The swap decision algorithm
- The UI display (countdown, tier labels)

## Risks

- **Token rotation during refresh breaks Claude Code** if we don't acquire the lock first. The lock is mandatory.
- **Lock contention** if Claude Code and jacked both try to refresh simultaneously. The retry logic (5 attempts, 1-2s jitter) handles this.
- **Stale lock from crashed process** requires PID-based detection and cleanup.
