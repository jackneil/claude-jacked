# Auto-Swap, Window Keeper & OAuth Bug Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Automatically swap accounts when approaching rate limits, keep 5-hour windows rolling on all accounts, and fix the OAuth flow bugs that block account switching.

**Architecture:** A new background loop in the jacked server (alongside the existing token refresh and heal loops) monitors usage every 5 minutes, makes swap decisions based on configurable thresholds and burn rate, and pings idle accounts to keep 5h windows open. The swap decision engine and window keeper are isolated modules with pure-function cores that are easy to test. OAuth flow bugs (stale dedup + stuck action flag) are fixed first since they're prerequisites for reliable account switching.

**Tech Stack:** Python 3.12+ (FastAPI, asyncio, httpx), vanilla JS, SQLite

**Design spec:** `docs/superpowers/specs/2026-03-31-auto-swap-window-keeper-design.md`

---

## Note on Task 7 (Dashboard UI)

Task 7 describes the UI component structure but does not include full HTML/JS code inline (it would be 500+ lines). The implementer should follow the existing dashboard patterns in `jacked/data/web/js/components/settings.js` and `accounts.js` — Tailwind dark theme, `bg-slate-800` cards, `border-slate-700` borders, API calls via the `api` helper, and WebSocket event handling via `websocket.js`.

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/api/routes/auth.py` | Auth API endpoints | Remove OAuth dedup, add settings endpoints |
| `jacked/data/web/js/components/oauth-flows.js` | OAuth flow frontend | Fix re-click behavior, clear stale poll |
| `jacked/web/database.py` | Database schema | Add swap_log table, auto_swap_enabled column |
| `jacked/web/auto_swap.py` | NEW: Swap decision engine | Pure-function scoring + swap execution |
| `jacked/web/window_keeper.py` | NEW: Window keeper | Schedule evaluation + ping logic |
| `jacked/api/usage_monitor.py` | NEW: Usage monitor loop | Orchestrates auto-swap + window keeper |
| `jacked/api/main.py` | Server startup | Register monitor loop + router, update shutdown |
| `jacked/api/routes/settings_swap.py` | NEW: Settings API routes | CRUD for auto-swap/window-keeper settings |
| `jacked/data/web/js/components/auto-swap.js` | NEW: Dashboard UI | Settings panel, swap log, indicators |
| `jacked/data/web/index.html` | HTML shell | Include new JS file |
| `tests/unit/test_auto_swap.py` | NEW: Swap engine tests | Decision algorithm, scoring, burn rate |
| `tests/unit/test_window_keeper.py` | NEW: Window keeper tests | Schedule evaluation, ping decision |
| `tests/unit/test_usage_monitor.py` | NEW: Monitor loop tests | Integration: swap trigger, no-target, cached responses |

---

### Task 1: Fix OAuth flow bugs

**Files:**
- Modify: `jacked/api/routes/auth.py:758-769`
- Modify: `jacked/data/web/js/components/oauth-flows.js:285-415`

Two bugs: (1) Backend dedup returns stale flows with empty auth_url — no browser opens on re-click. (2) Frontend _accountActionInFlight blocks re-clicks for up to 2 minutes.

Fix: Remove backend dedup (every click starts a new flow). On frontend, cancel stale poll on re-click.

- [ ] **Step 1: Remove the backend dedup**

In `jacked/api/routes/auth.py`, find the `start_cc_auth` function (around line 743). Replace lines 758-772 with:

```python
    from jacked.web.oauth import OAuthFlow

    # Always start a fresh flow — every click opens a new browser window.
    # Old flows timeout after 2 minutes and clean up automatically.
    flow = OAuthFlow(db, purpose="claude_code", target_account_id=account_id)
    result = await flow.start()
    return result
```

This removes the `_active_flows` import and the entire dedup `for` loop.

- [ ] **Step 2: Fix the frontend re-click behavior**

In `jacked/data/web/js/components/oauth-flows.js`, replace lines 285-287:

```javascript
async function startCcAuthFlow(accountId, email) {
    if (window.jackedState._accountActionInFlight) return;
    window.jackedState._accountActionInFlight = true;
```

With:

```javascript
async function startCcAuthFlow(accountId, email) {
    // If a previous OAuth flow is still polling, cancel it and start fresh.
    // The user clicking again means they want a new browser window.
    if (window.jackedState.flowPolling) {
        clearInterval(window.jackedState.flowPolling);
        window.jackedState.flowPolling = null;
    }
    window.jackedState._accountActionInFlight = true;
```

Apply the same fix to ALL three OAuth flow functions in the same file:
- `startAddAccountFlow` (search for `async function startAddAccountFlow`)
- `startReauthFlow` (search for `async function startReauthFlow`)
Both have the same `_accountActionInFlight` guard that blocks re-clicks. Each should cancel `flowPolling` if active before proceeding.

- [ ] **Step 3: Run tests and commit**

Run: `uv run python -m pytest tests/ --tb=short 2>&1 | tail -5`

```bash
git add jacked/api/routes/auth.py jacked/data/web/js/components/oauth-flows.js
git commit -m "fix: OAuth flow always opens new browser window on re-click

Remove backend dedup that returned stale flows with empty auth_url.
Cancel stale frontend poll on re-click instead of blocking."
```

---

### Task 2: Database schema — swap_log table and auto_swap_enabled column

**Files:**
- Modify: `jacked/web/database.py`

- [ ] **Step 1: Add swap_log table and auto_swap_enabled column**

In `jacked/web/database.py`, find the schema definition section (where `CREATE TABLE IF NOT EXISTS` statements are). Add the swap_log table after the last existing table:

```sql
CREATE TABLE IF NOT EXISTS swap_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    from_account_id INTEGER,
    to_account_id INTEGER,
    reason TEXT,
    trigger TEXT,
    from_5h_usage REAL,
    from_7d_usage REAL,
    to_5h_usage REAL,
    to_7d_usage REAL
);
CREATE INDEX IF NOT EXISTS idx_swap_log_ts ON swap_log(timestamp);
```

In the migration section (where `ALTER TABLE ... ADD COLUMN` statements are), add:

```python
try:
    conn.execute("ALTER TABLE accounts ADD COLUMN auto_swap_enabled INTEGER NOT NULL DEFAULT 1")
except Exception:
    pass
```

- [ ] **Step 2: Add DB helper methods**

Add `record_swap()` and `list_swaps()` methods to the Database class:

```python
def record_swap(self, from_account_id, to_account_id, reason, trigger,
                from_5h=None, from_7d=None, to_5h=None, to_7d=None):
    """Record an account swap event."""
    with self._writer() as conn:
        cursor = conn.execute(
            """INSERT INTO swap_log
               (from_account_id, to_account_id, reason, trigger,
                from_5h_usage, from_7d_usage, to_5h_usage, to_7d_usage)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (from_account_id, to_account_id, reason, trigger,
             from_5h, from_7d, to_5h, to_7d),
        )
        return cursor.lastrowid

def list_swaps(self, limit=50):
    """List recent swap events."""
    with self._reader() as conn:
        rows = conn.execute(
            "SELECT * FROM swap_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]
```

- [ ] **Step 3: Run tests and commit**

Run: `uv run python -m pytest tests/ --tb=short 2>&1 | tail -5`

```bash
git add jacked/web/database.py
git commit -m "feat: add swap_log table and auto_swap_enabled account column"
```

---

### Task 3: Auto-swap decision engine

**Files:**
- Create: `jacked/web/auto_swap.py`
- Create: `tests/unit/test_auto_swap.py`

Pure-function core with no I/O. Given accounts + usage + thresholds, returns swap decisions.

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_auto_swap.py` with tests for `should_swap()`, `score_candidate()`, `pick_best_target()`, and `update_burn_rate()`. Test cases:

- `should_swap` returns True when 5h >= critical (90%)
- `should_swap` returns False when below warning (80%)
- `should_swap` returns True when 7d >= threshold (85%)
- `should_swap` returns True when warning + high burn rate projects hitting critical
- `should_swap` returns False when warning + low burn rate
- `should_swap` returns False when usage_5h is None (no data yet — don't swap on missing data)
- `score_candidate` scores lower usage higher
- `score_candidate` gives bonus for inactive 5h window (resets_at is None or in the past)
- `score_candidate` handles None usage values (treats as 0)
- `pick_best_target` excludes current account, high-7d accounts, no-CC-token accounts, disabled auto_swap accounts
- `pick_best_target` returns the best-scored candidate
- `pick_best_target` returns None when no candidates are eligible
- `pick_best_target` returns None when only one account exists
- `update_burn_rate` on first observation sets rate to 0 (no spike on first tick after restart)

Use a `_acct()` helper to build minimal account dicts for testing:
```python
def _acct(id, usage_5h=0, usage_7d=0, cc_token=True, active=True,
          failures=0, valid=True, auto_swap=True, resets_5h=None):
    return {
        "id": id, "email": f"user{id}@test.com",
        "cached_usage_5h": usage_5h, "cached_usage_7d": usage_7d,
        "cached_5h_resets_at": resets_5h,
        "cc_access_token": "tok" if cc_token else None,
        "is_active": 1 if active else 0, "is_deleted": 0,
        "consecutive_failures": failures,
        "validation_status": "valid" if valid else "invalid",
        "auto_swap_enabled": 1 if auto_swap else 0,
        "priority": id - 1, "access_token": f"at_{id}",
    }
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`

- [ ] **Step 3: Implement the auto-swap engine**

Create `jacked/web/auto_swap.py` with:
- `BurnRate` dataclass: `rate_5h_per_min`, `last_check_5h`, `rate_7d_per_min`, `last_check_7d`, `last_check_time`
- `should_swap(usage_5h, usage_7d, critical_5h, warning_5h, threshold_7d, burn_rate, check_interval_min)` → bool
- `score_candidate(account)` → float (100 - usage_5h - usage_7d*0.5 + 15 if window inactive)
- `pick_best_target(accounts, current_id, threshold_7d)` → dict or None
- `update_burn_rate(rates_dict, account_id, current_5h, current_7d)` → BurnRate

See the design spec for the full algorithm. The key behaviors:
- `should_swap`: True if 5h >= critical, or 7d >= threshold, or (5h >= warning AND projected_5h >= critical). Returns False if usage_5h is None (no data yet).
- `score_candidate`: Higher is better. Bonus +15 for inactive/expired 5h window. Treats None usage as 0.
- `pick_best_target`: Filters ineligible, scores rest, returns top scorer. Returns None if no candidates.
- `update_burn_rate`: On first observation (no previous data), sets rate_5h_per_min=0 and rate_7d_per_min=0 — does NOT compute a rate from 0→current, which would cause a false spike.

- [ ] **Step 4: Run tests and commit**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: auto-swap decision engine with burn rate tracking"
```

---

### Task 4: Window keeper

**Files:**
- Create: `jacked/web/window_keeper.py`
- Create: `tests/unit/test_window_keeper.py`

Schedule evaluation (pure functions) + ping logic (async, spawns claude subprocess).

- [ ] **Step 1: Write failing tests**

Create `tests/unit/test_window_keeper.py` with tests for `is_active_hours()`, `is_prewake_time()`, `needs_ping()`:

- `is_active_hours` True during day (14:00, start=06:00, end=23:00)
- `is_active_hours` False before start (05:00)
- `is_active_hours` False after end (23:30)
- `is_prewake_time` True within check interval of prewake time
- `is_prewake_time` False outside window
- `needs_ping` True when resets_at is in the past
- `needs_ping` True when resets_at is None
- `needs_ping` False when resets_at is in the future

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py -v`

- [ ] **Step 3: Implement the window keeper**

Create `jacked/web/window_keeper.py` with:
- `is_active_hours(now, start, end)` → bool — checks if current time is within active hours
- `is_prewake_time(now, prewake, check_interval_min)` → bool — checks if within pre-wake window
- `needs_ping(resets_at)` → bool — checks if 5h window is expired or never opened
- `async ping_account(cc_refresh_token, scopes, timeout)` → bool — spawns `claude -p "." --max-turns 1` with OAuth env vars, strips ANTHROPIC_API_KEY. Uses `asyncio.create_subprocess_exec` with `find_bin("claude")`. If `find_bin` returns None, logs a warning and returns False (does not crash). Handles timeout (kills process), non-zero exit code, and general exceptions.

**Security note:** The refresh token is passed via `CLAUDE_CODE_OAUTH_REFRESH_TOKEN` environment variable, which is visible to other local processes via `ps eww` or `/proc/PID/environ`. This is acceptable for a localhost-only tool — the threat model is other local processes, and anyone with that access could read the keychain directly. Document this tradeoff in the code comment.

- [ ] **Step 4: Run tests and commit**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py -v`

```bash
git add jacked/web/window_keeper.py tests/unit/test_window_keeper.py
git commit -m "feat: window keeper schedule evaluation and ping logic"
```

---

### Task 5: Usage monitor loop

**Files:**
- Create: `jacked/api/usage_monitor.py`
- Modify: `jacked/api/main.py`

The orchestrator lives in its own module (`usage_monitor.py`), not in `main.py` — the loop is too complex for inline definition. `main.py` only imports and registers it.

- [ ] **Step 1: Create the monitor module**

Create `jacked/api/usage_monitor.py` with `async def usage_monitor_loop(app):` containing:

1. Read settings from DB (auto_swap_enabled, window_keeper_enabled, intervals, thresholds, schedule)
2. If neither enabled, sleep and continue
3. Fetch usage for all active accounts (with fresh-token support and 1s pacing)
   - **Handle `{"_cached": True}` returns**: skip that account's usage update, use the existing DB-cached values
4. **Window keeper FIRST** (before auto-swap): check schedule (`is_active_hours` or `is_prewake_time`), ping accounts that `needs_ping`
5. **Auto-swap SECOND**: identify active account, compute burn rate (first tick: rate=0, no spike), call `should_swap()`, call `pick_best_target()`
   - **If target found**: call `sync_credential_to_all_stores()`, record swap in DB, broadcast WebSocket `auto_swap_triggered`
   - **If target is None** (all accounts exhausted): log warning, broadcast WebSocket `all_accounts_exhausted`, set cooldown flag (don't warn again for 30 minutes)
6. Sleep for the configured interval

- [ ] **Step 2: Register in startup and shutdown**

In `jacked/api/main.py`, at the `asyncio.create_task` block (around line 139), add:
```python
from jacked.api.usage_monitor import usage_monitor_loop
usage_monitor_task = asyncio.create_task(usage_monitor_loop(app))
logger.info("Started usage monitor loop")
```

Also find the shutdown/cancellation block (around line 152-158) and add `usage_monitor_task` to the list of tasks being cancelled.

- [ ] **Step 3: Write integration tests**

Create `tests/unit/test_usage_monitor.py` with tests for the monitor loop's key decisions (mock fetch_usage, DB, and credential_helpers):
- Test: auto-swap triggers `sync_credential_to_all_stores` when 5h > critical
- Test: no swap when both features disabled
- Test: `pick_best_target` returns None → logs warning, broadcasts `all_accounts_exhausted`
- Test: `fetch_usage` returns `{"_cached": True}` → loop uses DB-cached values, doesn't crash
- Test: window keeper pings only during active hours (mock `is_active_hours`)

- [ ] **Step 4: Run tests and commit**

Run: `uv run python -m pytest tests/ --tb=short 2>&1 | tail -5`

```bash
git add jacked/api/usage_monitor.py jacked/api/main.py tests/unit/test_usage_monitor.py
git commit -m "feat: usage monitor loop orchestrates auto-swap and window keeper

Runs in its own module (not inline in main.py). Handles cached
responses, no-target exhaustion with cooldown, and runs window
keeper before auto-swap for correct evaluation ordering."
```

---

### Task 6: Settings API routes

**Files:**
- Create: `jacked/api/routes/settings_swap.py`
- Modify: `jacked/api/main.py` (register router)

- [ ] **Step 1: Create the router**

Create `jacked/api/routes/settings_swap.py` with:
- Pydantic model `SwapSettings` with all config fields and defaults
- `GET /swap-settings` — reads from DB `get_setting()`, returns `SwapSettings`
- `PUT /swap-settings` — writes to DB `set_setting()`, returns updated settings
- `GET /swap-log` — returns `db.list_swaps(limit)`

- [ ] **Step 2: Register the router**

In `jacked/api/main.py`, add:
```python
from jacked.api.routes.settings_swap import router as swap_settings_router
app.include_router(swap_settings_router, prefix="/api/settings")
```

- [ ] **Step 3: Run tests and commit**

Run: `uv run python -m pytest tests/ --tb=short 2>&1 | tail -5`

```bash
git add jacked/api/routes/settings_swap.py jacked/api/main.py
git commit -m "feat: REST API for auto-swap and window keeper settings"
```

---

### Task 7: Dashboard UI for auto-swap settings

**Files:**
- Create: `jacked/data/web/js/components/auto-swap.js`
- Modify: `jacked/data/web/js/components/accounts.js`
- Modify: `jacked/data/web/index.html`

Build a collapsible settings panel on the accounts page. Follow existing patterns in `settings.js`.

- [ ] **Step 1: Create the auto-swap component**

Create `jacked/data/web/js/components/auto-swap.js` with:

- `async function loadAutoSwapSettings()` — `GET /api/settings/swap-settings`, stores in `window.jackedState.swapSettings`
- `function renderAutoSwapPanel()` — returns HTML string with:
  - Collapsible header "Auto-Swap & Window Keeper" with toggle arrow
  - Section 1: Auto-Swap toggles and sliders
    - Global enable/disable toggle
    - 5h warning threshold slider (0-100, default 80)
    - 5h critical threshold slider (0-100, default 90)
    - 7d threshold slider (0-100, default 85)
    - Usage check interval dropdown (1min, 2min, 5min, 10min, 15min)
  - Section 2: Window Keeper
    - Global enable/disable toggle
    - Active hours: start time picker, end time picker
    - Pre-wake time picker
  - Section 3: Recent Swaps log (last 10, loaded from `/api/settings/swap-log`)
- `function bindAutoSwapEvents()` — attaches change handlers that PUT to `/api/settings/swap-settings`

Follow existing patterns:
- Tailwind dark theme: `bg-slate-800`, `border-slate-700`, `text-slate-300`
- Toggles: same pattern as settings.js feature toggles
- Sliders: `<input type="range">` with value label
- Time pickers: `<input type="time">`
- Use `api.get()` and `api.put()` helpers

- [ ] **Step 2: Embed in accounts page**

In `jacked/data/web/js/components/accounts.js`, in `renderAccounts()`, add after the account cards `<div>`:

```javascript
${typeof renderAutoSwapPanel === 'function' ? renderAutoSwapPanel() : ''}
```

Call `loadAutoSwapSettings()` in the page load sequence and `bindAutoSwapEvents()` after render.

- [ ] **Step 3: Include the JS file**

In `jacked/data/web/index.html`, add before the closing `</body>`:

```html
<script src="/js/components/auto-swap.js"></script>
```

- [ ] **Step 4: Add WebSocket handler for swap events**

In `jacked/data/web/js/websocket.js`, add a handler for `auto_swap_triggered` events:

```javascript
if (event === 'auto_swap_triggered') {
    showToast(`Auto-swapped to ${data.to_email}`, 'info', 5000);
    if (typeof loadActiveCredential === 'function') loadActiveCredential();
    if (typeof refreshAndRender === 'function') refreshAndRender();
}
```

- [ ] **Step 5: Commit**

```bash
git add jacked/data/web/js/components/auto-swap.js jacked/data/web/js/components/accounts.js jacked/data/web/index.html jacked/data/web/js/websocket.js
git commit -m "feat: dashboard UI for auto-swap and window keeper settings"
```

---

### Task 8: Run full test suite and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ -v --tb=short`

- [ ] **Step 2: Manual end-to-end test**

1. Enable auto-swap in dashboard, set low thresholds for testing
2. Verify usage monitor loop runs (check server logs)
3. Verify swap triggers when threshold hit
4. Check swap log in dashboard
5. Enable window keeper, verify idle accounts get pinged
6. Verify WebSocket notifications appear as toasts
