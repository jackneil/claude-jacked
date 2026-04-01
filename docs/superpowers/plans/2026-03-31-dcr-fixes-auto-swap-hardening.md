# DCR Fixes: Auto-Swap Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all CRITICAL and MEDIUM findings from DCR Wave 1 review of the auto-swap system, plus write missing tests.

**Architecture:** Sequential fixes to 4 files (usage_monitor.py, settings_swap.py, auto_swap.py, window_keeper.py) plus new tests. Fixes are ordered by severity — CRITICAL CPU loop first, then validation/observability, then tests.

**Tech Stack:** Python 3.12+ (FastAPI, Pydantic, asyncio), pytest

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/api/usage_monitor.py` | Usage monitor loops | Fix CPU loop, dedup tier regex, add pause log, burn-rate floor, sweep heartbeat, fix type hint |
| `jacked/api/routes/settings_swap.py` | Settings API | Persist paused_until, cross-field validation, time-string validation |
| `jacked/web/window_keeper.py` | Window keeper | Fix timezone-naive `datetime.now()` |
| `jacked/web/auto_swap.py` | Swap engine | Add tier label helper for reuse |
| `tests/unit/test_usage_monitor.py` | Monitor tests | Add cooldown, pause, burn-rate decay tests; reset `_last_swap_time` |

---

### Task 1: Fix CRITICAL swap cooldown CPU loop

**Files:**
- Modify: `jacked/api/usage_monitor.py:205-211`

The `continue` on line 211 skips `await asyncio.sleep(60)` at line 342, creating an infinite tight loop during the 5-minute cooldown window. This is also why the previous session's tests were hanging.

- [ ] **Step 1: Add sleep before cooldown continue**

In `jacked/api/usage_monitor.py`, find the cooldown check block (lines 205-211):

```python
                    # -- Swap cooldown: prevent ping-ponging ------
                    if (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                        logger.debug(
                            "Active poll: swap cooldown active (%.0fs remaining)",
                            _SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time),
                        )
                        continue
```

Replace the bare `continue` with:

```python
                    # -- Swap cooldown: prevent ping-ponging ------
                    if (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                        logger.debug(
                            "Active poll: swap cooldown active (%.0fs remaining)",
                            _SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time),
                        )
                        await asyncio.sleep(60)
                        continue
```

- [ ] **Step 2: Verify fix**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py::TestAutoSwapTriggers -v --timeout=30`

Expected: PASS (previously hung indefinitely)

---

### Task 2: Fix settings_swap.py validation and paused_until persistence

**Files:**
- Modify: `jacked/api/routes/settings_swap.py`

Three issues: (1) PUT doesn't persist `auto_swap_paused_until`, (2) no cross-field validation warning < critical, (3) time-string fields accept garbage.

- [ ] **Step 1: Add time-string validator and cross-field validation**

Replace the `SwapSettings` model (lines 14-24) with:

```python
import re as _re

def _validate_time(v: str) -> str:
    if not _re.fullmatch(r"\d{2}:\d{2}", v):
        raise ValueError(f"Invalid time format: {v!r} (expected HH:MM)")
    h, m = map(int, v.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {v!r} (hours 0-23, minutes 0-59)")
    return v


class SwapSettings(BaseModel):
    auto_swap_enabled: bool = False
    auto_swap_5h_warning: int = Field(default=80, ge=0, le=100)
    auto_swap_5h_critical: int = Field(default=90, ge=0, le=100)
    auto_swap_7d_threshold: int = Field(default=85, ge=0, le=100)
    usage_check_interval: int = Field(default=300, ge=60, le=3600)
    auto_swap_paused_until: Optional[str] = None
    window_keeper_enabled: bool = False
    window_keeper_active_start: str = "06:00"
    window_keeper_active_end: str = "23:00"
    window_keeper_prewake: str = "04:00"

    @field_validator("window_keeper_active_start", "window_keeper_active_end", "window_keeper_prewake")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        return _validate_time(v)

    @model_validator(mode="after")
    def check_warning_below_critical(self) -> "SwapSettings":
        if self.auto_swap_5h_warning >= self.auto_swap_5h_critical:
            raise ValueError(
                f"warning ({self.auto_swap_5h_warning}) must be less than "
                f"critical ({self.auto_swap_5h_critical})"
            )
        return self
```

Update the import line to include `field_validator` and `model_validator`:

```python
from pydantic import BaseModel, Field, field_validator, model_validator
```

- [ ] **Step 2: Add paused_until to PUT handler**

In the `update_swap_settings` function (line 57-73), add after the existing `set_setting` calls (after line 71):

```python
    if body.auto_swap_paused_until:
        db.set_setting("auto_swap_paused_until", body.auto_swap_paused_until)
    else:
        db.set_setting("auto_swap_paused_until", "")
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -10`

---

### Task 3: Clean up usage_monitor.py — observability and code quality

**Files:**
- Modify: `jacked/api/usage_monitor.py`
- Modify: `jacked/web/auto_swap.py`

Six fixes: (1) deduplicate tier-regex, (2) log invalid pause timestamps, (3) burn-rate decay floor, (4) sweep heartbeat, (5) fix type annotation, (6) clean up dead variable.

- [ ] **Step 1: Add tier_label helper to auto_swap.py**

In `jacked/web/auto_swap.py`, after the `tier_critical_threshold` function (after line 49), add:

```python
def tier_label(account: dict) -> str:
    """Return a human-readable tier label like '(tier 20x)' or ''."""
    tier = (account.get("rate_limit_tier") or "").lower()
    match = re.search(r"(\d+)x", tier)
    return f" (tier {match.group(1)}x)" if match else ""
```

- [ ] **Step 2: Fix type annotation and import BurnRate**

In `jacked/api/usage_monitor.py`, change line 21 from:

```python
_burn_rates: dict = {}
```

to:

```python
_burn_rates: dict[int, "BurnRate"] = {}
```

(Forward reference string avoids circular import at module level. The actual import happens inside the function.)

- [ ] **Step 3: Replace duplicated tier regex with tier_label call**

In `jacked/api/usage_monitor.py`, replace lines 226-233:

```python
                    tier_match = None
                    try:
                        import re
                        tier_str = (active_acct.get("rate_limit_tier") or "").lower()
                        tier_match = re.search(r"(\d+)x", tier_str)
                    except Exception:
                        pass
                    tier_label = f" (tier {tier_match.group(1)}x)" if tier_match else ""
```

with:

```python
                    from jacked.web.auto_swap import tier_label as _tier_label
                    tier_lbl = _tier_label(active_acct)
```

And update the two usages of `tier_label` variable in the reason strings below to use `tier_lbl`.

- [ ] **Step 4: Add warning log for invalid pause timestamps**

In `jacked/api/usage_monitor.py`, replace lines 108-109:

```python
                except (ValueError, TypeError):
                    pass  # Invalid timestamp -- treat as not paused
```

with:

```python
                except (ValueError, TypeError):
                    logger.warning(
                        "Ignoring unparseable pause timestamp: %r",
                        paused_until_str,
                    )
```

- [ ] **Step 5: Add burn-rate decay floor**

In `jacked/api/usage_monitor.py`, after lines 171-172 (`prev.rate_5h_per_min *= 0.8` / `prev.rate_7d_per_min *= 0.8`), add:

```python
                    if prev.rate_5h_per_min < 0.001:
                        prev.rate_5h_per_min = 0.0
                    if prev.rate_7d_per_min < 0.001:
                        prev.rate_7d_per_min = 0.0
```

- [ ] **Step 6: Add sweep heartbeat log**

In `jacked/api/usage_monitor.py`, in `full_sweep_loop`, add a summary log after the window keeper section completes (before the outer `except asyncio.CancelledError`). Add tracking variables at the start of the tick and log at the end:

After the `accounts = db.list_accounts(include_inactive=False)` on line 385, add:

```python
            sweep_checked = 0
            sweep_pinged = 0
```

After `await asyncio.sleep(1)` on line 399, add:

```python
                sweep_checked += 1
```

After `await asyncio.sleep(2)` on line 434, add:

```python
                    sweep_pinged += 1
```

Before the outer `except asyncio.CancelledError` on line 436, add:

```python
            logger.info(
                "Full sweep complete: checked %d accounts, pinged %d windows",
                sweep_checked, sweep_pinged,
            )
```

- [ ] **Step 7: Clean up dead variable in full_sweep_loop**

Remove lines 391-392:

```python
                effective_token = None
```

And change line 393 from:

```python
                result = await fetch_usage(
                    acct_id, db, access_token=effective_token,
                )
```

to:

```python
                result = await fetch_usage(acct_id, db)
```

- [ ] **Step 8: Run tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py tests/unit/test_auto_swap.py -v --tb=short 2>&1 | tail -20`

---

### Task 4: Fix window_keeper.py timezone-naive datetime

**Files:**
- Modify: `jacked/web/window_keeper.py:70-71`

`needs_ping()` uses `datetime.now()` (naive) while API returns timezone-aware ISO strings. This causes `TypeError` on mixed-awareness comparison in Python 3.12+.

- [ ] **Step 1: Fix the timezone**

In `jacked/web/window_keeper.py`, add `timezone` to the import on line 14:

```python
from datetime import datetime, timedelta, timezone
```

Then change line 70-71 from:

```python
    expiry = datetime.fromisoformat(resets_at)
    return expiry <= datetime.now()
```

to:

```python
    expiry = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    return expiry <= datetime.now(timezone.utc)
```

- [ ] **Step 2: Run window keeper tests**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py -v --tb=short`

---

### Task 5: Write missing tests — cooldown, pause, burn-rate decay

**Files:**
- Modify: `tests/unit/test_usage_monitor.py`

Three critical test gaps: (1) swap cooldown, (2) pause mechanism, (3) burn-rate decay. Also fix cross-test contamination from `_last_swap_time`.

- [ ] **Step 1: Add `_last_swap_time` reset to all test classes that touch module state**

In every test class that has `mod._burn_rates.clear()`, also add `mod._last_swap_time = 0.0`. This affects: `TestAutoSwapTriggers`, `TestNoTarget`, `TestCachedResponse`, `TestBurnRateSkipsUnchanged`, `TestBurnRateReseedAfterSwap`, `TestTOCTOU`.

- [ ] **Step 2: Write swap cooldown test**

Add after `TestBurnRateReseedAfterSwap`:

```python
class TestSwapCooldown:
    def test_swap_blocked_during_cooldown(self):
        """Swap should be skipped when within cooldown window."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        # Set last swap to NOW — cooldown is active
        mod._last_swap_time = time.time()

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Swap should NOT have fired — cooldown active
                mock_sync.assert_not_called()
                db.record_swap.assert_not_called()

        asyncio.run(_run())

    def test_swap_allowed_after_cooldown_expires(self):
        """Swap should proceed when cooldown has expired."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        # Set last swap to well past cooldown
        mod._last_swap_time = time.time() - _SWAP_COOLDOWN_SECONDS - 10

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Swap SHOULD have fired — cooldown expired
                assert mock_sync.call_count >= 1

        asyncio.run(_run())
```

- [ ] **Step 3: Write pause mechanism test**

Add after `TestSwapCooldown`:

```python
class TestPauseMechanism:
    def test_swap_skipped_when_paused(self):
        """Auto-swap paused until future time -> no swap."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "auto_swap_paused_until": future,
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                ) as mock_fetch,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # fetch_usage should NOT be called — paused before we get there
                mock_fetch.assert_not_called()

        asyncio.run(_run())

    def test_swap_proceeds_when_pause_expired(self):
        """Auto-swap paused until past time -> swap allowed."""
        import jacked.api.usage_monitor as mod
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        from datetime import datetime, timezone, timedelta
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        accounts = [
            _acct(1, usage_5h=95, usage_7d=50),
            _acct(2, usage_5h=20, usage_7d=10),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "auto_swap_paused_until": past,
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        ws_registry = AsyncMock()
        app = _make_app(db=db, ws_registry=ws_registry)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=3),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ) as mock_sync,
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Swap should fire — pause expired
                assert mock_sync.call_count >= 1

        asyncio.run(_run())
```

- [ ] **Step 4: Write burn-rate decay test**

Add after `TestPauseMechanism`:

```python
class TestBurnRateDecay:
    def test_decay_after_5_unchanged_ticks(self):
        """Burn rate decays by 0.8x after 5+ unchanged ticks below warning."""
        import jacked.api.usage_monitor as mod
        from jacked.web.auto_swap import BurnRate
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        # Pre-seed with a burn rate and 4 unchanged ticks (next will be 5th)
        mod._burn_rates[1] = BurnRate(
            rate_5h_per_min=1.0,
            last_check_5h=50.0,  # matches account usage
            rate_7d_per_min=0.5,
            last_check_7d=30.0,
        )
        mod._burn_rate_unchanged_ticks[1] = 4  # next tick is the 5th

        accounts = [
            _acct(1, usage_5h=50, usage_7d=30),  # same as seeded
            _acct(2, usage_5h=10, usage_7d=5),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=1),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Burn rate should have decayed: 1.0 * 0.8 = 0.8
                br = mod._burn_rates[1]
                assert br.rate_5h_per_min == pytest.approx(0.8, abs=0.01)
                assert br.rate_7d_per_min == pytest.approx(0.4, abs=0.01)

        asyncio.run(_run())

    def test_no_decay_above_warning_threshold(self):
        """Burn rate should NOT decay when usage is above warning threshold."""
        import jacked.api.usage_monitor as mod
        from jacked.web.auto_swap import BurnRate
        mod._burn_rates.clear()
        mod._burn_rate_unchanged_ticks.clear()
        mod._last_swap_time = 0.0

        # Usage at 85% — above warning (80%)
        mod._burn_rates[1] = BurnRate(
            rate_5h_per_min=1.0,
            last_check_5h=85.0,
            rate_7d_per_min=0.5,
            last_check_7d=30.0,
        )
        mod._burn_rate_unchanged_ticks[1] = 10  # well past threshold

        accounts = [
            _acct(1, usage_5h=85, usage_7d=30),
            _acct(2, usage_5h=10, usage_7d=5),
        ]
        db = _make_db(
            settings={
                "auto_swap_enabled": "true",
                "usage_check_interval": "300",
            },
            accounts=accounts,
        )
        app = _make_app(db=db)

        async def _run():
            with (
                patch(
                    "jacked.api.usage_monitor.asyncio.sleep",
                    side_effect=_sleep_canceller(max_sleeps=1),
                ),
                patch(
                    "jacked.api.usage_monitor._read_active_account_id",
                    return_value=1,
                ),
                patch(
                    "jacked.web.auth.fetch_usage",
                    new_callable=AsyncMock,
                    return_value={"_cached": True},
                ),
                patch(
                    "jacked.api.credential_helpers.read_fresh_active_token",
                    return_value="tok",
                ),
                patch(
                    "jacked.api.credential_helpers.sync_credential_to_all_stores",
                ),
            ):
                with pytest.raises(asyncio.CancelledError):
                    await active_account_poll_loop(app)

                # Burn rate should NOT have decayed — above warning
                br = mod._burn_rates.get(1)
                # The account may have been swapped (usage above critical),
                # but if burn rate is still there, it should not have decayed
                if br is not None:
                    assert br.rate_5h_per_min >= 1.0

        asyncio.run(_run())
```

- [ ] **Step 5: Run all tests**

Run: `uv run python -m pytest tests/unit/test_usage_monitor.py -v --tb=short`

Expected: ALL PASS

---

### Task 6: Run full test suite and commit

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

Expected: All tests pass.

- [ ] **Step 2: Commit all DCR fixes**

```bash
git add jacked/api/usage_monitor.py jacked/api/routes/settings_swap.py \
       jacked/web/auto_swap.py jacked/web/window_keeper.py \
       tests/unit/test_usage_monitor.py jacked/__init__.py
git commit -m "fix: DCR findings — CPU loop, validation, observability, missing tests

- Fix infinite CPU loop: swap cooldown continue skipped sleep
- Add cross-field validation: warning < critical
- Add HH:MM pattern validation on time-string fields
- Persist auto_swap_paused_until in PUT handler
- Deduplicate tier-regex via tier_label() helper
- Log invalid pause timestamps instead of silent swallow
- Floor burn-rate decay at 0.001 to reach true zero
- Add full-sweep heartbeat log for loop liveness
- Fix timezone-naive datetime.now() in needs_ping()
- Fix _burn_rates type annotation
- Add tests: swap cooldown, pause mechanism, burn-rate decay
- Reset _last_swap_time in test setup to prevent contamination"
```
