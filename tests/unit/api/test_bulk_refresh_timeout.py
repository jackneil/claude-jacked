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
    'got Future attached to a different loop').  Delegate to the
    production-path ``reset_locks()`` helper so this fixture dogfoods
    the same code the FastAPI lifespan uses — a future bug in
    reset_locks (e.g., forgetting a new field) will now break these
    tests instead of silently diverging.

    Also redirect Path.home() into a tmp path so refresh_all_usage's
    read of ~/.claude/.credentials.json hits a non-existent file — tests
    must not depend on the developer's real credential state.
    """
    from jacked.api.routes import auth as routes_auth
    routes_auth.reset_locks()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    yield
    routes_auth.reset_locks()


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

        await routes_auth._bulk_refresh_lock.acquire()
        routes_auth._bulk_refresh_acquired_at = time.time() - 500
        routes_auth._bulk_refresh_task = orphan_task

        db = MagicMock()
        db.list_accounts.return_value = []
        request = MagicMock()
        request.app.state.ws_registry = None
        monkeypatch.setattr(routes_auth, "_get_db", lambda r: db)

        await routes_auth.refresh_all_usage(request)

        # Event loop now delivers orphan.cancel().
        try:
            await asyncio.wait_for(orphan_task, timeout=1.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        # Tight assertion: orphan body is sleep(9999), so cancelled() is
        # the only way it terminates within 1s.
        return orphan_task.cancelled()

    assert asyncio.run(_run()) is True


def test_finally_preserves_newer_task_in_slot(monkeypatch):
    """End-to-end: if fake_fetch mutates _bulk_refresh_task mid-call
    (simulating a force-reset + new holder), the endpoint's finally
    MUST leave the slot pointing at the newer task — ownership check
    prevents clobber (/dc PM2/Q2 fix)."""
    from jacked.api.routes import auth as routes_auth

    async def _run():
        async def newer_body():
            await asyncio.sleep(0)
            return "newer"
        newer_task = asyncio.create_task(newer_body())

        async def fake_fetch(*args, **kwargs):
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

        assert routes_auth._bulk_refresh_task is newer_task, (
            f"finally clobbered newer holder; slot is now "
            f"{routes_auth._bulk_refresh_task!r}"
        )
        await newer_task

    asyncio.run(_run())
