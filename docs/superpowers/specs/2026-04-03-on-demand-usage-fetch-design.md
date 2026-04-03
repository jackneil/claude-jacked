# On-Demand Usage Fetch for Non-Active Accounts

**Date:** 2026-04-03
**Status:** Approved

## Problem

The full sweep loop fetches usage for ALL non-active accounts every `usage_check_interval` (default 120s) in the background, even when:
- No swap is being considered
- The user hasn't requested a refresh
- The window keeper only needs `cached_5h_resets_at` (already in DB) to decide pings

This wastes API calls, contributes to 429 rate limiting, and confuses the UI (non-active accounts show "updated just now" while the active account shows stale data).

## Solution

### 1. Remove bulk usage fetch from sweep loop

Delete the usage-fetch-all-accounts block from `full_sweep_loop`. The sweep becomes window-keeper-only: check schedule, ping expired windows. `needs_ping` uses `cached_5h_resets_at` already in the DB — it doesn't need a fresh API call.

### 2. Fetch candidate usage on-demand at swap time

In the active poll loop, when `should_swap` returns True (or the escape hatch fires), fetch fresh usage for all non-active candidate accounts BEFORE calling `pick_best_target`. This adds ~5 seconds of delay at swap time but ensures scoring uses current data.

### 3. UI auto-refresh remains independent

The "Auto: Off/2min/5min" UI dropdown still calls the bulk refresh API endpoint when the user chooses to enable it. This is user-driven and works through the coordinator (with `manual=True`).

## Files Affected

| File | Change |
|------|--------|
| `jacked/api/usage_monitor.py` | Remove usage fetch block from `full_sweep_loop`. Add on-demand candidate fetch before `pick_best_target` in active poll loop. |

## What This Does NOT Change

- Active account adaptive polling
- Window keeper ping logic (still checks `needs_ping`, still pings)
- Manual "Refresh All" button
- Coordinator ceiling and 429 recovery
- The `usage_check_interval` setting (still controls sweep/ping frequency)
