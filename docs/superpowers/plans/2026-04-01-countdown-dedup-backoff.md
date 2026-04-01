# Active Account Countdown, Fetch Dedup & 429 Backoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate redundant usage API calls that cause 429 rate limits, add per-account 429 backoff, and show a live countdown timer on the active account card.

**Architecture:** Replace the fixed 30s cache guard in `fetch_usage()` with a caller-specified `min_age` parameter. Add a module-level 429 backoff dict. On the frontend, add a 1-second interval that ticks down the "auto-check in Xs" display on the active account card.

**Tech Stack:** Python 3.12+ (httpx, FastAPI), vanilla JS

**Design spec:** `docs/superpowers/specs/2026-04-01-active-account-check-countdown-design.md`

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/web/auth.py` | Usage fetch | Add `min_age` param, 429 backoff dict, decouple `access_token` from cache bypass |
| `jacked/api/usage_monitor.py` | Server poll loops | Pass appropriate `min_age` to each `fetch_usage` call |
| `jacked/api/routes/auth.py` | Manual refresh endpoints | Pass `min_age=0` for manual refresh |
| `jacked/data/web/js/components/accounts.js` | Account cards | Add countdown span to `renderCacheAge` |
| `jacked/data/web/js/components/account-actions.js` | UI timers | Add 1s interval for countdown tick |
| `tests/unit/test_usage_refresh.py` | Tests | Update for `min_age` param, add 429 backoff tests |

---

### Task 1: Add `min_age` parameter and 429 backoff to `fetch_usage`

**Files:**
- Modify: `jacked/web/auth.py:320-418`
- Modify: `tests/unit/test_usage_refresh.py`

This is the core change. `fetch_usage` gains a `min_age` parameter that replaces the fixed 30s constant for cache checking. The `access_token` parameter no longer bypasses the cache. A new module-level `_usage_backoff` dict tracks per-account 429 backoff.

- [ ] **Step 1: Write failing tests for min_age and backoff**

Add to `tests/unit/test_usage_refresh.py`:

```python
class TestMinAgeParameter:
    """fetch_usage min_age controls cache freshness threshold."""

    def test_min_age_skips_when_fresh_enough(self):
        """Cache 10s old, min_age=55 -> skip (10 < 55)."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 10})
        result = asyncio.run(fetch_usage(1, db, min_age=55))
        assert result == {"_cached": True}

    def test_min_age_fetches_when_stale(self):
        """Cache 60s old, min_age=55 -> fetch (60 >= 55)."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 60})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=55))
        assert result is not None
        assert result != {"_cached": True}

    def test_min_age_zero_always_fetches(self):
        """min_age=0 -> always fetch, even with fresh cache."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 2})
        client = _mock_client(200, {
            "five_hour": {"utilization": 5.0},
            "seven_day": {"utilization": 10.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result is not None
        assert result != {"_cached": True}
        client.get.assert_called_once()

    def test_access_token_no_longer_bypasses_cache(self):
        """access_token alone should NOT bypass cache (use min_age=0 for that)."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 2})
        result = asyncio.run(fetch_usage(1, db, access_token="tok"))
        # Default min_age=30, cache is 2s old -> cached
        assert result == {"_cached": True}


class TestUsageBackoff:
    """fetch_usage should respect 429 backoff."""

    def test_429_sets_backoff(self):
        """After a 429, subsequent calls should be skipped."""
        import jacked.web.auth as mod
        mod._usage_backoff.clear()

        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(429, {}, headers={"retry-after": "60"})

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result is None  # 429 returns None

        # Now a subsequent call should be skipped due to backoff
        result2 = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result2 == {"_backed_off": True}

    def test_backoff_expires(self):
        """After backoff expires, fetches proceed normally."""
        import jacked.web.auth as mod
        mod._usage_backoff.clear()
        # Set backoff to the past
        mod._usage_backoff[1] = time.time() - 10

        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(200, {
            "five_hour": {"utilization": 0.0},
            "seven_day": {"utilization": 0.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, min_age=0))
        assert result is not None
        assert result != {"_backed_off": True}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_usage_refresh.py::TestMinAgeParameter tests/unit/test_usage_refresh.py::TestUsageBackoff -v --tb=short`

Expected: FAIL (min_age parameter doesn't exist, _usage_backoff doesn't exist)

- [ ] **Step 3: Implement min_age and backoff in fetch_usage**

In `jacked/web/auth.py`, add the backoff dict after `USAGE_CACHE_FRESHNESS_SECONDS` (line 39):

```python
USAGE_CACHE_FRESHNESS_SECONDS = 30

# Per-account 429 backoff: account_id -> don't-fetch-before timestamp
_usage_backoff: dict[int, float] = {}
```

Change the `fetch_usage` signature (line 320-323) to:

```python
async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
    min_age: int = USAGE_CACHE_FRESHNESS_SECONDS,
) -> Optional[dict]:
```

Replace the cache guard block (lines 335-346) with:

```python
    # 429 backoff check — skip if still in backoff period
    backoff_until = _usage_backoff.get(account_id, 0)
    if time.time() < backoff_until:
        logger.debug(
            f"Usage fetch backed off for account {account_id} "
            f"({int(backoff_until - time.time())}s remaining)"
        )
        return {"_backed_off": True}

    # Cache freshness guard — skip API call if data is fresh enough
    # for this caller's needs. min_age=0 always fetches.
    cached_at = account.get("usage_cached_at")
    if min_age > 0 and cached_at:
        try:
            age = int(time.time()) - int(cached_at)
            if age < min_age:
                logger.debug(f"Usage cache fresh for account {account_id} ({age}s old, min_age={min_age}), skipping")
                return {"_cached": True}
        except (ValueError, TypeError):
            pass  # Malformed timestamp — proceed with fetch
```

In the 429 handler (lines 392-406), add the backoff after the existing logging:

```python
            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after", "60")
                try:
                    backoff_seconds = max(int(retry_after), 60)
                except (ValueError, TypeError):
                    backoff_seconds = 60
                _usage_backoff[account_id] = time.time() + backoff_seconds
                # Do NOT increment consecutive_failures — 429 is a rate limit,
                # not an account health issue.
                db.record_account_error(
                    account_id,
                    f"Usage fetch rate limited (429) — retry after {backoff_seconds}s",
                    increment_failures=False,
                )
                logger.warning(
                    f"Usage fetch rate limited for account {account_id}, "
                    f"backing off {backoff_seconds}s"
                )
                return None
```

- [ ] **Step 4: Update existing test that checks access_token bypass**

In `tests/unit/test_usage_refresh.py`, the test `test_access_token_bypasses_fresh_cache` (around line 85) needs to be updated. The access_token no longer bypasses cache. Change the test to use `min_age=0`:

```python
    def test_access_token_with_min_age_zero_bypasses_cache(self):
        """Explicit access_token + min_age=0 should bypass cache."""
        db = _mock_db({"usage_cached_at": int(time.time()) - 2})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })

        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, access_token="fresh_token", min_age=0))

        assert result is not None
        assert result != {"_cached": True}
        client.get.assert_called_once()
```

Also update `test_cache_freshness_constant_exists` if it hard-asserts `== 30`.

- [ ] **Step 5: Run tests**

Run: `uv run python -m pytest tests/unit/test_usage_refresh.py -v --tb=short`

Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add jacked/web/auth.py tests/unit/test_usage_refresh.py
git commit -m "feat: min_age parameter and 429 backoff for fetch_usage

Replace fixed 30s cache guard with caller-specified min_age.
access_token no longer bypasses cache (use min_age=0).
429 responses set per-account backoff (min 60s)."
```

---

### Task 2: Update all fetch_usage callers with appropriate min_age

**Files:**
- Modify: `jacked/api/usage_monitor.py` (3 call sites)
- Modify: `jacked/api/routes/auth.py` (2 call sites)

- [ ] **Step 1: Update active poll loop**

In `jacked/api/usage_monitor.py`, find the active poll fetch (around line 145):

```python
            await fetch_usage(
                active_acct_id, db, access_token=effective_token,
            )
```

Change to:

```python
            await fetch_usage(
                active_acct_id, db, access_token=effective_token, min_age=55,
            )
```

(55s = 60s interval minus 5s buffer for timing drift)

- [ ] **Step 2: Update full sweep loop**

In `jacked/api/usage_monitor.py`, find the sweep fetch (around line 403):

```python
                result = await fetch_usage(acct_id, db)
```

Change to:

```python
                result = await fetch_usage(acct_id, db, min_age=int(check_interval) - 10)
```

(`check_interval - 10` = sweep interval minus buffer)

- [ ] **Step 3: Update post-ping fetch**

In `jacked/api/usage_monitor.py`, find the post-ping fetch (around line 450):

```python
                        await fetch_usage(acct["id"], db, access_token=cc_at)
```

Change to:

```python
                        await fetch_usage(acct["id"], db, access_token=cc_at, min_age=0)
```

(`min_age=0` = always fetch after successful ping to get fresh resets_at)

- [ ] **Step 4: Update single-account refresh endpoint**

In `jacked/api/routes/auth.py`, find the single-account refresh (around line 558):

```python
    usage_data = await fetch_usage(account_id, db, access_token=effective_token)
```

Change to:

```python
    usage_data = await fetch_usage(account_id, db, access_token=effective_token, min_age=0)
```

(Manual refresh = always fetch)

- [ ] **Step 5: Update bulk refresh endpoint**

In `jacked/api/routes/auth.py`, find the bulk refresh (around line 658):

```python
            usage_data = await fetch_usage(acct["id"], db, access_token=effective_token)
```

Change to:

```python
            usage_data = await fetch_usage(acct["id"], db, access_token=effective_token, min_age=0)
```

(Manual "Refresh All" = always fetch)

- [ ] **Step 6: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 7: Commit**

```bash
git add jacked/api/usage_monitor.py jacked/api/routes/auth.py
git commit -m "fix: callers pass min_age to prevent redundant usage fetches

Active poll: min_age=55 (60s interval - 5s buffer)
Sweep: min_age=interval-10
Post-ping: min_age=0 (always, need fresh resets_at)
Manual refresh: min_age=0 (user intent)"
```

---

### Task 3: Add countdown timer to active account card

**Files:**
- Modify: `jacked/data/web/js/components/accounts.js`
- Modify: `jacked/data/web/js/components/account-actions.js`

- [ ] **Step 1: Modify renderCacheAge to show countdown**

In `jacked/data/web/js/components/accounts.js`, change `renderCacheAge` (lines 80-86) from:

```javascript
function renderCacheAge(usageCachedAt) {
    if (usageCachedAt === null || usageCachedAt === undefined) {
        return '<span class="text-xs text-slate-500" data-cache-age>Usage: never fetched</span>';
    }
    const ago = timeAgoFromUnix(usageCachedAt);
    return `<span class="text-xs text-slate-500" data-cache-age>Usage updated ${escapeHtml(ago)}</span>`;
}
```

To:

```javascript
function renderCacheAge(usageCachedAt, acctId) {
    if (usageCachedAt === null || usageCachedAt === undefined) {
        return '<span class="text-xs text-slate-500" data-cache-age>Usage: never fetched</span>';
    }
    const ago = timeAgoFromUnix(usageCachedAt);
    let checkHtml = '';
    const ss = window.jackedState.swapSettings || {};
    if (ss.auto_swap_enabled && acctId === window.jackedState.activeCredentialAccountId) {
        const ageS = Math.floor(Date.now() / 1000) - usageCachedAt;
        const rem = Math.max(0, 60 - ageS);
        const label = rem > 0 ? escapeHtml(rem + 's') : 'checking\u2026';
        checkHtml = ' \u00b7 <span class="text-teal-500" data-next-check data-cached-at="' + usageCachedAt + '">' + label + '</span>';
    }
    return '<span class="text-xs text-slate-500" data-cache-age>Usage updated ' + escapeHtml(ago) + checkHtml + '</span>';
}
```

Then update the call site at line 249 from:

```javascript
    const cacheAgeHtml = renderCacheAge(acct.usage_cached_at);
```

To:

```javascript
    const cacheAgeHtml = renderCacheAge(acct.usage_cached_at, acct.id);
```

- [ ] **Step 2: Add countdown tick interval in account-actions.js**

In `jacked/data/web/js/components/account-actions.js`, add a countdown ticker. Find a suitable location (near the existing `_autoRefreshInterval` setup, around line 7) and add:

```javascript
let _checkCountdownInterval = null;

function _startCheckCountdown() {
    if (_checkCountdownInterval) return; // already running
    _checkCountdownInterval = setInterval(() => {
        const els = document.querySelectorAll('[data-next-check]');
        if (els.length === 0) return;
        const now = Math.floor(Date.now() / 1000);
        els.forEach(el => {
            const cachedAt = parseInt(el.getAttribute('data-cached-at'), 10);
            if (isNaN(cachedAt)) return;
            const rem = Math.max(0, 60 - (now - cachedAt));
            el.textContent = rem > 0 ? rem + 's' : 'checking\u2026';
        });
    }, 1000);
}

function _stopCheckCountdown() {
    if (_checkCountdownInterval) {
        clearInterval(_checkCountdownInterval);
        _checkCountdownInterval = null;
    }
}
```

Then find `bindAccountEvents()` (the function called on accounts page mount) and add `_startCheckCountdown()` at the end:

```javascript
    _startCheckCountdown();
```

Also add cleanup: find where the existing `_autoRefreshInterval` is cleared on page unmount (in `_stopAutoRefresh` or similar). Add `_stopCheckCountdown()` alongside it. Look for the `stopAutoRefresh` export or the route-change handler.

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/accounts.js jacked/data/web/js/components/account-actions.js
git commit -m "feat: live countdown timer on active account card

Shows 'auto-check in Xs' when auto-swap is enabled, ticking
down each second. Resets when usage_cached_at updates."
```

---

### Task 4: Run full test suite and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

Expected: All tests pass.

- [ ] **Step 2: Reinstall and restart**

```bash
jacked install
# kill + restart server
```

- [ ] **Step 3: Verify no more 429 spam**

Watch server logs for 60 seconds — the active account should only be fetched once per 60s, and sweep accounts should only be fetched once per sweep interval. No 429s.

- [ ] **Step 4: Verify countdown visible**

Open accounts page. Active account card should show "auto-check in Xs" ticking down. Other accounts should NOT show it.
