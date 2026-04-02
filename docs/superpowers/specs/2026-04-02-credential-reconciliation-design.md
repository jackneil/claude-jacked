# Credential Reconciliation Before Swap

**Date:** 2026-04-02
**Status:** Approved

## Problem

Three related credential sync issues:

1. **CC `invalid_grant` permanently kills refresh token.** A single `invalid_grant` on the CC refresh token clears `cc_refresh_token=None` in the DB. But the token may still be valid in the Keychain/credentials file (Claude Code refreshed successfully). The account becomes permanently degraded — shows "needs re-auth" in the UI even though Claude Code can use it.

2. **No credential reconciliation before swap.** When swapping accounts, jacked writes the new account's credentials but never reads back the outgoing account's live state from the Keychain. If Claude Code rotated the refresh token during its session, jacked's DB has the old (now-dead) token.

3. **Manual "Refresh All" blocked by coordinator ceiling.** Removing `force=True` means the 65s coordinator ceiling blocks user-initiated manual refreshes. Users see a stuck spinner.

## Solution

### 1. Credential reconciliation before every swap

Before writing new credentials (in `sync_credential_to_all_stores` or just before it's called), read the outgoing active account's live credentials from Keychain/file and reconcile with the DB.

**Steps:**
1. Read live credentials via `read_platform_credentials()` (Keychain first, file fallback)
2. Check `_jackedAccountId` matches the outgoing active account
3. Compare live `refreshToken` against DB `cc_refresh_token`:

| DB cc_refresh_token | Live refreshToken | Action |
|---|---|---|
| Same as live | Same | No action |
| Valid (not cleared) | Different | Update DB with live token (rotation happened) |
| NULL (cleared by invalid_grant) | Present | Import live token into DB (recovery) |
| NULL | NULL | No recovery possible |

4. Also update `cc_access_token` and `cc_expires_at` from live credentials

**Where this runs:** Add a `_reconcile_outgoing_credentials(active_account_id, db)` function called in `active_account_poll_loop` just before `sync_credential_to_all_stores`, and in the `use_account` API endpoint before credential write.

### 2. CC `invalid_grant` recovery from live credentials

Change `refresh_cc_token` in `auth.py`: on `invalid_grant`, BEFORE clearing `cc_refresh_token`, attempt to read the live credentials from Keychain/file. If a valid refresh token is there (Claude Code refreshed successfully), import it into the DB instead of clearing.

**Flow:**
```
On invalid_grant:
  1. Read live credentials from Keychain/file
  2. If live has a refresh token AND it differs from our (now-invalid) one:
     → Import live tokens into DB (access + refresh + expires)
     → Log: "CC invalid_grant recovered from live credentials"
     → Return True (recovered)
  3. Else:
     → Clear cc_refresh_token (no recovery possible)
     → Log existing warning
     → Return False
```

### 3. Manual refresh bypasses coordinator ceiling

Add `manual: bool = False` parameter to `fetch_usage`. When True, skip the coordinator ceiling check but still respect 429 backoff. Apply to:
- Single-account refresh endpoint (`routes/auth.py:558`)
- Bulk "Refresh All" endpoint (`routes/auth.py:666`)

### 4. Fix `cc_needs_auth` to account for primary fallback

Change the `cc_needs_auth` computation at `routes/auth.py:300-304`. Only flag True when the account is genuinely unusable — not when primary fallback works.

Current (too strict):
```python
cc_needs_auth = (
    cc_access_token is not None
    and cc_refresh_token is None
    and now >= cc_expires_at
)
```

New (accounts for primary fallback):
```python
cc_needs_auth = (
    cc_access_token is not None
    and cc_refresh_token is None
    and now >= (cc_expires_at or 0)
    and not bool(row.get("refresh_token"))  # primary can't save us either
)
```

If primary `refresh_token` exists, the account will work via fallback — don't flag "needs re-auth."

## Files Affected

| File | Change |
|------|--------|
| `jacked/api/credential_helpers.py` | Add `reconcile_outgoing_credentials()` — reads live creds, updates DB |
| `jacked/web/auth.py` | `refresh_cc_token`: recover from live creds on `invalid_grant`. `fetch_usage`: add `manual` param |
| `jacked/api/usage_monitor.py` | Call `reconcile_outgoing_credentials` before swap |
| `jacked/api/routes/auth.py` | Fix `cc_needs_auth` computation. Pass `manual=True` for user-initiated refreshes |

## What This Does NOT Change

- The primary token refresh logic (already resilient)
- The window keeper ping mechanism
- The swap decision algorithm
- The adaptive polling intervals
