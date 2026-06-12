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
        db.update_account(
            a["id"], validation_status="unknown",
            refresh_token="refresh_token_xyz",
            last_error="old error",
            last_error_at=datetime.now(timezone.utc).isoformat(),
        )

        async def _run():
            from jacked.web import auth as auth_mod

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
