# Credential Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Before every account swap, read the outgoing account's live credentials from Keychain/file, reconcile with the DB (importing rotated refresh tokens), recover from `invalid_grant` using live creds, fix the manual refresh button, and fix the false "needs re-auth" indicator.

**Architecture:** Add `reconcile_outgoing_credentials()` to `credential_helpers.py` that reads live creds and updates the DB. Call it before every `sync_credential_to_all_stores`. Modify `refresh_cc_token` to attempt live-cred recovery before clearing the refresh token. Add `manual` param to `fetch_usage`. Fix `cc_needs_auth` to account for primary fallback.

**Tech Stack:** Python 3.12+ (macOS Keychain via `security` CLI, SQLite)

**Design spec:** `docs/superpowers/specs/2026-04-02-credential-reconciliation-design.md`

---

## File Structure

| File | Role | Change |
|------|------|--------|
| `jacked/api/credential_helpers.py` | Credential management | Add `reconcile_outgoing_credentials()` |
| `jacked/web/auth.py` | Token refresh + usage fetch | `refresh_cc_token`: recover from live creds on `invalid_grant`. `fetch_usage`: add `manual` param |
| `jacked/api/usage_monitor.py` | Poll loop | Call reconcile before swap |
| `jacked/api/routes/auth.py` | API endpoints | Fix `cc_needs_auth`. Pass `manual=True` to `fetch_usage` |
| `tests/unit/test_credential_helpers.py` or inline | Tests | Test reconciliation logic |

---

### Task 1: Add `reconcile_outgoing_credentials` function

**Files:**
- Modify: `jacked/api/credential_helpers.py`

- [ ] **Step 1: Add the function after `read_fresh_active_token`**

After `read_fresh_active_token` (around line 218), add:

```python
def reconcile_outgoing_credentials(account_id: int, db) -> None:
    """Read live credentials for the active account and reconcile with DB.

    Before swapping away from an account, check if Claude Code rotated
    the refresh token during its session. If so, import the fresh token
    into our DB so we don't lose it.

    Called before sync_credential_to_all_stores() in the swap path and
    in the use_account endpoint.
    """
    # Read live credentials (Keychain first, file fallback)
    live = read_platform_credentials()
    if not live:
        # Fall back to .credentials.json
        cred_path = Path.home() / ".claude" / ".credentials.json"
        if cred_path.exists() and not cred_path.is_symlink():
            try:
                live = json.loads(cred_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return

    if not live:
        return

    # Only reconcile if the credentials belong to the outgoing account
    if live.get("_jackedAccountId") != account_id:
        return

    oauth = live.get("claudeAiOauth", {})
    live_access = oauth.get("accessToken")
    live_refresh = oauth.get("refreshToken")
    live_expires = oauth.get("expiresAt")  # milliseconds

    if not live_access:
        return

    # Read current DB state
    account = db.get_account(account_id)
    if not account:
        return

    db_cc_refresh = account.get("cc_refresh_token")

    updates = {}

    # Always import the latest access token
    if live_access != account.get("cc_access_token"):
        updates["cc_access_token"] = live_access

    # Import expiry (convert ms -> seconds)
    if live_expires:
        live_expires_s = int(live_expires / 1000) if live_expires > 1e12 else int(live_expires)
        if live_expires_s != account.get("cc_expires_at"):
            updates["cc_expires_at"] = live_expires_s

    # Reconcile refresh token
    if live_refresh:
        if db_cc_refresh is None:
            # Recovery: our token was cleared (invalid_grant) but Claude Code has one
            logger.info(
                "Account %d: recovering cc_refresh_token from live credentials",
                account_id,
            )
            updates["cc_refresh_token"] = live_refresh
        elif live_refresh != db_cc_refresh:
            # Rotation: Claude Code got a new token, update ours
            logger.info(
                "Account %d: importing rotated cc_refresh_token from live credentials",
                account_id,
            )
            updates["cc_refresh_token"] = live_refresh

    if updates:
        db.update_account(account_id, **updates)
        logger.info(
            "Account %d: reconciled %d credential field(s) from live store",
            account_id, len(updates),
        )
```

- [ ] **Step 2: Run tests to verify nothing broke**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/credential_helpers.py
git commit -m "feat: reconcile_outgoing_credentials reads live creds before swap"
```

---

### Task 2: Call reconciliation before every swap

**Files:**
- Modify: `jacked/api/usage_monitor.py`
- Modify: `jacked/api/routes/auth.py` (use_account endpoint)

- [ ] **Step 1: Add reconcile call in auto-swap path**

In `jacked/api/usage_monitor.py`, find where `sync_credential_to_all_stores` is called in the swap execution block (around line 386). Add reconciliation BEFORE the sync call. Find:

```python
                    sync_credential_to_all_stores(
                        target["id"], target,
                        email=target.get("email"),
                    )
```

Add BEFORE it:

```python
                    # Reconcile outgoing account's credentials before writing
                    # new ones — captures any token rotation by Claude Code.
                    from jacked.api.credential_helpers import reconcile_outgoing_credentials
                    reconcile_outgoing_credentials(active_acct_id, db)

                    sync_credential_to_all_stores(
                        target["id"], target,
                        email=target.get("email"),
                    )
```

- [ ] **Step 2: Add reconcile call in use_account endpoint**

In `jacked/api/routes/auth.py`, find the `use_account` endpoint. Search for where it calls `sync_credential_to_all_stores`. Add `reconcile_outgoing_credentials` before it. Find the active account ID (from `_read_active_account_id` or similar) and call:

```python
    from jacked.api.credential_helpers import reconcile_outgoing_credentials
    from jacked.api.usage_monitor import _read_active_account_id
    outgoing_id = _read_active_account_id()
    if outgoing_id and outgoing_id != account_id:
        reconcile_outgoing_credentials(outgoing_id, db)
```

Add this just before the `sync_credential_to_all_stores` call in the use_account endpoint.

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 4: Commit**

```bash
git add jacked/api/usage_monitor.py jacked/api/routes/auth.py
git commit -m "feat: reconcile outgoing credentials before every swap"
```

---

### Task 3: CC `invalid_grant` recovery from live credentials

**Files:**
- Modify: `jacked/web/auth.py`

- [ ] **Step 1: Modify the invalid_grant handler in refresh_cc_token**

In `jacked/web/auth.py`, find the `invalid_grant` handler in `refresh_cc_token` (around line 217-227). Replace:

```python
                        if error_data.get("error") == "invalid_grant":
                            logger.warning(
                                "Account %d: CC invalid_grant — clearing refresh token "
                                "(access token preserved until expiry)",
                                account_id,
                            )
                            db.update_account(
                                account_id,
                                cc_refresh_token=None,
                            )
                            return False
```

With:

```python
                        if error_data.get("error") == "invalid_grant":
                            # Before clearing, check if Claude Code already
                            # refreshed and wrote a valid token to the store.
                            from jacked.api.credential_helpers import (
                                read_platform_credentials,
                            )
                            live = read_platform_credentials()
                            if not live:
                                cred_path = Path.home() / ".claude" / ".credentials.json"
                                if cred_path.exists() and not cred_path.is_symlink():
                                    try:
                                        live = json.loads(cred_path.read_text(encoding="utf-8"))
                                    except (json.JSONDecodeError, OSError):
                                        live = None

                            live_refresh = None
                            if live and live.get("_jackedAccountId") == account_id:
                                oauth = live.get("claudeAiOauth", {})
                                live_refresh = oauth.get("refreshToken")
                                live_access = oauth.get("accessToken")
                                live_expires = oauth.get("expiresAt")

                            if live_refresh and live_refresh != account["cc_refresh_token"]:
                                # Claude Code refreshed — import the new token
                                live_exp_s = (
                                    int(live_expires / 1000) if live_expires and live_expires > 1e12
                                    else int(live_expires) if live_expires else None
                                )
                                updates = {
                                    "cc_refresh_token": live_refresh,
                                }
                                if live_access:
                                    updates["cc_access_token"] = live_access
                                if live_exp_s:
                                    updates["cc_expires_at"] = live_exp_s
                                db.update_account(account_id, **updates)
                                logger.info(
                                    "Account %d: CC invalid_grant recovered — "
                                    "imported fresh token from live credentials",
                                    account_id,
                                )
                                return True  # Recovered — no need to retry
                            else:
                                # No recovery possible — clear the token
                                logger.warning(
                                    "Account %d: CC invalid_grant — clearing refresh token "
                                    "(no live recovery available)",
                                    account_id,
                                )
                                db.update_account(
                                    account_id,
                                    cc_refresh_token=None,
                                )
                                return False
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add jacked/web/auth.py
git commit -m "feat: CC invalid_grant recovers from live credentials before clearing"
```

---

### Task 4: Add `manual` parameter to `fetch_usage`

**Files:**
- Modify: `jacked/web/auth.py`
- Modify: `jacked/api/routes/auth.py`
- Modify: `tests/unit/test_usage_refresh.py`

- [ ] **Step 1: Add `manual` param to fetch_usage**

In `jacked/web/auth.py`, change the `fetch_usage` signature from:

```python
async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
) -> Optional[dict]:
```

To:

```python
async def fetch_usage(
    account_id: int,
    db: Database,
    access_token: Optional[str] = None,
    manual: bool = False,
) -> Optional[dict]:
```

In the hard ceiling check, wrap it with `if not manual:`:

```python
    # Hard ceiling: never exceed 1 req per _USAGE_RATE_LIMIT_CEILING seconds.
    # manual=True bypasses this (user explicitly asked for fresh data).
    if not manual:
        elapsed = now - state["last_fetched_at"]
        if elapsed < _USAGE_RATE_LIMIT_CEILING:
            logger.debug(
                f"Usage ceiling for account {account_id}: {int(elapsed)}s < "
                f"{_USAGE_RATE_LIMIT_CEILING}s, returning cached"
            )
            return {"_cached": True}
```

Update the docstring to mention `manual`.

- [ ] **Step 2: Pass manual=True from refresh endpoints**

In `jacked/api/routes/auth.py`:
- Single-account refresh (~line 558): add `manual=True`
- Bulk refresh (~line 666): add `manual=True`

- [ ] **Step 3: Update tests**

In `tests/unit/test_usage_refresh.py`, add a test to `TestUsageCeiling`:

```python
    def test_manual_bypasses_ceiling(self):
        """manual=True should bypass the coordinator ceiling."""
        import jacked.web.auth as mod
        mod._account_usage_state.clear()
        state = mod._get_usage_state(1)
        state["last_fetched_at"] = time.time() - 30  # within ceiling

        db = _mock_db({"usage_cached_at": int(time.time()) - 30})
        client = _mock_client(200, {
            "five_hour": {"utilization": 10.0},
            "seven_day": {"utilization": 20.0},
        })
        with patch("jacked.web.auth.httpx.AsyncClient", return_value=client):
            result = asyncio.run(fetch_usage(1, db, manual=True))
        assert result is not None
        assert result != {"_cached": True}
        client.get.assert_called_once()
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auth.py jacked/api/routes/auth.py tests/unit/test_usage_refresh.py
git commit -m "feat: manual=True on fetch_usage bypasses coordinator ceiling"
```

---

### Task 5: Fix `cc_needs_auth` to account for primary fallback

**Files:**
- Modify: `jacked/api/routes/auth.py`

- [ ] **Step 1: Update cc_needs_auth computation**

In `jacked/api/routes/auth.py`, find the `cc_needs_auth` computation (around line 300-304). Change from:

```python
        cc_needs_auth=(
            row.get("cc_access_token") is not None
            and row.get("cc_refresh_token") is None
            and now >= (row.get("cc_expires_at") or 0)
        ),
```

To:

```python
        cc_needs_auth=(
            row.get("cc_access_token") is not None
            and row.get("cc_refresh_token") is None
            and now >= (row.get("cc_expires_at") or 0)
            and not bool(row.get("refresh_token"))  # primary fallback works
        ),
```

This means: only show "needs re-auth" when BOTH the CC token is dead AND the primary token can't save us.

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ --tb=short -q 2>&1 | tail -5`

- [ ] **Step 3: Commit**

```bash
git add jacked/api/routes/auth.py
git commit -m "fix: cc_needs_auth accounts for primary token fallback"
```

---

### Task 6: Run full test suite

- [ ] **Step 1: Run full test suite**

Run: `uv run python -m pytest tests/ --tb=short -q`

Expected: All tests pass.
