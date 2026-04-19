# 0.41.23 — Stuck-Checking Watchdog + Sweep Resilience

**Date:** 2026-04-19
**Target version:** 0.41.23
**Status:** Approved for planning

## Problem

On 2026-04-19, `jack@jackmd.com` (account 1) was stuck in the dashboard showing
"Checking usage…" for ~14 hours. Investigation revealed three independent
failure modes that together produced the stuck state:

1. **Orphaned `checking` state in DB.** `validate_account()` at
   `jacked/web/auth.py:886` writes `validation_status="checking"` to the DB
   *before* any network I/O. If the coroutine is abandoned mid-flight (server
   kill, asyncio cancellation, the force-reset path in the bulk-lock guard
   leaving behind a zombie task, or a sync blocker that outlives process
   death), the DB row stays at `checking` forever. Nothing ever resets it.
   The 0.41.18 "120s watchdog" only cleared the *UI card display* — it does
   not touch the DB.

2. **Force-reset lock leaves zombie task.** `refresh_all_usage` at
   `jacked/api/routes/auth.py:644-653` rebinds `_bulk_refresh_lock` to a
   fresh `asyncio.Lock()` when the prior holder has held for >180s. The
   orphaned coroutine is not cancelled — it continues running (possibly
   indefinitely) in the background, potentially re-writing stale values
   after the new holder completes, or blocking on whatever hung it.

3. **Sweep loop blocks silently on unbounded fetch_usage.** The existing
   exception guard at `jacked/api/usage_monitor.py:1139` catches exceptions
   — but cannot catch hangs.  `full_sweep_loop:1128` awaits `fetch_usage(...)`
   without an `asyncio.wait_for` bound, so a single slow or hung API call
   stalls the entire sweep indefinitely.  There's also no heartbeat log,
   so operators can't distinguish "sweep alive but idle" from "sweep
   blocked."  On 2026-04-19 the sweep stopped logging at 09:46:40 and
   never logged again until the server was restarted at 10:22:53 — 36
   minutes of silent blocking.

## Non-goals

- **Alerting / paging.** Out of scope for a local tool.
- **Rewriting the sweep architecture.** We're patching observed failure
  modes, not redesigning the system.
- **Changing `validate_account`'s "write checking before network" pattern.**
  That pattern is correct for preventing concurrent validation races; the
  bug is that nothing cleans up abandoned entries. Fix the cleanup.
- **No new `pytest-asyncio` dependency.**  Tests use the project's
  existing `asyncio.run()` wrapper pattern (see
  `tests/unit/test_usage_monitor.py:4`).

## Approach — four surgical components

### A. Stuck-checking DB watchdog

New function in `jacked/web/auth.py`:

```python
async def reset_stale_checking_accounts(db: Database, threshold_seconds: int = 120) -> int:
    """Reset accounts stuck in validation_status='checking' past the threshold.

    Returns the number of accounts reset.  Logs a warning per reset so
    operators can spot recurring stuck-state incidents in the logs.
    """
```

Uses `updated_at` as the staleness reference (already set by any DB write).
Sets `validation_status="unknown"` and `last_error="validation timed out —
reset by watchdog after Ns"` with `last_error_at` at reset time.

Registered as a new background task in `jacked/api/main.py`, alongside the
existing background tasks. Runs every 60s. Matches the pattern of the other
`start_*_loop()` helpers.

### B. Per-account `asyncio.wait_for` in bulk refresh

Modify `jacked/api/routes/auth.py:refresh_all_usage` loop body:

```python
try:
    usage_data = await asyncio.wait_for(
        fetch_usage(acct["id"], db, access_token=effective_token, manual=True),
        timeout=60.0,
    )
except asyncio.TimeoutError:
    logger.warning(
        "Bulk refresh: account %d fetch_usage exceeded 60s — marking failed",
        acct["id"],
    )
    usage_data = None
    # Record the failure so the account shows an error, not stuck "checking"
    db.record_account_error(
        acct["id"],
        "Usage fetch timed out after 60s during bulk refresh",
    )
```

60s is a generous upper bound: httpx timeout is 15s, token refresh adds
another 15-30s worst case. A single account taking >60s is a bug signal.

### C. Bulk-lock force-reset cancels the orphaned task (fire-and-forget)

Add module-level `_bulk_refresh_task: asyncio.Task | None = None` in
`jacked/api/routes/auth.py`. Store the running task reference at the top of
the `async with _bulk_refresh_lock:` block. On stale-lock detection:

```python
if held_for > _BULK_REFRESH_STALE_AFTER:
    logger.warning("Bulk refresh lock held %ds — forcing reset", int(held_for))
    orphan = _bulk_refresh_task
    if orphan is not None and not orphan.done():
        orphan.cancel()
        # Fire-and-forget: do NOT await the orphan.  The event loop
        # delivers the cancel on its next tick.  Awaiting here would
        # re-raise CancelledError through our handler in a way that's
        # impossible to distinguish portably from "we were cancelled
        # ourselves" on Python 3.10 (no Task.cancelling()).  The orphan
        # was hung >180s already — its cancellation-time DB writes are
        # no less suspect with a 2s wait than without one.
    _bulk_refresh_lock = asyncio.Lock()
    _bulk_refresh_acquired_at = 0.0
    _bulk_refresh_task = None
```

The `async with _bulk_refresh_lock:` body wraps its cleanup in a try/
finally that clears `_bulk_refresh_task` only when the slot still points
at the current task (`if _bulk_refresh_task is my_task`).  Prevents a
late-finishing orphan's finally from wiping a newer holder's slot.

### D. Sweep heartbeat + bounded fetch_usage

In `jacked/api/usage_monitor.py`'s `full_sweep_loop`:

1. Emit an INFO log at the TOP of every iteration (BEFORE any
   `if not window_keeper_enabled: continue` short-circuit).  Format:
   `Full-sweep heartbeat: iter=N`.  This guarantees a heartbeat fires
   every iteration regardless of config.
2. Wrap the internal `await fetch_usage(...)` at line 1128 in
   `asyncio.wait_for(..., 60.0)` — same pattern as Component B.  Bounds
   any single slow/hung account to 60s max.

Operator canary: no heartbeat for >10 min = sweep is blocked or dead.

### E. Validator success paths clear `last_error`

In `validate_account` at `jacked/web/auth.py:870`, both HTTP 200 success
paths (first-try at line 898-906, retry-after-refresh at line 920-928)
now also clear `last_error=None, last_error_at=None` on the DB write.

Without this, a row that the watchdog reset to `validation_status="unknown"`
with a "validation timed out — reset by watchdog" error would keep that
error banner forever even after a subsequent successful validation moved
the row to `validation_status="valid"`.

## Files touched

| File | Change |
| --- | --- |
| `jacked/web/auth.py` | Add `reset_stale_checking_accounts()` |
| `jacked/api/main.py` | Register watchdog task, start on lifespan startup |
| `jacked/api/routes/auth.py` | Per-account `wait_for`; track + cancel orphan task |
| `jacked/api/usage_monitor.py` | Sweep exception guard + heartbeat |
| `jacked/__init__.py` | Bump to `0.41.23` |
| `README.md` | Changelog entry |

## Tests

New file `tests/unit/test_stuck_checking_watchdog.py`:

1. `reset_stale_checking_accounts` resets only stale `checking` rows.
2. `reset_stale_checking_accounts` sets `last_error` with watchdog marker.
3. `reset_stale_checking_accounts` returns count == rows reset.
4. `reset_stale_checking_accounts` leaves `valid`/`invalid`/`unknown` alone.
5. `reset_stale_checking_accounts` ignores fresh `checking` (< threshold).

New file `tests/unit/api/test_bulk_refresh_timeout.py`:

1. `refresh_all_usage` with a hanging `fetch_usage` times out per-account
   at 60s and moves on to the next account.
2. `refresh_all_usage` records `last_error` on timeout.
3. Stale-lock detection cancels the prior task before resetting lock.

New file `tests/unit/api/test_sweep_heartbeat.py`:

1. Heartbeat fires every 5 min.
2. Exception in sweep iteration is logged with traceback, doesn't kill loop.
3. After exception, next iteration runs normally.

All tests run under `uv run python -m pytest` per project CLAUDE.md.

## Cross-platform considerations

This is all async Python + sqlite — no OS-specific paths. Works on macOS,
Linux, Windows identically. No changes to tray, launchd, systemd, or
Windows VBS paths.

## Acceptance criteria

- After 0.41.23 is running for >60s, a DB row with `validation_status=
  "checking"` older than 120s **cannot persist**. The watchdog clears it.
- A single hung `fetch_usage` call during bulk refresh does not block any
  other account from being refreshed.
- A sweep-loop exception logs a full traceback in `~/.claude/jacked-service.log`
  and does not silently kill the loop.
- Heartbeat log line appears in `~/.claude/jacked-service.log` at least once
  every 5 minutes while the server is running.
- All existing tests still pass. New tests added per above pass.

## Risks & open questions

**Risk: watchdog races with a legitimate in-flight validation.** Mitigation:
120s threshold is far above normal validation time (~1-3s). If a validation
ever legitimately takes >120s, we should reset it — that's the whole point.

**Risk: `asyncio.wait_for` cancellation leaves `validation_status="checking"`
in the DB.** The DB watchdog (component A) handles this: even if `wait_for`
cancels mid-call, the watchdog will clean up within 60-120s.

**Risk: `_bulk_refresh_task.cancel()` awakens the task into a partially-
applied state.** Acceptable — any subsequent DB writes it makes are either
idempotent (usage cache update) or superseded by the new bulk refresh.

**Open:** should the watchdog also clear the bulk lock if it's held past
a wall-clock limit AND all accounts have `validation_status != "checking"`?
Deferring this — components A+C already cover the observed failure mode.
