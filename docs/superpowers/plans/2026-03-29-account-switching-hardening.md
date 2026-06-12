# Account Switching Hardening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Harden the dashboard account-switching feature with missing validation, observability logging, and documentation to prevent regressions.

**Architecture:** Three small, independent changes: (1) add `is_deleted` guard in the `use_account` endpoint, (2) add logging + a safety comment explaining why `sync_credential_to_all_stores` is safe here but not in background loops, (3) update the design doc to reflect that dashboard switching is restored.

**Tech Stack:** Python 3.12+ (FastAPI), Markdown

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/api/routes/auth.py` | Auth API endpoints | Add `is_deleted` check, add safety comment, add debug log in bulk refresh |
| `tests/unit/test_use_account.py` | Endpoint tests | Add soft-deleted account test |
| `docs/superpowers/specs/2026-03-24-kill-background-credential-writes-design.md` | Design spec | Add note that dashboard switching is restored |

---

### Task 1: Add `is_deleted` guard and safety comment to `use_account`

**Files:**
- Modify: `jacked/api/routes/auth.py:779-832`
- Modify: `tests/unit/test_use_account.py`

- [ ] **Step 1: Add a soft-deleted account to the test fixture and write the failing test**

In `tests/unit/test_use_account.py`, add a fifth account to the `db` fixture (after account 4's INSERT, around line 65):

```python
        # Account 5: soft-deleted
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (5, 'eve@test.com', 'at_5', 'rt_5', 1900000000,
                       1, 1, 'valid', 'max', 't1',
                       'cc_at_5', 'cc_rt_5', 1900000000,
                       NULL, 0, NULL)"""
        )
```

Then add the test (after `test_use_account_invalid_status`):

```python
def test_use_account_deleted(client):
    """Returns 404 for soft-deleted account."""
    resp = client.post("/api/auth/accounts/5/use")
    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_use_account.py::test_use_account_deleted -v`
Expected: FAIL — returns 200 (no `is_deleted` check)

- [ ] **Step 3: Add `is_deleted` check and safety comment**

In `jacked/api/routes/auth.py`, after line 781 (`return _not_found(...)`) add the deleted check. Also add the safety comment before the `sync_credential_to_all_stores` call. The full replacement for lines 779-832:

```python
    account = db.get_account(account_id)
    if not account:
        return _not_found(f"No account with id={account_id}")

    # Defense-in-depth: get_account() already filters is_deleted=0,
    # but guard here in case that query changes in the future.
    if account.get("is_deleted"):
        return _not_found(f"No account with id={account_id}")

    if not account.get("is_active"):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Account is disabled — enable it first",
                    "code": "ACCOUNT_DISABLED",
                }
            },
        )

    if account.get("validation_status") == "invalid":
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": "Account has invalid credentials — re-auth first",
                    "code": "ACCOUNT_INVALID",
                }
            },
        )

    if not account.get("cc_access_token"):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": {
                    "message": (
                        "Account has no CC tokens — authorize Claude Code "
                        "tokens first (credentials without a refresh token "
                        "would expire in ~8 hours with no way to renew)"
                    ),
                    "code": "CC_TOKEN_MISSING",
                }
            },
        )

    from jacked.api.credential_helpers import sync_credential_to_all_stores

    # SAFETY: This is a user-initiated, one-shot credential write — NOT a
    # background loop.  The design spec (2026-03-24-kill-background-credential-
    # writes-design.md) prohibits credential file writes from background loops
    # (_token_refresh_loop, _heal_sweep_loop) because they caused session
    # logouts.  This endpoint is safe because: (1) it is user-initiated,
    # (2) it runs once per click, and (3) Claude Code v2.1.81+ handles
    # credential file changes gracefully.  Do NOT copy this pattern into
    # refresh_account_token() or any background loop.
    sync_credential_to_all_stores(
        account_id,
        account,
        email=account.get("email"),
        display_name=account.get("display_name"),
    )

    return UseAccountResponse(
        status="active",
        email=account.get("email", ""),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_use_account.py -v`
Expected: 10 passed (9 existing + 1 new)

- [ ] **Step 5: Commit**

```bash
git add jacked/api/routes/auth.py tests/unit/test_use_account.py
git commit -m "harden: add is_deleted guard and safety comment to use_account

Defense-in-depth: reject soft-deleted accounts with 404 (get_account()
already filters these, but guard here in case that changes). Add a
prominent comment explaining why sync_credential_to_all_stores is safe
here but must never be added to background loops (per the 2026-03-24
design spec)."
```

---

### Task 2: Add debug logging for silent fresh-token degradation

**Files:**
- Modify: `jacked/api/routes/auth.py:608-620` (refresh_all_usage)

When `active_acct_id` resolves to None (e.g., Claude Code overwrote the credential file without the `_jackedAccountId` stamp), the bulk refresh silently degrades to stale DB tokens. Add a debug log so this is visible.

- [ ] **Step 1: Add logging after the active_acct_id read**

In `jacked/api/routes/auth.py`, after the entire try/except block (after line 620), at the same indentation level as the `try:` statement (NOT inside the except block), add:

```python
        if active_acct_id is not None:
            logger.debug(
                "Bulk refresh: active account from credential file = %s",
                active_acct_id,
            )
        else:
            logger.debug(
                "Bulk refresh: no _jackedAccountId in credential file — "
                "all accounts will use DB tokens"
            )
```

- [ ] **Step 2: Verify no test regressions**

Run: `uv run python -m pytest tests/unit/test_use_account.py tests/unit/test_usage_refresh.py -v`
Expected: All pass

- [ ] **Step 3: Commit**

```bash
git add jacked/api/routes/auth.py
git commit -m "fix: add debug logging for fresh-token degradation in bulk refresh

Logs when _jackedAccountId is missing from the credential file,
making silent degradation to stale DB tokens visible in debug logs."
```

---

### Task 3: Update design doc

**Files:**
- Modify: `docs/superpowers/specs/2026-03-24-kill-background-credential-writes-design.md:26`

- [ ] **Step 1: Update the design doc**

Replace line 26:

```
Account switching is exclusively handled by `jacked claude <id>` which uses per-account `CLAUDE_CONFIG_DIR` directories and never touches the global credential file.
```

With:

```
Account switching has two modes: (1) `jacked claude <id>` uses per-account `CLAUDE_CONFIG_DIR` directories for session isolation, and (2) the dashboard "Use Account" button (`/accounts/{id}/use` endpoint) writes to the global credential stores for all non-isolated sessions. The dashboard endpoint is a user-initiated one-shot write, not a background loop — the read-only invariant for background processes is preserved.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-03-24-kill-background-credential-writes-design.md
git commit -m "docs: update design spec to reflect restored dashboard switching

The dashboard 'Use Account' button is restored as a user-initiated
one-shot credential write. Background loops remain DB-only."
```
