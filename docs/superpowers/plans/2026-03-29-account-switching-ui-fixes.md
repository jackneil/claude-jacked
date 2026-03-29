# Account Switching UI Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix active-account detection so the "Active in Claude Code" badge displays correctly, show "Use Account" on all enabled accounts, and remove the copy-command clutter.

**Architecture:** The active-credential endpoint currently relies on two detection layers: `_jackedAccountId` stamp (written only by `sync_credential_to_all_stores`) and exact token match. Both fail on the user's system because (1) the stamp was never written (accounts predate this feature, and the kill-background-credential-writes spec blocked all credential writes), and (2) Claude Code refreshed the token so it no longer matches the DB copy. Fix: add a third detection layer that matches the email+org from `~/.claude.json` — a file Claude Code maintains on all platforms and never clobbers during token refresh. For the UI: show "Use Account" on all enabled accounts regardless of status (the backend returns clear error messages), and hide the copy-command buttons.

**Tech Stack:** Python 3.12+ (FastAPI, Pydantic v2), vanilla JS, macOS Keychain (`security` CLI)

### Cross-Platform Considerations

| Platform | `.credentials.json` | Keychain | `~/.claude.json` |
|----------|---------------------|----------|-------------------|
| macOS    | May not exist (CC uses keychain exclusively) | Primary credential store; may lack `_jackedAccountId` stamp | Always exists after login; has `emailAddress` + `organizationUuid` |
| Linux    | Primary credential store (no keychain) | N/A | Always exists after login |
| Windows  | Primary credential store (no keychain) | N/A | Always exists at `%USERPROFILE%\.claude.json` |

The email+org fallback from `~/.claude.json` works identically on all platforms because `Path.home() / ".claude.json"` resolves correctly everywhere, and Claude Code writes this file during login on every OS.

### Multi-Org Edge Case

The user has two accounts with the same email (`jack.neil@hank.ai`) but different organizations. The email fallback must match on both `emailAddress` AND `organizationUuid` to avoid ambiguity. `~/.claude.json` contains both fields.

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/api/routes/auth.py` | Auth API endpoints | Add email+org fallback in `get_active_credential` |
| `jacked/data/web/js/components/accounts.js` | Account card rendering | Simplify button visibility, hide copy command |
| `tests/unit/test_use_account.py` | Endpoint tests | Add active-credential fallback tests |

---

### Task 1: Add email+org fallback to active-credential detection

**Files:**
- Modify: `jacked/api/routes/auth.py:859-914` (get_active_credential)
- Modify: `tests/unit/test_use_account.py`

The keychain has no `_jackedAccountId` stamp (accounts predate this feature). Token match fails because CC refreshed the access token. Fix with two improvements: (1) add refresh token matching to Layer 2 — the `refreshToken` in the credential stores matches `cc_refresh_token` in the DB and is more stable than the access token (only rotates when CC explicitly refreshes, not on every API call); (2) add email+org matching from `~/.claude.json` as a Layer 3 fallback for when even the refresh token has been rotated or the user logged in via Claude Code directly.

- [ ] **Step 1: Write failing tests for refresh token match and email+org fallback**

Add to `tests/unit/test_use_account.py`:

```python
def test_active_credential_refresh_token_match(client, db, tmp_path):
    """Matches active account via refresh token when access token has been refreshed."""
    # Keychain has refreshed access token (doesn't match DB) but original refresh token
    with mock.patch(
        "jacked.api.credential_helpers.read_platform_credentials",
        return_value={
            "claudeAiOauth": {
                "accessToken": "cc_refreshed_by_claude_code",
                "refreshToken": "cc_rt_1",  # matches account 1's cc_refresh_token
            }
        },
    ):
        # No credential file — macOS keychain-only scenario
        with mock.patch("jacked.api.routes.auth.Path.home", return_value=tmp_path):
            resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == 1
    assert data["email"] == "alice@test.com"


def test_active_credential_email_org_fallback(client, db, tmp_path):
    """Falls back to ~/.claude.json email+org when stamp and ALL token matches fail."""
    # Keychain has completely unknown tokens (CC logged in fresh, not via jacked)
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": "alice@test.com",
            "organizationUuid": None,
        }
    }))

    with (
        mock.patch("jacked.api.routes.auth.Path.home", return_value=tmp_path),
        mock.patch(
            "jacked.api.credential_helpers.read_platform_credentials",
            return_value={"claudeAiOauth": {
                "accessToken": "totally_unknown",
                "refreshToken": "also_unknown",
            }},
        ),
    ):
        resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == 1
    assert data["email"] == "alice@test.com"


def test_active_credential_email_org_disambiguates(client, db, tmp_path):
    """Email+org match picks the right account when email is shared across orgs."""
    # Add two accounts with same email, different orgs
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token,
                expires_at, is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (10, 'shared@test.com', 'org-aaa', 'at_10', 'rt_10',
                       1900000000, 1, 0, 'valid', 'pro', 't1',
                       NULL, 0, NULL)"""
        )
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, access_token, refresh_token,
                expires_at, is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (11, 'shared@test.com', 'org-bbb', 'at_11', 'rt_11',
                       1900000000, 1, 0, 'valid', 'pro', 't1',
                       NULL, 0, NULL)"""
        )

    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({
        "oauthAccount": {
            "emailAddress": "shared@test.com",
            "organizationUuid": "org-bbb",
        }
    }))

    with (
        mock.patch("jacked.api.routes.auth.Path.home", return_value=tmp_path),
        mock.patch(
            "jacked.api.credential_helpers.read_platform_credentials",
            return_value=None,
        ),
    ):
        resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] == 11  # org-bbb, not org-aaa
    assert data["email"] == "shared@test.com"


def test_active_credential_no_match(client, db, tmp_path):
    """Returns empty when nothing matches (no stamp, no token, no email)."""
    claude_json = tmp_path / ".claude.json"
    claude_json.write_text(json.dumps({
        "oauthAccount": {"emailAddress": "nobody@test.com"}
    }))

    with (
        mock.patch("jacked.api.routes.auth.Path.home", return_value=tmp_path),
        mock.patch(
            "jacked.api.credential_helpers.read_platform_credentials",
            return_value=None,
        ),
    ):
        resp = client.get("/api/auth/active-credential")

    assert resp.status_code == 200
    data = resp.json()
    assert data["account_id"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_use_account.py -k "active_credential" -v`
Expected: FAIL — no email fallback exists yet

- [ ] **Step 3: Add refresh token matching to Layer 2 and email+org fallback as Layer 3**

In `jacked/api/routes/auth.py`, find the `get_active_credential` function.

**First**, enhance Layer 2 (around line 898-912). Replace the entire Layer 2 block:

```python
    # Layer 2: Exact token match (CC token takes precedence over primary)
    access_token = cred_data.get("claudeAiOauth", {}).get("accessToken")
    if access_token:
        accounts = db.list_accounts(include_inactive=True)
        for acct in accounts:
            if acct.get("is_deleted"):
                continue
            if acct.get("cc_access_token") == access_token:
                return ActiveCredentialResponse(
                    account_id=acct["id"], email=acct["email"]
                )
            if acct.get("access_token") == access_token:
                return ActiveCredentialResponse(
                    account_id=acct["id"], email=acct["email"]
                )
```

With:

```python
    # Layer 2: Token match — try refresh token first (more stable than
    # access token because it only rotates on explicit refresh, not every
    # API call), then fall back to access token match.
    oauth_data = cred_data.get("claudeAiOauth", {})
    refresh_token = oauth_data.get("refreshToken")
    access_token = oauth_data.get("accessToken")

    if refresh_token or access_token:
        accounts = db.list_accounts(include_inactive=True)
        # Pass 1: refresh token match (most stable after dashboard switch)
        if refresh_token:
            for acct in accounts:
                if acct.get("is_deleted"):
                    continue
                if acct.get("cc_refresh_token") == refresh_token:
                    return ActiveCredentialResponse(
                        account_id=acct["id"], email=acct["email"]
                    )
        # Pass 2: access token match (works briefly before CC refreshes)
        if access_token:
            for acct in accounts:
                if acct.get("is_deleted"):
                    continue
                if acct.get("cc_access_token") == access_token:
                    return ActiveCredentialResponse(
                        account_id=acct["id"], email=acct["email"]
                    )
                if acct.get("access_token") == access_token:
                    return ActiveCredentialResponse(
                        account_id=acct["id"], email=acct["email"]
                    )
```

**Second**, replace the final `return ActiveCredentialResponse()` (after the Layer 2 block) with the Layer 3 fallback:

```python
    # Layer 3: Email + org match from ~/.claude.json
    #
    # On macOS, .credentials.json may not exist (Claude Code uses keychain
    # exclusively).  The keychain may lack the _jackedAccountId stamp
    # (accounts predating the dashboard-switching feature).  Token match
    # fails because Claude Code refreshes tokens independently.
    #
    # ~/.claude.json is maintained by Claude Code on ALL platforms and
    # always has oauthAccount.emailAddress + organizationUuid after login.
    # It is NOT overwritten during token refresh — only during login/logout.
    claude_config = Path.home() / ".claude.json"
    if claude_config.exists() and not claude_config.is_symlink():
        try:
            config = json.loads(claude_config.read_text(encoding="utf-8"))
            oauth_acct = config.get("oauthAccount", {})
            config_email = oauth_acct.get("emailAddress")
            config_org = oauth_acct.get("organizationUuid")
            if config_email:
                accounts = db.list_accounts(include_inactive=True)
                # Prefer email+org match (disambiguates same-email multi-org)
                for acct in accounts:
                    if acct.get("is_deleted"):
                        continue
                    if (
                        acct.get("email", "").lower() == config_email.lower()
                        and acct.get("organization_uuid") == config_org
                    ):
                        return ActiveCredentialResponse(
                            account_id=acct["id"], email=acct["email"]
                        )
                # Fall back to email-only match (org may be None for personal accounts)
                for acct in accounts:
                    if acct.get("is_deleted"):
                        continue
                    if acct.get("email", "").lower() == config_email.lower():
                        return ActiveCredentialResponse(
                            account_id=acct["id"], email=acct["email"]
                        )
        except (json.JSONDecodeError, OSError):
            pass

    return ActiveCredentialResponse()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_use_account.py -v`
Expected: 14 passed (10 existing + 4 new)

- [ ] **Step 5: Run existing tests to verify no regressions**

Run: `uv run python -m pytest tests/unit/test_credential_sync.py tests/unit/test_usage_refresh.py -v`
Expected: All pass

- [ ] **Step 6: Commit**

```bash
git add jacked/api/routes/auth.py tests/unit/test_use_account.py
git commit -m "fix: improve active-credential detection with refresh token + email fallback

Layer 2 now matches refresh tokens first (more stable than access
tokens — only rotates on explicit refresh, not every API call).
Layer 3 falls back to email+organizationUuid from ~/.claude.json,
which Claude Code maintains on all platforms and never overwrites
during token refresh. Disambiguates same-email multi-org accounts.

Fixes: active account not detected on macOS when keychain lacks
_jackedAccountId stamp (accounts predating dashboard switching)."
```

---

### Task 2: Fix button visibility and hide copy command

**Files:**
- Modify: `jacked/data/web/js/components/accounts.js:173-215` (renderActionButtons)

Two changes: (1) show "Use Account" on all enabled accounts (the backend returns clear errors for invalid/cc-missing), (2) hide the copy-command buttons (keep code commented for future use).

- [ ] **Step 1: Update renderActionButtons**

In `jacked/data/web/js/components/accounts.js`, replace the `renderActionButtons` function (lines 173-215) with:

```javascript
function renderActionButtons(acct) {
    const status = getAccountStatus(acct);
    const isActiveInCC = window.jackedState.activeCredentialAccountId === acct.id;

    // "Use Account" button or "Active" badge.
    // Show on all enabled accounts — backend validates and returns clear
    // error messages for cc-missing, invalid, etc.
    let setActiveHtml = '';
    if (isActiveInCC) {
        setActiveHtml = '<span class="text-xs px-3 py-1.5 bg-green-600/20 text-green-400 border border-green-600/30 rounded font-medium">Active in Claude Code</span>';
    } else if (acct.is_active) {
        setActiveHtml = `<button class="btn-use-account text-xs px-3 py-1.5 bg-teal-600/20 text-teal-400 hover:bg-teal-600/40 border border-teal-600/30 rounded font-medium transition-colors" data-id="${acct.id}" data-email="${escapeHtml(acct.email || '')}">Use Account</button>`;
    }

    // Copy launch command — hidden now that dashboard switching works.
    // Kept commented for future use (per-account isolated sessions).
    // const copyCmd = `jacked claude ${acct.id}`;
    // const copyHtml = `<button class="btn-copy-cmd ...">...</button>`;
    const copyHtml = '';

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

Changes from current code:
- Line `else if`: removed `&& status !== 'invalid' && status !== 'expired' && status !== 'disabled' && status !== 'cc-missing'` — now just `acct.is_active` (disabled accounts still don't show the button because `is_active` is false for them)
- Copy command: replaced with `const copyHtml = '';` — old code kept as comments above

- [ ] **Step 2: Commit**

```bash
git add jacked/data/web/js/components/accounts.js
git commit -m "fix: show Use Account on all enabled accounts, hide copy command

Show 'Use Account' button on all enabled accounts regardless of token
status — the backend returns clear error messages for cc-missing,
invalid, etc. Hide the 'jacked claude <id>' copy button now that
dashboard switching works (code kept as comments for future use)."
```

---

### Task 3: Run full test suite and verify in browser

**Files:** None (verification only)

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ -v --tb=short`
Expected: All pass, no regressions

- [ ] **Step 2: Manual browser test**

1. Reload the dashboard (`jacked webux` or refresh)
2. Verify the currently-active account shows "Active in Claude Code" green badge (email+org detection should match)
3. Verify ALL other enabled accounts show teal "Use Account" buttons
4. Verify `jacked claude <id>` copy buttons are gone
5. Click "Use Account" on an account without CC tokens — verify error toast explains what to do
6. Click "Use Account" on account 1 (has CC tokens) — verify it switches and badge updates
