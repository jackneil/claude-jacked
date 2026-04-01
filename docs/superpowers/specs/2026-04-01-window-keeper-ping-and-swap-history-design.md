# Window Keeper Ping Fix & Swap History UI

**Date:** 2026-04-01
**Status:** Draft

## Problem

Two issues with the auto-swap / window keeper system:

1. **Window keeper pings don't work.** `ping_account()` spawns `claude -p "." --max-turns 1` with `CLAUDE_CODE_OAUTH_REFRESH_TOKEN`, but Claude Code's credential resolution ignores that env var during `-p` mode — it only reads `CLAUDE_CODE_OAUTH_TOKEN` (access token) or the keychain (which has the active account's creds). Result: pings either use the wrong account or silently fail. Accounts 2 and 3 show expired 5h windows despite being pinged every sweep.

2. **Swap history not visible.** The "Recent Swaps" section is buried inside the collapsed auto-swap settings panel. Additionally, the swap log table has two rendering bugs: timestamps are parsed as epoch (they're ISO strings) and email fields reference `from_email`/`to_email` which don't exist in the API response.

## Solution

### 1. Replace subprocess ping with direct API call

Replace the `claude -p` subprocess in `ping_account()` with a direct `httpx.POST` to `https://api.anthropic.com/v1/messages` using the account's `cc_access_token` from the DB.

**Validated approach:** A test call with account 3's `cc_access_token` returned HTTP 200 and opened a fresh 5h window (resets_at moved from 2026-03-31 to 2026-04-01T15:59:59).

**Call details:**
- Endpoint: `POST https://api.anthropic.com/v1/messages`
- Headers: `Authorization: Bearer {cc_access_token}`, `anthropic-version: 2023-06-01`, `anthropic-beta: oauth-2025-04-20`
- Body: `{"model": "claude-haiku-4-5-20251001", "max_tokens": 1, "messages": [{"role": "user", "content": "hi"}]}`
- Cost: 8 input tokens + 1 output token per ping (trivial)

**Error handling:**
- 200: success, window opened
- 401: token expired — log warning, return False (30-minute refresh loop will fix it next cycle)
- 429: rate limited — log warning with retry-after, return False
- Other: log error, return False

**What this eliminates:**
- 10-30s subprocess startup → ~1-2s direct call
- `findbin("claude")` dependency for pings
- Credential resolution bugs (keychain vs env var confusion)
- Zombie subprocess risk
- `subprocess` import

**What stays:** The `ping_account` function signature changes from `(cc_refresh_token, scopes)` to `(cc_access_token)`. The call site in `usage_monitor.py` passes `acct.get("cc_access_token")` instead of `acct.get("cc_refresh_token")`. Schedule logic, `needs_ping()`, sweep loop — all unchanged.

### 2. Fix swap log rendering bugs

Two bugs in `auto-swap.js` `renderSwapLogTable()`:

**Bug A — Timestamp parsing:** Line 218 does `new Date(e.timestamp * 1000)` but `timestamp` is an ISO string like `"2026-04-01T10:45:39.078Z"`, not a Unix epoch. Fix: `new Date(e.timestamp)`.

**Bug B — Missing email fields:** Lines 220-221 reference `e.from_email` and `e.to_email` which don't exist in the `swap_log` DB table or API response. The response only has `from_account_id` and `to_account_id`.

**Fix approach:** Join account emails in the backend `list_swaps()` query. This is cleaner than client-side lookup because the account list may not be loaded when the swap log renders. Add `from_email` and `to_email` to the swap log response via a LEFT JOIN on the accounts table.

### 3. Add swap history section to accounts page

Move the swap history out of the collapsed settings panel and into its own always-visible section at the bottom of the accounts page.

- Standalone card: "Swap History" header, table showing last 20 swaps
- Columns: Time, From → To (email), Reason, Trigger (auto_swap / manual)
- Auto-refreshes when the page loads and on `auto_swap_triggered` WebSocket events
- Reuses the (fixed) `renderSwapLogTable()` function

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/window_keeper.py` | Replace subprocess with httpx.POST |
| `jacked/api/usage_monitor.py` | Pass cc_access_token instead of cc_refresh_token |
| `jacked/web/database.py` | Join emails into list_swaps() query |
| `jacked/data/web/js/components/auto-swap.js` | Fix timestamp parsing, fix email fields |
| `jacked/data/web/js/components/accounts.js` | Add swap history section |
| `tests/unit/test_window_keeper.py` | Update ping_account tests for new signature |

## What This Does NOT Do

- Does not change the token refresh loop (already works for all accounts)
- Does not change the sweep schedule or `needs_ping()` logic
- Does not change the auto-swap decision engine
- Does not add per-account window keeper toggles to the UI (future enhancement)
