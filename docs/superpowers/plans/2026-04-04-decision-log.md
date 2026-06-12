# Swap Decision Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a queryable decision log that records every swap check tick (stay/swap/manual), with full detail trace, 7-day retention, filterable API, and an expandable frontend table.

**Architecture:** New `decision_log` table in SQLite with JSON `detail` column. Each poll tick writes one row with the full decision trace. Manual switches via `use_account` also write a row. Frontend shows a filterable table with click-to-expand detail panels. All user-controlled strings are passed through `escapeHtml()` before DOM insertion.

**Tech Stack:** Python, SQLite, JavaScript, pytest

**Spec:** `docs/superpowers/specs/2026-04-04-decision-log-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `jacked/web/database.py` | `decision_log` table, `record_decision()`, `list_decisions()`, `prune_decision_log()` |
| `jacked/api/usage_monitor.py` | Build detail JSON each tick, call `record_decision()`, periodic prune |
| `jacked/api/routes/auth.py` | Call `record_decision()` in `use_account` |
| `jacked/api/routes/settings_swap.py` | `GET /api/settings/decision-log` endpoint |
| `jacked/data/web/js/components/auto-swap.js` | Decision log table with filtering and expandable rows |
| `jacked/data/web/js/components/accounts.js` | Decision log container on accounts page |
| `jacked/data/web/js/app.js` | Load decision log data |

---

### Task 1: Database table + methods

**Files:**
- Modify: `jacked/web/database.py`

- [ ] **Step 1: Add the `decision_log` table to the schema**

Find the `swap_log` CREATE TABLE statement (around line 348). After line 359 (end of swap_log), add:

```python
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    account_id INTEGER,
    action TEXT NOT NULL,
    trigger TEXT,
    target_id INTEGER,
    reason TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_decision_log_timestamp ON decision_log(timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_action ON decision_log(action);
```

- [ ] **Step 2: Add `record_decision` method**

After the `list_swaps` method (around line 2565), add:

```python
    def record_decision(
        self,
        account_id: int | None,
        action: str,
        trigger: str | None = None,
        target_id: int | None = None,
        reason: str | None = None,
        detail: dict | None = None,
    ):
        """Record a swap decision (stay, swap, or manual_switch)."""
        import json as _json
        detail_str = _json.dumps(detail) if detail else None
        with self._writer() as conn:
            conn.execute(
                """INSERT INTO decision_log
                   (account_id, action, trigger, target_id, reason, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, action, trigger, target_id, reason, detail_str),
            )
```

- [ ] **Step 3: Add `list_decisions` method**

```python
    def list_decisions(self, limit: int = 100, actions: list[str] | None = None) -> list[dict]:
        """List recent decision log entries, newest first."""
        import json as _json
        with self._reader() as conn:
            if actions:
                placeholders = ",".join("?" for _ in actions)
                rows = conn.execute(
                    f"""SELECT * FROM decision_log
                        WHERE action IN ({placeholders})
                        ORDER BY timestamp DESC LIMIT ?""",
                    (*actions, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT * FROM decision_log
                       ORDER BY timestamp DESC LIMIT ?""",
                    (limit,),
                ).fetchall()
            result = []
            for r in rows:
                d = dict(r)
                if d.get("detail"):
                    try:
                        d["detail"] = _json.loads(d["detail"])
                    except (ValueError, TypeError):
                        pass
                result.append(d)
            return result
```

- [ ] **Step 4: Add `prune_decision_log` method**

```python
    def prune_decision_log(self, days: int = 7):
        """Delete decision log entries older than the given number of days."""
        with self._writer() as conn:
            conn.execute(
                "DELETE FROM decision_log WHERE timestamp < datetime('now', ?)",
                (f"-{days} days",),
            )
```

- [ ] **Step 5: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add jacked/web/database.py
git commit -m "feat: decision_log table with record, list, and prune methods"
```

---

### Task 2: Record decisions in the poll loop

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Add a `_build_tick_detail` helper function**

Add before `_execute_swap` (around line 133):

```python
def _build_tick_detail(
    active_acct: dict,
    usage_5h: float | None,
    usage_7d: float | None,
    want_swap: bool,
    suppression: dict | None,
    escape_override: bool,
    candidates: list[dict] | None,
    proactive_target_id: int | None,
    cooldown_active: bool,
    decision: str,
) -> dict:
    """Build the detail JSON for a decision log entry."""
    from jacked.web.auto_swap import format_account_label
    detail = {
        "active": {
            "id": active_acct.get("id"),
            "email": active_acct.get("email", ""),
            "label": format_account_label(active_acct),
            "5h": usage_5h,
            "7d": usage_7d,
        },
        "should_swap": want_swap,
        "escape_override": escape_override,
        "cooldown_active": cooldown_active,
        "decision": decision,
    }
    if suppression:
        detail["suppression"] = suppression
    if candidates is not None:
        detail["candidates"] = candidates
    if proactive_target_id is not None:
        detail["proactive_target_id"] = proactive_target_id
    return detail
```

- [ ] **Step 2: Add decision tracking variables at start of tick**

After the settings reads (around line 188, after `active_end = ...`), add:

```python
            _decision_action = "stay"
            _decision_target_id = None
            _decision_reason = None
            _candidate_summaries = None
            _proactive_target_id = None
            _suppression = None
```

- [ ] **Step 3: Capture suppression info after should_swap**

After the `want_swap = should_swap(...)` call, add:

```python
            # Build suppression detail for decision log
            if not want_swap:
                if usage_5h is not None and usage_5h >= effective_critical:
                    _suppression = {"type": "5h_reset_imminent"}
                elif usage_7d is not None and usage_7d >= threshold_7d:
                    _suppression = {"type": "deficit", "usage_7d": usage_7d}
```

- [ ] **Step 4: Set decision trackers on swap execution**

After `await _execute_swap(...)` in the defensive path, add:

```python
                    _decision_action = "swap"
                    _decision_target_id = target["id"]
                    _decision_reason = reason
```

After `await _execute_swap(...)` in the proactive path, add:

```python
                                    _decision_action = "swap"
                                    _decision_target_id = target["id"]
                                    _decision_reason = reason
                                    _proactive_target_id = target["id"]
```

- [ ] **Step 5: Capture candidate summaries in proactive scan**

After `best_deficit_result = None` in the proactive block, add:

```python
                    _candidate_summaries = []
```

Inside the candidate loop, after computing `urgency` (regardless of whether this candidate is the best), add to the summary list. Find the line `if urgency > best_urgency:` and BEFORE it add:

```python
                        _candidate_summaries.append({
                            "id": acct["id"],
                            "email": acct.get("email", ""),
                            "label": format_account_label(acct),
                            "5h": acct.get("cached_usage_5h"),
                            "7d": acct.get("cached_usage_7d"),
                            "deficit": round(dr["deficit"], 1),
                            "windows_remaining": round(dr["effective_windows_remaining"], 1),
                            "urgency_tier": (
                                "CRITICAL" if dr["effective_windows_remaining"] < 1 else
                                "HIGH" if dr["effective_windows_remaining"] < 3 else
                                "MEDIUM" if dr["effective_windows_remaining"] < 5 else
                                "NORMAL"
                            ),
                            "threshold": round(threshold, 1),
                            "passes": dr["deficit"] > threshold,
                            "urgency_score": round(urgency, 2),
                        })
```

- [ ] **Step 6: Record decision at end of tick**

Just before `except asyncio.CancelledError:`, add:

```python
            # Record decision in the log
            if active_acct is not None:
                try:
                    _tick_detail = _build_tick_detail(
                        active_acct=active_acct,
                        usage_5h=usage_5h,
                        usage_7d=usage_7d,
                        want_swap=want_swap,
                        suppression=_suppression,
                        escape_override=escape_override if 'escape_override' in dir() else False,
                        candidates=_candidate_summaries,
                        proactive_target_id=_proactive_target_id,
                        cooldown_active=(time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS,
                        decision=_decision_action,
                    )
                    db.record_decision(
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger=(
                            ("proactive_7d" if _proactive_target_id else "auto_swap")
                            if _decision_action == "swap"
                            else "tick"
                        ),
                        target_id=_decision_target_id,
                        reason=_decision_reason or "no trigger",
                        detail=_tick_detail,
                    )
                except Exception:
                    logger.debug("Failed to record decision", exc_info=True)

            # Periodic prune (~1% of ticks)
            if random.random() < 0.01:
                try:
                    db.prune_decision_log()
                except Exception:
                    pass
```

- [ ] **Step 7: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 8: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: record decision log on every poll tick with full trace"
```

---

### Task 3: Record manual switches

**Files:**
- Modify: `jacked/api/routes/auth.py`

- [ ] **Step 1: Add decision log recording to `use_account`**

In `use_account`, after `sync_credential_to_all_stores(...)` (around line 868) and before `return UseAccountResponse(...)`, add:

```python
    try:
        db.record_decision(
            account_id=account_id,
            action="manual_switch",
            trigger="manual",
            target_id=account_id,
            reason="user selected via dashboard",
            detail={"source": "dashboard", "previous_account_id": outgoing_id},
        )
    except Exception:
        pass
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/api/routes/auth.py
git commit -m "feat: record manual account switches in decision log"
```

---

### Task 4: API endpoint

**Files:**
- Modify: `jacked/api/routes/settings_swap.py`

- [ ] **Step 1: Add the GET endpoint**

After the `get_swap_log` endpoint (around line 166), add:

```python
@router.get("/decision-log")
async def get_decision_log(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    action: list[str] = Query(default=[]),
):
    """Get recent decision log entries with optional action filter."""
    db = _get_db(request)
    if db is None:
        return []
    actions = action if action else None
    return db.list_decisions(limit=limit, actions=actions)
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ -q`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/api/routes/settings_swap.py
git commit -m "feat: GET /api/settings/decision-log endpoint"
```

---

### Task 5: Frontend decision log table

**Files:**
- Modify: `jacked/data/web/js/components/auto-swap.js`
- Modify: `jacked/data/web/js/components/accounts.js`
- Modify: `jacked/data/web/js/app.js`

- [ ] **Step 1: Add data loading function in auto-swap.js**

After `loadSwapLog` (around line 28), add:

```javascript
async function loadDecisionLog(showAll) {
    try {
        const params = showAll ? '?limit=200' : '?limit=100&action=swap&action=manual_switch';
        const data = await api.get('/api/settings/decision-log' + params);
        return Array.isArray(data) ? data : [];
    } catch (e) {
        console.error('Failed to load decision log:', e);
        return [];
    }
}
```

- [ ] **Step 2: Add the decision log table renderer**

After `renderSwapLogTable`, add the `renderDecisionLogTable` function and the `renderDecisionLog` + `toggleDecisionLogFilter` functions. All user-controlled strings (email, label, reason) MUST go through `escapeHtml()`. The detail panel uses `escapeHtml()` for all text content. The expandable rows use a click handler that toggles a hidden class on the detail row.

The table has 4 columns: Time, Action (badge), Account, Reason. Action badges: swap=teal, manual=blue, check=slate. Click a row to expand detail panel showing: active account state, suppression info, candidate table (if proactive evaluated), and decision flags.

This is a large JS function. Implement it following the same pattern as `renderSwapLogTable` — build HTML strings with `escapeHtml()` on all interpolated values, use template literals for structure.

- [ ] **Step 3: Add the container to accounts page**

In `jacked/data/web/js/components/accounts.js`, find the swap history section (around line 422). After its closing `</div>`, add:

```javascript
'<div class="mt-6 bg-slate-800 border border-slate-700 rounded-lg p-4">' +
'<h3 class="text-sm font-medium text-slate-300 mb-3">Decision Log</h3>' +
'<div id="decision-log-container" data-show-all="false">' +
'<div class="text-xs text-slate-500">Loading...</div>' +
'</div></div>'
```

- [ ] **Step 4: Load decision log data on page init**

In `jacked/data/web/js/app.js`, after where swap log data is loaded, add:

```javascript
if (typeof renderDecisionLog === 'function') renderDecisionLog('decision-log-container');
```

- [ ] **Step 5: Commit**

```bash
git add jacked/data/web/js/components/auto-swap.js jacked/data/web/js/components/accounts.js jacked/data/web/js/app.js
git commit -m "feat: decision log frontend with filtering and expandable detail rows"
```
