# On-Demand Usage Fetch for Non-Active Accounts

**Date:** 2026-04-03
**Status:** Approved (revised after DCR)

## Problem

The full sweep loop fetches usage for ALL non-active accounts every `usage_check_interval` (120s) in the background, even when no swap is being considered and the user hasn't requested a refresh. This wastes API calls, contributes to 429 rate limiting, and confuses the UI.

## Solution

### 1. Remove bulk usage fetch from sweep loop

Delete the usage-fetch-all-accounts block from `full_sweep_loop`. The sweep becomes window-keeper-only: check schedule, ping expired windows. `needs_ping` uses `cached_5h_resets_at` already in the DB.

Also remove the now-unnecessary `accounts = db.list_accounts()` re-read that followed the deleted bulk fetch (it was there to get "fresh data" after the fetch).

### 2. Fetch candidate usage on-demand at swap time

In the active poll loop, when `should_swap` returns True (or the escape hatch fires), fetch fresh usage for all non-active candidate accounts BEFORE calling `pick_best_target`.

Also fetch in the **exhaustion path** (all accounts at capacity, no target found) — the `next_recovery_at` computation needs fresh `cached_5h_resets_at` from non-active accounts.

**Fallback on failure:** If some or all candidate fetches fail (429/backoff/error), proceed with whatever data is available. Stale data is better than no swap when the active account is exhausted.

### 3. Staleness penalty in score_candidate

Add a staleness penalty: if `usage_cached_at` is older than 30 minutes, reduce the score. This prevents the reset proximity bonus (+30) from firing on ancient data where the "imminent reset" already happened hours ago.

Formula: if `usage_cached_at` is older than 1800 seconds (30 min), cap the reset proximity bonus to 0 and reduce the overall score by 10 points.

### 4. Prime-the-pump fetch on auto-swap enable

When the active poll loop first runs and finds auto-swap enabled, do a one-time bulk fetch for all accounts to establish baseline data. This prevents:
- "Never fetched" showing in the UI for non-active accounts
- Blind first swap with no candidate data

Track with a module-level flag (`_initial_fetch_done`) so it only runs once per server lifetime.

### 5. UI auto-refresh remains independent

The "Auto: Off/2min/5min" UI dropdown still calls the bulk refresh API endpoint when the user chooses to enable it. This is user-driven.

## Files Affected

| File | Change |
|------|--------|
| `jacked/api/usage_monitor.py` | Remove bulk fetch from sweep. Add on-demand fetch before `pick_best_target` and in exhaustion path. Add prime-the-pump on first auto-swap tick. |
| `jacked/web/auto_swap.py` | `score_candidate`: add staleness penalty when `usage_cached_at` is old |

## What This Does NOT Change

- Active account adaptive polling
- Window keeper ping logic
- Manual "Refresh All" button
- Coordinator ceiling and 429 recovery
- The `usage_check_interval` setting (controls sweep/ping frequency)
