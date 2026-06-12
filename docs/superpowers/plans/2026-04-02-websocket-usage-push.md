# WebSocket Usage Push Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After the active poll loop fetches usage, broadcast the fresh data via WebSocket so the browser updates immediately — no "checking..." gap, no client-side polling fallback.

**Architecture:** Add a `usage_poll_updated` WebSocket broadcast in the active poll loop after successful fetch. Add a JS handler that updates the account card DOM and resets the countdown timer. Remove the 15-second `refreshAndRender` recovery hack.

**Tech Stack:** Python 3.12+ (asyncio, WebSocket), vanilla JS

---

## File Structure

| File | Change |
|------|--------|
| `jacked/api/usage_monitor.py` | Broadcast `usage_poll_updated` after successful fetch in active poll loop |
| `jacked/data/web/js/websocket.js` | Add handler for `usage_poll_updated` — update card DOM + reset countdown |
| `jacked/data/web/js/components/account-actions.js` | Remove the 15s `refreshAndRender` recovery hack |

---

### Task 1: Broadcast usage data after active poll fetch

**Files:**
- Modify: `jacked/api/usage_monitor.py`

After `fetch_usage` succeeds and `active_acct` is re-read from DB (around line 195-200), broadcast the fresh account data via WebSocket. Add after the `active_acct` null check (after line 208):

- [ ] **Step 1: Add WebSocket broadcast after successful fetch**

Find the block around lines 194-208 where `active_acct` is read from DB. After the null check (`if active_acct is None: ... continue`), add:

```python
            # Push fresh usage data to connected dashboards so the
            # countdown timer and usage bars update immediately.
            ws_registry = getattr(app.state, "ws_registry", None)
            if ws_registry and active_acct:
                await ws_registry.broadcast(
                    "usage_poll_updated",
                    {
                        "account_id": active_acct_id,
                        "account_data": active_acct,
                    },
                )
```

Note: `ws_registry` is used later in the swap block too. Move the `getattr` above both usages so it's computed once. Actually, the swap block already has its own `ws_registry = getattr(...)`. Just add the new broadcast — the double `getattr` is harmless.

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py --tb=short -q`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: broadcast usage_poll_updated via WebSocket after active poll"
```

---

### Task 2: Add JS handler for `usage_poll_updated`

**Files:**
- Modify: `jacked/data/web/js/websocket.js`

- [ ] **Step 1: Add WebSocket handler**

Add after the existing `all_accounts_exhausted` handler (around line 436):

```javascript
jackedWS.on('usage_poll_updated', (msg) => {
    var d = msg.payload || msg;
    if (!d.account_id || !d.account_data) return;

    // Update the account in jackedState so countdown reads fresh data
    var accounts = window.jackedState.accounts || [];
    for (var i = 0; i < accounts.length; i++) {
        if (accounts[i].id === d.account_id) {
            // Merge fresh fields into existing account object
            Object.assign(accounts[i], d.account_data);
            break;
        }
    }

    // Surgically update the card DOM (usage bars + cache age)
    var card = document.querySelector('[data-account-id="' + d.account_id + '"]');
    if (card && typeof _usageUpdateCardDOM === 'function') {
        _usageUpdateCardDOM(card, d.account_data);
    }
});
```

This handler:
1. Updates `window.jackedState.accounts` so the countdown tick reads fresh `usage_cached_at`
2. Calls `_usageUpdateCardDOM` to surgically update usage bars and cache age text (same function used by `usage_refresh_progress`)

- [ ] **Step 2: Commit**

```bash
git add jacked/data/web/js/websocket.js
git commit -m "feat: handle usage_poll_updated WebSocket event in dashboard"
```

---

### Task 3: Remove the refreshAndRender recovery hack

**Files:**
- Modify: `jacked/data/web/js/components/account-actions.js`

The 15-second `refreshAndRender` recovery was a workaround for the server not pushing updates. Now that the server broadcasts via WebSocket, the recovery is unnecessary.

- [ ] **Step 1: Remove the _checkingTicks recovery block**

In `_startCheckCountdown`, remove the recovery block that calls `refreshAndRender` after 15 seconds of "checking...". Change:

```javascript
        // If stuck at "checking" for >15s, re-fetch accounts to get fresh
        // usage_cached_at from the server (the backend poll updated it but
        // didn't push via WebSocket).
        if (rem === 0) {
            _checkingTicks++;
            if (_checkingTicks >= 15 && typeof refreshAndRender === 'function') {
                _checkingTicks = 0;
                refreshAndRender();
            }
        } else {
            _checkingTicks = 0;
        }
```

To just:

```javascript
        // No recovery needed — server pushes fresh data via
        // usage_poll_updated WebSocket event.
```

Also remove the `_checkingTicks` variable declaration (around line 12: `var _checkingTicks = 0;`).

- [ ] **Step 2: Commit**

```bash
git add jacked/data/web/js/components/account-actions.js
git commit -m "fix: remove refreshAndRender recovery hack, WebSocket push handles it"
```

---

### Task 4: Run full test suite

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

Expected: All tests pass.
