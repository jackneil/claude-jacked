# Deep DCR Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 3 CRITICALs and 5 high-value MEDIUMs from the deep DCR pass: extract swap execution helper (fixes TOCTOU, partial-swap recovery, ordering, locking), convert WebSocket blocklist to whitelist, fix _initial_fetch_done, remove double fetch, add proactive logging, recompute stale deficit.

**Architecture:** The key structural change is extracting the duplicated 7-step swap execution into `_execute_swap()`. This single helper fixes 4 findings simultaneously: adds TOCTOU guard, wraps credential write in cross-process lock, uses consistent ordering, and enables partial-swap retry by tracking completion state. The remaining fixes are targeted one-liners.

**Tech Stack:** Python, pytest

---

## File Structure

| File | Responsibility |
|------|---------------|
| `jacked/api/usage_monitor.py` | ALL changes: extract `_execute_swap()`, convert WS whitelist, fix initial fetch, remove double fetch, add logging, fix stale deficit |

---

### Task 1: Convert WebSocket `_SENSITIVE_FIELDS` blocklist to whitelist

**Files:**
- Modify: `jacked/api/usage_monitor.py:249-267`

The blocklist approach means any new DB column automatically leaks via WebSocket. The REST API uses a whitelist via `_account_to_response`. Apply the same pattern.

- [ ] **Step 1: Replace the blocklist with a whitelist**

Find the `usage_poll_updated` broadcast block (lines 249-267). Replace the `_SENSITIVE_FIELDS` blocklist approach:

```python
            _ws = getattr(app.state, "ws_registry", None)
            if _ws and active_acct:
                # Strip sensitive fields before broadcasting — the raw DB row
                # includes OAuth tokens that must never reach WebSocket clients.
                _SENSITIVE_FIELDS = {
                    "access_token", "refresh_token",
                    "cc_access_token", "cc_refresh_token",
                }
                safe_acct = {
                    k: v for k, v in active_acct.items()
                    if k not in _SENSITIVE_FIELDS
                }
                await _ws.broadcast(
                    "usage_poll_updated",
                    {
                        "account_id": active_acct_id,
                        "account_data": safe_acct,
                    },
                )
```

With a whitelist approach:

```python
            _ws = getattr(app.state, "ws_registry", None)
            if _ws and active_acct:
                # Whitelist safe fields — new DB columns won't leak by default.
                # Mirrors _account_to_response in routes/auth.py.
                _WS_SAFE_FIELDS = {
                    "id", "email", "organization_uuid", "organization_name",
                    "display_name", "expires_at", "scopes",
                    "subscription_type", "rate_limit_tier", "has_extra_usage",
                    "priority", "is_active", "is_deleted",
                    "last_used_at", "cached_usage_5h", "cached_usage_7d",
                    "cached_5h_resets_at", "cached_7d_resets_at",
                    "usage_cached_at", "last_error", "last_error_at",
                    "consecutive_failures", "last_validated_at",
                    "validation_status", "created_at", "updated_at",
                    "cc_expires_at", "auto_swap_enabled",
                }
                safe_acct = {
                    k: v for k, v in active_acct.items()
                    if k in _WS_SAFE_FIELDS
                }
                await _ws.broadcast(
                    "usage_poll_updated",
                    {
                        "account_id": active_acct_id,
                        "account_data": safe_acct,
                    },
                )
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "fix: convert WebSocket account data from blocklist to whitelist"
```

---

### Task 2: Extract `_execute_swap()` helper

This is the key structural fix. It simultaneously addresses:
- CRITICAL: Proactive path missing TOCTOU guard
- CRITICAL: Partial swap arms cooldown, blocks retry
- MEDIUM: Swap execution ordering inconsistency
- MEDIUM: `sync_credential_to_all_stores` called without lock

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Add the `_execute_swap` helper function**

Add this function BEFORE `active_account_poll_loop` (around line 135):

```python
async def _execute_swap(
    db,
    active_acct_id: int,
    active_acct: dict,
    target: dict,
    reason: str,
    trigger: str,
    usage_5h: float | None,
    usage_7d: float | None,
    active_start: str,
    active_end: str,
    ws_registry=None,
) -> bool:
    """Execute a swap from active_acct to target. Returns True if credential write succeeded.

    Steps (canonical ordering):
    1. TOCTOU guard — re-read active account, abort if changed
    2. Record swap + arm cooldown (audit trail survives credential write failure)
    3. Reconcile outgoing credentials (capture token rotation)
    4. Write incoming credentials under cross-process lock
    5. Clean up burn-rate state
    6. Broadcast via WebSocket
    """
    global _last_swap_time

    from jacked.api.credential_helpers import (
        acquire_claude_lock,
        reconcile_outgoing_credentials,
        sync_credential_to_all_stores,
    )
    from jacked.web.auto_swap import format_account_label

    # 1. TOCTOU guard
    current_active = _read_active_account_id()
    if current_active != active_acct_id:
        logger.info(
            "Swap aborted: active account changed from %d to %s during evaluation",
            active_acct_id, current_active,
        )
        return False

    # 2. Record swap + arm cooldown BEFORE credential write
    _last_swap_time = time.time()
    db.record_swap(
        from_account_id=active_acct_id,
        to_account_id=target["id"],
        reason=reason,
        trigger=trigger,
        from_5h=usage_5h,
        from_7d=usage_7d,
        to_5h=target.get("cached_usage_5h"),
        to_7d=target.get("cached_usage_7d"),
    )

    # 3. Reconcile outgoing credentials
    reconcile_outgoing_credentials(active_acct_id, db)

    # 4. Write incoming credentials under cross-process lock
    credential_ok = False
    with acquire_claude_lock() as locked:
        if locked:
            sync_credential_to_all_stores(
                target["id"], target,
                email=target.get("email"),
            )
            credential_ok = True
        else:
            logger.warning(
                "Swap: could not acquire lock for credential write "
                "(account %d -> %d)", active_acct_id, target["id"],
            )

    # 5. Clean up burn-rate state
    _burn_rates.pop(active_acct_id, None)
    _burn_rate_unchanged_ticks.pop(active_acct_id, None)
    _burn_rates.pop(target["id"], None)
    _burn_rate_unchanged_ticks.pop(target["id"], None)

    # 6. Broadcast via WebSocket
    if ws_registry:
        await ws_registry.broadcast(
            "auto_swap_triggered",
            {
                "from_account_id": active_acct_id,
                "to_account_id": target["id"],
                "from_email": active_acct.get("email", ""),
                "to_email": target.get("email", ""),
                "from_label": format_account_label(active_acct),
                "to_label": format_account_label(target),
                "reason": reason,
            },
        )

    if not credential_ok:
        # Reset cooldown so next tick retries immediately
        _last_swap_time = 0.0
        logger.warning(
            "Swap recorded but credential write failed — will retry next tick"
        )

    return credential_ok
```

- [ ] **Step 2: Replace the defensive swap execution block**

Find the defensive swap execution (lines ~431-473, from the comment "Record swap and set cooldown BEFORE credential write" to the end of the WebSocket broadcast block). Also remove the TOCTOU guard above it (lines ~383-393) since `_execute_swap` handles it now. Replace the entire section from line 373 (`if target is not None:`) to line 473 (end of broadcast block) with:

```python
                if target is not None:
                    # -- Swap cooldown: prevent ping-ponging ------
                    if (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                        logger.debug(
                            "Active poll: swap cooldown active (%.0fs remaining)",
                            _SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time),
                        )
                        await asyncio.sleep(60)
                        continue

                    # -- Build descriptive reason -------------------------
                    if escape_override and not want_swap:
                        reason = (
                            f"escape hatch: suppressed swap overridden — "
                            f"target scores {score_candidate(target, active_start, active_end):.0f}"
                        )
                    elif usage_5h is not None and usage_5h >= effective_critical:
                        tier_lbl = _tier_label(active_acct)
                        reason = (
                            f"5h critical: {usage_5h:.1f}% >= "
                            f"{effective_critical:.0f}%{tier_lbl}"
                        )
                    elif usage_7d is not None and usage_7d >= threshold_7d:
                        reason = (
                            f"7d threshold: {usage_7d:.1f}% >= "
                            f"{threshold_7d:.0f}%"
                        )
                    else:
                        projected = usage_5h or 0
                        if br and br.rate_5h_per_min > 0:
                            mins = (check_interval / 60) * 2
                            projected = (usage_5h or 0) + br.rate_5h_per_min * mins
                        reason = (
                            f"burn-rate projection: {usage_5h:.1f}% -> "
                            f"{projected:.1f}% in {int((check_interval / 60) * 2)}min"
                        )

                    logger.info(
                        "Auto-swap: switching from account %d (5h=%.1f%%) "
                        "to account %d (5h=%.1f%%) — %s",
                        active_acct_id, usage_5h or 0,
                        target["id"],
                        target.get("cached_usage_5h") or 0,
                        reason,
                    )

                    ws_registry = getattr(app.state, "ws_registry", None)
                    await _execute_swap(
                        db, active_acct_id, active_acct, target,
                        reason=reason, trigger="auto_swap",
                        usage_5h=usage_5h, usage_7d=usage_7d,
                        active_start=active_start, active_end=active_end,
                        ws_registry=ws_registry,
                    )
```

- [ ] **Step 3: Replace the proactive swap execution block**

Find the proactive swap execution (lines ~558-598, from `reconcile_outgoing_credentials` to end of broadcast). Replace with a call to `_execute_swap`. The proactive block from the `if target:` guard (after re-fetch) becomes:

```python
                            if target:
                                # Recompute deficit with fresh data
                                deficit_result = compute_7d_deficit(target, active_start, active_end)
                                if not deficit_result or deficit_result["deficit"] <= PROACTIVE_SWAP_THRESHOLD:
                                    logger.debug(
                                        "Proactive: target %d deficit dropped below threshold after re-fetch",
                                        target["id"],
                                    )
                                else:
                                    target_score = score_candidate(target, active_start, active_end)
                                    reason = (
                                        f"proactive: burning {deficit_result['unused_7d']:.0f}% "
                                        f"unused 7d on {format_account_label(target)} — "
                                        f"{deficit_result['effective_hours_remaining']:.0f} "
                                        f"effective hours left "
                                        f"({deficit_result['effective_windows_remaining']:.1f} windows), "
                                        f"score={target_score:.0f}"
                                    )
                                    logger.info(
                                        "Proactive swap: account %d is %.0f%% behind 7d schedule "
                                        "(score=%.0f)",
                                        target["id"], deficit_result["deficit"], target_score,
                                    )

                                    ws_registry = getattr(app.state, "ws_registry", None)
                                    await _execute_swap(
                                        db, active_acct_id, active_acct, target,
                                        reason=reason, trigger="proactive_7d",
                                        usage_5h=usage_5h, usage_7d=usage_7d,
                                        active_start=active_start, active_end=active_end,
                                        ws_registry=ws_registry,
                                    )
```

Note this also fixes the stale `deficit_result` issue — it recomputes after the re-fetch.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "refactor: extract _execute_swap helper — fixes TOCTOU, locking, ordering, partial-swap recovery"
```

---

### Task 3: Fix `_initial_fetch_done` and remove double fetch

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Move `_initial_fetch_done = True` to after the loop**

Find lines 214-223:

```python
            global _initial_fetch_done
            if not _initial_fetch_done:
                _initial_fetch_done = True
                from jacked.web.auth import fetch_usage as _prime_fetch
                logger.info("Auto-swap: priming usage data for all accounts")
                all_accts = db.list_accounts(include_inactive=False)
                for a in all_accts:
                    if a["id"] != active_acct_id:
                        await _prime_fetch(a["id"], db)
                        await asyncio.sleep(1)
```

Replace with:

```python
            global _initial_fetch_done
            if not _initial_fetch_done:
                from jacked.web.auth import fetch_usage as _prime_fetch
                logger.info("Auto-swap: priming usage data for all accounts")
                all_accts = db.list_accounts(include_inactive=False)
                primed = 0
                for a in all_accts:
                    if a["id"] != active_acct_id:
                        try:
                            await _prime_fetch(a["id"], db)
                            primed += 1
                        except Exception:
                            logger.debug("Prime fetch failed for account %d", a["id"])
                        await asyncio.sleep(1)
                if primed > 0:
                    _initial_fetch_done = True
                    logger.info("Auto-swap: primed %d/%d accounts", primed, len(all_accts) - 1)
```

- [ ] **Step 2: Remove the double `_fetch_candidate_usage` call on the exhaustion path**

Find the exhaustion path (around line 474-476):

```python
                else:
                    # Fetch fresh data for recovery estimate
                    accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)
```

The data was already fetched at line 349 moments earlier. Remove the redundant fetch:

```python
                else:
                    # accounts already fetched at line 349 — no need to re-fetch
```

- [ ] **Step 3: Add proactive scheduler "no target" logging**

In the proactive block, after `pick_best_target` returns, add logging for the skip cases. After line ~533 (`target = pick_best_target(...)`), add:

```python
                    if not target:
                        logger.debug("Proactive: no eligible target found")
                    elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                        logger.debug("Proactive: target found but cooldown active")
```

And after the deficit check (where `deficit_result["deficit"] <= PROACTIVE_SWAP_THRESHOLD`), the logging is already handled in Task 2's replacement code.

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "fix: initial fetch retry on failure, remove double candidate fetch, add proactive logging"
```
