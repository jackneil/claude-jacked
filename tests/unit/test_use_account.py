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
