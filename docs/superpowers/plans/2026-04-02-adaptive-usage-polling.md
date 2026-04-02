# Adaptive Usage Polling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the fixed 60s active account poll with adaptive urgency-based polling, centralize all usage fetches through a coordinator that enforces a per-account rate limit ceiling, and make the UI auto-refresh skip the active account when auto-swap is enabled.

**Architecture:** Refactor `fetch_usage` in auth.py into a coordinator with per-account state (last fetched, next eligible, backoff, urgency tier). The active poll loop reads urgency tier to determine sleep interval. UI auto-refresh skips the active account when auto-swap is on. All callers go through the same coordinator — no caller can exceed 1 request per 65 seconds per account.

**Tech Stack:** Python 3.12+ (asyncio, httpx), vanilla JS

**Design spec:** `docs/superpowers/specs/2026-04-02-adaptive-usage-polling-design.md`

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/web/auth.py` | Usage coordinator | Replace `_usage_backoff` with `_account_usage_state` dict, add `compute_urgency_tier()`, enforce 65s hard ceiling |
| `jacked/api/usage_monitor.py` | Active poll loop | Use adaptive sleep based on urgency tier, add jitter, import `random` |
| `jacked/data/web/js/components/account-actions.js` | UI auto-refresh | Skip active account when auto-swap enabled |
| `jacked/data/web/js/components/accounts.js` | Countdown | Show tier name, use adaptive interval |
| `tests/unit/test_usage_refresh.py` | Tests | Urgency tier computation, ceiling enforcement |

---

### Task 1: Add per-account usage state and urgency tier computation

**Files:**
- Modify: `jacked/web/auth.py`
- Modify: `tests/unit/test_usage_refresh.py`

Replace `_usage_backoff` with a richer per-account state dict. Add `compute_urgency_tier()` pure function.

- [ ] **Step 1: Write failing tests for urgency tier**

Add to `tests/unit/test_usage_refresh.py`:

```python
from jacked.web.auth import compute_urgency_tier


class TestUrgencyTier:
    """Tests for adaptive polling urgency tier computation."""

    def test_idle_low_usage_no_burn(self):
        """Usage < 50%, burn rate ~0 -> IDLE (300s)."""
        tier, interval = compute_urgency_tier(
            usage_5h=20.0, usage_7d=30.0,
            burn_rate_5h=0.0, critical_5h=90.0,
        )
        assert tier == "idle"
        assert interval == 300

    def test_normal_moderate_usage(self):
        """Usage 50-70%, low burn -> NORMAL (150s)."""
        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=0.2, critical_5h=90.0,
        )
        assert tier == "normal"
        assert interval == 150

    def test_warning_high_usage(self):
        """Usage 70-85% -> WARNING (90s)."""
        tier, interval = compute_urgency_tier(
            usage_5h=75.0, usage_7d=30.0,
            burn_rate_5h=0.5, critical_5h=90.0,
        )
        assert tier == "warning"
        assert interval == 90

    def test_critical_very_high_usage(self):
        """Usage > 85% -> CRITICAL (65s)."""
        tier, interval = compute_urgency_tier(
            usage_5h=88.0, usage_7d=30.0,
            burn_rate_5h=1.0, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 65

    def test_burn_rate_projects_critical_soon(self):
        """Usage 60% but burn rate projects critical in 5 min -> CRITICAL."""
        # 60 + (6.0 * 5) = 90 -> hits critical within 5 min
        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=6.0, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 65

    def test_burn_rate_projects_critical_in_15_min(self):
        """Usage 60% but burn rate projects critical in 15 min -> WARNING."""
        # 60 + (2.0 * 15) = 90 -> hits critical within 15 min but not 5
        tier, interval = compute_urgency_tier(
            usage_5h=60.0, usage_7d=30.0,
            burn_rate_5h=2.0, critical_5h=90.0,
        )
        assert tier == "warning"
        assert interval == 90

    def test_7d_escalation(self):
        """7d > 80% bumps up one tier regardless of 5h."""
        # 5h=20%, burn=0 would be IDLE, but 7d=85% bumps to NORMAL
        tier, interval = compute_urgency_tier(
            usage_5h=20.0, usage_7d=85.0,
            burn_rate_5h=0.0, critical_5h=90.0,
        )
        assert tier == "normal"
        assert interval == 150

    def test_7d_escalation_stacks_with_warning(self):
        """7d > 80% bumps WARNING to CRITICAL."""
        tier, interval = compute_urgency_tier(
            usage_5h=75.0, usage_7d=85.0,
            burn_rate_5h=0.5, critical_5h=90.0,
        )
        assert tier == "critical"
        assert interval == 65

    def test_none_usage_defaults_to_idle(self):
        """None usage (never fetched) -> IDLE."""
        tier, interval = compute_urgency_tier(
            usage_5h=None, usage_7d=None,
            burn_rate_5h=0.0, critical_5h=90.0,
        )
        assert tier == "idle"
        assert interval == 300
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_usage_refresh.py::TestUrgencyTier -v --tb=short`

Expected: FAIL — `compute_urgency_tier` doesn't exist

- [ ] **Step 3: Implement urgency tier and per-account state**

In `jacked/web/auth.py`, replace the `_usage_backoff` dict and add new state/functions after `USAGE_CACHE_FRESHNESS_SECONDS`:

```python
USAGE_CACHE_FRESHNESS_SECONDS = 30

# Hard ceiling: never fetch usage more than once per this many seconds per account.
# The Anthropic usage API rate limits at ~1 req/60s/account.
_USAGE_RATE_LIMIT_CEILING = 65  # 60s limit + 5s safety margin

# Adaptive polling intervals by urgency tier
_TIER_INTERVALS = {
    "idle": 300,      # 5 min — usage stable, nothing happening
    "normal": 150,    # 2.5 min — moderate activity
    "warning": 90,    # 1.5 min — approaching thresholds
    "critical": 65,   # ~1 min — near the wall, fastest safe rate
}
_TIER_ORDER = ["idle", "normal", "warning", "critical"]

# Per-account usage coordinator state
_account_usage_state: dict[int, dict] = {}


def _get_usage_state(account_id: int) -> dict:
    """Get or create per-account usage coordinator state."""
    if account_id not in _account_usage_state:
        _account_usage_state[account_id] = {
            "last_fetched_at": 0.0,
            "backoff_until": 0.0,
            "tier": "idle",
            "interval": _TIER_INTERVALS["idle"],
        }
    return _account_usage_state[account_id]


def compute_urgency_tier(
    usage_5h: float | None,
    usage_7d: float | None,
    burn_rate_5h: float,
    critical_5h: float = 90.0,
) -> tuple[str, int]:
    """Compute the adaptive polling urgency tier and interval.

    Returns (tier_name, interval_seconds).
    """
    u5 = usage_5h if usage_5h is not None else 0.0
    u7 = usage_7d if usage_7d is not None else 0.0

    # Base tier from 5h usage level
    if u5 > 85:
        tier = "critical"
    elif u5 > 70:
        tier = "warning"
    elif u5 > 50:
        tier = "normal"
    else:
        tier = "idle"

    # Burn rate projection escalation
    if burn_rate_5h > 0.01:
        mins_to_critical = (critical_5h - u5) / burn_rate_5h if u5 < critical_5h else 0
        if mins_to_critical <= 5:
            tier = "critical"
        elif mins_to_critical <= 15 and _TIER_ORDER.index(tier) < _TIER_ORDER.index("warning"):
            tier = "warning"

    # 7-day escalation: bump up one tier if 7d > 80%
    if u7 > 80:
        idx = _TIER_ORDER.index(tier)
        if idx < len(_TIER_ORDER) - 1:
            tier = _TIER_ORDER[idx + 1]

    return tier, _TIER_INTERVALS[tier]
```

Then remove the old `_usage_backoff` dict (line 41-42).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_usage_refresh.py::TestUrgencyTier -v --tb=short`

Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auth.py tests/unit/test_usage_refresh.py
git commit -m "feat: urgency tier computation for adaptive usage polling"
```

---

### Task 2: Refactor fetch_usage into coordinator with hard ceiling

**Files:**
- Modify: `jacked/web/auth.py`
- Modify: `tests/unit/test_usage_refresh.py`

Replace the `min_age` / `_usage_backoff` approach with the coordinator.

- [ ] **Step 1: Write failing tests for hard ceiling**

Add to `tests/unit/test_usage_refresh.py`:

```python
class TestUsageCeiling:
    """fetch_usage enforces hard per-account rate limit ceiling."""

    def test_fetch_within_ceiling_returns_cached(self):
        """Fetched 30s ago, ceiling=65s -> return cached."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30

        db = _mock_db({"usage_cached_at": int(time.time()) - 30})
        result = asyncio.run(fetch_usage(1, db))
        assert result == {"_cached": True}

    def test_fetch_past_ceiling_proceeds(self):
        """Fetched 70s ago, ceiling=65s -> proceed with API call."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 70

        db = _mock_db({"usage_cached_at": int(time.time()) - 70})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))
        assert result is not None
        assert result != {"_cached": True}

    def test_force_respects_ceiling(self):
        """force=True but within ceiling -> still cached (rate limit is absolute)."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30

        db = _mock_db({"usage_cached_at": int(time.time()) - 30})
        result = asyncio.run(fetch_usage(1, db, force=True))
        assert result == {"_cached": True}

    def test_429_sets_backoff_in_state(self):
        """429 -> backoff recorded in per-account state."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()

        db = _mock_db({"usage_cached_at": int(time.time()) - 120})
        client = _mock_client(429, {}, headers={"retry-after": "120"})
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db))
        assert result is None
        assert mod._get_usage_state(1)["backoff_until"] > time.time()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_usage_refresh.py::TestUsageCeiling -v --tb=short`

- [ ] **Step 3: Refactor fetch_usage**

In `jacked/web/auth.py`, change the `fetch_usage` signature and body:

```python
async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
    force: bool = False,
) -> Optional[dict]:
    """Centralized usage fetch coordinator.

    All callers go through this single entry point. Enforces:
    - Hard per-account rate limit ceiling (65s between API calls)
    - 429 backoff per account
    - Returns cached data when the account isn't eligible yet

    force=True means "I really want fresh data" but still respects
    the hard ceiling — the rate limit is absolute.
    """
    account = db.get_account(account_id)
    if not account:
        return None

    now = time.time()
    state = _get_usage_state(account_id)

    # 429 backoff check
    if now < state["backoff_until"]:
        logger.debug(
            f"Usage fetch backed off for account {account_id} "
            f"({int(state['backoff_until'] - now)}s remaining)"
        )
        return {"_backed_off": True}

    # Hard ceiling: never exceed 1 req per _USAGE_RATE_LIMIT_CEILING seconds
    elapsed = now - state["last_fetched_at"]
    if elapsed < _USAGE_RATE_LIMIT_CEILING:
        logger.debug(
            f"Usage ceiling for account {account_id}: {int(elapsed)}s < "
            f"{_USAGE_RATE_LIMIT_CEILING}s, returning cached"
        )
        return {"_cached": True}

    token = access_token or account["access_token"]

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                USAGE_URL,
                headers={
                    "Authorization": f"Bearer {token}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )

            if resp.status_code == 200:
                data = resp.json()
                five_hour = data.get("five_hour", {})
                seven_day = data.get("seven_day", {})

                db.update_account_usage_cache(
                    account_id,
                    five_hour=five_hour.get("utilization"),
                    seven_day=seven_day.get("utilization"),
                    five_hour_resets_at=five_hour.get("resets_at"),
                    seven_day_resets_at=seven_day.get("resets_at"),
                    raw=data,
                )
                db.clear_account_errors(account_id)

                # Update coordinator state
                state["last_fetched_at"] = time.time()

                logger.info(f"Usage fetched for account {account_id}")
                return data

            if resp.status_code in (401, 403):
                error_msg = f"Usage fetch failed (HTTP {resp.status_code}) — token may be revoked"
                db.update_account(
                    account_id,
                    validation_status="invalid",
                    last_error=error_msg,
                    last_error_at=datetime.now(timezone.utc).isoformat(),
                )
                logger.warning(
                    f"Usage fetch auth failure for account {account_id}: {resp.status_code}"
                )
                return None

            if resp.status_code == 429:
                retry_after = resp.headers.get("retry-after", "65")
                try:
                    backoff_seconds = max(int(retry_after), _USAGE_RATE_LIMIT_CEILING)
                except (ValueError, TypeError):
                    backoff_seconds = _USAGE_RATE_LIMIT_CEILING
                state["backoff_until"] = time.time() + backoff_seconds
                state["last_fetched_at"] = time.time()  # count as a "fetch" for ceiling
                db.record_account_error(
                    account_id,
                    f"Usage fetch rate limited (429) — backing off {backoff_seconds}s",
                    increment_failures=False,
                )
                logger.warning(
                    f"Usage fetch rate limited for account {account_id}, "
                    f"backing off {backoff_seconds}s"
                )
                return None

            db.record_account_error(
                account_id, f"Usage fetch failed (HTTP {resp.status_code})"
            )
            logger.warning(
                f"Usage fetch HTTP {resp.status_code} for account {account_id}"
            )
            return None

    except Exception as e:
        db.record_account_error(account_id, f"Usage fetch error: {e}")
        logger.warning(f"Usage fetch failed for account {account_id}: {e}")
        return None
```

- [ ] **Step 4: Update all callers to remove min_age**

All callers that previously passed `min_age` should be updated:
- `usage_monitor.py:145-148` — remove `min_age=55`, just pass `access_token`
- `usage_monitor.py:403` — remove `min_age=int(check_interval) - 10`
- `usage_monitor.py:450` — change `min_age=0` to `force=True`
- `jacked/api/routes/auth.py:558` — change `min_age=0` to `force=True`
- `jacked/api/routes/auth.py:658` — change `min_age=0` to `force=True`

Also update tests that reference `min_age` — the `TestMinAgeParameter` class should be renamed/updated to test the ceiling behavior. The `test_access_token_no_longer_bypasses_cache` test changes behavior (ceiling replaces min_age).

- [ ] **Step 5: Run all tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 6: Commit**

```bash
git add jacked/web/auth.py jacked/api/usage_monitor.py jacked/api/routes/auth.py tests/unit/test_usage_refresh.py
git commit -m "feat: centralized usage coordinator with hard rate limit ceiling

Replace min_age/backoff with per-account coordinator state.
Hard ceiling of 65s per account (API rate limit + safety margin).
force=True for manual refresh / post-ping, still respects ceiling.
All callers go through the same entry point."
```

---

### Task 3: Adaptive sleep in active poll loop

**Files:**
- Modify: `jacked/api/usage_monitor.py`

Replace the fixed `await asyncio.sleep(60)` with adaptive interval based on urgency tier.

- [ ] **Step 1: Add random import and adaptive sleep**

At the top of `usage_monitor.py`, add `import random`.

Replace the final sleep at the end of `active_account_poll_loop` (line 354):

```python
        await asyncio.sleep(60)
```

With:

```python
        # Adaptive interval: urgency tier determines how long to wait.
        # Faster when usage is high or climbing, slower when idle.
        from jacked.web.auth import compute_urgency_tier, _get_usage_state
        active_id = _read_active_account_id()
        if active_id is not None:
            acct_data = db.get_account(active_id) if db else None
            br = _burn_rates.get(active_id)
            usage_state = _get_usage_state(active_id)
            tier, base_interval = compute_urgency_tier(
                usage_5h=acct_data.get("cached_usage_5h") if acct_data else None,
                usage_7d=acct_data.get("cached_usage_7d") if acct_data else None,
                burn_rate_5h=br.rate_5h_per_min if br else 0.0,
                critical_5h=_setting_float(db, "auto_swap_5h_critical", 90) if db else 90,
            )
            usage_state["tier"] = tier
            usage_state["interval"] = base_interval
            # ±15% jitter to prevent sync patterns
            jitter = base_interval * 0.15
            interval = base_interval + random.uniform(-jitter, jitter)
            logger.debug(
                "Active poll: tier=%s interval=%.0fs (base=%ds)",
                tier, interval, base_interval,
            )
        else:
            interval = 60
        await asyncio.sleep(interval)
```

Also update the docstring of `active_account_poll_loop` to reflect adaptive polling.

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py --tb=short -q`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: adaptive poll interval based on urgency tier

Idle: 5 min, Normal: 2.5 min, Warning: 90s, Critical: 65s.
±15% jitter on each tick. Burns through API budget only when needed."
```

---

### Task 4: UI auto-refresh skips active account

**Files:**
- Modify: `jacked/data/web/js/components/account-actions.js`

- [ ] **Step 1: Modify _triggerUsageRefresh to skip active account**

The UI refresh calls `api.post('/api/auth/accounts/refresh-all-usage')` which hits the server bulk endpoint. Rather than changing the server endpoint (which other callers might use), have the UI tell the server which account to skip.

Actually, the simpler approach: the bulk refresh endpoint already goes through `fetch_usage` which has the coordinator ceiling. If the active account was just fetched by the server poll 30s ago, the ceiling returns cached. So the dedup already works via the coordinator.

But to be explicit and avoid even the API call overhead, add a query parameter. In `_triggerUsageRefresh`, change:

```javascript
        const result = await api.post('/api/auth/accounts/refresh-all-usage');
```

To:

```javascript
        const ss = window.jackedState.swapSettings || {};
        const activeId = window.jackedState.activeCredentialAccountId;
        const skipParam = (ss.auto_swap_enabled && activeId) ? '?skip_account=' + activeId : '';
        const result = await api.post('/api/auth/accounts/refresh-all-usage' + skipParam);
```

Then in the backend `refresh-all-usage` endpoint (`jacked/api/routes/auth.py`), add the `skip_account` parameter and skip that account in the loop.

- [ ] **Step 2: Update countdown to show tier**

In `jacked/data/web/js/components/accounts.js`, update `renderCacheAge` to show the tier name when available. In the countdown span, after the time show the tier:

Change the countdown label from:
```javascript
checkHtml = ' \u00b7 <span class="text-teal-500" data-next-check ...>' + label + '</span>';
```

To include tier info by reading from state:
```javascript
// Read tier from usage state if available (set by server poll)
var tierLabel = '';
var acctState = (window.jackedState._usageTiers || {})[acctId];
if (acctState) tierLabel = ' (' + acctState + ')';
checkHtml = ' \u00b7 <span class="text-teal-500" data-next-check ...>' + label + tierLabel + '</span>';
```

The tier info needs to come from the server. Add it to the WebSocket broadcast or account data. For now, compute it client-side from the account's usage data (simpler, no server change needed):

```javascript
var tierLabel = '';
if (ss.auto_swap_enabled && acctId === window.jackedState.activeCredentialAccountId) {
    var u5 = (window.jackedState.accounts || []).find(function(a) { return a.id === acctId; });
    if (u5) {
        var usage = u5.cached_usage_5h || 0;
        if (usage > 85) tierLabel = ' (critical)';
        else if (usage > 70) tierLabel = ' (warning)';
        else if (usage > 50) tierLabel = ' (normal)';
        else tierLabel = ' (idle)';
    }
}
```

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/account-actions.js jacked/data/web/js/components/accounts.js jacked/api/routes/auth.py
git commit -m "feat: UI refresh skips active account, countdown shows tier"
```

---

### Task 5: Run full test suite and verify

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

- [ ] **Step 2: Verify no 429s**

After reinstall and server restart, watch logs for 5 minutes. The active account should poll at the adaptive interval (likely idle=5min initially), and no 429s should appear.

- [ ] **Step 3: Verify countdown shows tier**

Open dashboard, check active account card shows "auto-check in Xs (idle)" or similar.
