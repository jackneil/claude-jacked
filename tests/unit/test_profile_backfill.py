"""AC1: the one-shot paced startup profile backfill.

For every claude row (not deleted) with no stored profile, the backfill
calls fetch_profile(allow_refresh=False) one row at a time with a pause,
never raises, never refreshes a token, and logs ONE coverage line.
"""

import asyncio
import logging
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import jacked.web.auth as auth
from jacked.web.database import Database

ALICE_UUID = "4f1c2a9e-1111-4c3b-9d2e-aaaaaaaaaaaa"
BOB_UUID = "7a6b5c4d-4444-4f3e-8d2c-dddddddddddd"
ORG_A = "9b0e7d55-2222-4a1f-8c3d-bbbbbbbbbbbb"


def _profile(email, uuid, org_uuid=ORG_A):
    return {
        "account": {
            "uuid": uuid,
            "email": email,
            "display_name": email.split("@")[0],
            "full_name": None,
            "created_at": "2024-11-02T08:30:00Z",
        },
        "organization": {
            "uuid": org_uuid,
            "name": "Acme",
            "organization_type": "claude_max",
            "billing_type": "stripe_subscription",
            "rate_limit_tier": "default_claude_max_20x",
            "seat_tier": None,
            "has_extra_usage_enabled": False,
            "subscription_created_at": "2025-03-01T10:00:00Z",
            "claude_code_trial_ends_at": None,
            "claude_code_trial_duration_days": None,
        },
    }


def _resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body or {}
    return r


def _client(by_token: dict):
    """An httpx.AsyncClient stand-in that answers per bearer token."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)

    async def _get(url, headers=None, **kw):
        token = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
        result = by_token[token]
        if isinstance(result, Exception):
            raise result
        return result

    client.get = AsyncMock(side_effect=_get)
    return client


@pytest.fixture
def db(tmp_path):
    d = Database(str(tmp_path / "t.db"))
    yield d
    d.close()


def _add(db, email, token, *, provider="claude", expires_in=3600):
    return db.create_account(
        email,
        token,
        int(time.time()) + expires_in,
        organization_uuid=ORG_A,
        organization_name="Acme",
        provider=provider,
    )


def _run(db, client, **kw):
    sleeps = []

    async def _fake_sleep(s):
        sleeps.append(s)

    with patch.object(auth.httpx, "AsyncClient", return_value=client), \
         patch.object(auth.asyncio, "sleep", _fake_sleep):
        result = asyncio.run(auth.backfill_missing_profiles(db, **kw))
    return result, sleeps


class TestProfileBackfill:
    def test_fills_claude_rows_missing_profile(self, db, caplog):
        a = _add(db, "alice@test.com", "tok-a")
        b = _add(db, "bob@test.com", "tok-b")
        client = _client({
            "tok-a": _resp(200, _profile("alice@test.com", ALICE_UUID)),
            "tok-b": _resp(200, _profile("bob@test.com", BOB_UUID)),
        })
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            result, sleeps = _run(db, client, pause_seconds=1.0)

        assert db.get_account(a["id"])["account_uuid"] == ALICE_UUID
        assert db.get_account(b["id"])["account_uuid"] == BOB_UUID
        assert db.get_account(a["id"])["profile_json"] is not None
        assert result["fetched"] == 2
        assert result["failed"] == 0
        # Paced: a pause between rows, never a burst.
        assert sleeps and all(s == 1.0 for s in sleeps)
        assert client.get.await_count == 2

    def test_skips_codex_deleted_and_already_filled(self, db):
        filled = _add(db, "filled@test.com", "tok-f")
        db.update_account(filled["id"], profile_json="{}")
        _add(db, "codex@test.com", "tok-c", provider="codex")
        gone = _add(db, "gone@test.com", "tok-g")
        db.delete_account(gone["id"])
        client = _client({})  # any GET would KeyError -> counted as failure
        result, _ = _run(db, client, pause_seconds=0)
        assert client.get.await_count == 0
        assert result["fetched"] == 0 and result["failed"] == 0

    def test_includes_inactive_claude_rows(self, db):
        a = _add(db, "alice@test.com", "tok-a")
        db.update_account(a["id"], is_active=False)
        client = _client({"tok-a": _resp(200, _profile("alice@test.com", ALICE_UUID))})
        _run(db, client, pause_seconds=0)
        assert db.get_account(a["id"])["account_uuid"] == ALICE_UUID

    def test_401_never_refreshes(self, db):
        a = _add(db, "alice@test.com", "tok-a")
        refresh = AsyncMock(return_value="fresh")
        client = _client({"tok-a": _resp(401)})
        with patch.object(auth, "_try_refresh_primary_token", refresh), \
             patch.object(auth, "_refresh_token_flow", AsyncMock(side_effect=AssertionError)):
            result, _ = _run(db, client, pause_seconds=0)
        refresh.assert_not_awaited()
        assert result["failed"] == 1
        assert db.get_account(a["id"])["profile_json"] is None

    def test_skips_expired_primary_token(self, db):
        a = _add(db, "alice@test.com", "tok-a", expires_in=-60)
        client = _client({})
        result, _ = _run(db, client, pause_seconds=0)
        assert client.get.await_count == 0
        assert result["skipped_expired"] == 1
        assert db.get_account(a["id"])["profile_json"] is None

    def test_one_failure_does_not_stop_the_rest(self, db):
        _add(db, "alice@test.com", "tok-a")
        b = _add(db, "bob@test.com", "tok-b")
        client = _client({
            "tok-a": RuntimeError("boom"),
            "tok-b": _resp(200, _profile("bob@test.com", BOB_UUID)),
        })
        result, _ = _run(db, client, pause_seconds=0)
        assert db.get_account(b["id"])["account_uuid"] == BOB_UUID
        assert result["fetched"] == 1
        assert result["failed"] == 1

    def test_never_raises_on_db_failure(self, caplog):
        broken = MagicMock()
        broken.list_claude_accounts_missing_profile.side_effect = RuntimeError("db gone")
        with caplog.at_level(logging.WARNING, logger="jacked.auth"):
            result = asyncio.run(auth.backfill_missing_profiles(broken, pause_seconds=0))
        assert result is None

    def test_foreign_profile_is_not_stored(self, db):
        a = _add(db, "alice@test.com", "tok-a")
        client = _client({"tok-a": _resp(200, _profile("bob@test.com", BOB_UUID))})
        _run(db, client, pause_seconds=0)
        row = db.get_account(a["id"])
        assert row["account_uuid"] is None
        assert row["profile_json"] is None

    def test_logs_one_values_free_coverage_line(self, db, caplog):
        _add(db, "alice@test.com", "tok-a")
        _add(db, "bob@test.com", "tok-b")
        client = _client({
            "tok-a": _resp(200, _profile("alice@test.com", ALICE_UUID)),
            "tok-b": _resp(401),
        })
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            _run(db, client, pause_seconds=0)
        coverage = [r.getMessage() for r in caplog.records
                    if "identity coverage" in r.getMessage()]
        assert len(coverage) == 1
        line = coverage[0]
        assert "with_uuid=1" in line and "without_uuid=1" in line
        assert "with_profile=1" in line and "without_profile=1" in line
        all_text = " ".join(r.getMessage() for r in caplog.records)
        for secret in ("alice@test.com", "bob@test.com", "tok-a", "tok-b", ALICE_UUID):
            assert secret not in all_text

    def test_coverage_line_logged_when_nothing_to_do(self, db, caplog):
        a = _add(db, "alice@test.com", "tok-a")
        db.update_account(a["id"], profile_json="{}", account_uuid=ALICE_UUID)
        with caplog.at_level(logging.INFO, logger="jacked.auth"):
            _run(db, _client({}), pause_seconds=0)
        coverage = [r.getMessage() for r in caplog.records
                    if "identity coverage" in r.getMessage()]
        assert len(coverage) == 1
        assert "with_uuid=1" in coverage[0]


class TestBackfillScheduledAtStartup:
    def test_lifespan_schedules_backfill_once(self, monkeypatch):
        """The lifespan starts the backfill as a background task (non-blocking)
        and cancels it on shutdown."""
        from fastapi.testclient import TestClient

        import jacked.api.main as api_main

        calls = []

        async def _fake_backfill_task(app):
            calls.append(app)

        monkeypatch.setattr(api_main, "_profile_backfill_once", _fake_backfill_task)
        with TestClient(api_main.app):
            pass
        assert len(calls) == 1

    def test_backfill_task_waits_then_runs_with_db(self, monkeypatch):
        import jacked.api.main as api_main

        seen = {}

        async def _fake_backfill(db, **kw):
            seen["db"] = db

        sleeps = []

        async def _fake_sleep(s):
            sleeps.append(s)

        monkeypatch.setattr(auth, "backfill_missing_profiles", _fake_backfill)
        monkeypatch.setattr(api_main.asyncio, "sleep", _fake_sleep)
        app = MagicMock()
        app.state.db = sentinel = object()
        asyncio.run(api_main._profile_backfill_once(app))
        assert seen["db"] is sentinel
        assert sleeps == [api_main.PROFILE_BACKFILL_DELAY]

    def test_backfill_task_skips_without_db(self, monkeypatch):
        import jacked.api.main as api_main

        called = AsyncMock()
        monkeypatch.setattr(auth, "backfill_missing_profiles", called)

        async def _no_sleep(s):
            return None

        monkeypatch.setattr(api_main.asyncio, "sleep", _no_sleep)
        app = MagicMock()
        app.state.db = None
        asyncio.run(api_main._profile_backfill_once(app))
        called.assert_not_awaited()

    def test_backfill_task_swallows_errors(self, monkeypatch):
        import jacked.api.main as api_main

        monkeypatch.setattr(
            auth, "backfill_missing_profiles", AsyncMock(side_effect=RuntimeError("x"))
        )

        async def _no_sleep(s):
            return None

        monkeypatch.setattr(api_main.asyncio, "sleep", _no_sleep)
        app = MagicMock()
        app.state.db = object()
        asyncio.run(api_main._profile_backfill_once(app))  # must not raise
