# Org Identity in Swap Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make swap notifications, swap history, and reason strings show which org an account belongs to so duplicate emails (e.g. two `jack.neil@hank.ai` accounts) are distinguishable.

**Architecture:** One Python helper `format_account_label(account)` in `auto_swap.py` produces a human-readable label like `jack.neil@hank.ai (Hank.ai)`. A JS equivalent `formatAccountLabel(entry, prefix)` does the same client-side for the swap history table. The WebSocket payloads get `from_label`/`to_label` fields, and the `list_swaps` DB query gets extended to JOIN org/display_name data.

**Tech Stack:** Python, JavaScript, SQLite

**Spec:** `docs/superpowers/specs/2026-04-03-org-identity-swap-notifications-design.md`

---

## File Structure

| File | Responsibility |
|------|---------------|
| `jacked/web/auto_swap.py` | Add `format_account_label()` pure function |
| `jacked/api/usage_monitor.py` | Add `from_label`/`to_label` to both WebSocket broadcast payloads, use label in proactive reason string |
| `jacked/web/database.py` | Extend `list_swaps()` JOIN to include org_name and display_name for both accounts |
| `jacked/data/web/js/websocket.js` | Use `to_label` in swap toast banner |
| `jacked/data/web/js/components/auto-swap.js` | Add `formatAccountLabel()`, use in `renderSwapLogTable` |
| `tests/unit/test_auto_swap.py` | Tests for `format_account_label` |

---

### Task 1: Add `format_account_label` Python helper + tests

**Files:**
- Modify: `jacked/web/auto_swap.py`
- Test: `tests/unit/test_auto_swap.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/unit/test_auto_swap.py`, after the imports at the top add `format_account_label` to the import list:

```python
from jacked.web.auto_swap import (
    BurnRate,
    _resets_within,
    compute_7d_deficit,
    compute_effective_working_hours,
    format_account_label,
    pick_best_target,
    score_candidate,
    should_swap,
    tier_critical_threshold,
    update_burn_rate,
)
```

Then add at the end of the file:

```python
# ---------------------------------------------------------------------------
# format_account_label
# ---------------------------------------------------------------------------

class TestFormatAccountLabel:
    def test_personal_org(self):
        """Personal org (ends with 's Organization') shows as (personal)."""
        acct = {"email": "jack@jackmd.com", "organization_name": "jack@jackmd.com's Organization", "display_name": "Jack"}
        assert format_account_label(acct) == "jack@jackmd.com (personal)"

    def test_real_org(self):
        """Real org name is shown in parens."""
        acct = {"email": "jack.neil@hank.ai", "organization_name": "Hank.ai", "display_name": "Jack"}
        assert format_account_label(acct) == "jack.neil@hank.ai (Hank.ai)"

    def test_custom_label_prepended(self):
        """User-set display_name that differs from default is prepended."""
        acct = {"email": "jack.neil@hank.ai", "organization_name": "Hank.ai", "display_name": "Hank Team"}
        assert format_account_label(acct) == "Hank Team — jack.neil@hank.ai (Hank.ai)"

    def test_default_display_name_not_shown(self):
        """Default display_name (just first name) is NOT prepended."""
        acct = {"email": "jack.neil@hank.ai", "organization_name": "Hank.ai", "display_name": "Jack"}
        result = format_account_label(acct)
        assert not result.startswith("Jack —")
        assert result == "jack.neil@hank.ai (Hank.ai)"

    def test_no_org_name(self):
        """Missing org_name shows just email."""
        acct = {"email": "user@test.com", "organization_name": None, "display_name": None}
        assert format_account_label(acct) == "user@test.com"

    def test_empty_org_name(self):
        """Empty string org_name shows just email."""
        acct = {"email": "user@test.com", "organization_name": "", "display_name": None}
        assert format_account_label(acct) == "user@test.com"

    def test_no_display_name(self):
        """None display_name is fine — just email + org."""
        acct = {"email": "jack@test.com", "organization_name": "Acme Corp", "display_name": None}
        assert format_account_label(acct) == "jack@test.com (Acme Corp)"

    def test_display_name_empty_string(self):
        """Empty string display_name is treated as no label."""
        acct = {"email": "jack@test.com", "organization_name": "Acme Corp", "display_name": ""}
        assert format_account_label(acct) == "jack@test.com (Acme Corp)"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py::TestFormatAccountLabel -v`
Expected: FAIL — `format_account_label` not found in imports

- [ ] **Step 3: Implement `format_account_label`**

In `jacked/web/auto_swap.py`, add after the `tier_label` function (around line 35):

```python
def format_account_label(account: dict) -> str:
    """Human-readable account label for swap notifications and logs.

    Format: [Label — ] email [(org)]
    - Personal orgs (ending "'s Organization") show as "(personal)"
    - Real org names shown as-is: "(Hank.ai)"
    - Custom display_name prepended only if it differs from the default
      (default = first name matching email prefix, or generic names)
    """
    email = account.get("email") or "unknown"
    org_name = account.get("organization_name") or ""
    display_name = (account.get("display_name") or "").strip()

    # Build org suffix
    org_suffix = ""
    if org_name:
        if org_name.endswith("'s Organization"):
            org_suffix = " (personal)"
        else:
            org_suffix = f" ({org_name})"

    # Check if display_name is custom (not the Anthropic default)
    label_prefix = ""
    if display_name:
        # Default display_name is typically the first name from the email
        # e.g. "Jack" for jack.neil@hank.ai. Don't show these.
        email_prefix = email.split("@")[0].split(".")[0].lower()
        if display_name.lower() != email_prefix and display_name.lower() != "user":
            label_prefix = f"{display_name} — "

    return f"{label_prefix}{email}{org_suffix}"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "feat: add format_account_label helper for org-aware display"
```

---

### Task 2: Add labels to WebSocket payloads and reason strings

**Files:**
- Modify: `jacked/api/usage_monitor.py`

- [ ] **Step 1: Add import**

In the late imports block inside `active_account_poll_loop` (around line 197, where `from jacked.web.auto_swap import` is), add `format_account_label` to the import list:

```python
            from jacked.web.auto_swap import (
                should_swap,
                pick_best_target,
                update_burn_rate,
                tier_critical_threshold,
                tier_label as _tier_label,
                score_candidate,
                _resets_within,
                RESET_SUPPRESS_MINUTES,
                SUPPRESS_OVERRIDE_SCORE,
                format_account_label,
            )
```

- [ ] **Step 2: Update defensive swap WebSocket broadcast**

Find the defensive swap broadcast (around line 446-455):

```python
                    if ws_registry:
                        await ws_registry.broadcast(
                            "auto_swap_triggered",
                            {
                                "from_account_id": active_acct_id,
                                "to_account_id": target["id"],
                                "to_email": target.get("email", ""),
                                "reason": reason,
                            },
                        )
```

Replace with:

```python
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
```

- [ ] **Step 3: Update proactive swap reason string**

Find the proactive reason string (around line 527):

```python
                                    f"unused 7d on {target.get('email', '?')} — "
```

Replace with:

```python
                                    f"unused 7d on {format_account_label(target)} — "
```

- [ ] **Step 4: Update proactive swap WebSocket broadcast**

Find the proactive swap broadcast (around line 569-577):

```python
                                if ws_registry:
                                    await ws_registry.broadcast(
                                        "auto_swap_triggered",
                                        {
                                            "from_account_id": active_acct_id,
                                            "to_account_id": target["id"],
                                            "to_email": target.get("email", ""),
                                            "reason": reason,
                                        },
                                    )
```

Replace with:

```python
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
```

- [ ] **Step 5: Run tests**

Run: `uv run python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add jacked/api/usage_monitor.py
git commit -m "feat: add org labels to swap WebSocket payloads and reason strings"
```

---

### Task 3: Extend `list_swaps` to include org and display_name

**Files:**
- Modify: `jacked/web/database.py`

- [ ] **Step 1: Update the `list_swaps` SQL query**

In `jacked/web/database.py`, find the `list_swaps` method (around line 2550). Replace the query:

```python
    def list_swaps(self, limit=50):
        """List recent swap events with account emails."""
        with self._reader() as conn:
            rows = conn.execute(
                """SELECT s.*,
                          fa.email AS from_email,
                          ta.email AS to_email
                   FROM swap_log s
                   LEFT JOIN accounts fa ON fa.id = s.from_account_id
                   LEFT JOIN accounts ta ON ta.id = s.to_account_id
                   ORDER BY s.timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
```

With:

```python
    def list_swaps(self, limit=50):
        """List recent swap events with account emails and org info."""
        with self._reader() as conn:
            rows = conn.execute(
                """SELECT s.*,
                          fa.email AS from_email,
                          fa.organization_name AS from_org_name,
                          fa.display_name AS from_display_name,
                          ta.email AS to_email,
                          ta.organization_name AS to_org_name,
                          ta.display_name AS to_display_name
                   FROM swap_log s
                   LEFT JOIN accounts fa ON fa.id = s.from_account_id
                   LEFT JOIN accounts ta ON ta.id = s.to_account_id
                   ORDER BY s.timestamp DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ -v`
Expected: ALL PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/web/database.py
git commit -m "feat: extend list_swaps JOIN to include org_name and display_name"
```

---

### Task 4: Update frontend swap toast and swap history table

**Files:**
- Modify: `jacked/data/web/js/websocket.js`
- Modify: `jacked/data/web/js/components/auto-swap.js`

- [ ] **Step 1: Update swap toast banner in websocket.js**

In `jacked/data/web/js/websocket.js`, find the `auto_swap_triggered` handler (around line 404). Change:

```javascript
    const toEmail = d.to_email || 'another account';
```

To:

```javascript
    const toLabel = d.to_label || d.to_email || 'another account';
```

And change:

```javascript
    text.textContent = 'Auto-swapped to ' + toEmail + (reason ? ' \u2014 ' + reason : '');
```

To:

```javascript
    text.textContent = 'Auto-swapped to ' + toLabel + (reason ? ' \u2014 ' + reason : '');
```

- [ ] **Step 2: Add `formatAccountLabel` JS helper in auto-swap.js**

In `jacked/data/web/js/components/auto-swap.js`, add before the `renderSwapLogTable` function (around line 200):

```javascript
/**
 * Format an account label from swap log entry fields.
 * @param {Object} entry - swap log entry
 * @param {string} prefix - 'from' or 'to'
 * @returns {string} formatted label like "jack@test.com (Hank.ai)"
 */
function formatAccountLabel(entry, prefix) {
    const email = entry[prefix + '_email'] || '\u2014';
    const orgName = entry[prefix + '_org_name'] || '';
    const displayName = (entry[prefix + '_display_name'] || '').trim();

    let orgSuffix = '';
    if (orgName) {
        if (orgName.endsWith("'s Organization")) {
            orgSuffix = ' (personal)';
        } else {
            orgSuffix = ' (' + orgName + ')';
        }
    }

    let labelPrefix = '';
    if (displayName) {
        const emailPrefix = email.split('@')[0].split('.')[0].toLowerCase();
        if (displayName.toLowerCase() !== emailPrefix && displayName.toLowerCase() !== 'user') {
            labelPrefix = displayName + ' \u2014 ';
        }
    }

    return labelPrefix + email + orgSuffix;
}
```

- [ ] **Step 3: Update `renderSwapLogTable` to use labels**

In `renderSwapLogTable` (around line 206 after the new helper), change:

```javascript
        const from = escapeHtml(e.from_email || '\u2014');
        const to = escapeHtml(e.to_email || '\u2014');
```

To:

```javascript
        const from = escapeHtml(formatAccountLabel(e, 'from'));
        const to = escapeHtml(formatAccountLabel(e, 'to'));
```

- [ ] **Step 4: Manual test**

Open the dashboard in a browser, verify:
1. The swap history table shows org info (e.g. `jack.neil@hank.ai (Hank.ai)`)
2. If a swap fires, the toast banner shows the org-aware label

- [ ] **Step 5: Commit**

```bash
git add jacked/data/web/js/websocket.js jacked/data/web/js/components/auto-swap.js
git commit -m "feat: show org identity in swap toast and swap history table"
```
