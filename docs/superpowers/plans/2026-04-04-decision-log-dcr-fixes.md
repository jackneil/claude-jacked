# Decision Log + Proactive Scheduler DCR Fixes

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix 5 CRITICALs from DCR: decision log gaps (cooldown skip, missing candidates, regular path blind), silent prune failure, and late-night proactive swap guard.

**Architecture:** (1) Replace cooldown `continue` with recording + sleep. (2) Include all candidates in summaries regardless of pass/fail. (3) Build candidate summaries for regular swap path too. (4) Log prune failures, add deterministic fallback. (5) Add minimum-remaining-today guard to proactive scheduler.

**Tech Stack:** Python

---

### Task 1: Fix cooldown `continue` skipping decision log

**Files:** `jacked/api/usage_monitor.py`

- [ ] **Step 1:** Find the defensive swap cooldown block (lines 537-544):

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
```

Replace with recording BEFORE the sleep+continue:

```python
                if target is not None:
                    # -- Swap cooldown: prevent ping-ponging ------
                    if (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                        _decision_reason = (
                            f"swap needed but cooldown active "
                            f"({_SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time):.0f}s remaining)"
                        )
                        logger.debug("Active poll: %s", _decision_reason)
                        await asyncio.sleep(60)
                        continue
```

The `_decision_reason` will be picked up by the decision recording at end of tick. But wait — `continue` still skips it. We need to record BEFORE the continue. Add right before `await asyncio.sleep(60)`:

```python
                        # Record decision before skipping tick
                        try:
                            _tick_detail = _build_tick_detail(
                                active_acct=active_acct,
                                usage_5h=usage_5h, usage_7d=usage_7d,
                                want_swap=want_swap, suppression=_suppression,
                                escape_override=escape_override,
                                candidates=None, proactive_target_id=None,
                                cooldown_active=True, decision="stay",
                            )
                            db.record_decision(
                                account_id=active_acct_id, action="stay",
                                trigger="tick", target_id=target["id"],
                                reason=_decision_reason, detail=_tick_detail,
                            )
                        except Exception:
                            pass
```

- [ ] **Step 2:** Run tests: `uv run python -m pytest tests/ -q`

- [ ] **Step 3:** Commit: `git commit -m "fix: record decision before cooldown continue — no more invisible blocked swaps"`

---

### Task 2: Include all candidates in proactive summaries (not just passing ones)

**Files:** `jacked/api/usage_monitor.py`

- [ ] **Step 1:** In the proactive candidate loop, candidates with `deficit <= 0` hit `continue` at line 674-675 and are never added to `_candidate_summaries`. Add a summary BEFORE the continue. Replace:

```python
                        dr = compute_7d_deficit(acct, active_start, active_end)
                        if not dr or dr["deficit"] <= 0:
                            continue
```

With:

```python
                        dr = compute_7d_deficit(acct, active_start, active_end)
                        if not dr:
                            continue
                        if dr["deficit"] <= 0:
                            _candidate_summaries.append({
                                "id": acct["id"],
                                "email": acct.get("email", ""),
                                "7d": acct.get("cached_usage_7d"),
                                "deficit": round(dr["deficit"], 1),
                                "windows_remaining": round(dr["effective_windows_remaining"], 1),
                                "passes": False,
                                "skip_reason": "ahead_of_schedule",
                            })
                            continue
```

Similarly, candidates below threshold (line 682-683) hit `continue`. Add a summary. Replace:

```python
                        if dr["deficit"] <= threshold:
                            continue
```

With:

```python
                        if dr["deficit"] <= threshold:
                            _candidate_summaries.append({
                                "id": acct["id"],
                                "email": acct.get("email", ""),
                                "7d": acct.get("cached_usage_7d"),
                                "deficit": round(dr["deficit"], 1),
                                "windows_remaining": round(dr["effective_windows_remaining"], 1),
                                "threshold": round(threshold, 1),
                                "passes": False,
                                "skip_reason": "below_threshold",
                            })
                            continue
```

- [ ] **Step 2:** Run tests: `uv run python -m pytest tests/ -q`

- [ ] **Step 3:** Commit: `git commit -m "fix: include all evaluated candidates in decision log, not just passing ones"`

---

### Task 3: Add candidate summaries for regular (defensive) swap path

**Files:** `jacked/api/usage_monitor.py`

- [ ] **Step 1:** The defensive swap path calls `pick_best_target` but never builds candidate summaries. After `_fetch_candidate_usage` in the defensive path (around line 511-512) and before the `pick_best_target` call, add candidate summary building:

Find:
```python
                # Fetch fresh usage for candidates before scoring
                accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)
```

After it, before `target = pick_best_target(...)`, add:

```python
                # Build candidate summaries for decision log
                _candidate_summaries = []
                for _cand in accounts:
                    if _cand["id"] == active_acct_id:
                        continue
                    _cand_score = score_candidate(_cand, active_start, active_end)
                    _cand_deficit = compute_7d_deficit(_cand, active_start, active_end)
                    _candidate_summaries.append({
                        "id": _cand["id"],
                        "email": _cand.get("email", ""),
                        "label": format_account_label(_cand),
                        "5h": _cand.get("cached_usage_5h"),
                        "7d": _cand.get("cached_usage_7d"),
                        "score": round(_cand_score, 1),
                        "deficit": round(_cand_deficit["deficit"], 1) if _cand_deficit else None,
                    })
```

Note: `compute_7d_deficit` needs to be imported. It's already imported in the proactive block but not in the defensive path. Add it to the late imports at the top of the function (around line 210, in the `from jacked.web.auto_swap import` block).

- [ ] **Step 2:** Run tests: `uv run python -m pytest tests/ -q`

- [ ] **Step 3:** Commit: `git commit -m "fix: build candidate summaries for defensive swap path in decision log"`

---

### Task 4: Fix silent prune failure + add deterministic fallback

**Files:** `jacked/api/usage_monitor.py`

- [ ] **Step 1:** Add a module-level counter for deterministic prune. Near the other module-level state (around line 30), add:

```python
_ticks_since_prune = 0
```

- [ ] **Step 2:** Replace the probabilistic prune block (lines 807-812):

```python
            # Periodic prune (~1% of ticks)
            if random.random() < 0.01:
                try:
                    db.prune_decision_log()
                except Exception:
                    pass
```

With:

```python
            # Periodic prune — deterministic fallback every 500 ticks (~6-40 hours)
            _ticks_since_prune += 1
            if _ticks_since_prune >= 500 or random.random() < 0.01:
                try:
                    db.prune_decision_log()
                    _ticks_since_prune = 0
                except Exception:
                    logger.warning("Failed to prune decision log", exc_info=True)
```

Also add `global _ticks_since_prune` at the top of the try block in the function (near the other globals).

- [ ] **Step 3:** Run tests: `uv run python -m pytest tests/ -q`

- [ ] **Step 4:** Commit: `git commit -m "fix: log prune failures, add deterministic fallback every 500 ticks"`

---

### Task 5: Add minimum-remaining-today guard to proactive scheduler

**Files:**
- `jacked/web/auto_swap.py` — add `MIN_PROACTIVE_MINUTES` constant
- `jacked/api/usage_monitor.py` — add guard in proactive path

- [ ] **Step 1:** In `jacked/web/auto_swap.py`, after the `URGENCY_HOURS` constant (around line 175), add:

```python
MIN_PROACTIVE_MINUTES = 30  # don't proactively swap if fewer than this many
                             # working minutes remain today — not worth opening
                             # a 5h window for a few minutes of use
```

- [ ] **Step 2:** In `jacked/api/usage_monitor.py`, in the proactive scheduler block, right after `if usage_5h is not None and usage_5h < warning_5h:` and before `accounts = await _fetch_candidate_usage(...)`, add a time-of-day guard:

```python
                    # Don't proactively swap if we're near the end of active hours —
                    # opening a 5h window for a few minutes of use is wasteful.
                    from jacked.web.auto_swap import MIN_PROACTIVE_MINUTES
                    now_local = datetime.now()
                    s_h, s_m = map(int, active_end.split(":"))
                    active_end_today = now_local.replace(hour=s_h, minute=s_m, second=0, microsecond=0)
                    minutes_until_end = (active_end_today - now_local).total_seconds() / 60.0
                    if minutes_until_end < MIN_PROACTIVE_MINUTES:
                        logger.debug(
                            "Proactive: skipping — only %.0f min until active hours end",
                            minutes_until_end,
                        )
                    else:
```

Then indent the entire remaining proactive block (from `accounts = await _fetch_candidate_usage(...)` to the end) under the `else:`.

**Alternative simpler approach** — add a guard that `continue`s early (but records the decision first):

After `if usage_5h is not None and usage_5h < warning_5h:`, add:

```python
                    # Guard: don't swap if near end of active hours today
                    from jacked.web.auto_swap import MIN_PROACTIVE_MINUTES
                    _now_local = datetime.now()
                    _end_h, _end_m = map(int, active_end.split(":"))
                    _active_end_today = _now_local.replace(hour=_end_h, minute=_end_m, second=0, microsecond=0)
                    _minutes_left_today = (_active_end_today - _now_local).total_seconds() / 60.0
                    if 0 < _minutes_left_today < MIN_PROACTIVE_MINUTES:
                        logger.debug(
                            "Proactive: skipping — only %.0f min until active hours end",
                            _minutes_left_today,
                        )
```

Use the simple `if` guard followed by an `else:` that contains the rest of the proactive block. The `0 < _minutes_left_today` guard handles the case where we're already past `active_end` (negative value).

- [ ] **Step 3:** Run tests: `uv run python -m pytest tests/ -q`

- [ ] **Step 4:** Commit: `git commit -m "fix: skip proactive swaps within 30 min of active hours end"`
