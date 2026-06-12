# Kill Background Credential Writes

**Date:** 2026-03-24
**Status:** Approved
**Problem:** Jacked server background loops cause Claude Code session logouts

---

## Problem

The jacked web server runs two background loops that proactively write to Claude Code's credential files:

- **Token refresh loop** (every 30 min) — `_token_refresh_loop()` in `jacked/api/main.py` calls `refresh_all_expiring_tokens()` which calls `sync_credential_to_all_stores()`, overwriting `~/.claude/.credentials.json`, `~/.claude.json`, per-account credential files, and the macOS Keychain entry `"Claude Code-credentials"`
- **Heal sweep loop** (every 5 min) — `_heal_sweep_loop()` in `jacked/api/main.py` calls `heal_invalid_accounts()` which can re-validate and overwrite tokens Claude Code has already invalidated

Claude Code exclusively owns `~/.claude/.credentials.json` and the Keychain entry. When the server overwrites these files via atomic `os.replace()`, Claude Code's file watcher detects the change, re-reads credentials, and the resulting race condition (changed token, stale Keychain read, or `_jackedAccountId` stamp confusion) causes active sessions across all terminal windows to appear logged out.

The dashboard's "Use account" button (`use_account` route in `jacked/api/routes/auth.py`) also calls `sync_credential_to_all_stores()` — this feature never worked reliably for the same reason.

**Observed behavior:** User's Claude Code sessions across multiple terminals repeatedly log out while the jacked server is running. Killing the server stops the logouts.

## Solution

Remove all credential **file** writes from the server's token refresh path. The background loops continue running — they still refresh tokens and store them in the DB so the dashboard's accounts/usage tracking page works. The only change is that refreshed tokens are no longer written to Claude Code's credential files (`~/.claude/.credentials.json`, Keychain). The server becomes read-only with respect to Claude Code's credential stores while still maintaining its own DB-side token state.

Account switching has two modes: (1) `jacked claude <id>` uses per-account `CLAUDE_CONFIG_DIR` directories for session isolation, and (2) the dashboard "Use Account" button (`/accounts/{id}/use` endpoint) writes to the global credential stores for all non-isolated sessions. The dashboard endpoint is a user-initiated one-shot write, not a background loop — the read-only invariant for background processes is preserved.

## Changes

### 1. Strip credential file writes from token refresh (the core fix)

**File:** `jacked/web/auth.py`

- In `refresh_account_token()`: remove the entire conditional block that reads `.credentials.json`, checks `_jackedAccountId`, and calls `sync_credential_to_all_stores()` (the try/except block around line 253-279). This includes the `is_active` gate and the import.
- Keep the DB update — refreshed tokens are still stored in the database so the dashboard can show accurate account status and usage tracking
- **Background loops keep running:** `_token_refresh_loop()` (every 30 min) and `_heal_sweep_loop()` (every 5 min) in `jacked/api/main.py` continue to call `refresh_all_expiring_tokens()` and `heal_invalid_accounts()`. They refresh tokens via the Anthropic API and store results in the DB. The only difference: the refreshed tokens no longer propagate to Claude Code's credential files.
- **This also neutralizes the heal loop path:** `heal_invalid_accounts()` calls `refresh_account_token()`, so removing the credential write from `refresh_account_token()` makes the heal loop DB-only too.
- **This also neutralizes the manual `/accounts/{id}/refresh` endpoint** in `jacked/api/routes/auth.py`, which calls `refresh_account_token()` directly. After this change, manual refresh updates the DB only.

### 2. Replace `use_account` endpoint

**File:** `jacked/api/routes/auth.py`

- Replace the `use_account()` route handler (line 737) with a response that directs users to the CLI:
  - Return HTTP 410 Gone with message: "Use `jacked claude <id>` to switch accounts. Dashboard credential switching has been removed to prevent session interference."
- This ensures any dashboard UI calling this endpoint gets a clear error rather than a silent failure

### 3. Update dashboard UI

**File:** Dashboard frontend (JS/HTML in `jacked/data/web/`)

- Replace the "Use account" button with a "Launch session" action that shows the `jacked claude <id>` command for the user to copy
- Account cards continue to show status (valid/invalid/expiring) from DB data — this is read-only and unaffected

## What stays untouched

| Component | Why it's safe |
|-----------|---------------|
| `jacked/api/main.py` background loops | Still running. `_token_refresh_loop()` and `_heal_sweep_loop()` continue refreshing tokens and healing accounts, but now only update the DB (credential file writes removed from `refresh_account_token()`). Dashboard usage tracking and account status work as before. |
| `session_account_tracker.py` hook | Only reads credentials, never writes |
| `security_gatekeeper.py` hook | Never touches credentials |
| `launch.py` `_token_sync_loop()` | Reads per-account `CLAUDE_CONFIG_DIR` files → syncs to DB (read direction only, never touches global credentials) |
| `launch.py` `write_platform_credentials()` | Used by `jacked claude <id>` — one-shot during explicit user action. **Note:** writes to the global macOS Keychain (not just per-account dirs), but this is acceptable because it's user-initiated and happens once at launch, not in a background loop. The per-account `CLAUDE_CONFIG_DIR` file write is isolated. |
| `oauth.py` `sync_credential_to_all_stores()` call | Used during initial OAuth account registration — one-shot during explicit user action. **Acknowledged exception:** this does write to the global `~/.claude/.credentials.json` and Keychain, which could briefly affect running sessions. Acceptable because account registration is rare (typically once per account) and user-initiated. If this proves problematic, a future change could scope the OAuth write to only per-account dirs. |
| `jacked/api/routes/auth.py` `/accounts/{id}/refresh` | Calls `refresh_account_token()` — after change #2, this updates DB only (credential file write removed from `refresh_account_token`). Safe. |
| `credential_helpers.py` module | Functions remain available for `launch.py` and `oauth.py` one-shot paths |

## Files modified

| File | Change | Risk |
|------|--------|------|
| `jacked/web/auth.py` | Remove `sync_credential_to_all_stores()` block from `refresh_account_token()` | Low — DB update stays, background loops unaffected |
| `jacked/api/routes/auth.py` | Replace `use_account` handler with 410 Gone | Low — feature was broken |
| Dashboard frontend | "Use account" → "Launch session" with CLI command | Low — UI only |

## Testing

- Start jacked server, open multiple Claude Code terminals, verify no logouts over 30+ minutes
- Verify `jacked claude <id>` still works for account switching
- Verify dashboard still shows account status correctly (reads from DB)
- Verify OAuth registration flow still works for adding new accounts
- Run existing test suite to confirm no regressions

## Rollback

If credential file sync is needed in the future (e.g., a safe locking protocol is implemented), the `sync_credential_to_all_stores()` call can be restored in `refresh_account_token()` by reverting the single change to `auth.py`. No data migration needed — the DB schema is unchanged.
