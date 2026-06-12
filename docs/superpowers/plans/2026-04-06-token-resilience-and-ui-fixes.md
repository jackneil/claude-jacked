# Token Resilience, Poll Accuracy, and Decision Log Live Updates — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix CC token false re-auth, stuck "checking..." countdown, circuit breaker permanent death, add decision log WebSocket push, and unify token exchange code.

**Architecture:** Mode-based `RefreshMode` enum drives a single `_refresh_token_flow` orchestrator replacing 4 divergent callers. Circuit breaker moves from in-memory to DB columns with scaled cooldowns. Live credential reconciliation runs periodically and on-demand with safety guards against importing single-use refresh tokens. Poll metadata moves from frontend guesswork to backend-provided WebSocket fields.

**Tech Stack:** Python/FastAPI backend, SQLite, vanilla JS frontend, WebSocket

---

## File Map

| File | Role | Tasks |
|------|------|-------|
| `jacked/web/database.py` | DB schema, migration, Pydantic model, `_ACCOUNT_UPDATE_COLS`, `record_decision` | 1 |
| `jacked/web/auth.py` | `RefreshMode`, `_refresh_token_flow`, refactored callers, heal loop, circuit breaker | 2, 3, 4 |
| `jacked/api/credential_helpers.py` | `reconcile_credentials_from_live_store` with cache + safety guard | 5 |
| `jacked/api/routes/auth.py` | On-demand reconciliation, decision log WS push for manual switch | 5, 8 |
| `jacked/api/usage_monitor.py` | Poll metadata in WS broadcast, decision log WS push, poll watchdog | 6, 8 |
| `jacked/web/auto_swap.py` | Normalize active hours defaults | 7 |
| `jacked/data/web/js/components/account-actions.js` | Backend-provided countdown, stale guard | 9 |
| `jacked/data/web/js/websocket.js` | Decision log WS handler, reconnect fetch | 9, 10 |
| `jacked/data/web/js/components/auto-swap.js` | Re-render on decision log WS push | 10 |
| `docs/architecture/auto-swap-system.md` | Full doc update | 11 |
| `tests/unit/test_auto_swap.py` | Active hours default test fixes | 7 |
| `tests/unit/test_token_refresh.py` | New: tests for `_refresh_token_flow` and circuit breaker | 2, 3 |

## Parallelization Groups

Tasks within a group can run in parallel. Groups must be sequential.

- **Group A (independent):** Tasks 1, 7, 10
- **Group B (depends on Task 1):** Tasks 2, 3
- **Group C (depends on Tasks 2-3):** Tasks 4, 5
- **Group D (depends on Task 5):** Task 6
- **Group E (independent of Groups A-D):** Tasks 8, 9
- **Group F (depends on all):** Task 11

---

### Task 1: DB Schema — Circuit Breaker Columns + record_decision Return ID

**Files:**
- Modify: `jacked/web/database.py:38-72` (Pydantic `Account` model)
- Modify: `jacked/web/database.py:641-650` (migration block)
- Modify: `jacked/web/database.py:893-922` (`_ACCOUNT_UPDATE_COLS`)
- Modify: `jacked/web/database.py:2582-2600` (`record_decision`)
- Test: `tests/unit/test_token_refresh.py` (new file)

- [ ] **Step 1: Write failing test — circuit breaker columns exist after migration**

```python
# tests/unit/test_token_refresh.py
"""Tests for token refresh flow and circuit breaker DB persistence."""
import time
import pytest
from jacked.web.database import Database


class TestCircuitBreakerColumns:
    """Verify circuit breaker columns are present and writable."""

    def test_circuit_breaker_columns_exist(self, tmp_path):
        """New DB has refresh_last_failed_at and refresh_failure_type columns."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("cb@test.com", "tok", int(time.time()) + 3600)
        row = db.get_account(acct["id"])
        assert row["refresh_last_failed_at"] is None
        assert row["refresh_failure_type"] is None

    def test_circuit_breaker_columns_writable(self, tmp_path):
        """Can write circuit breaker state via update_account."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("cb@test.com", "tok", int(time.time()) + 3600)
        now = int(time.time())
        db.update_account(acct["id"],
                          refresh_last_failed_at=now,
                          refresh_failure_type="invalid_grant")
        row = db.get_account(acct["id"])
        assert row["refresh_last_failed_at"] == now
        assert row["refresh_failure_type"] == "invalid_grant"

    def test_circuit_breaker_columns_clearable(self, tmp_path):
        """Can clear circuit breaker state to None."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("cb@test.com", "tok", int(time.time()) + 3600)
        db.update_account(acct["id"],
                          refresh_last_failed_at=int(time.time()),
                          refresh_failure_type="network_error")
        db.update_account(acct["id"],
                          refresh_last_failed_at=None,
                          refresh_failure_type=None)
        row = db.get_account(acct["id"])
        assert row["refresh_last_failed_at"] is None
        assert row["refresh_failure_type"] is None


class TestRecordDecisionReturnsId:
    """Verify record_decision returns the inserted row ID."""

    def test_returns_integer_id(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        row_id = db.record_decision(
            account_id=1,
            action="stay",
            trigger="tick",
            reason="test",
        )
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_returns_incrementing_ids(self, tmp_path):
        db = Database(str(tmp_path / "test.db"))
        id1 = db.record_decision(account_id=1, action="stay", trigger="tick")
        id2 = db.record_decision(account_id=1, action="swap", trigger="auto_swap")
        assert id2 > id1
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_token_refresh.py -v`
Expected: FAIL — `refresh_last_failed_at` not in DB, `record_decision` returns None

- [ ] **Step 3: Add circuit breaker fields to Pydantic Account model**

In `jacked/web/database.py`, add two fields to the `Account` class after `cc_expires_at` (line 70):

```python
    cc_expires_at: Optional[int] = None
    refresh_last_failed_at: Optional[int] = None
    refresh_failure_type: Optional[str] = None
    created_at: Optional[str] = None
```

- [ ] **Step 4: Add columns to `_ACCOUNT_UPDATE_COLS` whitelist**

In `jacked/web/database.py`, add to the `_ACCOUNT_UPDATE_COLS` frozenset (after `"organization_uuid"`, line 920):

```python
            "organization_uuid",
            "refresh_last_failed_at",
            "refresh_failure_type",
```

- [ ] **Step 5: Add migration for new columns**

In `jacked/web/database.py`, after the `auto_swap_enabled` migration block (after line 650), add:

```python
            # Migration: add circuit breaker columns to accounts
            cursor = conn.execute("PRAGMA table_info(accounts)")
            acct_cols_cb = {row[1] for row in cursor.fetchall()}
            if "refresh_last_failed_at" not in acct_cols_cb:
                try:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN refresh_last_failed_at INTEGER"
                    )
                except sqlite3.OperationalError:
                    pass
            if "refresh_failure_type" not in acct_cols_cb:
                try:
                    conn.execute(
                        "ALTER TABLE accounts ADD COLUMN refresh_failure_type TEXT"
                    )
                except sqlite3.OperationalError:
                    pass
```

- [ ] **Step 6: Make `record_decision` return `lastrowid`**

In `jacked/web/database.py`, modify `record_decision` (line 2582-2600):

```python
    def record_decision(
        self,
        account_id: int | None,
        action: str,
        trigger: str | None = None,
        target_id: int | None = None,
        reason: str | None = None,
        detail: dict | None = None,
    ) -> int:
        """Record a swap decision. Returns the inserted row ID."""
        import json as _json
        detail_str = _json.dumps(detail) if detail else None
        with self._writer() as conn:
            cursor = conn.execute(
                """INSERT INTO decision_log
                   (account_id, action, trigger, target_id, reason, detail)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (account_id, action, trigger, target_id, reason, detail_str),
            )
            return cursor.lastrowid
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_token_refresh.py -v`
Expected: All 5 tests PASS

- [ ] **Step 8: Run full test suite to catch regressions**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All tests pass (record_decision callers don't use the return value yet)

- [ ] **Step 9: Commit**

```bash
git add jacked/web/database.py tests/unit/test_token_refresh.py
git commit -m "feat: add circuit breaker DB columns + record_decision returns row ID

Add refresh_last_failed_at and refresh_failure_type to accounts table
for DB-persisted circuit breaker. Add to Pydantic Account model and
_ACCOUNT_UPDATE_COLS whitelist. record_decision now returns lastrowid
for WebSocket broadcast."
```

---

### Task 2: RefreshMode Enum + `_refresh_token_flow` Core

**Files:**
- Modify: `jacked/web/auth.py:78-170` (add RefreshMode, cooldown constants, remove _primary_refresh_state)
- Modify: `jacked/web/auth.py` (add `_refresh_token_flow` after `_exchange_refresh_token`)
- Test: `tests/unit/test_token_refresh.py`

This is the core orchestrator. Implement the function with all 10 steps from the spec, then test each mode individually in Tasks 3-4.

- [ ] **Step 1: Write failing test — RefreshMode enum exists and _refresh_token_flow is callable**

Add to `tests/unit/test_token_refresh.py`:

```python
class TestRefreshMode:
    """Verify RefreshMode enum and _refresh_token_flow exist."""

    def test_refresh_mode_enum_values(self):
        from jacked.web.auth import RefreshMode
        assert RefreshMode.PRIMARY.value == "primary"
        assert RefreshMode.CC.value == "cc"
        assert RefreshMode.CC_OR_PRIMARY_429.value == "cc_429"
        assert RefreshMode.PRIMARY_CIRCUIT_BREAKER.value == "primary_cb"

    def test_cooldown_constants_exist(self):
        from jacked.web.auth import CIRCUIT_BREAKER_COOLDOWNS
        assert "invalid_grant" in CIRCUIT_BREAKER_COOLDOWNS
        assert CIRCUIT_BREAKER_COOLDOWNS["invalid_grant"] == 600
        assert CIRCUIT_BREAKER_COOLDOWNS["network_error"] == 60
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_token_refresh.py::TestRefreshMode -v`
Expected: FAIL — `RefreshMode` not defined

- [ ] **Step 3: Implement RefreshMode enum, cooldown constants, delete _primary_refresh_state**

In `jacked/web/auth.py`, after the existing imports (line ~17), add:

```python
from enum import Enum
```

After the `TokenExchangeResult` dataclass (after line 86), add:

```python
class RefreshMode(str, Enum):
    """Token refresh modes — one per caller, behavior hardcoded per mode."""
    PRIMARY = "primary"
    CC = "cc"
    CC_OR_PRIMARY_429 = "cc_429"
    PRIMARY_CIRCUIT_BREAKER = "primary_cb"


# Circuit breaker cooldown by error type (seconds)
CIRCUIT_BREAKER_COOLDOWNS: dict[str, int] = {
    "invalid_grant": 600,
    "network_error": 60,
    "http_429": 120,
    "http_5xx": 120,
}
_DEFAULT_CB_COOLDOWN = 300
```

Delete `_primary_refresh_state` dict and `_get_primary_refresh_state` function (lines 160-169).

- [ ] **Step 4: Implement `_refresh_token_flow`**

After the `_exchange_refresh_token` function (after line 141), add the full orchestrator. This is a large function — implement all 10 steps per spec section 1b. Key implementation details:

1. Resolve token: PRIMARY modes use `refresh_token`, CC modes use `cc_refresh_token`, CC_OR_PRIMARY_429 tries CC then primary
2. Acquire lock: use existing `_get_refresh_lock` for PRIMARY modes, `_get_cc_refresh_lock` for CC modes. CC_OR_PRIMARY_429 acquires CC lock first, then cross-process lock inside
3. Re-read under lock to detect stale token
4. Circuit breaker check (PRIMARY_CIRCUIT_BREAKER only): read `refresh_last_failed_at`/`refresh_failure_type` from DB, compute cooldown by error type
5. Call `_exchange_refresh_token`
6. On success: single `update_account` with token columns + `refresh_last_failed_at=None, refresh_failure_type=None`. DB retry 3x. Then optional credential store write + fetch_profile.
7. On `invalid_grant`: CC/CC_429 modes attempt live access-token-only recovery (NOT refresh token). Single atomic DB write with cleared cc_refresh_token + circuit breaker set.
8. On other errors: single atomic DB write with circuit breaker set.
9. Return `TokenExchangeResult`

The function signature:

```python
async def _refresh_token_flow(
    account_id: int,
    db: Database,
    mode: RefreshMode,
) -> TokenExchangeResult:
```

All logging per the Observability Contract in the spec.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_token_refresh.py -v`
Expected: PASS

- [ ] **Step 6: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass (existing callers unchanged yet)

- [ ] **Step 7: Commit**

```bash
git add jacked/web/auth.py tests/unit/test_token_refresh.py
git commit -m "feat: add RefreshMode enum and _refresh_token_flow orchestrator

Single mid-level orchestrator for all token exchange paths. DB-persisted
circuit breaker with scaled cooldowns by error type. Atomic DB writes for
token + circuit breaker state. Live credential recovery imports access
token only (never cc_refresh_token) per safety rule."
```

---

### Task 3: Refactor 4 Callers to Use `_refresh_token_flow`

**Files:**
- Modify: `jacked/web/auth.py:252-703` (all 4 caller functions)
- Test: `tests/unit/test_token_refresh.py`

- [ ] **Step 1: Write failing tests for caller behavior**

Add to `tests/unit/test_token_refresh.py`:

```python
class TestRefreshAccountToken:
    """refresh_account_token uses PRIMARY mode."""

    @pytest.mark.asyncio
    async def test_marks_invalid_after_two_consecutive_401s(self, tmp_path):
        """401/403 on token exchange should NOT mark invalid on first failure."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("test@t.com", "tok", int(time.time()) - 100,
                                 refresh_token="rt-test")
        from jacked.web.auth import refresh_account_token
        from unittest.mock import patch, AsyncMock

        mock_result = TokenExchangeResult(
            success=False, error="http_401", status_code=401)
        with patch("jacked.web.auth._exchange_refresh_token",
                   new_callable=AsyncMock, return_value=mock_result):
            await refresh_account_token(acct["id"], db)

        row = db.get_account(acct["id"])
        # First failure should NOT mark invalid
        assert row["validation_status"] != "invalid"
        assert row["refresh_last_failed_at"] is not None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_token_refresh.py::TestRefreshAccountToken -v`
Expected: FAIL (current code marks invalid immediately)

- [ ] **Step 3: Refactor `refresh_account_token` to use `_refresh_token_flow`**

Replace the body of `refresh_account_token` (lines 381-500). Keep the function signature. The new body:

```python
async def refresh_account_token(account_id: int, db: Database) -> bool:
    account = db.get_account(account_id)
    if not account:
        return False
    if not should_refresh(account):
        return True
    if not account.get("refresh_token"):
        return True

    result = await _refresh_token_flow(account_id, db, RefreshMode.PRIMARY)

    if result.success:
        return True

    # Caller-specific error policy: mark invalid only after 2 consecutive failures
    if result.status_code in (401, 403):
        if account.get("refresh_failure_type") in ("http_401", "http_403"):
            # Second consecutive auth failure — mark invalid
            db.update_account(
                account_id,
                validation_status="invalid",
                last_error=f"Token revoked (HTTP {result.status_code})",
                last_error_at=datetime.now(timezone.utc).isoformat(),
            )
        # First failure already recorded by _refresh_token_flow
        return False

    if result.error == "network_error":
        db.record_account_error(
            account_id, "Network error during token refresh",
            increment_failures=False)
        return False

    return False
```

- [ ] **Step 4: Refactor `refresh_cc_token` to use `_refresh_token_flow`**

Replace body of `refresh_cc_token` (lines 252-378):

```python
async def refresh_cc_token(account_id: int, db: Database) -> bool:
    account = db.get_account(account_id)
    if not account:
        return False
    if not should_refresh_cc(account):
        return True
    if not account.get("cc_refresh_token"):
        return True

    result = await _refresh_token_flow(account_id, db, RefreshMode.CC)
    return result.success
```

- [ ] **Step 5: Refactor `_try_refresh_on_429` to use `_refresh_token_flow`**

Replace body of `_try_refresh_on_429` (lines 503-620):

```python
async def _try_refresh_on_429(
    account_id: int, db: Database, state: dict,
) -> str | None:
    result = await _refresh_token_flow(account_id, db, RefreshMode.CC_OR_PRIMARY_429)
    return result.access_token if result.success else None
```

- [ ] **Step 6: Refactor `_try_refresh_primary_token` to use `_refresh_token_flow`**

Replace body of `_try_refresh_primary_token` (lines 623-703):

```python
async def _try_refresh_primary_token(
    account_id: int,
    db,
    stale_token: str | None = None,
) -> str | None:
    result = await _refresh_token_flow(account_id, db, RefreshMode.PRIMARY_CIRCUIT_BREAKER)
    return result.access_token if result.success else None
```

Note: the `stale_token` parameter is now handled inside `_refresh_token_flow` (step 3 re-reads under lock). The caller no longer needs to pass it — the flow reads from DB.

- [ ] **Step 7: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add jacked/web/auth.py tests/unit/test_token_refresh.py
git commit -m "refactor: replace 4 token refresh callers with _refresh_token_flow

refresh_account_token, refresh_cc_token, _try_refresh_on_429, and
_try_refresh_primary_token now delegate to _refresh_token_flow with
their respective RefreshMode. Inline POST logic eliminated. 401/403
now requires 2 consecutive failures before marking invalid."
```

---

### Task 4: Heal Loop Fix

**Files:**
- Modify: `jacked/web/auth.py:1109-1167` (`heal_invalid_accounts`)
- Test: `tests/unit/test_token_refresh.py`

- [ ] **Step 1: Write failing test — heal loop clears circuit breaker and skips should_refresh gate**

```python
class TestHealLoop:

    @pytest.mark.asyncio
    async def test_heal_clears_circuit_breaker(self, tmp_path):
        """heal_invalid_accounts should clear CB state before attempting recovery."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("heal@test.com", "tok", int(time.time()) + 3600,
                                 refresh_token="rt-test")
        # Mark invalid with circuit breaker set
        db.update_account(acct["id"],
                          validation_status="invalid",
                          refresh_last_failed_at=int(time.time()),
                          refresh_failure_type="invalid_grant")

        from jacked.web.auth import heal_invalid_accounts
        from unittest.mock import patch, AsyncMock

        with patch("jacked.web.auth.refresh_account_token",
                   new_callable=AsyncMock, return_value=True):
            result = await heal_invalid_accounts()

        assert result["healed"] == 1
        row = db.get_account(acct["id"])
        assert row["refresh_last_failed_at"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_token_refresh.py::TestHealLoop -v`
Expected: FAIL — heal loop doesn't clear circuit breaker

- [ ] **Step 3: Fix `heal_invalid_accounts`**

Modify the function (auth.py ~line 1109). Key changes:
1. Clear circuit breaker state under per-account lock before recovery
2. Drop `should_refresh()` gate — always attempt refresh in healing mode
3. Call `reconcile_credentials_from_live_store` before `validate_account` (will be available after Task 5 — for now, wrap in try/except ImportError)

```python
async def heal_invalid_accounts() -> dict:
    db = Database()
    result = {"checked": 0, "healed": 0, "confirmed_invalid": 0}

    accounts = db.list_accounts(include_inactive=True)
    for account in accounts:
        if account.get("is_deleted"):
            continue
        status = account.get("validation_status", "valid")
        if status not in ("invalid", "unknown"):
            continue

        result["checked"] += 1
        account_id = account["id"]

        # Clear circuit breaker under lock before attempting recovery
        lock = _get_refresh_lock(account_id)
        async with lock:
            logger.info("Account %d: clearing circuit breaker for heal attempt", account_id)
            db.update_account(account_id,
                              refresh_last_failed_at=None,
                              refresh_failure_type=None)

        # Always attempt refresh if refresh_token exists (healing mode — skip should_refresh gate)
        healed = False
        if account.get("refresh_token"):
            async with lock:
                success = await refresh_account_token(account_id, db)
                if success:
                    healed = True

        if not healed:
            # Try reconciling from live credentials before validating
            try:
                from jacked.api.credential_helpers import reconcile_credentials_from_live_store
                reconcile_credentials_from_live_store(account_id, db)
            except (ImportError, Exception):
                pass

            validation = await validate_account(account_id, db)
            healed = validation.get("valid", False)

        if healed:
            result["healed"] += 1
        else:
            result["confirmed_invalid"] += 1

    if result["checked"] > 0:
        logger.info(
            "Heal sweep: checked=%d, healed=%d, confirmed_invalid=%d",
            result["checked"], result["healed"], result["confirmed_invalid"],
        )
    return result
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 5: Commit**

```bash
git add jacked/web/auth.py tests/unit/test_token_refresh.py
git commit -m "fix: heal loop clears circuit breaker and skips should_refresh gate

Heal loop now clears DB circuit breaker state under per-account lock
before attempting recovery. Drops should_refresh() gate — healing is
recovery mode that always attempts refresh if refresh_token exists."
```

---

### Task 5: Live Credential Reconciliation — Rename, Cache, Safety Guard

**Files:**
- Modify: `jacked/api/credential_helpers.py:299-377` (rename + guard)
- Modify: `jacked/api/routes/auth.py:300-305` (on-demand reconciliation)
- Modify: `jacked/api/usage_monitor.py:242-243` (update call site in `_execute_swap`)
- Test: `tests/unit/test_token_refresh.py`

- [ ] **Step 1: Write failing test — reconciliation skips cc_refresh_token during invalid_grant**

```python
class TestReconciliationSafetyGuard:

    def test_skips_cc_refresh_token_when_invalid_grant(self, tmp_path):
        """reconcile_credentials_from_live_store must NOT import cc_refresh_token
        when circuit breaker failure type is invalid_grant."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("recon@test.com", "tok", int(time.time()) + 3600)
        # Set circuit breaker to invalid_grant
        db.update_account(acct["id"],
                          refresh_last_failed_at=int(time.time()),
                          refresh_failure_type="invalid_grant",
                          cc_access_token="old-access",
                          cc_refresh_token=None)

        from jacked.api.credential_helpers import reconcile_credentials_from_live_store
        from unittest.mock import patch

        live_creds = {
            "_jackedAccountId": acct["id"],
            "claudeAiOauth": {
                "accessToken": "new-access-from-cc",
                "refreshToken": "dangerous-refresh-token",
                "expiresAt": (int(time.time()) + 3600) * 1000,
            }
        }
        with patch("jacked.api.credential_helpers.read_platform_credentials",
                   return_value=live_creds):
            reconcile_credentials_from_live_store(acct["id"], db)

        row = db.get_account(acct["id"])
        # Access token should be imported
        assert row["cc_access_token"] == "new-access-from-cc"
        # Refresh token must NOT be imported during invalid_grant
        assert row["cc_refresh_token"] is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run python -m pytest tests/unit/test_token_refresh.py::TestReconciliationSafetyGuard -v`
Expected: FAIL — function doesn't exist yet with new name

- [ ] **Step 3: Rename and modify reconciliation function**

In `jacked/api/credential_helpers.py`, rename `reconcile_outgoing_credentials` → `reconcile_credentials_from_live_store`. Add the `invalid_grant` safety guard for `cc_refresh_token`. Add 30-second cache.

Key changes to the function:
1. Read `refresh_failure_type` from account DB row
2. If `refresh_failure_type == "invalid_grant"`, skip `cc_refresh_token` import but still import `cc_access_token` and `cc_expires_at`
3. Add module-level cache: `_live_cred_cache = {"account_id": None, "data": None, "expires_at": 0}`
4. Log per observability contract when skipping refresh token import

Also add a backward-compatible alias:
```python
reconcile_outgoing_credentials = reconcile_credentials_from_live_store
```

- [ ] **Step 4: Update call sites**

Update `_execute_swap` in `jacked/api/usage_monitor.py` line 243:
```python
reconcile_credentials_from_live_store(active_acct_id, db)
```

Update import in `jacked/api/usage_monitor.py` line 215:
```python
from jacked.api.credential_helpers import (
    acquire_claude_lock,
    reconcile_credentials_from_live_store,
    sync_credential_to_all_stores,
)
```

Update import in `jacked/api/routes/auth.py` wherever `reconcile_outgoing_credentials` is imported.

- [ ] **Step 5: Add on-demand reconciliation before `cc_needs_auth` computation**

In `jacked/api/routes/auth.py`, before the `AccountResponse(...)` constructor at line ~270, add:

```python
# On-demand credential reconciliation for active account
if _is_active_account(row["id"]) and (
    row.get("cc_refresh_token") is None
    or (row.get("cc_expires_at") or 0) < now
):
    from jacked.api.credential_helpers import reconcile_credentials_from_live_store
    reconcile_credentials_from_live_store(row["id"], db)
    row = db.get_account(row["id"])  # Re-read after reconciliation
```

Add a helper function `_is_active_account(account_id)` that reads `_jackedAccountId` from the credential file (cached).

- [ ] **Step 6: Add periodic reconciliation to `refresh_all_expiring_tokens`**

In `jacked/web/auth.py`, at the start of `refresh_all_expiring_tokens` (after reading accounts list), add:

```python
# Reconcile active account credentials from live store
from jacked.api.credential_helpers import reconcile_credentials_from_live_store
active_id = _read_active_account_id_from_creds()
if active_id:
    try:
        reconcile_credentials_from_live_store(active_id, db)
    except Exception:
        logger.debug("Periodic credential reconciliation failed", exc_info=True)
```

- [ ] **Step 7: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 8: Commit**

```bash
git add jacked/api/credential_helpers.py jacked/api/routes/auth.py \
       jacked/api/usage_monitor.py jacked/web/auth.py \
       tests/unit/test_token_refresh.py
git commit -m "feat: live credential reconciliation with invalid_grant safety guard

Rename reconcile_outgoing_credentials → reconcile_credentials_from_live_store.
Never import cc_refresh_token when circuit breaker shows invalid_grant.
Add 30s cache for live credential reads. Add on-demand reconciliation
in account list API for active account. Add periodic reconciliation
in refresh_all_expiring_tokens."
```

---

### Task 6: fetch_usage 401 — Live Credential Import Before Marking Invalid

**Files:**
- Modify: `jacked/web/auth.py:783-808` (fetch_usage 401 handler)
- Test: `tests/unit/test_token_refresh.py`

- [ ] **Step 1: Write failing test — fetch_usage tries live credential import before marking invalid**

```python
class TestFetchUsage401:

    @pytest.mark.asyncio
    async def test_tries_live_cred_import_before_marking_invalid(self, tmp_path):
        """fetch_usage should attempt live credential import for active account
        before marking invalid on 401."""
        db = Database(str(tmp_path / "test.db"))
        acct = db.create_account("usage@test.com", "old-tok",
                                 int(time.time()) + 3600,
                                 refresh_token="rt-test")

        from jacked.web.auth import fetch_usage
        from unittest.mock import patch, AsyncMock, MagicMock

        # Mock: 401 response, then refresh fails, but live creds have fresh token
        mock_resp = MagicMock()
        mock_resp.status_code = 401

        with patch("jacked.web.auth._try_refresh_primary_token",
                   new_callable=AsyncMock, return_value=None), \
             patch("jacked.api.credential_helpers.reconcile_credentials_from_live_store") as mock_recon, \
             patch("httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__ = AsyncMock(return_value=MagicMock(
                get=AsyncMock(return_value=mock_resp)))
            mock_client.return_value.__aexit__ = AsyncMock(return_value=False)

            await fetch_usage(acct["id"], db, manual=True)

        # Should have attempted reconciliation
        mock_recon.assert_called_once()
```

- [ ] **Step 2: Implement the fix**

In `fetch_usage` (auth.py ~line 796), before marking invalid, try live credential import:

```python
            if resp.status_code in (401, 403):
                if _retry_depth < 1:
                    fresh = await _try_refresh_primary_token(
                        account_id, db, stale_token=token,
                    )
                    if fresh:
                        state["last_fetched_at"] = 0
                        return await fetch_usage(
                            account_id, db, access_token=fresh,
                            _retry_depth=_retry_depth + 1,
                        )

                    # Refresh failed — try live credential import for active account
                    try:
                        from jacked.api.credential_helpers import reconcile_credentials_from_live_store
                        reconcile_credentials_from_live_store(account_id, db)
                        refreshed_acct = db.get_account(account_id)
                        live_token = refreshed_acct.get("access_token") if refreshed_acct else None
                        if live_token and live_token != token:
                            state["last_fetched_at"] = 0
                            return await fetch_usage(
                                account_id, db, access_token=live_token,
                                _retry_depth=_retry_depth + 1,
                            )
                    except Exception:
                        logger.debug("Live credential import failed during 401 recovery", exc_info=True)

                # Both refresh and live import failed — mark invalid
                error_msg = f"Usage fetch failed (HTTP {resp.status_code}) — token refresh failed"
                db.update_account(...)  # existing invalid-marking code
```

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add jacked/web/auth.py tests/unit/test_token_refresh.py
git commit -m "fix: fetch_usage tries live credential import before marking invalid

On 401, after refresh fails, attempt reconcile_credentials_from_live_store
to import fresh access token from Claude Code's live credentials. Only
mark invalid after both refresh and live import fail."
```

---

### Task 7: Active Hours Default Normalization

**Files:**
- Modify: `jacked/web/auto_swap.py` (5 function signatures)
- Modify: `tests/unit/test_auto_swap.py` (update test defaults)

This task is independent — can run in parallel with Tasks 1-6.

- [ ] **Step 1: Update function defaults in auto_swap.py**

Change `active_start="07:00"` → `"06:00"` and `active_end="22:00"` → `"23:00"` in:

1. `compute_effective_working_hours` (line 130-131)
2. `compute_7d_deficit` (line 242-243)
3. `should_swap` (line 328-329)
4. `score_candidate` (line 415-416)
5. `pick_best_target` (line 512-513)

- [ ] **Step 2: Update tests that hardcode old defaults**

Search `tests/unit/test_auto_swap.py` for `active_start="07:00"` or `active_end="22:00"` and update to `"06:00"` / `"23:00"`. Also search for tests that rely on the old default behavior without explicitly passing the parameter — these will now compute different values.

Run: `uv run python -m pytest tests/unit/test_auto_swap.py -v`

Fix any failures by updating expected values or explicitly passing the old defaults for tests that are testing specific hour calculations.

- [ ] **Step 3: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add jacked/web/auto_swap.py tests/unit/test_auto_swap.py
git commit -m "fix: normalize active hours defaults to 06:00-23:00

compute_effective_working_hours, compute_7d_deficit, should_swap,
score_candidate, and pick_best_target all had 07:00-22:00 defaults
while compute_burn_per_window and compute_urgency_threshold used
06:00-23:00. Now consistent across all functions."
```

---

### Task 8: Decision Log WebSocket Push

**Files:**
- Modify: `jacked/api/usage_monitor.py:900-926` (add WS broadcast after record_decision)
- Modify: `jacked/api/routes/auth.py:887-908` (add WS broadcast for manual switch)

- [ ] **Step 1: Add WS broadcast after auto-swap decision recording**

In `jacked/api/usage_monitor.py`, after the `db.record_decision(...)` call at ~line 913, add:

```python
                    decision_id = db.record_decision(
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger=(
                            ("proactive_7d" if _proactive_target_id else "auto_swap")
                            if _decision_action == "swap"
                            else "tick"
                        ),
                        target_id=_decision_target_id,
                        reason=_decision_reason or "no trigger",
                        detail=_tick_detail,
                    )
                    # Broadcast decision to connected dashboards
                    if ws_registry and decision_id:
                        try:
                            await ws_registry.broadcast(
                                "decision_log_entry",
                                {
                                    "id": decision_id,
                                    "account_id": active_acct_id,
                                    "email": active_acct.get("email", ""),
                                    "label": format_account_label(active_acct),
                                    "action": _decision_action,
                                    "trigger": (
                                        ("proactive_7d" if _proactive_target_id else "auto_swap")
                                        if _decision_action == "swap"
                                        else "tick"
                                    ),
                                    "reason": _decision_reason or "no trigger",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "detail": _tick_detail,
                                },
                            )
                        except Exception:
                            logger.debug("Decision log WS broadcast failed", exc_info=True)
```

Note: `ws_registry` is already available in the poll loop (line 416). The `format_account_label` is already imported (line 361).

- [ ] **Step 2: Add WS broadcast for manual switch in routes/auth.py**

In `jacked/api/routes/auth.py`, after the `db.record_decision(...)` call at ~line 888, add similar broadcast. Get `ws_registry` from `request.app.state`.

- [ ] **Step 3: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 4: Commit**

```bash
git add jacked/api/usage_monitor.py jacked/api/routes/auth.py
git commit -m "feat: broadcast decision_log_entry via WebSocket

New WS event for real-time decision log updates. Broadcasts after both
auto-swap tick decisions and manual account switches. Includes account
email/label for self-describing entries."
```

---

### Task 9: Poll Countdown Fix — Backend + Frontend

**Files:**
- Modify: `jacked/api/usage_monitor.py:392-443,943-947` (move poll interval computation, add to WS payload)
- Modify: `jacked/data/web/js/components/account-actions.js:10-34` (use backend values)

- [ ] **Step 1: Move `_compute_poll_interval` before broadcast in poll loop**

In `jacked/api/usage_monitor.py`, move the `_compute_poll_interval` call from line 943 to BEFORE the WS broadcast at line 416. Then inject the values into the broadcast payload:

After `safe_acct` is built (line 435), add:

```python
                safe_acct["_poll_interval"] = int(_poll_interval)
                safe_acct["_poll_tier"] = _poll_tier
                safe_acct["_last_poll_at"] = int(time.time())
```

At the bottom of the loop (line 943), the interval is still needed for sleep:

```python
        # Already computed before broadcast — just sleep
        logger.debug("Active poll: tier=%s interval=%.0fs", _poll_tier, _poll_interval)
        await asyncio.sleep(_poll_interval)
```

- [ ] **Step 2: Add poll loop watchdog**

At the top of `active_account_poll_loop`, add watchdog tracking:

```python
    _last_tick_at = 0.0
```

Inside the loop, after the tick completes, check:

```python
            now_tick = time.time()
            if _last_tick_at > 0 and (now_tick - _last_tick_at) > 2 * _poll_interval:
                logger.warning(
                    "Active poll loop delayed — last tick %ds ago, expected interval %ds",
                    int(now_tick - _last_tick_at), int(_poll_interval),
                )
            _last_tick_at = now_tick
```

- [ ] **Step 3: Update frontend countdown to use backend values**

In `jacked/data/web/js/components/account-actions.js`, replace lines 23-31:

```javascript
        var pollInterval = activeAcct._poll_interval || 300;
        var lastPollAt = activeAcct._last_poll_at || cachedAt;
        if (!lastPollAt) {
            // No poll data yet (restart/reconnect) — show "starting..."
            els.forEach(function(el) {
                el.textContent = 'starting\u2026';
            });
            return;
        }
        var rem = Math.max(0, pollInterval - (now - lastPollAt));
        var tierLabel = activeAcct._poll_tier ? ' (' + activeAcct._poll_tier + ')' : '';
        els.forEach(function(el) {
            if (rem > 0) {
                el.textContent = rem + 's' + tierLabel;
            } else if (lastPollAt && (now - lastPollAt) > 2 * pollInterval) {
                el.textContent = 'delayed';
            } else {
                el.textContent = 'checking\u2026';
            }
            el.setAttribute('data-cached-at', String(lastPollAt));
        });
```

- [ ] **Step 4: Add state fetch on WS reconnect**

In `jacked/data/web/js/websocket.js`, in the `open` handler, add a fetch to get fresh state:

```javascript
jackedWS.on('open', () => {
    // ... existing open handler code ...
    // Fetch fresh account data on reconnect so countdown has _last_poll_at
    if (typeof refreshAndRender === 'function') refreshAndRender();
});
```

- [ ] **Step 5: Test manually**

Start jacked webux, observe the countdown. It should now show the correct tier label and not get stuck on "checking...".

- [ ] **Step 6: Run backend tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: All pass

- [ ] **Step 7: Commit**

```bash
git add jacked/api/usage_monitor.py \
       jacked/data/web/js/components/account-actions.js \
       jacked/data/web/js/websocket.js
git commit -m "fix: poll countdown uses backend-provided interval instead of guessing

Move _compute_poll_interval before WS broadcast. Include _poll_interval,
_poll_tier, _last_poll_at in usage_poll_updated payload. Frontend now
counts down from backend's actual interval. Shows 'delayed' when poll
loop is stuck, 'starting...' on reconnect. Add poll loop watchdog."
```

---

### Task 10: Decision Log Frontend — WS Handler + Re-render

**Files:**
- Modify: `jacked/data/web/js/websocket.js` (add `decision_log_entry` handler)
- Modify: `jacked/data/web/js/components/auto-swap.js` (no changes needed — `renderDecisionLog` already exists)

- [ ] **Step 1: Add decision_log_entry WS handler**

In `jacked/data/web/js/websocket.js`, after the existing event handlers (after `usage_poll_updated`), add:

```javascript
jackedWS.on('decision_log_entry', (msg) => {
    const container = document.getElementById('decision-log-container');
    if (container) {
        renderDecisionLog('decision-log-container');
    }
});
```

- [ ] **Step 2: Test with browser**

Open the dashboard to the accounts page with decision log visible. Trigger an auto-swap or manual switch. The decision log should update in real-time without toggling the filter.

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/websocket.js
git commit -m "feat: decision log updates in real-time via WebSocket

New decision_log_entry WS handler re-renders the decision log table
when a new decision arrives. No manual filter toggle needed."
```

---

### Task 11: Architecture Doc Update

**Files:**
- Modify: `docs/architecture/auto-swap-system.md`

- [ ] **Step 1: Update the architecture doc**

Update all sections per spec section 8. Key additions:
- 401 auto-refresh system with DB-persisted circuit breaker
- Circuit breaker cooldown table (scaled by error type)
- Live credential reconciliation (periodic + on-demand)
- Safety rule: never import cc_refresh_token during invalid_grant
- Decision log WebSocket push
- `auto_swap_enabled` flag documentation
- Skip reason values: `near_exhaustion`, `recoverable_too_low`, `ahead_of_schedule`, `below_threshold`
- Urgency score formula as first-class definition
- 429 backoff sequence: 65s → 130s → 260s → 520s → cap 900s
- Normalized active hours defaults (06:00-23:00)
- Window keeper execution context (in `full_sweep_loop`)
- `_WS_SAFE_FIELDS` maintenance note
- Poll metadata in WS payload (`_poll_interval`, `_poll_tier`, `_last_poll_at`)
- Update "Last updated" date to 2026-04-06

- [ ] **Step 2: Commit**

```bash
git add docs/architecture/auto-swap-system.md
git commit -m "docs: update auto-swap architecture for token resilience changes

Add 401 auto-refresh, DB circuit breaker, live credential reconciliation,
decision log WS push, poll metadata, normalized active hours, and all
skip reason documentation."
```

---

## Self-Review Checklist

1. **Spec coverage:**
   - Spec §1 (token exchange) → Tasks 2, 3
   - Spec §2 (circuit breaker to DB) → Tasks 1, 2
   - Spec §3 (credential reconciliation) → Tasks 5, 6
   - Spec §3d (heal loop) → Task 4
   - Spec §4 (poll countdown) → Task 9
   - Spec §5 (active hours) → Task 7
   - Spec §6 (decision log WS) → Tasks 8, 10
   - Spec §7 (decision log QA) → Not in plan (browser QA done via /qa skill after implementation)
   - Spec §8 (arch doc) → Task 11
   - Observability contract → Covered in Task 2 (built into _refresh_token_flow)

2. **Placeholder scan:** No TBDs or TODOs. All code steps have code blocks. All test steps have test code or explicit commands.

3. **Type consistency:** `RefreshMode` enum used consistently. `_refresh_token_flow` signature matches across tasks. `reconcile_credentials_from_live_store` name consistent. `TokenExchangeResult` referenced in callers matches existing dataclass.
