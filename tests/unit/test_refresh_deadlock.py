"""Regression tests for the per-account refresh lock self-deadlock.

refresh_all_expiring_tokens and heal_invalid_accounts used to hold the
per-account primary refresh lock while calling into _refresh_token_flow,
which re-acquires the same non-reentrant asyncio.Lock — the second acquire
blocked forever and wedged every later 401-recovery path for that account
until server restart. _refresh_token_flow (PRIMARY mode) also called
fetch_profile while still holding the lock; fetch_profile's 401 path
re-enters the flow on the same lock.

These tests pin the fixed behavior:
- the sweep and heal passes complete (no unbounded lock wait)
- lock acquisition in _refresh_token_flow is bounded (error="lock_timeout")
- the PRIMARY post-refresh profile fetch runs outside the lock
- healing marks an account valid only after a real, successful exchange
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jacked.web import auth
from jacked.web.auth import (
    RefreshMode,
    TokenExchangeResult,
    heal_invalid_accounts,
    refresh_all_expiring_tokens,
)
from jacked.web.database import Database


@pytest.fixture(autouse=True)
def _fresh_locks():
    """Each test runs in its own event loop — drop locks bound to old loops."""
    auth.reset_locks()
    yield
    auth.reset_locks()


def _make_db(tmp_path, expires_at: int, validation_status: str | None = None) -> tuple:
    db = Database(str(tmp_path / "test.db"))
    acct = db.create_account(
        "deadlock@test.com", "old-at", expires_at, refresh_token="old-rt",
    )
    if validation_status:
        db.update_account(acct["id"], validation_status=validation_status)
    return db, acct


def _success_exchange() -> AsyncMock:
    return AsyncMock(return_value=TokenExchangeResult(
        success=True,
        access_token="new-at",
        refresh_token="new-rt",
        expires_in=3600,
    ))


# ---------------------------------------------------------------------------
# refresh_all_expiring_tokens must not deadlock on its own lock
# ---------------------------------------------------------------------------


class TestRefreshSweepNoDeadlock:
    def test_expired_account_refresh_completes(self, tmp_path):
        """Sweep over an expired account finishes — hung forever pre-fix."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)
        exchange = _success_exchange()

        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.fetch_profile", new_callable=AsyncMock), \
             patch("jacked.api.credential_helpers.read_active_account_id",
                   return_value=None):
            result = asyncio.run(
                asyncio.wait_for(refresh_all_expiring_tokens(), timeout=5))

        assert result["refreshed"] == 1
        assert result["failed"] == 0
        exchange.assert_awaited_once()
        row = db.get_account(acct["id"])
        assert row["access_token"] == "new-at"
        assert row["refresh_token"] == "new-rt"

    def test_failing_account_counts_failed_not_stalled(self, tmp_path):
        """One bad account is counted as failed; the pass still completes."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)
        exchange = AsyncMock(return_value=TokenExchangeResult(
            success=False, error="network_error"))

        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.api.credential_helpers.read_active_account_id",
                   return_value=None):
            result = asyncio.run(
                asyncio.wait_for(refresh_all_expiring_tokens(), timeout=5))

        assert result["failed"] == 1
        assert result["refreshed"] == 0


# ---------------------------------------------------------------------------
# heal_invalid_accounts must not deadlock and must not phantom-heal
# ---------------------------------------------------------------------------


class TestHealNoDeadlock:
    def test_heal_expired_invalid_account_completes(self, tmp_path):
        """Heal over an invalid account with an expired token finishes —
        hung forever pre-fix (outer lock + flow's inner acquire)."""
        db, acct = _make_db(
            tmp_path, expires_at=int(time.time()) - 100,
            validation_status="invalid",
        )
        exchange = _success_exchange()

        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.fetch_profile", new_callable=AsyncMock):
            result = asyncio.run(
                asyncio.wait_for(heal_invalid_accounts(), timeout=5))

        assert result["healed"] == 1
        assert result["confirmed_invalid"] == 0
        exchange.assert_awaited_once()
        assert db.get_account(acct["id"])["validation_status"] == "valid"

    def test_heal_exchanges_even_when_token_not_expiring(self, tmp_path):
        """Healing must perform a real exchange even for an unexpired token
        (refresh_account_token's should_refresh gate used to skip it)."""
        db, acct = _make_db(
            tmp_path, expires_at=int(time.time()) + 3600,
            validation_status="invalid",
        )
        exchange = _success_exchange()

        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.fetch_profile", new_callable=AsyncMock):
            result = asyncio.run(
                asyncio.wait_for(heal_invalid_accounts(), timeout=5))

        exchange.assert_awaited_once()
        assert result["healed"] == 1
        assert db.get_account(acct["id"])["validation_status"] == "valid"

    def test_failed_exchange_does_not_phantom_heal(self, tmp_path):
        """Regression: with an unexpired token and a failing exchange, the
        old code reported phantom success (should_refresh gate returned True
        without exchanging) and wrote validation_status='valid'."""
        db, acct = _make_db(
            tmp_path, expires_at=int(time.time()) + 3600,
            validation_status="invalid",
        )
        exchange = AsyncMock(return_value=TokenExchangeResult(
            success=False, error="invalid_grant", status_code=400))
        validate = AsyncMock(return_value={"valid": False, "error": "still dead"})

        with patch("jacked.web.auth.Database", return_value=db), \
             patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.validate_account", validate), \
             patch("jacked.api.credential_helpers."
                   "reconcile_credentials_from_live_store", MagicMock()):
            result = asyncio.run(
                asyncio.wait_for(heal_invalid_accounts(), timeout=5))

        exchange.assert_awaited_once()
        assert result["healed"] == 0
        assert result["confirmed_invalid"] == 1
        assert db.get_account(acct["id"])["validation_status"] != "valid"


# ---------------------------------------------------------------------------
# _refresh_token_flow: bounded lock acquire
# ---------------------------------------------------------------------------


class TestBoundedLockAcquire:
    def test_held_lock_returns_lock_timeout(self, tmp_path, monkeypatch):
        """A pre-acquired primary lock yields error='lock_timeout', not a hang."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)
        monkeypatch.setattr(auth, "_REFRESH_LOCK_ACQUIRE_TIMEOUT", 0.1)
        exchange = AsyncMock()

        async def scenario():
            lock = auth._get_refresh_lock(acct["id"])
            await lock.acquire()
            try:
                return await auth._refresh_token_flow(
                    acct["id"], db, RefreshMode.PRIMARY)
            finally:
                lock.release()

        with patch("jacked.web.auth._exchange_refresh_token", exchange):
            result = asyncio.run(asyncio.wait_for(scenario(), timeout=5))

        assert result.success is False
        assert result.error == "lock_timeout"
        exchange.assert_not_awaited()


# ---------------------------------------------------------------------------
# PRIMARY post-refresh profile fetch runs outside the lock
# ---------------------------------------------------------------------------


class TestProfileFetchOutsideLock:
    def test_profile_401_reentry_does_not_deadlock(self, tmp_path):
        """fetch_profile's 401 path re-enters _refresh_token_flow on the same
        primary lock — pre-fix this self-deadlocked because the profile fetch
        ran while the lock was still held."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)
        exchange = _success_exchange()

        async def fake_profile(account_id, db_, access_token=None, **kwargs):
            # Mimic the 401-recovery re-entry, then fail like a flaky API
            await auth._try_refresh_primary_token(account_id, db_)
            raise RuntimeError("simulated profile fetch failure")

        with patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.fetch_profile",
                   new=AsyncMock(side_effect=fake_profile)):
            result = asyncio.run(asyncio.wait_for(
                auth._refresh_token_flow(acct["id"], db, RefreshMode.PRIMARY),
                timeout=5))

        # Outer refresh succeeds; the re-entrant recovery exchange also ran
        assert result.success is True
        assert exchange.await_count == 2
        assert db.get_account(acct["id"])["access_token"] == "new-at"

    def test_profile_fetch_exception_is_non_fatal(self, tmp_path):
        """A plain exception from the post-refresh profile fetch is swallowed."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)
        exchange = _success_exchange()

        with patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.fetch_profile",
                   new=AsyncMock(side_effect=RuntimeError("boom"))):
            result = asyncio.run(asyncio.wait_for(
                auth._refresh_token_flow(acct["id"], db, RefreshMode.PRIMARY),
                timeout=5))

        assert result.success is True
        assert db.get_account(acct["id"])["access_token"] == "new-at"

    def test_lock_is_released_after_flow(self, tmp_path):
        """The primary lock must be free once the flow returns (finally path)."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)
        exchange = _success_exchange()

        async def scenario():
            res = await auth._refresh_token_flow(
                acct["id"], db, RefreshMode.PRIMARY)
            return res, auth._get_refresh_lock(acct["id"]).locked()

        with patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.fetch_profile", new_callable=AsyncMock):
            result, still_locked = asyncio.run(
                asyncio.wait_for(scenario(), timeout=5))

        assert result.success is True
        assert still_locked is False


# ---------------------------------------------------------------------------
# Caller cancellation must not lose a rotated refresh token
# ---------------------------------------------------------------------------


class TestShieldedRefreshSurvivesCancellation:
    """Anthropic ROTATES refresh tokens on exchange. Every refresh path is
    wrapped in asyncio.wait_for somewhere (routes, sweep, heal, poll loop);
    a cancellation landing after the exchange POST is consumed upstream but
    before the rotated pair is persisted used to discard the new refresh
    token forever (permanent invalid_grant). The locked section now runs as
    a shielded task that finishes the exchange + DB write + lock release in
    the background while the caller observes its timeout."""

    def test_timeout_mid_exchange_still_persists_rotated_token(self, tmp_path):
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_exchange(refresh_token, timeout=15.0):
                started.set()
                # Hold the "POST in flight" window open past the caller's
                # timeout — upstream has consumed the old refresh token.
                await release.wait()
                return TokenExchangeResult(
                    success=True, access_token="new-at",
                    refresh_token="new-rt", expires_in=3600,
                )

            with patch("jacked.web.auth._exchange_refresh_token",
                       new=AsyncMock(side_effect=slow_exchange)), \
                 patch("jacked.web.auth.fetch_profile", new_callable=AsyncMock):
                caller = asyncio.create_task(asyncio.wait_for(
                    auth._refresh_token_flow(acct["id"], db, RefreshMode.PRIMARY),
                    timeout=0.1,
                ))
                await started.wait()
                with pytest.raises(asyncio.TimeoutError):
                    await caller
                # Pre-fix: the cancellation killed the exchange here and the
                # rotated token was lost. Now the shielded task finishes.
                release.set()
                pending = list(auth._inflight_refresh_tasks)
                if pending:
                    await asyncio.gather(*pending)
                await asyncio.sleep(0)  # let done-callbacks run
                return (
                    db.get_account(acct["id"]),
                    auth._get_refresh_lock(acct["id"]).locked(),
                    len(auth._inflight_refresh_tasks),
                )

        row, still_locked, inflight = asyncio.run(
            asyncio.wait_for(scenario(), timeout=5))

        assert row["refresh_token"] == "new-rt", (
            "rotated refresh token must be persisted despite caller timeout"
        )
        assert row["access_token"] == "new-at"
        assert still_locked is False
        assert inflight == 0

    def test_direct_cancel_mid_exchange_still_persists_rotated_token(self, tmp_path):
        """Direct task cancellation (e.g. SWEEP_PASS_TIMEOUT cancelling a
        whole pass mid-account) behaves the same as a wait_for timeout."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)

        async def scenario():
            started = asyncio.Event()
            release = asyncio.Event()

            async def slow_exchange(refresh_token, timeout=15.0):
                started.set()
                await release.wait()
                return TokenExchangeResult(
                    success=True, access_token="new-at",
                    refresh_token="new-rt", expires_in=3600,
                )

            with patch("jacked.web.auth._exchange_refresh_token",
                       new=AsyncMock(side_effect=slow_exchange)), \
                 patch("jacked.web.auth.fetch_profile", new_callable=AsyncMock):
                caller = asyncio.create_task(
                    auth._refresh_token_flow(acct["id"], db, RefreshMode.PRIMARY))
                await started.wait()
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
                release.set()
                pending = list(auth._inflight_refresh_tasks)
                if pending:
                    await asyncio.gather(*pending)
                return (
                    db.get_account(acct["id"]),
                    auth._get_refresh_lock(acct["id"]).locked(),
                )

        row, still_locked = asyncio.run(asyncio.wait_for(scenario(), timeout=5))

        assert row["refresh_token"] == "new-rt"
        assert row["access_token"] == "new-at"
        assert still_locked is False

    def test_uncancelled_flow_unchanged(self, tmp_path):
        """The shield must not change the happy path: result, DB state, and
        post-refresh profile fetch all behave exactly as before."""
        db, acct = _make_db(tmp_path, expires_at=int(time.time()) - 100)
        exchange = _success_exchange()
        profile = AsyncMock()

        with patch("jacked.web.auth._exchange_refresh_token", exchange), \
             patch("jacked.web.auth.fetch_profile", profile):
            result = asyncio.run(asyncio.wait_for(
                auth._refresh_token_flow(acct["id"], db, RefreshMode.PRIMARY),
                timeout=5))

        assert result.success is True
        assert result.refresh_token == "new-rt"
        profile.assert_awaited_once()
        assert db.get_account(acct["id"])["refresh_token"] == "new-rt"
        assert not auth._inflight_refresh_tasks
