# Auto-Swap & Window Keeper Design

**Date:** 2026-03-31
**Status:** Approved

## Problem

Users with multiple Claude accounts burn through rate limits on one account while others sit idle. The 5-hour usage window starts when an API call is made with that account — if no call is made, the window stays closed. Users must manually monitor usage percentages, decide when to switch, and remember to "ping" idle accounts to keep their 5h windows rolling. This is tedious and wastes capacity.

## Solution

Two features in the jacked server's background loop:

1. **Auto-Swap** — automatically switch the active Claude Code account when the current account approaches its rate limit, choosing the optimal next account based on usage levels and burn rate.

2. **Window Keeper** — keep 5-hour windows rolling on all accounts during active hours by making lightweight Claude Code calls when windows expire. During quiet hours, let windows expire naturally. Before wake time, ping all accounts to open fresh windows.

## Architecture

### Usage Monitor Loop

A new background loop in `jacked/api/main.py` alongside the existing `_token_refresh_loop` (30min) and `_heal_sweep_loop` (5min).

- **Default interval:** 5 minutes (configurable via dashboard settings)
- **Each tick:**
  1. Fetch usage for all active accounts (reuses `fetch_usage` with fresh-token support)
  2. Evaluate auto-swap: should the current active account be swapped?
  3. Evaluate window keeper: should any idle account be pinged?
  4. Execute decisions (swap credentials, ping accounts)
  5. Broadcast status via WebSocket for dashboard updates

### Auto-Swap Decision Algorithm

```
On each usage check:

1. Identify the currently active account (via _jackedAccountId stamp or email match)

2. Compute burn rate for active account:
   - delta = current_5h_usage - previous_5h_usage
   - rate = delta / check_interval_minutes
   - time_to_critical = (critical_threshold - current_5h_usage) / rate

3. Decide whether to swap:
   - IF 5h_usage >= critical_threshold (default 90%): SWAP NOW
   - IF 5h_usage >= warning_threshold (default 80%) AND time_to_critical < 2 * check_interval: SWAP NOW
   - IF 7d_usage >= 7d_threshold (default 85%): SWAP NOW
   - OTHERWISE: no swap needed

4. Choose the best target account:
   a. Filter candidates:
      - is_active = true, is_deleted = false
      - consecutive_failures < 3
      - validation_status != 'invalid'
      - has CC tokens (cc_access_token is not NULL)
      - auto_swap_enabled = true (per-account toggle, default true)
      - NOT the current active account
   b. Score each candidate:
      - Start with 100 points
      - Subtract 5h_usage (lower is better)
      - Subtract 7d_usage * 0.5 (factor in but weight less)
      - Add bonus for accounts with INACTIVE 5h windows (encourages opening them)
      - Adjust for 7d window position: more remaining days = more room
   c. Pick highest score. If tied, use priority order.

5. Execute swap:
   - Call sync_credential_to_all_stores(target_account_id, target_account)
   - Log the swap with reason
   - Broadcast WebSocket event: "auto_swap_triggered"
   - Record in DB: swap_log table (timestamp, from_account, to_account, reason)
```

### Window Keeper

The window keeper runs within the same usage monitor loop.

**Active hours** (default 6:00 AM - 11:00 PM local time):
- For each account where `cached_5h_resets_at` is in the past (window expired) or NULL (never opened):
  - Spawn a lightweight Claude Code process:
    ```bash
    CLAUDE_CODE_OAUTH_REFRESH_TOKEN=<cc_refresh_token> \
    CLAUDE_CODE_OAUTH_SCOPES="user:profile user:inference user:sessions:claude_code user:mcp_servers user:file_upload" \
    ANTHROPIC_API_KEY= \
    claude -p "." --max-turns 1
    ```
  - This makes one API call with that account's token, opening a new 5h window
  - Wait for process to complete (timeout: 30 seconds)
  - Fetch fresh usage to confirm window opened

**Quiet hours** (default 11:00 PM - pre-wake time):
- Do nothing. Let windows expire naturally.

**Pre-wake activation** (default 4:00 AM):
- Ping ALL accounts to open fresh 5h windows
- These windows last until 9:00 AM, giving the user full capacity when they start working at 6:00 AM

### Configuration

Stored in the jacked DB `settings` table (key-value pairs):

| Setting | Default | Description |
|---------|---------|-------------|
| `auto_swap_enabled` | `false` | Global toggle for auto-swap |
| `auto_swap_5h_warning` | `80` | 5h utilization warning threshold (%) |
| `auto_swap_5h_critical` | `90` | 5h utilization critical threshold — triggers swap (%) |
| `auto_swap_7d_threshold` | `85` | 7d utilization threshold — avoid swapping TO accounts above this (%) |
| `usage_check_interval` | `300` | Usage check interval in seconds |
| `window_keeper_enabled` | `false` | Global toggle for window keeper |
| `window_keeper_active_start` | `06:00` | Start of active hours (local time, HH:MM) |
| `window_keeper_active_end` | `23:00` | End of active hours (local time, HH:MM) |
| `window_keeper_prewake` | `04:00` | Pre-wake activation time (local time, HH:MM) |

Per-account settings in the `accounts` table:

| Field | Default | Description |
|-------|---------|-------------|
| `auto_swap_enabled` | `true` | Include this account in auto-swap rotation |

### Dashboard UI

**Accounts page additions:**
- Auto-swap settings panel (collapsible, below the account cards)
  - Global toggles for auto-swap and window keeper
  - Threshold sliders with current values
  - Time pickers for active hours and pre-wake
  - Usage check interval selector
- Per-account card:
  - Toggle: "Include in auto-swap"
  - Badge: "Auto-swapped at {time}" when a swap occurred
  - Window status: "5h window active (resets in 2h 15m)" or "5h window closed"
- Toast notifications on swap events
- Activity log showing recent swaps and window pings

### Swap Log Table

New DB table `swap_log`:

```sql
CREATE TABLE IF NOT EXISTS swap_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    from_account_id INTEGER,
    to_account_id INTEGER,
    reason TEXT,
    trigger TEXT,  -- 'auto_swap' | 'window_keeper' | 'manual'
    from_5h_usage REAL,
    from_7d_usage REAL,
    to_5h_usage REAL,
    to_7d_usage REAL
);
```

### Burn Rate Tracking

Ephemeral in-memory dict (not persisted to DB):

```python
_burn_rates: dict[int, BurnRate] = {}

@dataclass
class BurnRate:
    account_id: int
    previous_5h: float  # last check's 5h utilization
    previous_7d: float  # last check's 7d utilization
    rate_5h_per_min: float  # percentage points per minute
    rate_7d_per_min: float
    last_check: float  # time.time()
```

Updated on each usage check. Reset on server restart (conservative — no stale data).

## What This Does NOT Do

- Does not change mid-conversation behavior (credential switch affects the next CC API call naturally)
- Does not auto-swap back when the original account resets (future enhancement)
- Does not manage per-model rate limits separately (uses aggregate 5h/7d only)
- Does not touch the `_token_refresh_loop` or `_heal_sweep_loop` — this is a new, independent loop

## Files Affected

| File | Change |
|------|--------|
| `jacked/api/main.py` | Add `_usage_monitor_loop` background task |
| `jacked/web/auto_swap.py` | NEW: swap decision engine, burn rate tracker |
| `jacked/web/window_keeper.py` | NEW: window ping logic, schedule evaluation |
| `jacked/web/database.py` | Add `swap_log` table, settings queries, per-account `auto_swap_enabled` |
| `jacked/api/routes/auth.py` | Add settings endpoints for thresholds/schedule |
| `jacked/data/web/js/components/accounts.js` | Auto-swap settings panel, per-account toggles |
| `jacked/data/web/js/components/account-actions.js` | Swap event handling |
| `tests/unit/test_auto_swap.py` | NEW: swap decision tests |
| `tests/unit/test_window_keeper.py` | NEW: window keeper tests |

## Testing

- Unit tests for swap decision algorithm (various usage levels, burn rates, edge cases)
- Unit tests for window keeper schedule evaluation (active hours, quiet hours, pre-wake)
- Unit tests for target account scoring
- Integration test: mock usage data → verify correct swap decision → verify credentials written
- Manual test: run with dashboard open, observe auto-swap triggering and notifications
