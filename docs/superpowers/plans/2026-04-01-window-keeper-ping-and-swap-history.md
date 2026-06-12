# Window Keeper Ping Fix & Swap History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix window keeper pings to actually open 5h windows (direct API call instead of broken subprocess), fix swap log rendering bugs, and add a visible swap history section to the accounts page.

**Architecture:** Replace `ping_account()` subprocess with a direct `httpx.POST` to the Anthropic messages API using the account's `cc_access_token`. Fix `list_swaps()` to JOIN account emails. Fix JS timestamp parsing and email field references. Add swap history card to accounts page.

**Tech Stack:** Python 3.12+ (httpx, FastAPI, SQLite), vanilla JS (Tailwind dark theme)

**Design spec:** `docs/superpowers/specs/2026-04-01-window-keeper-ping-and-swap-history-design.md`

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/web/window_keeper.py` | Window ping logic | Replace subprocess with httpx.POST |
| `jacked/api/usage_monitor.py` | Sweep loop | Pass cc_access_token to ping_account |
| `jacked/web/database.py` | DB queries | JOIN emails into list_swaps() |
| `jacked/data/web/js/components/auto-swap.js` | Swap log renderer | Fix timestamp + email bugs, bump limit to 20 |
| `jacked/data/web/js/components/accounts.js` | Accounts page | Add swap history section |
| `tests/unit/test_window_keeper.py` | Tests | Add test for new ping_account |

---

### Task 1: Replace ping_account subprocess with direct API call

**Files:**
- Modify: `jacked/web/window_keeper.py:86-122`
- Modify: `jacked/api/usage_monitor.py:436-446`

- [ ] **Step 1: Rewrite ping_account in window_keeper.py**

Replace the entire `ping_account` function (lines 86-122) and the imports/comments above it (lines 79-85) with:

```python
async def ping_account(
    cc_access_token: str,
    timeout: int = 30,
) -> bool:
    """Open/refresh a 5-hour usage window via a minimal API call.

    Makes a single haiku inference call with the account's access token.
    This is ~10x faster than the old subprocess approach and avoids
    credential resolution bugs (the subprocess used the keychain, which
    has the active account's creds — not the target account's).

    Returns True on success (HTTP 200), False on any failure.
    """
    import httpx
    from jacked.web.oauth import OAUTH_BETA_HEADER

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Authorization": f"Bearer {cc_access_token}",
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        if resp.status_code == 200:
            return True
        if resp.status_code == 401:
            logger.warning("ping_account: 401 — token expired, will refresh next cycle")
        elif resp.status_code == 429:
            logger.warning("ping_account: 429 — rate limited")
        else:
            logger.warning("ping_account: HTTP %d — %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.exception("ping_account: unexpected error")
        return False
```

Also remove the now-unused imports at the top of the file. Remove `import os`, `import subprocess`, and `from jacked.findbin import find_bin`. Keep `import asyncio` (used by caller), `import logging`, and the datetime imports.

- [ ] **Step 2: Update the call site in usage_monitor.py**

In `jacked/api/usage_monitor.py`, find the window keeper ping block (around lines 430-446). Change:

```python
                    if not acct.get("auto_swap_enabled"):
                        continue
                    cc_rt = acct.get("cc_refresh_token")
                    if not cc_rt:
                        continue

                    scopes = acct.get("scopes") or ""
                    logger.info(
                        "Window keeper: pinging account %d (%s)",
                        acct["id"], acct.get("email", "?"),
                    )
                    await ping_account(cc_rt, scopes)
```

To:

```python
                    if not acct.get("auto_swap_enabled"):
                        continue
                    cc_at = acct.get("cc_access_token")
                    if not cc_at:
                        continue

                    logger.info(
                        "Window keeper: pinging account %d (%s)",
                        acct["id"], acct.get("email", "?"),
                    )
                    await ping_account(cc_at)
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py tests/unit/test_usage_monitor.py --tb=short -q`

Expected: All pass (ping_account is mocked in usage_monitor tests, and window_keeper tests don't test ping_account yet).

- [ ] **Step 4: Commit**

```bash
git add jacked/web/window_keeper.py jacked/api/usage_monitor.py
git commit -m "fix: replace subprocess ping with direct API call

The subprocess spawned claude -p with CLAUDE_CODE_OAUTH_REFRESH_TOKEN,
but Claude Code ignores that env var during -p mode — it reads the
keychain instead (active account's creds). Direct httpx.POST with
cc_access_token opens the 5h window correctly. ~10x faster too."
```

---

### Task 2: Add ping_account test

**Files:**
- Modify: `tests/unit/test_window_keeper.py`

- [ ] **Step 1: Add test for successful ping**

Append to `tests/unit/test_window_keeper.py`:

```python
import asyncio
from unittest.mock import AsyncMock, patch, MagicMock


class TestPingAccount:
    def test_ping_success(self):
        """HTTP 200 from messages API -> returns True."""
        from jacked.web.window_keeper import ping_account

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        async def _run():
            with patch("jacked.web.window_keeper.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await ping_account("fake-token")
                assert result is True
                mock_client.post.assert_called_once()
                call_kwargs = mock_client.post.call_args
                assert "Bearer fake-token" in call_kwargs[1]["headers"]["Authorization"]

        asyncio.run(_run())

    def test_ping_401_returns_false(self):
        """HTTP 401 (expired token) -> returns False."""
        from jacked.web.window_keeper import ping_account

        mock_resp = MagicMock()
        mock_resp.status_code = 401
        mock_resp.text = "Unauthorized"

        async def _run():
            with patch("jacked.web.window_keeper.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.return_value = mock_resp
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await ping_account("expired-token")
                assert result is False

        asyncio.run(_run())

    def test_ping_exception_returns_false(self):
        """Network error -> returns False, doesn't crash."""
        from jacked.web.window_keeper import ping_account

        async def _run():
            with patch("jacked.web.window_keeper.httpx.AsyncClient") as mock_client_cls:
                mock_client = AsyncMock()
                mock_client.post.side_effect = Exception("connection refused")
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=False)
                mock_client_cls.return_value = mock_client

                result = await ping_account("any-token")
                assert result is False

        asyncio.run(_run())
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py -v --tb=short`

Expected: 11 passed (8 existing + 3 new).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_window_keeper.py
git commit -m "test: add ping_account tests for direct API call"
```

---

### Task 3: Fix list_swaps to JOIN account emails

**Files:**
- Modify: `jacked/web/database.py:2550-2557`

- [ ] **Step 1: Update list_swaps query**

In `jacked/web/database.py`, replace the `list_swaps` method (lines 2550-2557):

```python
    def list_swaps(self, limit=50):
        """List recent swap events."""
        with self._reader() as conn:
            rows = conn.execute(
                "SELECT * FROM swap_log ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
```

With:

```python
    def list_swaps(self, limit=50):
        """List recent swap events with account emails."""
        with self._reader() as conn:
            rows = conn.execute(
                """SELECT s.*,
                          fa.email AS from_email,
                          ta.email AS to_email
                   FROM swap_log s
                   LEFT JOIN accounts fa ON fa.id = s.from_account_id
                   LEFT JOIN accounts ta ON ta.id = s.to_account_id
                   ORDER BY s.timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add jacked/web/database.py
git commit -m "fix: join account emails into swap log query"
```

---

### Task 4: Fix swap log rendering bugs in auto-swap.js

**Files:**
- Modify: `jacked/data/web/js/components/auto-swap.js:218,22`

- [ ] **Step 1: Fix timestamp parsing**

In `jacked/data/web/js/components/auto-swap.js`, find line 218:

```javascript
            ? new Date(e.timestamp * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
```

Replace with:

```javascript
            ? new Date(e.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })
```

(Remove the `* 1000` — timestamp is an ISO string, not a Unix epoch.)

- [ ] **Step 2: Bump swap log limit from 10 to 20**

On line 22, change:

```javascript
        const data = await api.get('/api/settings/swap-log?limit=10');
```

To:

```javascript
        const data = await api.get('/api/settings/swap-log?limit=20');
```

- [ ] **Step 3: Verify email fields already match**

Lines 220-221 reference `e.from_email` and `e.to_email`. After Task 3, these are now populated by the backend JOIN. No JS change needed — field names already match.

- [ ] **Step 4: Commit**

```bash
git add jacked/data/web/js/components/auto-swap.js
git commit -m "fix: swap log timestamp parsed as ISO string not epoch"
```

---

### Task 5: Add swap history section to accounts page

**Files:**
- Modify: `jacked/data/web/js/components/accounts.js:392-396`

- [ ] **Step 1: Add swap history HTML to renderAccounts**

In `jacked/data/web/js/components/accounts.js`, find the closing of the `renderAccounts()` return template. After the `accounts-list` div (around line 394), add the swap history section. Change:

```javascript
            <div id="accounts-list" class="flex flex-col gap-3">
                ${cardsHtml}
            </div>
        </div>
    `;
```

To:

```javascript
            <div id="accounts-list" class="flex flex-col gap-3">
                ${cardsHtml}
            </div>

            <div class="mt-6 bg-slate-800 border border-slate-700 rounded-lg p-4">
                <h3 class="text-sm font-medium text-slate-300 mb-3">Swap History</h3>
                <div id="swap-history-container">
                    <div class="text-xs text-slate-500">Loading...</div>
                </div>
            </div>
        </div>
    `;
```

- [ ] **Step 2: Load swap history after page renders**

Find where `bindAutoSwapEvents` is called after the accounts page renders. After that call, add:

```javascript
    // Load swap history into accounts page
    if (typeof loadSwapLog === 'function' && typeof renderSwapLogTable === 'function') {
        loadSwapLog().then(entries => {
            const container = document.getElementById('swap-history-container');
            if (container) {
                container.textContent = '';
                const wrapper = document.createElement('div');
                wrapper.innerHTML = renderSwapLogTable(entries);
                container.appendChild(wrapper);
            }
        });
    }
```

Note: `loadSwapLog()` and `renderSwapLogTable()` are already globally available from `auto-swap.js`. The `renderSwapLogTable` function uses `escapeHtml()` on all user data before inserting into the template, then we use a wrapper element to safely parse the pre-escaped HTML.

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/accounts.js
git commit -m "feat: add swap history section to accounts page"
```

---

### Task 6: Run full test suite and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

Expected: All tests pass.

- [ ] **Step 2: Reinstall and restart**

```bash
jacked install
kill $(pgrep -f "jacked webux") 2>/dev/null; sleep 2; jacked webux --no-browser &
```

- [ ] **Step 3: Verify window keeper ping works**

Wait for the next sweep (~5 seconds if window keeper is enabled). Check logs:
- "Window keeper: pinging account N" — ping fired
- No "timed out" or "401" errors
- After sweep completes, check `cached_5h_resets_at` updated to a future timestamp

- [ ] **Step 4: Verify swap history visible**

Open the dashboard accounts page. Scroll to bottom — "Swap History" section should show swap events with correct timestamp, emails, and reason.
