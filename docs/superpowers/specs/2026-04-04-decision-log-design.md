# Swap Decision Log

**Date:** 2026-04-04
**Status:** Approved

## Problem

The system only records when swaps HAPPEN (swap_log table). There is no record of:
- Why the system decided NOT to swap on a given tick
- What candidates were evaluated and their scores
- What thresholds were applied and whether they passed
- Manual account switches via the dashboard
- The full decision trace needed to debug "why didn't it swap to Account X?"

## Solution

### 1. `decision_log` table

```sql
CREATE TABLE IF NOT EXISTS decision_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    account_id INTEGER,
    action TEXT NOT NULL,          -- 'stay', 'swap', 'manual_switch'
    trigger TEXT,                  -- 'tick', 'manual', 'proactive_7d', 'auto_swap'
    target_id INTEGER,             -- NULL when action = 'stay'
    reason TEXT,                   -- human-readable summary
    detail TEXT                    -- JSON blob with full decision trace
);
```

### 2. Detail JSON structure

**Tick (stay or swap):**
```json
{
  "active": {"id": 5, "email": "...", "label": "...", "5h": 30.0, "7d": 84.0},
  "should_swap": false,
  "suppression": {"type": "deficit", "deficit": 8.1},
  "escape_override": false,
  "candidates_fetched": true,
  "candidates": [
    {"id": 1, "email": "...", "label": "...", "5h": 0.0, "7d": 86.0,
     "score": 50.0, "deficit": 7.6, "windows_remaining": 1.1,
     "urgency_tier": "HIGH", "threshold": 4.2, "passes_urgency": true,
     "urgency_score": 0.84}
  ],
  "proactive_target_id": 1,
  "cooldown_active": false,
  "decision": "swap"
}
```

**Manual switch:**
```json
{"source": "dashboard", "previous_account_id": 5}
```

### 3. Retention

7-day auto-prune. On each tick, `DELETE FROM decision_log WHERE timestamp < datetime('now', '-7 days')`. At ~1500 entries/day, that's ~10500 rows max.

### 4. Database methods

- `record_decision(account_id, action, trigger, target_id, reason, detail)` — insert row
- `list_decisions(limit=100, actions=None)` — query with optional action filter, newest-first
- `prune_decision_log()` — delete rows older than 7 days

### 5. API

`GET /api/settings/decision-log?limit=100&action=swap&action=manual_switch`

- `limit` — max rows (default 100, max 1000)
- `action` — filter by action type, repeatable query param (default: all)
- Returns rows newest-first with `detail` as parsed JSON

### 6. Recording points

| Location | Action | Trigger | Detail |
|---|---|---|---|
| `usage_monitor.py` — `_execute_swap` | `swap` | `auto_swap` or `proactive_7d` | Full tick trace |
| `usage_monitor.py` — end of tick (no swap) | `stay` | `tick` | Full tick trace |
| `routes/auth.py` — `use_account` | `manual_switch` | `manual` | Previous account ID |

### 7. Frontend

Decision Log section on accounts page. Table:

| Time | Account | Action | Reason |
|---|---|---|---|

- Color-coded: swap = teal, manual = blue, stay = dimmed
- Default filter: swaps + manual only (hide frequent "stay" ticks)
- Toggle: "Show all checks" reveals every tick
- Click row → expandable detail panel showing formatted JSON trace

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/database.py` | New table, `record_decision()`, `list_decisions()`, `prune_decision_log()` |
| `jacked/api/usage_monitor.py` | Build detail JSON each tick, call `record_decision()` for both stay and swap outcomes, call `prune_decision_log()` periodically |
| `jacked/api/routes/auth.py` | Call `record_decision()` in `use_account` for manual switches |
| `jacked/api/routes/settings_swap.py` | New `GET /api/settings/decision-log` endpoint |
| `jacked/data/web/js/components/auto-swap.js` | Decision log table UI with filtering and expandable rows |
| `jacked/data/web/js/app.js` | Load decision log data |
