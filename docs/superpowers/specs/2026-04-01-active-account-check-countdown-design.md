# Active Account Countdown, Smart Fetch Deduplication & 429 Backoff

**Date:** 2026-04-01
**Status:** Approved

## Problem

Three related issues with usage monitoring:

1. **No visibility into active account polling.** The server polls the active account every 60s for auto-swap decisions, but the UI shows no indication of when the next check happens.

2. **Redundant usage fetches cause 429 rate limits.** The active account gets hit by multiple independent callers: the 60s active poll, the 5-min sweep, the UI auto-refresh, and post-ping fetches. Account 5 (udifi) is getting 429'd every minute because these callers don't coordinate. The 30s cache guard helps but is bypassed when `access_token` is passed directly.

3. **No 429 backoff.** When the usage endpoint returns 429, we log a warning and retry at the normal interval. We should back off.

## Solution

### 1. Smart Fetch Deduplication

Each caller has its own check interval. Before fetching, check if `usage_cached_at` is fresh enough for that caller's needs. If another caller already updated it recently enough, skip.

**Rule:** Before calling the usage API for an account, compare:
```
time_since_last_check = now - usage_cached_at
if time_since_last_check < caller_interval:
    skip (already fresh enough for this caller)
```

| Caller | Interval | Behavior |
|--------|----------|----------|
| Active poll loop (60s) | 55s | Skip if checked within 55s (5s buffer for timing drift) |
| Full sweep loop | `usage_check_interval - 10` | Skip if checked within interval minus buffer |
| UI auto-refresh | Whatever the user configured | Skip if checked within that interval |
| Manual "Refresh" button | 0 (always fetch) | Never skip — user explicitly asked |
| Post-ping fetch | 0 (always fetch) | Never skip — need fresh resets_at after ping |

**Implementation:** The cache guard in `fetch_usage()` already checks `usage_cached_at`. Currently it uses a fixed 30s `USAGE_CACHE_FRESHNESS_SECONDS`. Change this to accept a `min_age` parameter so each caller specifies its own threshold:

```python
async def fetch_usage(account_id, db, access_token=None, min_age=30):
    # If min_age > 0 and cache is fresher than min_age, skip
    if min_age > 0 and cached_at:
        age = now - cached_at
        if age < min_age:
            return {"_cached": True}
    # Otherwise fetch from API
```

- Active poll: `fetch_usage(id, db, access_token=tok, min_age=55)`
- Sweep: `fetch_usage(id, db, min_age=interval-10)`
- UI refresh: `fetch_usage(id, db)` (uses default 30s)
- Manual button: `fetch_usage(id, db, min_age=0)` (always fetch)
- Post-ping: `fetch_usage(id, db, access_token=tok, min_age=0)` (always fetch)

**Key change:** The `access_token` parameter no longer bypasses the cache guard. Instead, `min_age=0` explicitly bypasses it. This decouples "use this token" from "force refresh."

### 2. 429 Backoff

When `fetch_usage` gets a 429, record a per-account backoff timestamp. On subsequent calls, skip if still in backoff.

**Mechanism:** Module-level dict `_usage_backoff: dict[int, float]` maps account ID to "don't fetch before this time." On 429:
```python
retry_after = int(resp.headers.get("retry-after", "60"))
_usage_backoff[account_id] = time.time() + max(retry_after, 60)
```

Before each fetch, check: `if time.time() < _usage_backoff.get(account_id, 0): skip`.

The backoff is checked BEFORE the cache guard, so even `min_age=0` (manual refresh) respects it. Exception: if the user explicitly clicks "Refresh" on a single account, we could bypass backoff — but hammering a 429'd endpoint won't help, so it's better to show a message like "Rate limited, try again in Xs."

### 3. Active Account Countdown Timer

On the active account card, when auto-swap is enabled, show a live countdown: `· auto-check in 45s`.

**Rendering:** Append to the existing `renderCacheAge()` output:
```
Usage updated 15s ago · auto-check in 45s  [refresh icon]
```

- Color: `text-teal-500` (matches auto-swap theme)
- Calculation: `remaining = (usage_cached_at + 60) - now`
- At zero or negative: show "checking..."
- Only visible when: `swapSettings.auto_swap_enabled === true` AND account is the active CC account

**Ticking:** A single page-scoped `setInterval(1000)` updates all `[data-next-check]` elements each second. Uses `data-cached-at` attribute on the element for calculation. Cleared on route change.

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auth.py` | Change `fetch_usage` to accept `min_age` parameter; decouple `access_token` from cache bypass; add 429 backoff dict and check |
| `jacked/api/usage_monitor.py` | Pass `min_age=55` in active poll, `min_age=interval-10` in sweep; post-ping uses `min_age=0` |
| `jacked/api/routes/auth.py` | Manual refresh endpoint passes `min_age=0`; bulk refresh passes `min_age=0` |
| `jacked/data/web/js/components/accounts.js` | Modify `renderCacheAge()` to accept acctId, render countdown span with `data-next-check` and `data-cached-at` |
| `jacked/data/web/js/components/account-actions.js` | Add `setInterval(1000)` for countdown tick; clear on unmount |
| `tests/unit/test_usage_refresh.py` | Update tests for new `min_age` parameter; add 429 backoff test |

## What This Does NOT Do

- Does not change the 60-second active poll interval itself
- Does not add new API endpoints
- Does not change the UI auto-refresh dropdown behavior (it still triggers client-side refresh, which calls the existing endpoint)
- Does not show countdown for non-active accounts
