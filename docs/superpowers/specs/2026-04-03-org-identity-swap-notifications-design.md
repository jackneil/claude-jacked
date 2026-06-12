# Org Identity in Swap Notifications

**Date:** 2026-04-03
**Status:** Approved

## Problem

Two `user1@example.com` accounts exist — one personal max plan, one Acme team plan. The swap toast, swap history table, and reason strings all show bare email addresses, making it impossible to tell which account was swapped from/to.

## Solution

### 1. `format_account_label` helper (Python + JS)

Pure function in `jacked/web/auto_swap.py`:

```python
def format_account_label(account: dict) -> str:
```

Logic:
1. Start with `email`
2. Append org context in parens: if `organization_name` ends with `'s Organization`, show `(personal)`. Otherwise show the org name, e.g. `(Acme)`.
3. If `display_name` is set AND differs from the Anthropic default pattern (just a first name matching the email prefix or "User"), prepend it with ` — `: `Hank Max — user1@example.com (Acme)`.

Output examples with current data:
- `user1@example.com (personal)` — max plan, no custom label
- `user1@example.com (Acme)` — team plan, no custom label
- `Hank Max — user1@example.com (personal)` — if user labels it "Hank Max"

JS equivalent: `formatAccountLabel(account)` in a shared utility or inline where needed.

### 2. WebSocket `auto_swap_triggered` payload

Currently sends:
```json
{
  "from_account_id": 1,
  "to_account_id": 7,
  "to_email": "user1@example.com",
  "reason": "..."
}
```

Add `from_label`, `to_label`, and `from_email`:
```json
{
  "from_account_id": 1,
  "to_account_id": 7,
  "from_email": "user3@example.com",
  "to_email": "user1@example.com",
  "from_label": "user3@example.com (personal)",
  "to_label": "user1@example.com (Acme)",
  "reason": "..."
}
```

Both the defensive swap broadcast and proactive swap broadcast in `usage_monitor.py` need updating.

### 3. Swap toast banner (websocket.js)

Currently: `Auto-swapped to user1@example.com — reason`

Change to: `Auto-swapped to user1@example.com (Acme) — reason`

Use `to_label` from the WebSocket payload. Fall back to `to_email` if `to_label` is missing (backward compat).

### 4. Swap history table (auto-swap.js)

Currently the from→to column shows: `user3@example.com → user1@example.com`

Change to: `user3@example.com (personal) → user1@example.com (Acme)`

The swap log API already JOINs to get `from_email` and `to_email`. Extend the JOIN to also return `from_org_name`, `to_org_name`, `from_display_name`, `to_display_name`. The JS `renderSwapLogTable` formats these client-side using a `formatAccountLabel` function.

### 5. Swap log API (`list_swaps`)

Update the SQL JOIN in `database.py` `list_swaps()` to also select `organization_name` and `display_name` for both from/to accounts. Field names: `from_org_name`, `to_org_name`, `from_display_name`, `to_display_name`.

### 6. Reason strings in usage_monitor.py

The proactive swap reason already includes the target email:
```
proactive: burning 15% unused 7d on user1@example.com — 12 effective hours left
```

Replace bare email with label:
```
proactive: burning 15% unused 7d on user1@example.com (Acme) — 12 effective hours left
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
