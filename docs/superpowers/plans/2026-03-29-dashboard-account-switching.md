# Dashboard Account Switching Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restore the dashboard "Use Account" button so clicking it writes that account's credentials to all stores (global `.credentials.json`, macOS Keychain, `~/.claude.json`) and all running Claude Code sessions seamlessly switch to that account.

**Architecture:** The `/accounts/{id}/use` endpoint (currently 410 Gone) is resurrected to call `sync_credential_to_all_stores()`. Claude Code v2.1.81+ dynamically re-reads credentials, so running sessions pick up the change without logging out. A new `read_fresh_active_token()` helper reads the current access token from the credential file/keychain so usage checks use the token Claude Code has been refreshing (the DB copy goes stale within minutes). The frontend adds a "Use Account" button on each card that calls the endpoint and updates the active-account indicator.

**Tech Stack:** Python 3.12+ (FastAPI, httpx, Pydantic v2), vanilla JS (no build step), macOS Keychain (`security` CLI), SQLite

### Known Limitations

**Dashboard switch vs `jacked claude <id>` interaction:** Both mechanisms write to the global credential stores. Dashboard switching targets all non-isolated sessions. `jacked claude <id>` launches use per-account `CLAUDE_CONFIG_DIR` isolation and are unaffected by global store changes after launch. However, `jacked claude <id>` also writes to the global stores at launch time (one-shot), which stomps the dashboard-activated account. This is the same behavior that existed before this feature — the dashboard button doesn't worsen it. Users should understand: dashboard switching controls the "global default" account; per-account launches are isolated.

**Cache bypass when passing fresh token:** The `fetch_usage()` function's cache guard is bypassed when an `access_token` parameter is provided (line `auth.py:339`). To avoid always hitting the Anthropic API for the active account, the plan only passes the fresh token when it differs from the DB token. When tokens match (CC hasn't refreshed yet), the cache guard works normally.

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/api/credential_helpers.py` | Unified credential read/write | Add `read_fresh_active_token()` |
| `jacked/api/routes/auth.py` | Auth API endpoints | Resurrect `use_account`, update `refresh_usage`/`refresh_all_usage` to pass fresh token |
| `jacked/data/web/js/components/accounts.js` | Account card rendering | Add "Use Account" button |
| `jacked/data/web/js/components/account-actions.js` | Event handlers | Wire up click handler |
| `tests/unit/test_credential_sync.py` | Credential helper tests | Add `read_fresh_active_token` tests |
| `tests/unit/test_use_account.py` | New: endpoint tests | Test use_account endpoint |

---

### Task 1: Add `read_fresh_active_token()` to credential helpers

**Files:**
- Modify: `jacked/api/credential_helpers.py`
- Test: `tests/unit/test_credential_sync.py`

This helper reads the current access token from the credential stores (Keychain first on macOS, then `.credentials.json`). After an account is activated, Claude Code refreshes the access token on its own schedule — so the DB copy goes stale. This helper provides the fresh token for usage/profile API calls.

- [ ] **Step 1: Write failing tests for `read_fresh_active_token`**

Add to `tests/unit/test_credential_sync.py`:

```python
# ------------------------------------------------------------------
# read_fresh_active_token
# ------------------------------------------------------------------


def test_read_fresh_active_token_from_file():
    """Reads access token from .credentials.json for matching account."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(json.dumps({
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "fresh_token_from_file"},
        }))

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result == "fresh_token_from_file"


def test_read_fresh_active_token_from_keychain():
    """Prefers keychain over file when both have tokens."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        # File has a different token
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(json.dumps({
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "file_token"},
        }))

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value={
                    "_jackedAccountId": 1,
                    "claudeAiOauth": {"accessToken": "keychain_token"},
                },
            ),
        ):
            result = read_fresh_active_token(1)

    assert result == "keychain_token"


def test_read_fresh_active_token_wrong_account():
    """Returns None when credential stores belong to a different account."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()
        cred_path = cred_dir / ".credentials.json"
        cred_path.write_text(json.dumps({
            "_jackedAccountId": 2,
            "claudeAiOauth": {"accessToken": "other_account_token"},
        }))

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result is None


def test_read_fresh_active_token_no_credentials():
    """Returns None when no credential file or keychain entry exists."""
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=_WIN) as tmp:
        tmp_path = Path(tmp)
        cred_dir = tmp_path / ".claude"
        cred_dir.mkdir()

        with (
            mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
            mock.patch(
                "jacked.api.credential_helpers.read_platform_credentials",
                return_value=None,
            ),
        ):
            result = read_fresh_active_token(1)

    assert result is None
```

Also add `read_fresh_active_token` to the import at the top of the test file:

```python
from jacked.api.credential_helpers import (
    build_oauth_data,
    read_fresh_active_token,
    read_platform_credentials,
    sync_credential_to_all_stores,
    update_claude_config_email,
    write_platform_credentials,
)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py -k "read_fresh_active_token" -v`
Expected: FAIL — `ImportError: cannot import name 'read_fresh_active_token'`

- [ ] **Step 3: Implement `read_fresh_active_token()`**

Add to `jacked/api/credential_helpers.py` after the `read_platform_credentials()` function (after line 162):

```python
def read_fresh_active_token(account_id: int) -> str | None:
    """Read the current access token from credential stores for an account.

    After activation, Claude Code refreshes the access token on its own
    schedule, making the DB copy stale.  This reads the live token from
    the same stores Claude Code uses (Keychain first, then file).

    Returns the access token string, or None if the stores don't belong
    to this account or are unreadable.
    """
    # Try keychain first (same precedence as Claude Code on macOS)
    kc_data = read_platform_credentials()
    if kc_data and kc_data.get("_jackedAccountId") == account_id:
        token = kc_data.get("claudeAiOauth", {}).get("accessToken")
        if token:
            return token

    # Fall back to global .credentials.json
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            if data.get("_jackedAccountId") == account_id:
                token = data.get("claudeAiOauth", {}).get("accessToken")
                if token:
                    return token
        except (json.JSONDecodeError, OSError):
            pass

    return None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py -k "read_fresh_active_token" -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add jacked/api/credential_helpers.py tests/unit/test_credential_sync.py
git commit -m "feat: add read_fresh_active_token() for live credential reads

After account activation, Claude Code refreshes the access token
independently. This helper reads the current token from the keychain
or .credentials.json so usage checks use the live value, not the
stale DB copy."
```

---

### Task 2: Resurrect the `/accounts/{id}/use` endpoint

**Files:**
- Modify: `jacked/api/routes/auth.py:736-754`
- Create: `tests/unit/test_use_account.py`

Replace the 410 Gone stub with a working endpoint that calls `sync_credential_to_all_stores()`. The endpoint validates that the account is enabled, not invalid/expired, and has CC tokens (accounts without CC tokens would write un-refreshable credentials — Claude Code can't refresh `refreshToken: null`).

- [ ] **Step 1: Write failing tests for the use_account endpoint**

Create `tests/unit/test_use_account.py`:

```python
"""Tests for the /accounts/{id}/use endpoint (dashboard account switching)."""

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from jacked.api.routes.auth import router
from jacked.web.database import Database


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    with db._writer() as conn:
        # Account 1: fully valid with CC tokens
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (1, 'alice@test.com', 'at_1', 'rt_1', 1900000000,
                       1, 0, 'valid', 'max', 't1',
                       'cc_at_1', 'cc_rt_1', 1900000000,
                       NULL, 0, NULL)"""
        )
        # Account 2: disabled
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (2, 'bob@test.com', 'at_2', 'rt_2', 1900000000,
                       0, 0, 'valid', 'pro', 't2',
                       NULL, 0, NULL)"""
        )
        # Account 3: valid but no CC tokens
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (3, 'carol@test.com', 'at_3', 'rt_3', 1900000000,
                       1, 0, 'valid', 'pro', 't2',
                       NULL, 0, NULL)"""
        )
        # Account 4: invalid validation status
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (4, 'dave@test.com', 'at_4', 'rt_4', 1900000000,
                       1, 0, 'invalid', 'max', 't1',
                       'cc_at_4', 'cc_rt_4', 1900000000,
                       NULL, 2, 'Token revoked')"""
        )
    yield db
    db.close()


@pytest.fixture
def app(db, tmp_path):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_use_account_success(client, tmp_path):
    """Activating a valid account with CC tokens writes credentials to all stores."""
    with mock.patch(
        "jacked.api.credential_helpers.sync_credential_to_all_stores"
    ) as mock_sync:
        resp = client.post("/api/auth/accounts/1/use")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["email"] == "alice@test.com"
    mock_sync.assert_called_once()
    assert mock_sync.call_args.args[0] == 1  # account_id
    assert mock_sync.call_args.args[1]["email"] == "alice@test.com"


def test_use_account_not_found(client):
    """Returns 404 for non-existent account."""
    resp = client.post("/api/auth/accounts/999/use")
    assert resp.status_code == 404


def test_use_account_disabled(client):
    """Returns 400 for disabled account."""
    resp = client.post("/api/auth/accounts/2/use")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["error"]["message"].lower()


def test_use_account_no_cc_tokens(client):
    """Returns 400 for account without CC tokens (would be un-refreshable)."""
    resp = client.post("/api/auth/accounts/3/use")
    assert resp.status_code == 400
    assert "cc" in resp.json()["error"]["message"].lower() or \
           "authorize" in resp.json()["error"]["message"].lower()


def test_use_account_invalid_status(client):
    """Returns 400 for account with invalid validation status."""
    resp = client.post("/api/auth/accounts/4/use")
    assert resp.status_code == 400
    assert "invalid" in resp.json()["error"]["message"].lower() or \
           "re-auth" in resp.json()["error"]["message"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_use_account.py -v`
Expected: FAIL — the endpoint still returns 410

- [ ] **Step 3: Replace the 410 stub with working endpoint**

In `jacked/api/routes/auth.py`, replace lines 736–754 (the `use_account` function) with:

```python
@router.post("/accounts/{account_id}/use", response_model=UseAccountResponse)
async def use_account(account_id: int, request: Request):
    """Switch all Claude Code sessions to this account's credentials.

    Writes the account's tokens to all credential stores (global
    .credentials.json, macOS Keychain, ~/.claude.json).  Claude Code
    v2.1.81+ dynamically re-reads credentials, so running sessions
    pick up the new account without logging out.

    Rejects disabled accounts, accounts with invalid validation status,
    and accounts without CC tokens (which would be un-refreshable).
    """
    db = _get_db(request)
    if db is None:
        return _db_unavailable()

    account = db.get_account(account_id)
    if not account:
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

Note: Uses the late-import pattern (`from ... import` inside function body) to match the existing `get_active_credential` endpoint at line 767 of the same file. Do NOT add a top-level import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_use_account.py -v`
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add jacked/api/routes/auth.py tests/unit/test_use_account.py
git commit -m "feat: resurrect /accounts/{id}/use for dashboard account switching

Claude Code v2.1.81+ dynamically re-reads credentials from the
keychain and .credentials.json, so writing new credentials no longer
causes session logouts. The endpoint calls sync_credential_to_all_stores()
to update all stores atomically."
```

---

### Task 3: Wire usage refresh to use fresh token for active account

**Files:**
- Modify: `jacked/api/routes/auth.py:538-570` (refresh_usage and refresh_all_usage)

After switching accounts, Claude Code refreshes the access token independently. The DB copy goes stale. Usage checks for the active account should read the live token from the credential stores.

**Important:** The `fetch_usage()` cache guard (`auth.py:339`) is bypassed when `access_token` is non-None. To avoid always hitting the Anthropic API, we only pass the fresh token when it differs from the DB token. When they match (CC hasn't refreshed yet), we pass `None` so the cache guard works normally.

**Optimization:** In `refresh_all_usage`, reading the keychain for every account would be wasteful (subprocess per iteration). Instead, read the active account ID once upfront from the credential file, and only call `read_fresh_active_token` for that one account.

- [ ] **Step 1: Write failing tests for fresh-token usage refresh**

Add to `tests/unit/test_use_account.py`:

```python
def test_refresh_usage_uses_fresh_token_for_active_account(client, tmp_path):
    """Usage refresh reads fresh token from credential stores for active account.
    Only passes the fresh token when it differs from the DB token."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="fresh_token_from_keychain",
        ) as mock_fresh,
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.3}},
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    mock_fresh.assert_called_once_with(1)
    # DB token is "at_1", fresh token is different → should be passed
    assert mock_fetch.call_args.kwargs.get("access_token") == "fresh_token_from_keychain"


def test_refresh_usage_skips_fresh_when_unchanged(client, tmp_path):
    """When fresh token matches DB token, don't pass it (preserves cache guard)."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="at_1",  # same as DB token for account 1
        ),
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.3}},
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    # Token unchanged → access_token should be None (cache guard intact)
    assert mock_fetch.call_args.kwargs.get("access_token") is None


def test_refresh_usage_falls_back_to_db_token(client, tmp_path):
    """When credential stores don't have this account, falls back to DB token."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value=None,
        ),
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.3}},
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    # No fresh token → access_token should be None
    assert mock_fetch.call_args.kwargs.get("access_token") is None


def test_refresh_all_usage_only_reads_fresh_for_active(client, db, tmp_path):
    """Bulk refresh reads credential file once, only passes fresh token for active account."""
    # Write a credential file with active account ID = 1
    cred_dir = tmp_path / ".claude"
    cred_dir.mkdir(exist_ok=True)
    cred_path = cred_dir / ".credentials.json"
    cred_path.write_text(json.dumps({
        "_jackedAccountId": 1,
        "claudeAiOauth": {"accessToken": "cc_refreshed_token"},
    }))

    call_tokens = {}

    async def capture_fetch_usage(account_id, db_arg, access_token=None):
        call_tokens[account_id] = access_token
        return {"five_hour": {"utilization": 0.1}, "seven_day": {"utilization": 0.2}}

    with (
        mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="cc_refreshed_token",
        ) as mock_fresh,
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            side_effect=capture_fetch_usage,
        ),
    ):
        resp = client.post("/api/auth/accounts/refresh-all-usage")

    assert resp.status_code == 200
    # Account 1 is active — should get fresh token (differs from DB "at_1")
    assert call_tokens.get(1) == "cc_refreshed_token"
    # Account 3 is not active — should get None (DB token used internally)
    assert call_tokens.get(3) is None
    # read_fresh_active_token called only for the active account
    mock_fresh.assert_called_once_with(1)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_use_account.py -k "refresh_usage" -v`
Expected: FAIL — `read_fresh_active_token` not imported in routes

- [ ] **Step 3: Update refresh_usage to pass fresh token (only when changed)**

In `jacked/api/routes/auth.py`, update the `refresh_usage` endpoint (around line 541).

Replace:
```python
    usage_data = await fetch_usage(account_id, db)
```

With:
```python
    from jacked.api.credential_helpers import read_fresh_active_token

    fresh_token = read_fresh_active_token(account_id)
    # Only pass fresh_token when it differs from DB token — passing a non-None
    # access_token to fetch_usage() bypasses its cache freshness guard (auth.py:339).
    db_token = account.get("access_token")
    effective_token = fresh_token if (fresh_token and fresh_token != db_token) else None
    usage_data = await fetch_usage(account_id, db, access_token=effective_token)
```

For `refresh_all_usage` (around line 620), read the active account ID once upfront to avoid per-iteration keychain reads. Before the `for` loop (after `total = len(accounts)`), add:

```python
        # Read active account ID once from file — avoids per-iteration keychain
        # subprocess calls.  File-only is acceptable because
        # sync_credential_to_all_stores() writes both file and keychain
        # atomically, so they should always agree on _jackedAccountId.
        from jacked.api.credential_helpers import read_fresh_active_token
        active_acct_id = None
        cred_path = Path.home() / ".claude" / ".credentials.json"
        if cred_path.exists() and not cred_path.is_symlink():
            try:
                cred_data = json.loads(cred_path.read_text(encoding="utf-8"))
                active_acct_id = cred_data.get("_jackedAccountId")
            except (json.JSONDecodeError, OSError):
                pass
```

Then replace:
```python
            usage_data = await fetch_usage(acct["id"], db)
```

With:
```python
            effective_token = None
            if acct["id"] == active_acct_id:
                fresh_token = read_fresh_active_token(acct["id"])
                db_token = acct.get("access_token")
                if fresh_token and fresh_token != db_token:
                    effective_token = fresh_token
            usage_data = await fetch_usage(acct["id"], db, access_token=effective_token)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_use_account.py -v`
Expected: 9 passed

- [ ] **Step 5: Run the existing usage refresh tests to verify no regressions**

Run: `uv run python -m pytest tests/unit/test_usage_refresh.py -v`
Expected: All existing tests pass

- [ ] **Step 6: Commit**

```bash
git add jacked/api/routes/auth.py tests/unit/test_use_account.py
git commit -m "feat: usage refresh reads fresh token from credential stores

After account activation, Claude Code refreshes the access token
independently. Usage checks now read the live token from the keychain
or .credentials.json via read_fresh_active_token(). Only passes the
fresh token when it differs from the DB copy, preserving the cache
freshness guard. Bulk refresh reads active account ID once upfront
to avoid per-iteration keychain subprocess calls."
```

---

### Task 4: Add "Use Account" button to dashboard UI

**Files:**
- Modify: `jacked/data/web/js/components/accounts.js:173-215` (renderActionButtons)
- Modify: `jacked/data/web/js/components/account-actions.js:228-231` (bindAccountEvents)

- [ ] **Step 1: Add the button to account card rendering**

In `jacked/data/web/js/components/accounts.js`, replace the `renderActionButtons` function (lines 173–215) with:

```javascript
function renderActionButtons(acct) {
    const status = getAccountStatus(acct);
    const isActiveInCC = window.jackedState.activeCredentialAccountId === acct.id;

    // "Use Account" button or "Active" badge
    let setActiveHtml = '';
    if (isActiveInCC) {
        setActiveHtml = '<span class="text-xs px-3 py-1.5 bg-green-600/20 text-green-400 border border-green-600/30 rounded font-medium">Active in Claude Code</span>';
    } else if (acct.is_active && status !== 'invalid' && status !== 'expired' && status !== 'disabled' && status !== 'cc-missing') {
        setActiveHtml = `<button class="btn-use-account text-xs px-3 py-1.5 bg-teal-600/20 text-teal-400 hover:bg-teal-600/40 border border-teal-600/30 rounded font-medium transition-colors" data-id="${acct.id}" data-email="${escapeHtml(acct.email || '')}">Use Account</button>`;
    }

    // Copy launch command button
    const copyCmd = `jacked claude ${acct.id}`;
    const copyHtml = `<button class="btn-copy-cmd text-xs px-3 py-1.5 text-slate-400 hover:text-slate-200 hover:bg-slate-700 rounded transition-colors" data-cmd="${escapeHtml(copyCmd)}" title="Copy launch command">
        <svg class="w-4 h-4 inline-block mr-1" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 5H6a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2v-1M8 5a2 2 0 002 2h2a2 2 0 002-2M8 5a2 2 0 012-2h2a2 2 0 012 2m0 0h2a2 2 0 012 2v3m2 4H10m0 0l3-3m-3 3l3 3"/></svg>
        ${escapeHtml(copyCmd)}
    </button>`;

    // Re-auth button (if invalid/expired) — pills also handle this, keep for backward compat
    const showReauth = status === 'invalid' || status === 'expired';
    let reauthHtml = '';
    if (showReauth) {
        reauthHtml = `<button class="btn-reauth text-xs px-3 py-1.5 bg-blue-600/20 text-blue-400 hover:bg-blue-600/40 rounded transition-colors" data-id="${acct.id}" data-email="${escapeHtml(acct.email || '')}">Re-auth</button>`;
    }

    // Toggle active/disabled
    const toggleLabel = acct.is_active ? 'Disable' : 'Enable';
    const toggleClass = acct.is_active ? 'text-yellow-400 hover:text-yellow-300' : 'text-green-400 hover:text-green-300';

    return `
        <div class="flex items-center flex-wrap gap-2 mt-2 pt-2 border-t border-slate-700/50">
            ${setActiveHtml}
            ${copyHtml}
            <div class="flex-1"></div>
            ${reauthHtml}
            <button class="btn-toggle text-xs px-3 py-1.5 ${toggleClass} hover:bg-slate-700 rounded transition-colors" data-id="${acct.id}" data-active="${acct.is_active}">${toggleLabel}</button>
            <button class="btn-delete text-xs px-3 py-1.5 text-red-400 hover:text-red-300 hover:bg-red-900/30 rounded transition-colors" data-id="${acct.id}" title="Delete account">
                <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 7l-.867 12.142A2 2 0 0116.138 21H7.862a2 2 0 01-1.995-1.858L5 7m5 4v6m4-6v6m1-10V4a1 1 0 00-1-1h-4a1 1 0 00-1 1v3M4 7h16"/></svg>
            </button>
        </div>
    `;
}
```

- [ ] **Step 2: Wire up the click handler**

In `jacked/data/web/js/components/account-actions.js`, replace the comment block at lines 229–231:

```javascript
    // "Set Active" removed — account switching is via `jacked claude <id>` only.
    // The .btn-set-active button no longer renders; this comment replaces the
    // old event listener that called /api/auth/accounts/{id}/use (now 410 Gone).
```

With:

```javascript
    // "Use Account" button — switches all Claude Code sessions to this account
    document.querySelectorAll('.btn-use-account').forEach(btn => {
        btn.addEventListener('click', async () => {
            if (window.jackedState._accountActionInFlight) {
                showToast('Another action is in progress', 'warning', 2000);
                return;
            }
            const id = btn.dataset.id;
            const email = btn.dataset.email || '';
            window.jackedState._accountActionInFlight = true;
            btn.disabled = true;
            btn.textContent = 'Switching\u2026';
            try {
                await api.post(`/api/auth/accounts/${id}/use`);
                showToast(`Switched to ${email}`, 'success');
                await loadActiveCredential();
            } catch (e) {
                showToast(e.message, 'error');
            } finally {
                window.jackedState._accountActionInFlight = false;
                await refreshAndRender();
            }
        });
    });
```

- [ ] **Step 3: Remove stale session-tip banner text**

In `jacked/data/web/js/components/accounts.js`, update the session isolation tip banner (around line 350) to mention both switching methods. Replace:

```javascript
                        Use <code class="bg-slate-800 px-1.5 py-0.5 rounded text-teal-400 text-xs">jacked claude &lt;id&gt;</code> to launch Claude Code with isolated credentials per account.
```

With:

```javascript
                        Click <strong>Use Account</strong> to switch all sessions, or use <code class="bg-slate-800 px-1.5 py-0.5 rounded text-teal-400 text-xs">jacked claude &lt;id&gt;</code> to launch with isolated credentials.
```

- [ ] **Step 4: Manual browser test**

1. Run `jacked webux` to start the dashboard
2. Navigate to Accounts tab
3. Verify the non-active accounts show a teal "Use Account" button
4. Verify the currently active account shows a green "Active in Claude Code" badge
5. Click "Use Account" on a different account
6. Verify the toast shows "Switched to {email}"
7. Verify the badge moves to the newly activated account
8. Verify a running Claude Code session picks up the new credentials (check `/status`)

- [ ] **Step 5: Commit**

```bash
git add jacked/data/web/js/components/accounts.js jacked/data/web/js/components/account-actions.js
git commit -m "feat: add Use Account button to dashboard for credential switching

Adds a teal 'Use Account' button on each account card (hidden for the
already-active account, which shows 'Active in Claude Code' badge).
Clicking it POSTs to /accounts/{id}/use, switches credentials in all
stores, and refreshes the UI to reflect the new active account."
```

---

### Task 5: Run full test suite and verify

**Files:** None (verification only)

- [ ] **Step 1: Run the full test suite**

Run: `uv run python -m pytest tests/ -v --tb=short`
Expected: All tests pass, no regressions

- [ ] **Step 2: Run credential-specific tests**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py tests/unit/test_use_account.py tests/unit/test_usage_refresh.py -v`
Expected: All pass

- [ ] **Step 3: Commit any fixups if needed, then final commit message**

If all clean, no action needed. If fixups were required, commit them with:

```bash
git commit -m "fix: address test regressions from account switching feature"
```
