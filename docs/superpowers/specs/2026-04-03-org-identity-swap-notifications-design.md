# Org Identity in Swap Notifications

**Date:** 2026-04-03
**Status:** Approved

## Problem

Two `jack.neil@hank.ai` accounts exist — one personal max plan, one Hank.ai team plan. The swap toast, swap history table, and reason strings all show bare email addresses, making it impossible to tell which account was swapped from/to.

## Solution

### 1. `format_account_label` helper (Python + JS)

Pure function in `jacked/web/auto_swap.py`:

```python
def format_account_label(account: dict) -> str:
```

Logic:
1. Start with `email`
2. Append org context in parens: if `organization_name` ends with `'s Organization`, show `(personal)`. Otherwise show the org name, e.g. `(Hank.ai)`.
3. If `display_name` is set AND differs from the Anthropic default pattern (just a first name matching the email prefix or "User"), prepend it with ` — `: `Hank Max — jack.neil@hank.ai (Hank.ai)`.

Output examples with current data:
- `jack.neil@hank.ai (personal)` — max plan, no custom label
- `jack.neil@hank.ai (Hank.ai)` — team plan, no custom label
- `Hank Max — jack.neil@hank.ai (personal)` — if user labels it "Hank Max"

JS equivalent: `formatAccountLabel(account)` in a shared utility or inline where needed.

### 2. WebSocket `auto_swap_triggered` payload

Currently sends:
```json
{
  "from_account_id": 1,
  "to_account_id": 7,
  "to_email": "jack.neil@hank.ai",
  "reason": "..."
}
```

Add `from_label`, `to_label`, and `from_email`:
```json
{
  "from_account_id": 1,
  "to_account_id": 7,
  "from_email": "jack@jackmd.com",
  "to_email": "jack.neil@hank.ai",
  "from_label": "jack@jackmd.com (personal)",
  "to_label": "jack.neil@hank.ai (Hank.ai)",
  "reason": "..."
}
```

Both the defensive swap broadcast and proactive swap broadcast in `usage_monitor.py` need updating.

### 3. Swap toast banner (websocket.js)

Currently: `Auto-swapped to jack.neil@hank.ai — reason`

Change to: `Auto-swapped to jack.neil@hank.ai (Hank.ai) — reason`

Use `to_label` from the WebSocket payload. Fall back to `to_email` if `to_label` is missing (backward compat).

### 4. Swap history table (auto-swap.js)

Currently the from→to column shows: `jack@jackmd.com → jack.neil@hank.ai`

Change to: `jack@jackmd.com (personal) → jack.neil@hank.ai (Hank.ai)`

The swap log API already JOINs to get `from_email` and `to_email`. Extend the JOIN to also return `from_org_name`, `to_org_name`, `from_display_name`, `to_display_name`. The JS `renderSwapLogTable` formats these client-side using a `formatAccountLabel` function.

### 5. Swap log API (`list_swaps`)

Update the SQL JOIN in `database.py` `list_swaps()` to also select `organization_name` and `display_name` for both from/to accounts. Field names: `from_org_name`, `to_org_name`, `from_display_name`, `to_display_name`.

### 6. Reason strings in usage_monitor.py

The proactive swap reason already includes the target email:
```
proactive: burning 15% unused 7d on jack.neil@hank.ai — 12 effective hours left
```

Replace bare email with label:
```
proactive: burning 15% unused 7d on jack.neil@hank.ai (Hank.ai) — 12 effective hours left
```

Use `format_account_label(target)` when building the reason string.

## Files Affected

| File | Change |
|------|--------|
| `jacked/web/auto_swap.py` | Add `format_account_label()` pure function |
| `jacked/api/usage_monitor.py` | Add `from_label`/`to_label` to WebSocket payloads, use label in reason strings |
| `jacked/web/database.py` | Extend `list_swaps()` JOIN to include org_name and display_name |
| `jacked/data/web/js/websocket.js` | Use `to_label` in swap toast banner |
| `jacked/data/web/js/components/auto-swap.js` | Add `formatAccountLabel()`, use in `renderSwapLogTable` |
| `tests/unit/test_auto_swap.py` | Tests for `format_account_label` |
