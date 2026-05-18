# 7-Day Window Keeper Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the window keeper to also ping accounts whose 7-day window has reset but not yet restarted. Currently it only pings for expired 5-hour windows, leaving the 7d window "floating" when it resets mid-5h-window.

**Architecture:** Add `needs_7d_ping()` pure function that returns True when `cached_7d_resets_at` is in the past (window reset but new one not started). Update the full sweep loop to OR this with the existing `needs_ping()` check so either condition triggers a ping. The same ping call starts both windows simultaneously.

**Tech Stack:** Python, pytest

---

## The Problem

The 5h and 7d windows don't line up. An account can have:
- An active 5h window (`needs_ping` returns False)
- But its 7d window has already reset (`cached_7d_resets_at < now`)

The window keeper only checks the 5h window, so it never pings this account. The new 7d window stays "un-started" — it only begins counting once the user happens to use the account. Meanwhile, the cached usage data is stale (shows the old 7d usage from before the reset), so the proactive scheduler can't tell that there's fresh capacity to burn.

Concrete example from live data:
- user3@example.com: 5h active until 22:00 UTC, 7d reset 2.1 hours ago, cached data still shows old 7d usage
- `needs_ping` returns False (5h still active)
- Window keeper never pings
- New 7d window doesn't start until the user happens to make a call

## The Fix

Add a second trigger to the window keeper: ping accounts whose 7d window has reset but whose usage data is stale (older than the reset). The same ping that would open a 5h window also starts a new 7d window — they're the same API call.

Key insight: pinging an account with an active 5h window but an expired 7d window is SAFE — it uses one message (haiku, max_tokens=1), costs almost nothing, and its whole purpose is to start the new 7d window counter.

---

## File Structure

| File | Responsibility |
|------|---------------|
| `jacked/web/window_keeper.py` | Add `needs_7d_ping()` pure function |
| `jacked/api/usage_monitor.py` | Use `needs_7d_ping()` in sweep loop alongside `needs_ping()` |
| `tests/unit/test_window_keeper.py` | Tests for the new function |

---

### Task 1: Add `needs_7d_ping` pure function + tests

**Files:**
- Modify: `jacked/web/window_keeper.py`
- Test: `tests/unit/test_window_keeper.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_window_keeper.py`:

```python
class TestNeeds7dPing:
    def test_none_resets_at_needs_ping(self):
        """No 7d window ever opened → needs ping."""
        from jacked.web.window_keeper import needs_7d_ping
        assert needs_7d_ping(None, None) is True

    def test_future_reset_no_ping(self):
        """7d window still active → no ping needed."""
        from datetime import datetime, timezone, timedelta
        import time as _time
        from jacked.web.window_keeper import needs_7d_ping

        future = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
        now = int(_time.time())
        assert needs_7d_ping(future, now) is False

    def test_past_reset_with_stale_data_needs_ping(self):
        """7d window reset and cached data is older than reset → needs ping."""
        from datetime import datetime, timezone, timedelta
        from jacked.web.window_keeper import needs_7d_ping

        # Reset was 2 hours ago
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        past_ts = int((datetime.now(timezone.utc) - timedelta(hours=3)).timestamp())
        # Cached 3 hours ago (before the reset) → stale
        assert needs_7d_ping(past, past_ts) is True

    def test_past_reset_with_fresh_data_no_ping(self):
        """7d reset already happened and data was refreshed after → no ping.

        Someone already pinged this account; the new 7d window is running.
        """
        from datetime import datetime, timezone, timedelta
        from jacked.web.window_keeper import needs_7d_ping

        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        # Cached 1 hour ago — after the reset
        fresh_ts = int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp())
        assert needs_7d_ping(past, fresh_ts) is False

    def test_none_cached_at_with_past_reset_needs_ping(self):
        """Never cached + reset in past → needs ping."""
        from datetime import datetime, timezone, timedelta
        from jacked.web.window_keeper import needs_7d_ping

        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        assert needs_7d_ping(past, None) is True

    def test_garbage_string_returns_false(self):
        """Unparseable timestamp → safe default: no ping."""
        from jacked.web.window_keeper import needs_7d_ping
        assert needs_7d_ping("not-a-date", 12345) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py::TestNeeds7dPing -v`
Expected: FAIL — `needs_7d_ping` not found

- [ ] **Step 3: Implement `needs_7d_ping`**

In `jacked/web/window_keeper.py`, after the `needs_ping` function (around line 72), add:

```python
def needs_7d_ping(resets_at: str | None, usage_cached_at: int | None) -> bool:
    """Return True if an account's 7-day window has reset but not restarted.

    The 7-day window resets at `cached_7d_resets_at`. A new 7d window
    only STARTS counting when an API call is made to the account. If
    the reset happened but no call has been made since, the window is
    "floating" — cached data still shows the old usage.

    Returns True when:
    - resets_at is None → never opened
    - resets_at is in the past AND usage_cached_at is older than resets_at
      (the reset happened but we haven't refreshed since)

    Returns False when:
    - resets_at is in the future → window still active
    - resets_at is in the past but data was refreshed after the reset
      (new window already running)
    - resets_at is unparseable → safe default
    """
    if resets_at is None:
        return True
    try:
        expiry = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
        if expiry.tzinfo is None:
            expiry = expiry.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False

    now = datetime.now(timezone.utc)
    if expiry > now:
        # 7d window still active
        return False

    # Reset is in the past — check if we've refreshed since
    if usage_cached_at is None:
        return True
    return usage_cached_at < expiry.timestamp()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_window_keeper.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/window_keeper.py tests/unit/test_window_keeper.py
git commit -m "feat: needs_7d_ping — detect 7d windows that reset but didn't restart"
```

---

### Task 2: Use `needs_7d_ping` in sweep loop

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Update the sweep loop import and logic**

In `jacked/api/usage_monitor.py`, find the sweep loop imports (around line 981):

```python
                needs_ping,
```

Add `needs_7d_ping` to the import:

```python
                needs_ping,
                needs_7d_ping,
```

- [ ] **Step 2: Update the ping decision to include 7d check**

Find the ping decision (around line 1007):

```python
                    if not needs_ping(acct.get("cached_5h_resets_at")):
                        continue
```

Replace with:

```python
                    needs_5h = needs_ping(acct.get("cached_5h_resets_at"))
                    needs_7d = needs_7d_ping(
                        acct.get("cached_7d_resets_at"),
                        acct.get("usage_cached_at"),
                    )
                    if not needs_5h and not needs_7d:
                        continue
```

Also update the log message (around line 1015) to indicate which window triggered the ping:

```python
                    logger.info(
                        "Window keeper: pinging account %d (%s)%s%s",
                        acct["id"], acct.get("email", "?"),
                        " [5h expired]" if needs_5h else "",
                        " [7d reset]" if needs_7d else "",
                    )
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 4: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: sweep loop pings accounts with floating 7d windows"
```

---

### Task 3: Update architecture doc

**Files:**
- Modify: `docs/architecture/auto-swap-system.md`

- [ ] **Step 1: Update the Window Keeper section**

Find the "Window Keeper" section (around line 172). Update the bullet list:

```
- Checks `needs_ping` for each account (uses `cached_5h_resets_at` from DB)
```

Replace with:

```
- Checks `needs_ping` (5h expired) AND `needs_7d_ping` (7d reset with stale data) for each account
- The 5h and 7d windows don't always line up — the 7d can reset mid-5h-window, leaving it "floating" until the next API call. `needs_7d_ping` catches this.
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/auto-swap-system.md
git commit -m "docs: document needs_7d_ping in window keeper section"
```
