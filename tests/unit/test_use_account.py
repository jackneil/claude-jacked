"""Tests for the /accounts/{id}/use endpoint (dashboard account switching)."""

import json
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from jacked.api.routes.auth import router
from jacked.web.database import Database


@pytest.fixture
def db(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    with db._writer() as conn:
        # Account 1: fully valid with CC tokens
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (1, 'alice@test.com', 'at_1', 'rt_1', 1900000000,
                       1, 0, 'valid', 'max', 't1',
                       'cc_at_1', 'cc_rt_1', 1900000000,
                       NULL, 0, NULL)"""
        )
        # Account 2: disabled
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (2, 'bob@test.com', 'at_2', 'rt_2', 1900000000,
                       0, 0, 'valid', 'pro', 't2',
                       NULL, 0, NULL)"""
        )
        # Account 3: valid but no CC tokens
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                scopes, consecutive_failures, last_error)
               VALUES (3, 'carol@test.com', 'at_3', 'rt_3', 1900000000,
                       1, 0, 'valid', 'pro', 't2',
                       NULL, 0, NULL)"""
        )
        # Account 4: invalid validation status
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (4, 'dave@test.com', 'at_4', 'rt_4', 1900000000,
                       1, 0, 'invalid', 'max', 't1',
                       'cc_at_4', 'cc_rt_4', 1900000000,
                       NULL, 2, 'Token revoked')"""
        )
        # Account 5: soft-deleted
        conn.execute(
            """INSERT INTO accounts
               (id, email, access_token, refresh_token, expires_at,
                is_active, is_deleted, validation_status,
                subscription_type, rate_limit_tier,
                cc_access_token, cc_refresh_token, cc_expires_at,
                scopes, consecutive_failures, last_error)
               VALUES (5, 'eve@test.com', 'at_5', 'rt_5', 1900000000,
                       1, 1, 'valid', 'max', 't1',
                       'cc_at_5', 'cc_rt_5', 1900000000,
                       NULL, 0, NULL)"""
        )
    yield db
    db.close()


@pytest.fixture
def app(db, tmp_path):
    from fastapi import FastAPI
    app = FastAPI()
    app.include_router(router, prefix="/api/auth")
    app.state.db = db
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


def test_use_account_success(client, tmp_path):
    """Activating a valid account with CC tokens writes credentials to all stores."""
    with mock.patch(
        "jacked.api.credential_helpers.sync_credential_to_all_stores"
    ) as mock_sync:
        resp = client.post("/api/auth/accounts/1/use")

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "active"
    assert data["email"] == "alice@test.com"
    mock_sync.assert_called_once()
    assert mock_sync.call_args.args[0] == 1  # account_id
    assert mock_sync.call_args.args[1]["email"] == "alice@test.com"


def test_use_account_not_found(client):
    """Returns 404 for non-existent account."""
    resp = client.post("/api/auth/accounts/999/use")
    assert resp.status_code == 404


def test_use_account_disabled(client):
    """Returns 400 for disabled account."""
    resp = client.post("/api/auth/accounts/2/use")
    assert resp.status_code == 400
    assert "disabled" in resp.json()["error"]["message"].lower()


def test_use_account_no_cc_tokens(client):
    """Returns 400 for account without CC tokens (would be un-refreshable)."""
    resp = client.post("/api/auth/accounts/3/use")
    assert resp.status_code == 400
    assert "cc" in resp.json()["error"]["message"].lower() or \
           "authorize" in resp.json()["error"]["message"].lower()


def test_use_account_invalid_status(client):
    """Returns 400 for account with invalid validation status."""
    resp = client.post("/api/auth/accounts/4/use")
    assert resp.status_code == 400
    assert "invalid" in resp.json()["error"]["message"].lower() or \
           "re-auth" in resp.json()["error"]["message"].lower()


def test_use_account_deleted(client):
    """Returns 404 for soft-deleted account."""
    resp = client.post("/api/auth/accounts/5/use")
    assert resp.status_code == 404


def test_refresh_usage_uses_fresh_token_for_active_account(client, tmp_path):
    """Usage refresh reads fresh token from credential stores for active account.
    Only passes the fresh token when it differs from the DB token."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="fresh_token_from_keychain",
        ) as mock_fresh,
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.3}},
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    mock_fresh.assert_called_once_with(1)
    # DB token is "at_1", fresh token is different → should be passed
    assert mock_fetch.call_args.kwargs.get("access_token") == "fresh_token_from_keychain"


def test_refresh_usage_skips_fresh_when_unchanged(client, tmp_path):
    """When fresh token matches DB token, don't pass it (preserves cache guard)."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="at_1",  # same as DB token for account 1
        ),
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.3}},
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    # Token unchanged → access_token should be None (cache guard intact)
    assert mock_fetch.call_args.kwargs.get("access_token") is None


def test_refresh_usage_falls_back_to_db_token(client, tmp_path):
    """When credential stores don't have this account, falls back to DB token."""
    with (
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value=None,
        ),
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            return_value={"five_hour": {"utilization": 0.5}, "seven_day": {"utilization": 0.3}},
        ) as mock_fetch,
    ):
        resp = client.post("/api/auth/accounts/1/refresh-usage")

    assert resp.status_code == 200
    # No fresh token → access_token should be None
    assert mock_fetch.call_args.kwargs.get("access_token") is None


def test_refresh_all_usage_only_reads_fresh_for_active(client, db, tmp_path):
    """Bulk refresh reads credential file once, only passes fresh token for active account."""
    # Write a credential file with active account ID = 1
    cred_dir = tmp_path / ".claude"
    cred_dir.mkdir(exist_ok=True)
    cred_path = cred_dir / ".credentials.json"
    cred_path.write_text(json.dumps({
        "_jackedAccountId": 1,
        "claudeAiOauth": {"accessToken": "cc_refreshed_token"},
    }))

    call_tokens = {}

    async def capture_fetch_usage(account_id, db_arg, access_token=None):
        call_tokens[account_id] = access_token
        return {"five_hour": {"utilization": 0.1}, "seven_day": {"utilization": 0.2}}

    with (
        mock.patch("jacked.api.credential_helpers.Path.home", return_value=tmp_path),
        mock.patch(
            "jacked.api.credential_helpers.read_fresh_active_token",
            return_value="cc_refreshed_token",
        ) as mock_fresh,
        mock.patch(
            "jacked.api.routes.auth.fetch_usage",
            side_effect=capture_fetch_usage,
        ),
    ):
        resp = client.post("/api/auth/accounts/refresh-all-usage")

    assert resp.status_code == 200
    # Account 1 is active — should get fresh token (differs from DB "at_1")
    assert call_tokens.get(1) == "cc_refreshed_token"
    # Account 3 is not active — should get None (DB token used internally)
    assert call_tokens.get(3) is None
    # read_fresh_active_token called only for the active account
    mock_fresh.assert_called_once_with(1)
