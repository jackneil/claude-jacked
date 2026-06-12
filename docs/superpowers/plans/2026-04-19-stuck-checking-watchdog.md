# Stuck-Checking Watchdog + Sweep Resilience — 0.41.23 (v3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the "stuck validation_status='checking'" failure mode observed in prod 2026-04-19. Cover TOCTOU race, orphan-task clobber, `wait_for`-leaves-'checking'-stranded, heartbeat-dead-by-default, NULL `updated_at`, and keep the test approach consistent with the project (no `pytest-asyncio`).

**Architecture:** Atomic WHERE-guarded DB UPDATEs for all watchdog writes. Per-account `asyncio.wait_for` with explicit status reset at the same call site (not relying on the watchdog for cleanup). Ownership-checked task-slot cleanup. All async tests use `asyncio.run()` wrappers — matches the existing project pattern (verified in `tests/unit/test_usage_monitor.py:4` — "asyncio.run() for async tests (no pytest-asyncio dependency)").

**Tech Stack:** Python 3.10+, asyncio, sqlite (via `jacked/web/database.py`), FastAPI lifespan tasks, pytest (no `pytest-asyncio`).

**Spec:** `docs/superpowers/specs/2026-04-19-stuck-checking-watchdog-design.md` (updated in Task 7).

---

## File Structure

| File | Role |
| --- | --- |
| `jacked/web/database.py` | Add `list_stuck_checking_accounts()` + `reset_stuck_checking()` — atomic WHERE-guarded UPDATE with NULL-safe `updated_at` handling |
| `jacked/web/auth.py` | Add `reset_stale_checking_accounts()` wrapper; clear `last_error` in validator success paths |
| `jacked/api/routes/auth.py` | Add missing `from datetime import datetime, timezone` import; per-account `asyncio.wait_for`; explicit status reset on timeout; track + cancel orphan task with ownership-checked cleanup |
| `jacked/api/main.py` | Register watchdog task in `lifespan()`, append to `tasks_to_cancel` list |
| `jacked/api/usage_monitor.py` | Sweep heartbeat at TOP of iteration; `asyncio.wait_for` wrap on `fetch_usage` at line 1128 |
| `jacked/__init__.py` | Version bump to `0.41.23` |
| `README.md` | Changelog entry |
| `docs/superpowers/specs/2026-04-19-stuck-checking-watchdog-design.md` | Fix diagnosis #3, scope sweep's fetch_usage, document `last_error` clearing |
| `tests/unit/api/__init__.py` | **NEW** — package init for pytest discovery |
| `tests/unit/test_stuck_checking_watchdog.py` | **NEW** — DB method tests + wrapper tests, `asyncio.run()` pattern |
| `tests/unit/api/test_bulk_refresh_timeout.py` | **NEW** — per-account timeout, explicit status write, orphan cancel, end-to-end finally-ownership test |
| `tests/unit/api/test_sweep_heartbeat.py` | **NEW** — heartbeat fires when disabled |

---

## Task 0: Create `tests/unit/api/__init__.py`

**Files:**
- Create: `tests/unit/api/__init__.py`

- [ ] **Step 1: Create empty package file**

Run: `: > tests/unit/api/__init__.py`

- [ ] **Step 2: Verify pytest discovers the path**

Run: `uv run python -m pytest tests/unit/api/ --collect-only 2>&1 | tail -5`
Expected: "no tests ran" (no error).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/api/__init__.py
git commit -m "chore(tests): add tests/unit/api package init"
```

---

## Task 1: DB methods — atomic conditional UPDATE with NULL-safe `updated_at`

**Files:**
- Modify: `jacked/web/database.py` (append after `clear_account_errors` ~line 1220)
- Test: `tests/unit/test_stuck_checking_watchdog.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/test_stuck_checking_watchdog.py`:

```python
"""Tests for stuck-checking DB methods + async wrapper.

Uses a real in-memory Database so the SQL WHERE guard runs; uses
asyncio.run() wrappers (project convention — no pytest-asyncio)."""
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, AsyncMock

import pytest

from jacked.web.database import Database


@pytest.fixture
def db():
    d = Database(":memory:")
    yield d
    d.close()


def _age_account(db, account_id, seconds_ago):
    """Rewrite updated_at to a specific past timestamp."""
    backdated = (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()
    with db._writer() as conn:
        conn.execute("UPDATE accounts SET updated_at = ? WHERE id = ?",
                     (backdated, account_id))


def _null_updated_at(db, account_id):
    with db._writer() as conn:
        conn.execute("UPDATE accounts SET updated_at = NULL WHERE id = ?",
                     (account_id,))


class TestListStuckCheckingAccounts:
    def test_returns_only_stale_checking_rows(self, db):
        a = db.create_account("a@t.com", "tok", 9999999999)
        b = db.create_account("b@t.com", "tok", 9999999999)
        c = db.create_account("c@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        db.update_account(b["id"], validation_status="checking")
        db.update_account(c["id"], validation_status="valid")
        _age_account(db, a["id"], 200)
        _age_account(db, b["id"], 30)
        _age_account(db, c["id"], 9999)

        stuck = db.list_stuck_checking_accounts(120)
        assert [r["id"] for r in stuck] == [a["id"]]

    def test_includes_inactive(self, db):
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking", is_active=False)
        _age_account(db, a["id"], 200)
        assert len(db.list_stuck_checking_accounts(120)) == 1

    def test_excludes_deleted(self, db):
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        _age_account(db, a["id"], 200)
        with db._writer() as conn:
            conn.execute("UPDATE accounts SET is_deleted = 1 WHERE id = ?",
                         (a["id"],))
        assert db.list_stuck_checking_accounts(120) == []

    def test_threshold_boundary_exclusive(self, db):
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        _age_account(db, a["id"], 119)
        assert db.list_stuck_checking_accounts(120) == []
        _age_account(db, a["id"], 121)
        assert len(db.list_stuck_checking_accounts(120)) == 1

    def test_null_updated_at_treated_as_stale(self, db):
        """NULL updated_at → treat as extremely stale (definitely stuck).
        Matches /dc round-2 finding: strftime('%s', NULL) returns NULL
        which fails comparison and would otherwise hide the row forever."""
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        _null_updated_at(db, a["id"])
        stuck = db.list_stuck_checking_accounts(120)
        assert len(stuck) == 1
        assert stuck[0]["id"] == a["id"]


class TestResetStuckChecking:
    def test_resets_stuck_checking_atomically(self, db):
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        _age_account(db, a["id"], 200)
        count = db.reset_stuck_checking(a["id"], 120, "watchdog test")
        assert count == 1
        row = db.get_account(a["id"])
        assert row["validation_status"] == "unknown"
        assert "watchdog test" in row["last_error"]
        assert row["last_error_at"] is not None

    def test_refuses_to_clobber_racing_valid_write(self, db):
        """WHERE guard: validator beat watchdog to move row to 'valid'.
        UPDATE must be a no-op — PM1 race fix."""
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="valid")
        _age_account(db, a["id"], 200)
        count = db.reset_stuck_checking(a["id"], 120, "should not fire")
        assert count == 0
        row = db.get_account(a["id"])
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None  # not clobbered

    def test_refuses_if_fresh(self, db):
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        _age_account(db, a["id"], 30)
        count = db.reset_stuck_checking(a["id"], 120, "should not fire")
        assert count == 0
        assert db.get_account(a["id"])["validation_status"] == "checking"

    def test_null_updated_at_is_reset(self, db):
        """NULL updated_at means "definitely stuck" — must be reset."""
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        _null_updated_at(db, a["id"])
        count = db.reset_stuck_checking(a["id"], 120, "null-case")
        assert count == 1
        assert db.get_account(a["id"])["validation_status"] == "unknown"

    def test_zero_rowcount_when_account_missing(self, db):
        assert db.reset_stuck_checking(99999, 120, "x") == 0
```

- [ ] **Step 2: Run — expect AttributeError**

Run: `uv run python -m pytest tests/unit/test_stuck_checking_watchdog.py -v 2>&1 | tail -20`
Expected: AttributeError — `list_stuck_checking_accounts` / `reset_stuck_checking` don't exist.

- [ ] **Step 3: Verify `datetime` import at top of `database.py`**

Run: `grep -n "^from datetime" jacked/web/database.py`
Expected: a `from datetime import datetime, timezone` line already present (it's used elsewhere). If not, add it in the imports section.

- [ ] **Step 4: Implement both methods**

Append to `jacked/web/database.py` after `clear_account_errors` (after line ~1220 — find the last method before the file's end):

```python
def list_stuck_checking_accounts(self, threshold_seconds: int) -> list[dict]:
    """Return non-deleted accounts where validation_status='checking'
    AND (updated_at is NULL OR updated_at older than threshold_seconds).

    NULL updated_at is treated as "definitely stuck" — otherwise
    strftime('%s', NULL) returns NULL and the row is hidden forever.

    Includes inactive accounts (they can still be stuck and need cleanup).

    >>> db = Database(":memory:")
    >>> db.list_stuck_checking_accounts(120)
    []
    """
    with self._reader() as conn:
        cursor = conn.execute(
            """SELECT * FROM accounts
               WHERE validation_status = 'checking'
                 AND is_deleted = 0
                 AND (
                   updated_at IS NULL
                   OR (strftime('%s','now') - strftime('%s', updated_at)) > ?
                 )
               ORDER BY updated_at ASC""",
            (threshold_seconds,),
        )
        return [dict(row) for row in cursor.fetchall()]


def reset_stuck_checking(
    self,
    account_id: int,
    threshold_seconds: int,
    reason: str,
) -> int:
    """Atomically reset validation_status='checking' to 'unknown' IFF
    the row still reads 'checking' AND (updated_at is NULL OR stale
    past threshold_seconds).

    WHERE guard prevents clobbering a row that a concurrent validator
    already moved to 'valid' (PM1 TOCTOU fix).

    Returns rowcount (0 if already moved).

    >>> db = Database(":memory:")
    >>> db.reset_stuck_checking(1, 120, "x")
    0
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    with self._writer() as conn:
        cursor = conn.execute(
            """UPDATE accounts
               SET validation_status = 'unknown',
                   last_error = ?,
                   last_error_at = ?,
                   updated_at = ?
               WHERE id = ?
                 AND validation_status = 'checking'
                 AND is_deleted = 0
                 AND (
                   updated_at IS NULL
                   OR (strftime('%s','now') - strftime('%s', updated_at)) > ?
                 )""",
            (reason, now_iso, now_iso, account_id, threshold_seconds),
        )
        return cursor.rowcount
```

- [ ] **Step 5: Run — expect PASS (10 tests)**

Run: `uv run python -m pytest tests/unit/test_stuck_checking_watchdog.py -v`
Expected: 10 tests PASS.

- [ ] **Step 6: Commit**

```bash
git add jacked/web/database.py tests/unit/test_stuck_checking_watchdog.py
git commit -m "feat(db): list_stuck_checking_accounts + atomic reset_stuck_checking

Atomic WHERE-guarded UPDATE with NULL-safe updated_at handling.
WHERE clause prevents clobbering a row that a concurrent validator
moved to 'valid' (TOCTOU fix from /dc round-1 PM1).  Includes a
fallback for NULL updated_at: treat as 'definitely stuck' so rows
with malformed timestamps can't hide from cleanup forever (/dc
round-2 Medium 1)."
```

---

## Task 2: Async wrapper + clear `last_error` in validator success

**Files:**
- Modify: `jacked/web/auth.py`
- Test: extend `tests/unit/test_stuck_checking_watchdog.py`

- [ ] **Step 1: Append wrapper + validator tests to existing test file**

Append to `tests/unit/test_stuck_checking_watchdog.py`:

```python
class TestResetStaleCheckingAccountsWrapper:
    def test_wrapper_resets_stuck_rows(self, db):
        a = db.create_account("a@t.com", "tok", 9999999999)
        b = db.create_account("b@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        db.update_account(b["id"], validation_status="checking")
        _age_account(db, a["id"], 200)
        _age_account(db, b["id"], 200)

        async def _run():
            from jacked.web.auth import reset_stale_checking_accounts
            return await reset_stale_checking_accounts(db, threshold_seconds=120)

        count = asyncio.run(_run())
        assert count == 2
        assert db.get_account(a["id"])["validation_status"] == "unknown"
        assert db.get_account(b["id"])["validation_status"] == "unknown"

    def test_wrapper_reports_race_loss(self, db):
        """Watchdog scanned, then validator wrote 'valid' before reset ran.
        The atomic UPDATE must return 0 and NOT clobber the valid row."""
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(a["id"], validation_status="checking")
        _age_account(db, a["id"], 200)

        real_reset = db.reset_stuck_checking

        def racing_reset(account_id, threshold_seconds, reason):
            # Simulate a concurrent validator moving row to 'valid'
            # between list_stuck_checking_accounts and reset_stuck_checking
            db.update_account(account_id, validation_status="valid")
            return real_reset(account_id, threshold_seconds, reason)

        async def _run():
            from jacked.web.auth import reset_stale_checking_accounts
            with patch.object(db, "reset_stuck_checking", side_effect=racing_reset):
                return await reset_stale_checking_accounts(db, 120)

        count = asyncio.run(_run())
        assert count == 0
        row = db.get_account(a["id"])
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None


class TestValidateAccountClearsLastError:
    def test_first_try_success_clears_last_error(self, db):
        """After a watchdog reset leaves last_error='...watchdog...',
        successful validation MUST clear last_error (otherwise the UI
        shows a stale error banner on a valid account — /dc Q12 fix)."""
        a = db.create_account("a@t.com", "tok", 9999999999)
        db.update_account(
            a["id"], validation_status="unknown",
            last_error="validation timed out — reset by watchdog",
            last_error_at=datetime.now(timezone.utc).isoformat(),
        )

        async def _run():
            from jacked.web import auth as auth_mod
            from unittest.mock import MagicMock

            class FakeResp:
                status_code = 200
                def json(self):
                    return {}

            fake_client = AsyncMock()
            fake_client.__aenter__.return_value = fake_client
            fake_client.__aexit__.return_value = False
            fake_client.get.return_value = FakeResp()

            with patch.object(auth_mod.httpx, "AsyncClient", return_value=fake_client):
                await auth_mod.validate_account(a["id"], db)

        asyncio.run(_run())
        row = db.get_account(a["id"])
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None
        assert row["last_error_at"] is None

    def test_retry_after_refresh_success_clears_last_error(self, db):
        """Second success path (401 → refresh → retry → 200) must also
        clear last_error.  /dc round-2 Medium 5 fix."""
        a = db.create_account("a@t.com", "tok", 9999999999)
        # Seed a refresh token + stale error
        db.update_account(
            a["id"], validation_status="unknown",
            refresh_token="refresh_token_xyz",
            last_error="old error",
            last_error_at=datetime.now(timezone.utc).isoformat(),
        )

        async def _run():
            from jacked.web import auth as auth_mod
            from unittest.mock import MagicMock

            call_count = {"n": 0}

            class FakeResp401:
                status_code = 401
                def json(self):
                    return {}

            class FakeResp200:
                status_code = 200
                def json(self):
                    return {}

            async def fake_get(*args, **kwargs):
                call_count["n"] += 1
                return FakeResp401() if call_count["n"] == 1 else FakeResp200()

            fake_client = AsyncMock()
            fake_client.__aenter__.return_value = fake_client
            fake_client.__aexit__.return_value = False
            fake_client.get = fake_get

            async def fake_refresh(*args, **kwargs):
                return "fresh_access_token"

            with patch.object(auth_mod.httpx, "AsyncClient", return_value=fake_client), \
                 patch.object(auth_mod, "_try_refresh_primary_token", side_effect=fake_refresh):
                await auth_mod.validate_account(a["id"], db)

        asyncio.run(_run())
        row = db.get_account(a["id"])
        assert row["validation_status"] == "valid"
        assert row["last_error"] is None
        assert row["last_error_at"] is None
```

- [ ] **Step 2: Run — expect ImportError / failures**

Run: `uv run python -m pytest tests/unit/test_stuck_checking_watchdog.py -v 2>&1 | tail -30`
Expected: ImportError on `reset_stale_checking_accounts`; `last_error` not cleared on the validator tests.

- [ ] **Step 3: Implement the async wrapper**

Append to `jacked/web/auth.py` after `validate_account` (after line 972):

```python
async def reset_stale_checking_accounts(
    db: "Database",
    threshold_seconds: int = 120,
) -> int:
    """Reset accounts stuck in validation_status='checking' past threshold.

    Iterates the scan result, delegating to db.reset_stuck_checking's
    WHERE-guarded atomic UPDATE so a concurrent validator can't be
    clobbered.  Returns total rows actually reset.
    """
    import time
    reset_count = 0
    for acct in db.list_stuck_checking_accounts(threshold_seconds=threshold_seconds):
        now = int(time.time())
        updated_at = acct.get("updated_at")
        if updated_at:
            try:
                updated_at_dt = datetime.fromisoformat(
                    updated_at.replace("Z", "+00:00"),
                )
                age = int(now - updated_at_dt.timestamp())
            except (ValueError, TypeError):
                age = threshold_seconds
        else:
            age = threshold_seconds  # NULL updated_at

        reason = f"validation timed out after {age}s — reset by watchdog"
        rows_reset = db.reset_stuck_checking(
            acct["id"], threshold_seconds=threshold_seconds, reason=reason,
        )
        if rows_reset > 0:
            logger.warning(
                "Stuck-checking watchdog: account %d was 'checking' for %ds — "
                "reset to 'unknown'",
                acct["id"], age,
            )
            reset_count += rows_reset
    return reset_count
```

- [ ] **Step 4: Clear `last_error` in BOTH validator success paths**

In `jacked/web/auth.py`, find `validate_account` (line 870). Modify BOTH success sites:

**Site A — first-try 200 (around line 898-906):**

```python
# before
db.update_account(
    account_id,
    validation_status="valid",
    last_validated_at=int(time.time()),
    consecutive_failures=0,
)

# after
db.update_account(
    account_id,
    validation_status="valid",
    last_validated_at=int(time.time()),
    consecutive_failures=0,
    last_error=None,
    last_error_at=None,
)
```

**Site B — retry-after-refresh 200 (around line 920-928):** apply the same `last_error=None, last_error_at=None` addition.

- [ ] **Step 5: Run — expect PASS (16 tests: 10 from Task 1 + 6 new)**

Run: `uv run python -m pytest tests/unit/test_stuck_checking_watchdog.py -v`
Expected: all 16 tests PASS.

- [ ] **Step 6: Verify no regressions in existing validator tests**

Run: `uv run python -m pytest tests/unit/ -k "validate" -v 2>&1 | tail -15`
Expected: no new failures.

- [ ] **Step 7: Commit**

```bash
git add jacked/web/auth.py tests/unit/test_stuck_checking_watchdog.py
git commit -m "feat(auth): reset_stale_checking_accounts wrapper + clear last_error on success

- Async wrapper iterates stuck rows, calls atomic reset per row.
- validate_account's both success paths (first-try + retry-after-refresh)
  now clear last_error/last_error_at.  Prevents stale watchdog-reset
  error banner from persisting on a now-valid account (/dc Q12 fix)."
```

---

## Task 3: Register the watchdog task

**Files:**
- Modify: `jacked/api/main.py`

- [ ] **Step 1: Add the loop function**

Insert into `jacked/api/main.py` BEFORE `lifespan()` (around line 83):

```python
async def _stuck_checking_watchdog_loop(app):
    """Reset stuck validation_status='checking' rows every 60s.

    See 0.41.23 spec for details.
    """
    from jacked.web.auth import reset_stale_checking_accounts

    interval = 60
    while True:
        try:
            db = getattr(app.state, "db", None)
            if db is not None:
                count = await reset_stale_checking_accounts(db, threshold_seconds=120)
                if count > 0:
                    logger.info(
                        "Stuck-checking watchdog reset %d account(s)", count,
                    )
        except asyncio.CancelledError:
            logger.info("Stuck-checking watchdog cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Stuck-checking watchdog error", exc_info=True)
        await asyncio.sleep(interval)
```

- [ ] **Step 2: Create and register the task inside `lifespan`**

In `lifespan()`, find line 146 (the `full_sweep_task = asyncio.create_task(full_sweep_loop(app))` line) and the log lines just below. Insert AFTER those, BEFORE the analytics block:

```python
stuck_checking_task = asyncio.create_task(_stuck_checking_watchdog_loop(app))
logger.info("Started stuck-checking watchdog (every 60s)")
```

- [ ] **Step 3: Append to the shutdown `tasks_to_cancel` list**

Find the line in shutdown that reads (currently at line 169):

```python
tasks_to_cancel = [refresh_task, session_watch_task, logs_watch_task, sweeper_task, heal_task, active_poll_task, full_sweep_task]
```

Modify to:

```python
tasks_to_cancel = [refresh_task, session_watch_task, logs_watch_task, sweeper_task, heal_task, active_poll_task, full_sweep_task, stuck_checking_task]
```

- [ ] **Step 4: Run full test suite — no regressions**

Run: `uv run python -m pytest tests/unit/ -x 2>&1 | tail -15`
Expected: all pass (preexisting dual_token failure allowed).

- [ ] **Step 5: Commit**

```bash
git add jacked/api/main.py
git commit -m "feat(api): register stuck-checking watchdog in lifespan

Background task, 60s interval, 120s staleness threshold.
Appended to shutdown tasks_to_cancel list."
```

---

## Task 4: Per-account `wait_for` + explicit status reset + ownership-checked slot

**Files:**
- Modify: `jacked/api/routes/auth.py`
- Test: `tests/unit/api/test_bulk_refresh_timeout.py` (new)

- [ ] **Step 1: Write the failing tests using `asyncio.run()` pattern**

Create `tests/unit/api/test_bulk_refresh_timeout.py`:

```python
"""Tests for per-account timeout + orphan-task cancel + ownership-checked slot.

Uses asyncio.run() wrappers (project convention — no pytest-asyncio)."""
import asyncio
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def _reset_module_state(monkeypatch, tmp_path):
    """Reset bulk-refresh module state before each test.

    asyncio.run() creates a fresh event loop per test.  A Lock acquired
    in test A's loop cannot be re-used in test B's loop (asyncio raises
    'got Future attached to a different loop').  Create a fresh Lock
    here (binding is lazy; it'll attach to whichever loop uses it first).

    Also redirect Path.home() into a tmp path so refresh_all_usage's
    read of ~/.claude/.credentials.json hits a non-existent file — tests
    must not depend on the developer's real credential state (/dc
    round-3 M2 hermeticity fix).
    """
    from jacked.api.routes import auth as routes_auth
    routes_auth._bulk_refresh_lock = asyncio.Lock()
    routes_auth._bulk_refresh_task = None
    routes_auth._bulk_refresh_acquired_at = 0.0
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield
    # Teardown: restore to import-time state so this file's tests can't
    # pollute tests in other files that touch refresh_all_usage
    # (e.g. tests/unit/test_use_account.py).
    routes_auth._bulk_refresh_lock = asyncio.Lock()
    routes_auth._bulk_refresh_task = None
    routes_auth._bulk_refresh_acquired_at = 0.0


def test_hanging_account_times_out_and_others_complete(monkeypatch):
    """One hanging fetch_usage must not block the others."""
    from jacked.api.routes import auth as routes_auth

    db = MagicMock()
    db.list_accounts.return_value = [
        {"id": 1, "email": "a@x"},
        {"id": 2, "email": "b@x"},
        {"id": 3, "email": "c@x"},
    ]
    db.get_account.return_value = {"id": 1, "last_error": None}

    never_fires = asyncio.Event()

    async def fake_fetch(acct_id, db_arg, access_token=None, manual=False):
        if acct_id == 2:
            await never_fires.wait()
            return None
        return {"five_hour": {"utilization": 0.0},
                "seven_day": {"utilization": 0.0}}

    request = MagicMock()
    request.app.state.ws_registry = None

    monkeypatch.setattr(routes_auth, "_BULK_PER_ACCOUNT_TIMEOUT", 0.25)
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    async def _run():
        start = time.monotonic()
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            resp = await routes_auth.refresh_all_usage(request)
        elapsed = time.monotonic() - start
        never_fires.set()
        return resp, elapsed

    resp, elapsed = asyncio.run(_run())
    # 3 accts × (0.25s timeout + 2s pacing) ≈ 6.75s
    assert elapsed < 10.0, f"expected <10s bound, got {elapsed:.1f}s"
    result_by_id = {r["account_id"]: r for r in resp.results}
    assert result_by_id[1]["success"] is True
    assert result_by_id[2]["success"] is False
    assert result_by_id[3]["success"] is True


def test_timeout_writes_validation_status_unknown(monkeypatch):
    """Timeout path MUST write validation_status='unknown' at the same site,
    not rely on the watchdog's 60s tick (/dc PM3 fix)."""
    from jacked.api.routes import auth as routes_auth

    db = MagicMock()
    db.list_accounts.return_value = [{"id": 1, "email": "a@x"}]
    db.get_account.return_value = {"id": 1, "last_error": None}

    never_fires = asyncio.Event()

    async def fake_fetch(*args, **kwargs):
        await never_fires.wait()

    request = MagicMock()
    request.app.state.ws_registry = None
    monkeypatch.setattr(routes_auth, "_BULK_PER_ACCOUNT_TIMEOUT", 0.2)
    monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

    async def _run():
        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            await routes_auth.refresh_all_usage(request)
        never_fires.set()

    asyncio.run(_run())
    # Find the update_account call with validation_status='unknown' + last_error
    calls = db.update_account.call_args_list
    assert any(
        call.kwargs.get("validation_status") == "unknown"
        and "timed out" in (call.kwargs.get("last_error") or "").lower()
        for call in calls
    ), f"expected explicit status+error write after timeout; got {calls}"


def test_stale_lock_cancels_orphan_task(monkeypatch):
    """Force-reset of _bulk_refresh_lock calls orphan.cancel().  The
    handler is fire-and-forget; the event loop delivers the cancel on
    its own schedule.  After the endpoint returns, we await the orphan
    (suppressing CancelledError) to verify it actually got cancelled."""
    from jacked.api.routes import auth as routes_auth

    async def _run():
        orphan_ran = asyncio.Event()

        async def orphan():
            orphan_ran.set()
            await asyncio.sleep(9999)

        orphan_task = asyncio.create_task(orphan())
        await orphan_ran.wait()

        # Fresh lock + pre-seeded stale state
        await routes_auth._bulk_refresh_lock.acquire()
        routes_auth._bulk_refresh_acquired_at = time.time() - 500
        routes_auth._bulk_refresh_task = orphan_task

        db = MagicMock()
        db.list_accounts.return_value = []
        request = MagicMock()
        request.app.state.ws_registry = None
        monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

        await routes_auth.refresh_all_usage(request)

        # Event loop now delivers orphan.cancel().  Await with suppression.
        try:
            await asyncio.wait_for(orphan_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        # Tight assertion: the orphan body is `sleep(9999)` — the only
        # way for it to be .cancelled() within 1s is via our cancel().
        return orphan_task.cancelled()

    assert asyncio.run(_run()) is True


def test_finally_preserves_newer_task_in_slot(monkeypatch):
    """End-to-end: if fake_fetch mutates _bulk_refresh_task mid-call
    (simulating a force-reset + new holder), the endpoint's finally
    MUST leave the slot pointing at the newer task — ownership check
    prevents clobber (/dc PM2/Q2 fix)."""
    from jacked.api.routes import auth as routes_auth

    async def _run():
        routes_auth._bulk_refresh_lock = asyncio.Lock()
        routes_auth._bulk_refresh_task = None
        routes_auth._bulk_refresh_acquired_at = 0.0

        # Create a newer_task that should survive the endpoint's finally
        async def newer_body():
            await asyncio.sleep(0)
            return "newer"
        newer_task = asyncio.create_task(newer_body())

        async def fake_fetch(*args, **kwargs):
            # Simulate: mid-call, a force-reset path installed a different
            # task in the slot.  Our finally must NOT wipe it.
            routes_auth._bulk_refresh_task = newer_task
            return {"five_hour": {"utilization": 0.0},
                    "seven_day": {"utilization": 0.0}}

        db = MagicMock()
        db.list_accounts.return_value = [{"id": 1, "email": "a@x"}]
        db.get_account.return_value = {"id": 1, "last_error": None}
        request = MagicMock()
        request.app.state.ws_registry = None
        monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

        with patch.object(routes_auth, "fetch_usage", side_effect=fake_fetch):
            await routes_auth.refresh_all_usage(request)

        # After the endpoint returns, the slot should STILL be newer_task
        assert routes_auth._bulk_refresh_task is newer_task, (
            f"finally clobbered newer holder; slot is now "
            f"{routes_auth._bulk_refresh_task!r}"
        )
        await newer_task

    asyncio.run(_run())
```

- [ ] **Step 2: Run — expect failures (missing module state, imports)**

Run: `uv run python -m pytest tests/unit/api/test_bulk_refresh_timeout.py -v 2>&1 | tail -30`
Expected: AttributeError on `_BULK_PER_ACCOUNT_TIMEOUT` / `_bulk_refresh_task`, or the ownership-check not yet implemented.

- [ ] **Step 3: Add explicit `datetime` import to `jacked/api/routes/auth.py`**

Current imports at lines 7-26 do NOT include `datetime`. Add at the top of the imports (after existing stdlib imports):

```python
from datetime import datetime, timezone
```

- [ ] **Step 4: Add module constants + task slot**

In `jacked/api/routes/auth.py` near line 36:

```python
_bulk_refresh_lock = asyncio.Lock()
_bulk_refresh_acquired_at: float = 0.0
# 0.41.23: track the task holding the lock so the stale-lock guard
# can cancel it before swapping in a fresh lock.
_bulk_refresh_task: "asyncio.Task | None" = None
_BULK_REFRESH_STALE_AFTER = 180.0
# 0.41.23: max seconds per account in bulk refresh before we declare
# the account hung and move on.
_BULK_PER_ACCOUNT_TIMEOUT = 60.0
```

- [ ] **Step 5: Replace stale-lock block (lines 644-658) with orphan-cancel**

```python
# before (lines 644-658)
global _bulk_refresh_lock, _bulk_refresh_acquired_at
if _bulk_refresh_lock.locked():
    held_for = time.time() - _bulk_refresh_acquired_at if _bulk_refresh_acquired_at else 0
    if held_for > _BULK_REFRESH_STALE_AFTER:
        logger.warning(
            "Bulk refresh lock held %ds (> %ds) — forcing reset",
            int(held_for), int(_BULK_REFRESH_STALE_AFTER),
        )
        _bulk_refresh_lock = asyncio.Lock()
        _bulk_refresh_acquired_at = 0.0
    else:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Usage refresh already in progress"},
        )

# after
global _bulk_refresh_lock, _bulk_refresh_acquired_at, _bulk_refresh_task
if _bulk_refresh_lock.locked():
    held_for = time.time() - _bulk_refresh_acquired_at if _bulk_refresh_acquired_at else 0
    if held_for > _BULK_REFRESH_STALE_AFTER:
        logger.warning(
            "Bulk refresh lock held %ds (> %ds) — forcing reset",
            int(held_for), int(_BULK_REFRESH_STALE_AFTER),
        )
        orphan = _bulk_refresh_task
        if orphan is not None and not orphan.done():
            orphan.cancel()
            # DO NOT await the orphan.  The event loop delivers the
            # CancelledError to the orphan on its next tick — we don't
            # need to synchronize.  Awaiting here (even with wait_for)
            # re-raises CancelledError through our own handler and is
            # impossible to distinguish portably from "we were cancelled
            # ourselves" in Python 3.10 (no Task.cancelling()).  Fire
            # and forget is robust and race-free (/dc round-3 C1 fix).
        _bulk_refresh_lock = asyncio.Lock()
        _bulk_refresh_acquired_at = 0.0
        _bulk_refresh_task = None
    else:
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={"detail": "Usage refresh already in progress"},
        )
```

Rationale for fire-and-forget cancel: the orphan has been hung for >180s
already.  Any DB writes from its cancellation cleanup would be suspect
regardless of whether we wait 0s or 2s.  Moving on immediately lets the
new bulk-refresh caller proceed; the cancelled orphan runs its cleanup
on the event loop's own schedule.

- [ ] **Step 6: Wrap `fetch_usage` in `wait_for` + explicit status reset on timeout**

In `refresh_all_usage`, find the fetch at line 721:

```python
# before
usage_data = await fetch_usage(acct["id"], db, access_token=effective_token, manual=True)

# after
try:
    usage_data = await asyncio.wait_for(
        fetch_usage(acct["id"], db, access_token=effective_token, manual=True),
        timeout=_BULK_PER_ACCOUNT_TIMEOUT,
    )
except asyncio.TimeoutError:
    logger.warning(
        "Bulk refresh: account %d fetch_usage exceeded %.0fs — "
        "marking failed and resetting validation_status",
        acct["id"], _BULK_PER_ACCOUNT_TIMEOUT,
    )
    timeout_error = (
        f"Usage fetch timed out after {int(_BULK_PER_ACCOUNT_TIMEOUT)}s "
        f"during bulk refresh"
    )
    # Reset validation_status in the same call that records the error so
    # the row doesn't sit at 'checking' waiting for the watchdog's next
    # 60s tick (/dc PM3 fix).
    db.update_account(
        acct["id"],
        validation_status="unknown",
        last_error=timeout_error,
        last_error_at=datetime.now(timezone.utc).isoformat(),
    )
    usage_data = None
```

- [ ] **Step 7: Ownership-checked task-slot cleanup via try/finally**

Currently the code at line 660 onward is:

```python
async with _bulk_refresh_lock:
    _bulk_refresh_acquired_at = time.time()
    accounts = db.list_accounts(include_inactive=False)
    refreshed = 0
    failed = 0
    results = []
    # ... lots of body ...
    return BulkUsageRefreshResponse(...)
```

Wrap the body in try/finally and add ownership check:

```python
async with _bulk_refresh_lock:
    _bulk_refresh_acquired_at = time.time()
    my_task = asyncio.current_task()
    _bulk_refresh_task = my_task
    try:
        accounts = db.list_accounts(include_inactive=False)
        # ... entire existing body, up through `return BulkUsageRefreshResponse(...)` ...
    finally:
        # Only clear the slot if we still own it.  A force-reset during
        # our long-running loop may have replaced _bulk_refresh_task with
        # a newer holder — clearing it would wipe the new holder's state
        # (/dc PM2/Q2 fix).
        if _bulk_refresh_task is my_task:
            _bulk_refresh_task = None
```

Python's `return` inside a `try` with a `finally` runs the finally BEFORE the return propagates. This is safe — the response is built, finally runs, response returns.

- [ ] **Step 8: Run — expect PASS (4 tests)**

Run: `uv run python -m pytest tests/unit/api/test_bulk_refresh_timeout.py -v`
Expected: 4 tests PASS.

Also check no regressions elsewhere:

Run: `uv run python -m pytest tests/unit/ -k "bulk or refresh or usage" 2>&1 | tail -15`

- [ ] **Step 9: Commit**

```bash
git add jacked/api/routes/auth.py tests/unit/api/test_bulk_refresh_timeout.py
git commit -m "feat(api): per-account timeout + ownership-checked slot + orphan cancel

- Add missing 'from datetime import datetime, timezone' to routes/auth.py.
- Wrap each fetch_usage in bulk refresh with asyncio.wait_for(60s).
- On timeout, write validation_status='unknown' + last_error at the
  same call site — no reliance on watchdog (/dc PM3 fix).
- Track _bulk_refresh_task; on stale-lock force-reset call
  orphan.cancel() fire-and-forget (no await, no wait_for).  Any
  distinguish between 'we cancelled the orphan' vs 'we were cancelled
  ourselves' would require Python 3.11's Task.cancelling(); we skip
  the race entirely by not awaiting (/dc round-3 C1).
- finally-block uses identity check ('is my_task') so a late
  orphan-finally cannot wipe a newer holder's slot (/dc PM2/Q2 fix)."
```

---

## Task 5: Sweep heartbeat at TOP + bounded sweep `fetch_usage`

**Files:**
- Modify: `jacked/api/usage_monitor.py`
- Test: `tests/unit/api/test_sweep_heartbeat.py` (new)

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/api/test_sweep_heartbeat.py`:

```python
"""Tests for full_sweep_loop heartbeat + bounded fetch_usage.

Uses asyncio.run() wrappers (project convention)."""
import asyncio
import logging
from unittest.mock import MagicMock, patch


def test_heartbeat_fires_when_window_keeper_disabled(caplog):
    """Default config: window_keeper_enabled=False → heartbeat MUST still fire."""
    from jacked.api import usage_monitor

    caplog.set_level(logging.INFO, logger="jacked.api.usage_monitor")

    async def _run():
        app = MagicMock()
        db = MagicMock()
        app.state.db = db
        db.list_accounts.return_value = []

        with patch.object(usage_monitor, "_setting_bool", return_value=False), \
             patch.object(usage_monitor, "_setting_float", return_value=0.05):
            task = asyncio.create_task(usage_monitor.full_sweep_loop(app))
            await asyncio.sleep(0.3)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    heartbeats = [r.getMessage() for r in caplog.records
                  if "heartbeat" in r.getMessage().lower()]
    assert len(heartbeats) >= 1, "expected ≥1 heartbeat when window keeper off"
    assert any("iter=" in m for m in heartbeats), \
        f"expected 'iter=N' in heartbeat; got {heartbeats}"


def test_heartbeat_includes_monotonic_iteration_count(caplog):
    """Heartbeat iter= must increment monotonically across sweeps."""
    from jacked.api import usage_monitor

    caplog.set_level(logging.INFO, logger="jacked.api.usage_monitor")

    async def _run():
        app = MagicMock()
        db = MagicMock()
        app.state.db = db
        db.list_accounts.return_value = []

        with patch.object(usage_monitor, "_setting_bool", return_value=False), \
             patch.object(usage_monitor, "_setting_float", return_value=0.03):
            task = asyncio.create_task(usage_monitor.full_sweep_loop(app))
            await asyncio.sleep(0.2)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    asyncio.run(_run())
    iters = []
    for r in caplog.records:
        msg = r.getMessage()
        if "heartbeat" in msg.lower() and "iter=" in msg:
            iters.append(int(msg.split("iter=")[1].split()[0]))
    assert len(iters) >= 2, f"expected ≥2 heartbeats; got {iters}"
    assert iters == sorted(iters), f"iters not monotonic: {iters}"
```

- [ ] **Step 2: Run — expect FAIL**

Run: `uv run python -m pytest tests/unit/api/test_sweep_heartbeat.py -v 2>&1 | tail -15`
Expected: FAIL — no heartbeat log.

- [ ] **Step 3: Modify `full_sweep_loop`**

In `jacked/api/usage_monitor.py`, at line 1015. Two changes:

**3a. Heartbeat at TOP of iteration (before any `continue`)**:

```python
async def full_sweep_loop(app):
    """Fetch usage for all non-active accounts and run window keeper.

    Runs at the user-configurable ``usage_check_interval`` (default 300s).
    Never crashes — all errors are caught and logged per tick.
    Emits a heartbeat INFO log at the TOP of every iteration (before
    any early-return shortcut), so operators see a heartbeat regardless
    of window-keeper state (0.41.23).
    """
    _default_interval = 300
    iter_count = 0

    while True:
        iter_count += 1
        logger.info("Full-sweep heartbeat: iter=%d", iter_count)
        check_interval = _default_interval
        try:
            # ... existing body unchanged ...
```

**3b. Wrap the sweep's own `fetch_usage` at line 1128 in `wait_for`**:

```python
# before (line 1128)
await fetch_usage(acct["id"], db, access_token=cc_at)

# after
try:
    await asyncio.wait_for(
        fetch_usage(acct["id"], db, access_token=cc_at),
        timeout=60.0,
    )
except asyncio.TimeoutError:
    logger.warning(
        "Full sweep: fetch_usage for account %d exceeded 60s — moving on",
        acct["id"],
    )
```

- [ ] **Step 4: Run — expect PASS**

Run: `uv run python -m pytest tests/unit/api/test_sweep_heartbeat.py -v`
Expected: 2 tests PASS.

- [ ] **Step 5: No regressions**

Run: `uv run python -m pytest tests/unit/ -k "sweep or usage_monitor" 2>&1 | tail -15`
Expected: existing tests still pass.

- [ ] **Step 6: Commit**

```bash
git add jacked/api/usage_monitor.py tests/unit/api/test_sweep_heartbeat.py
git commit -m "feat(monitor): heartbeat at loop top + bound sweep fetch_usage

- Heartbeat log moved to the TOP of each iteration, before the
  early-return shortcuts for window_keeper_enabled=False (default).
- Sweep's internal fetch_usage wrapped in asyncio.wait_for(60s)
  — no more indefinite hang on a single slow account."
```

---

## Task 6: Update spec — diagnosis #3, Component D scope, document `last_error` clearing

**Files:**
- Modify: `docs/superpowers/specs/2026-04-19-stuck-checking-watchdog-design.md`

- [ ] **Step 1: Fix §Problem #3**

Replace the paragraph starting "3. **Sweep loop dies silently.**" with:

```markdown
3. **Sweep loop blocks silently on unbounded fetch_usage.** The existing
   exception guard at `jacked/api/usage_monitor.py:1139` catches exceptions
   — but cannot catch hangs.  `full_sweep_loop:1128` awaits `fetch_usage(...)`
   without an `asyncio.wait_for` bound, so a single slow or hung API call
   stalls the entire sweep indefinitely.  There's also no heartbeat log,
   so operators can't distinguish "sweep alive but idle" from "sweep
   blocked."
```

- [ ] **Step 2: Expand Component D**

Replace §D with:

```markdown
### D. Sweep heartbeat + bounded fetch_usage

In `jacked/api/usage_monitor.py`'s `full_sweep_loop`:

1. Emit an INFO log at the TOP of every iteration (BEFORE any
   `if not window_keeper_enabled: continue` short-circuit).  Format:
   `Full-sweep heartbeat: iter=N`.  This guarantees a heartbeat fires
   every iteration regardless of config.
2. Wrap the internal `await fetch_usage(...)` at line 1128 in
   `asyncio.wait_for(..., 60.0)` — same pattern as Task 4.  Bounds any
   single slow/hung account to 60s max.

Operator canary: no heartbeat for >10 min = sweep is blocked or dead.
```

- [ ] **Step 3: Add a new §E documenting `last_error` clearing**

Append after §D:

```markdown
### E. Validator success paths clear `last_error`

In `validate_account` at `jacked/web/auth.py:870`, both HTTP 200 success
paths (first-try at line 898-906, retry-after-refresh at line 920-928)
now also clear `last_error=None, last_error_at=None` on the DB write.

Without this, a row that the watchdog reset to `validation_status="unknown"`
with a "validation timed out — reset by watchdog" error would keep that
error banner forever even after a subsequent successful validation moved
the row to `validation_status="valid"`.
```

- [ ] **Step 4: Add to §Non-goals**

Append:

```markdown
- **`validate_account`'s "write checking before network" pattern is unchanged.**
  Correct for concurrent-validation races; cleanup is the fix point
  (watchdog A + explicit reset on timeout B), not the write site.
- **No new `pytest-asyncio` dependency.**  Tests use the project's
  existing `asyncio.run()` wrapper pattern (see `tests/unit/test_usage_monitor.py:4`).
```

- [ ] **Step 5: Commit**

```bash
git add docs/superpowers/specs/2026-04-19-stuck-checking-watchdog-design.md
git commit -m "docs(spec): fix diagnosis #3, scope sweep fetch_usage, document last_error clearing"
```

---

## Task 7: Version bump + changelog + full test suite

**Files:**
- Modify: `jacked/__init__.py`
- Modify: `README.md`

- [ ] **Step 1: Bump to 0.41.23**

Edit `jacked/__init__.py`:

```python
__version__ = "0.41.23"
```

- [ ] **Step 2: Changelog entry in README**

Insert above "### 0.41.22" in README:

```markdown
### 0.41.23 — Stuck-checking watchdog + sweep resilience (2026-04-19)

- **Fix**: `validation_status="checking"` could persist indefinitely
  in the DB if the owning coroutine was cancelled or the server was
  restarted mid-validation.  New watchdog runs every 60s with an
  atomic WHERE-guarded UPDATE (no TOCTOU race with concurrent
  validators) and resets any account stuck past 120s.  NULL
  `updated_at` is treated as "definitely stuck" so malformed
  timestamps can't hide a row from cleanup.
- **Fix**: Bulk `/accounts/refresh-all-usage` now wraps each account
  in a 60s `asyncio.wait_for` and explicitly writes
  `validation_status="unknown"` on timeout.  A hung account no longer
  blocks the rest of the bulk refresh and no longer strands the row
  at 'checking'.
- **Fix**: When the bulk-refresh lock is force-reset after being
  held past 180s, the orphaned task is now explicitly cancelled
  (fire-and-forget — the event loop delivers the cancel on its next
  tick; we don't synchronize on the orphan's unwind).  The task-slot
  cleanup uses an identity check so a late-finishing orphan's `finally`
  can't wipe a new holder's slot.
- **Fix**: `validate_account` success paths now clear `last_error`
  and `last_error_at`.  Previously, a watchdog-reset row that later
  validated successfully would keep the watchdog's "timed out" error
  banner forever.
- **Fix**: `full_sweep_loop` wraps its internal `fetch_usage` call
  in `asyncio.wait_for(60s)` — a single hung account no longer stalls
  the entire sweep.
- **Observability**: `full_sweep_loop` emits `Full-sweep heartbeat: iter=N`
  at the top of every iteration, including when `window_keeper_enabled`
  is False (the default).  Operators can spot stuck sweeps by grepping
  the log for gaps >10 min without a heartbeat.
```

- [ ] **Step 3: Run the FULL test suite**

Run: `uv run python -m pytest 2>&1 | tail -20`
Expected: all tests pass (1 preexisting unrelated dual_token failure allowed).

- [ ] **Step 4: Commit**

```bash
git add jacked/__init__.py README.md
git commit -m "chore: bump to 0.41.23 with changelog"
```

---

## Task 8: Ship — push, tag, release

- [ ] **Step 1: Push to master**

`git push origin master`

- [ ] **Step 2: Tag + push tag**

```bash
git tag -a v0.41.23 -m "v0.41.23 — stuck-checking watchdog + sweep resilience"
git push origin v0.41.23
```

- [ ] **Step 3: GitHub release**

```bash
gh release create v0.41.23 --title "v0.41.23 — stuck-checking watchdog + sweep resilience" --notes "$(cat <<'EOF'
## Fix: validation_status='checking' no longer stuck indefinitely

### The bug
On 2026-04-19, user3@example.com was stuck "Checking usage…" for ~14 hours.
`validate_account()` writes `validation_status="checking"` before the network
call; if the coroutine is abandoned (server restart, cancellation, orphan
task from bulk-lock force-reset), the DB row stays 'checking' forever.

### The fix
- Atomic WHERE-guarded DB watchdog runs every 60s (TOCTOU-safe).
- Per-account `asyncio.wait_for(60s)` in bulk refresh with explicit status
  reset on timeout.
- Orphan-task cancellation on bulk-lock force-reset, with ownership-checked
  slot cleanup.
- Validator success paths clear `last_error`/`last_error_at`.
- `full_sweep_loop` heartbeat + bounded `fetch_usage`.
- NULL `updated_at` handled (legacy row defense).

Full changelog: see README.md.
EOF
)"
```

- [ ] **Step 4: Verify PyPI publish**

```bash
gh run list --workflow=publish.yml --limit=1
```

Use Monitor to poll for completion. Expected success in ~30s.

- [ ] **Step 5: Post-ship sanity**

```bash
uv tool install --force claude-jacked==0.41.23
jacked check-version
```

Expected: `current=0.41.23, outdated=False`.

Restart the local service (new CLI has native_restart). Tail `~/.claude/jacked-service.log` for ~2 min — expect:
- `Started stuck-checking watchdog (every 60s)` at startup
- `Full-sweep heartbeat: iter=N` every interval
- No stuck "Checking usage…" in the dashboard

---

## Self-Review (post-v3)

- [x] /dc round-1 CRITICAL: Q1 (atomic DB method ✓), Q2 (ownership check ✓), PM1 (WHERE-guarded UPDATE ✓), PM3 (explicit status reset on timeout ✓).
- [x] /dc round-1 MEDIUM: Q3 (heartbeat at top ✓), Q4+Q5 (asyncio.run() pattern, no asyncio.sleep patch ✓), Q6 (wall-clock bound ✓), Q8 (bounded sweep fetch_usage ✓).
- [x] /dc round-2 CRITICAL #1 (pytest-asyncio): ELIMINATED — all tests use `asyncio.run()` wrappers matching project convention.
- [x] /dc round-2 CRITICAL #2 (integration test doesn't exercise race): removed that misleading integration test; the race is mechanically tested in `test_wrapper_reports_race_loss` via `side_effect` on `db.reset_stuck_checking`, and the core WHERE-guard behavior is directly tested in `test_refuses_to_clobber_racing_valid_write` against a real DB.
- [x] /dc round-2 CRITICAL #3 (tautological slot test): rewritten as `test_finally_preserves_newer_task_in_slot` driving `refresh_all_usage` end-to-end with fake_fetch mutating `_bulk_refresh_task` mid-call.
- [x] /dc round-2 MEDIUM 1 (NULL updated_at): SQL adds `OR updated_at IS NULL`; tests `test_null_updated_at_treated_as_stale` + `test_null_updated_at_is_reset`.
- [x] /dc round-2 MEDIUM 2 (vague shutdown cleanup): Task 3 Step 3 specifies exact edit to `tasks_to_cancel` list at line 169.
- [x] /dc round-2 MEDIUM 3 (CancelledError swallow): Task 4 Step 5 omits `asyncio.CancelledError` from the orphan-cancel except clause.
- [x] /dc round-2 MEDIUM 5 (second success-path test): Task 2 Step 1's `test_retry_after_refresh_success_clears_last_error`.
- [x] /dc round-2 MEDIUM 6 (datetime import): Task 4 Step 3 mandatory explicit import addition.
- [x] /dc round-2 LOW 3 (spec documents last_error clearing): Task 6 Step 3 adds §E.
- [x] /dc round-3 CRITICAL C1 (CancelledError leak from orphan-cancel): Task 4 Step 5 is FIRE-AND-FORGET — `orphan.cancel()` with no await, no `wait_for`, no except clause. No CancelledError re-raise path exists, so the race is eliminated rather than handled. Python 3.10 compatible (no reliance on 3.11's `Task.cancelling()`).
- [x] /dc round-3 MEDIUM M1 (module-state leak across asyncio.run loops): autouse fixture `_reset_module_state` in `test_bulk_refresh_timeout.py` resets `_bulk_refresh_lock` / `_bulk_refresh_task` / `_bulk_refresh_acquired_at` before each test.
- [x] /dc round-3 MEDIUM M2 (tests read real ~/.claude/.credentials.json): same autouse fixture redirects `Path.home()` to a tmp_path, making the credential-file read a guaranteed miss.
- [x] No placeholders. Concrete SQL, concrete line numbers, concrete commands.
- [x] Cross-task identifier consistency: `_bulk_refresh_task`, `_BULK_PER_ACCOUNT_TIMEOUT`, `list_stuck_checking_accounts`, `reset_stuck_checking`, `reset_stale_checking_accounts`, `_stuck_checking_watchdog_loop`, `stuck_checking_task`.
- [x] Version bump + changelog in Task 7. Ship in Task 8.
