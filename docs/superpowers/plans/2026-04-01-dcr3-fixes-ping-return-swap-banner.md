# DCR3 Fixes: Ping Return Value, Swap Banner, Dedup History

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 findings from DCR3: handle ping return value, add persistent swap banner, remove duplicate swap history, split httpx timeout, validate midnight-crossing hours.

**Architecture:** Targeted fixes across 5 files. No new modules. Each fix is independent.

**Tech Stack:** Python 3.12+ (httpx, FastAPI), vanilla JS

---

## File Structure

| File | Change |
|------|--------|
| `jacked/web/window_keeper.py` | Split httpx timeout |
| `jacked/api/usage_monitor.py` | Check ping return value, fetch usage after successful ping |
| `jacked/api/routes/settings_swap.py` | Validate start < end for active hours |
| `jacked/data/web/js/components/auto-swap.js` | Remove in-panel "Recent Swaps" section, escapeHtml timestamp |
| `jacked/data/web/js/websocket.js` | Replace 5s toast with persistent dismissible banner |

---

### Task 1: Check ping return value + fetch usage after success

**Files:**
- Modify: `jacked/api/usage_monitor.py` (sweep loop, around lines 440-450)

The ping return value is currently discarded. Fix: only count as pinged on success, and after a successful ping, fetch usage to update `cached_5h_resets_at` so `needs_ping` returns False next sweep.

- [ ] **Step 1: Update the ping block in full_sweep_loop**

Find the window keeper ping block (around lines 440-450):

```python
                    logger.info(
                        "Window keeper: pinging account %d (%s)",
                        acct["id"], acct.get("email", "?"),
                    )
                    await ping_account(cc_at)
                    await asyncio.sleep(2)  # pacing
                    sweep_pinged += 1
```

Replace with:

```python
                    logger.info(
                        "Window keeper: pinging account %d (%s)",
                        acct["id"], acct.get("email", "?"),
                    )
                    success = await ping_account(cc_at)
                    if success:
                        sweep_pinged += 1
                        # Fetch fresh usage so cached_5h_resets_at updates
                        # and needs_ping returns False next sweep.
                        await fetch_usage(acct["id"], db)
                    await asyncio.sleep(2)  # pacing
```

Note: `fetch_usage` is already imported (line 385 in the same function).

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py --tb=short -q`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "fix: check ping return value, fetch usage after success"
```

---

### Task 2: Split httpx timeout in ping_account

**Files:**
- Modify: `jacked/web/window_keeper.py` (ping_account function)

- [ ] **Step 1: Change timeout from integer to Timeout object**

In `jacked/web/window_keeper.py`, in the `ping_account` function, find:

```python
        async with httpx.AsyncClient(timeout=timeout) as client:
```

Replace with:

```python
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=timeout, write=10)) as client:
```

This bounds DNS/connect to 10s max (fail fast on unreachable hosts) while allowing up to 30s for the API response.

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py --tb=short -q`

- [ ] **Step 3: Commit**

```bash
git add jacked/web/window_keeper.py
git commit -m "fix: split httpx timeout — 10s connect, 30s read"
```

---

### Task 3: Validate active hours start < end

**Files:**
- Modify: `jacked/api/routes/settings_swap.py`

- [ ] **Step 1: Add cross-field validation for active hours**

In the `SwapSettings` model, add a second `model_validator` (or extend the existing one). After the existing `check_warning_below_critical` validator, add:

```python
    @model_validator(mode="after")
    def check_active_hours_not_crossing_midnight(self) -> "SwapSettings":
        if not self.window_keeper_enabled:
            return self  # skip validation when disabled
        start_h, start_m = map(int, self.window_keeper_active_start.split(":"))
        end_h, end_m = map(int, self.window_keeper_active_end.split(":"))
        if start_h * 60 + start_m >= end_h * 60 + end_m:
            raise ValueError(
                f"active_start ({self.window_keeper_active_start}) must be before "
                f"active_end ({self.window_keeper_active_end}) — midnight-crossing ranges not supported"
            )
        return self
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/routes/settings_swap.py
git commit -m "fix: reject midnight-crossing active hours in settings validation"
```

---

### Task 4: Remove duplicate in-panel swap history + escapeHtml timestamp

**Files:**
- Modify: `jacked/data/web/js/components/auto-swap.js`

- [ ] **Step 1: Remove the "Recent Swaps" section from the auto-swap panel**

In `jacked/data/web/js/components/auto-swap.js`, find the "Section 3: Recent Swaps" block (around lines 189-198):

```html
                        <!-- Section 3: Recent Swaps (collapsed by default) -->
                        <div class="border-t border-slate-700/50 pt-4">
                            <button id="btn-toggle-swap-log" ...>
                                <span>Recent Swaps</span>
                                ...
                            </button>
                            <div id="swap-log-container" class="hidden mt-3">
                                <div class="text-xs text-slate-500">Loading...</div>
                            </div>
                        </div>
```

Remove this entire block (the `<div class="border-t border-slate-700/50 pt-4">` through its closing `</div>`).

Also remove the corresponding event binding code in `bindAutoSwapEvents()` that handles the toggle button (search for `btn-toggle-swap-log`, around lines 304-320).

- [ ] **Step 2: EscapeHtml the timestamp in renderSwapLogTable**

In `renderSwapLogTable`, find line 218:

```javascript
        const ts = e.timestamp
            ? new Date(e.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
            : '\u2014';
```

Wrap the result in escapeHtml for consistency:

```javascript
        const ts = e.timestamp
            ? escapeHtml(new Date(e.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }))
            : '\u2014';
```

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/auto-swap.js
git commit -m "fix: remove duplicate in-panel swap history, escapeHtml timestamp"
```

---

### Task 5: Replace swap toast with persistent dismissible banner

**Files:**
- Modify: `jacked/data/web/js/websocket.js` (auto_swap_triggered handler)

- [ ] **Step 1: Replace the toast with a persistent banner**

In `jacked/data/web/js/websocket.js`, find the `auto_swap_triggered` handler (around line 404-413):

```javascript
jackedWS.on('auto_swap_triggered', (msg) => {
    const d = msg.payload || msg;
    const toEmail = d.to_email || 'another account';
    // Dismiss exhaustion banner — a swap means we found a target
    window.jackedState._exhaustionData = null;
    if (typeof renderExhaustionBanner === 'function') renderExhaustionBanner();
    showToast(`Auto-swapped to ${toEmail}`, 'info', 5000);
    if (typeof loadActiveCredential === 'function') loadActiveCredential();
    if (typeof refreshAndRender === 'function') refreshAndRender();
});
```

Replace with:

```javascript
jackedWS.on('auto_swap_triggered', (msg) => {
    const d = msg.payload || msg;
    const toEmail = d.to_email || 'another account';
    const reason = d.reason || '';
    // Dismiss exhaustion banner — a swap means we found a target
    window.jackedState._exhaustionData = null;
    if (typeof renderExhaustionBanner === 'function') renderExhaustionBanner();
    // Persistent swap banner — dismissible, auto-hides after 5 minutes
    const existing = document.getElementById('swap-banner');
    if (existing) existing.remove();
    const banner = document.createElement('div');
    banner.id = 'swap-banner';
    banner.className = 'fixed top-4 left-1/2 -translate-x-1/2 z-50 bg-teal-900/95 border border-teal-600 rounded-lg px-5 py-3 shadow-lg max-w-lg flex items-center gap-3';
    const text = document.createElement('span');
    text.className = 'text-sm text-teal-100';
    text.textContent = `Auto-swapped to ${toEmail}` + (reason ? ` — ${reason}` : '');
    const close = document.createElement('button');
    close.className = 'text-teal-400 hover:text-white text-lg leading-none ml-auto';
    close.textContent = '\u00d7';
    close.onclick = () => banner.remove();
    banner.appendChild(text);
    banner.appendChild(close);
    document.body.appendChild(banner);
    setTimeout(() => { if (banner.parentNode) banner.remove(); }, 300000);
    if (typeof loadActiveCredential === 'function') loadActiveCredential();
    if (typeof refreshAndRender === 'function') refreshAndRender();
});
```

This creates a fixed-position banner at the top center of the page with the swap reason, a dismiss button, and auto-removal after 5 minutes. Uses `textContent` for all user data (XSS-safe).

- [ ] **Step 2: Commit**

```bash
git add jacked/data/web/js/websocket.js
git commit -m "feat: persistent swap notification banner (5min, dismissible)"
```

---

### Task 6: Run full test suite and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

- [ ] **Step 2: Reinstall and restart server**

- [ ] **Step 3: Verify swap history loads on accounts page**

- [ ] **Step 4: Verify in-panel "Recent Swaps" is gone from auto-swap settings**
