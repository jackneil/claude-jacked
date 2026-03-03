"""Tests for account label (display_name) via PATCH /api/auth/accounts/{id}."""

import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from jacked.api.main import app


def _make_account_row(**overrides):
    """Minimal account row dict matching DB schema."""
    base = {
        "id": 1,
        "email": "test@example.com",
        "organization_uuid": "",
        "organization_name": None,
        "display_name": None,
        "expires_at": int(time.time()) + 3600,
        "scopes": None,
        "subscription_type": "max_5x",
        "rate_limit_tier": None,
        "has_extra_usage": False,
        "priority": 0,
        "is_active": True,
        "is_deleted": False,
        "last_used_at": None,
        "cached_usage_5h": None,
        "cached_usage_7d": None,
        "cached_5h_resets_at": None,
        "cached_7d_resets_at": None,
        "usage_cached_at": None,
        "last_error": None,
        "last_error_at": None,
        "consecutive_failures": 0,
        "last_validated_at": None,
        "validation_status": "unknown",
        "created_at": "2025-01-01T00:00:00",
        "updated_at": "2025-01-01T00:00:00",
        "cc_expires_at": None,
        "has_cc_token": False,
        "cc_needs_auth": False,
    }
    base.update(overrides)
    return base


@pytest.fixture()
def mock_db():
    db = MagicMock()
    app.state.db = db
    yield db
    app.state.db = None


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


class TestPatchDisplayName:
    """PATCH /api/auth/accounts/{id} — display_name handling."""

    def test_set_label(self, client, mock_db):
        row = _make_account_row()
        updated = _make_account_row(display_name="Work Max")
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "Work Max"})

        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Work Max"
        mock_db.update_account.assert_called_once_with(1, display_name="Work Max")

    def test_clear_label_empty_string(self, client, mock_db):
        row = _make_account_row(display_name="Work Max")
        updated = _make_account_row(display_name=None)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": ""})

        assert resp.status_code == 200
        assert resp.json()["display_name"] is None
        mock_db.update_account.assert_called_once_with(1, display_name=None)

    def test_clear_label_null(self, client, mock_db):
        row = _make_account_row(display_name="Work Max")
        updated = _make_account_row(display_name=None)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": None})

        assert resp.status_code == 200
        assert resp.json()["display_name"] is None
        mock_db.update_account.assert_called_once_with(1, display_name=None)

    def test_field_not_sent_preserves_label(self, client, mock_db):
        """PATCH without display_name field should not touch existing label."""
        row = _make_account_row(display_name="Work Max")
        mock_db.get_account.side_effect = [row, row]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"is_active": True})

        assert resp.status_code == 200
        mock_db.update_account.assert_called_once_with(1, is_active=True)

    def test_whitespace_only_clears(self, client, mock_db):
        row = _make_account_row()
        updated = _make_account_row(display_name=None)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "   "})

        assert resp.status_code == 200
        mock_db.update_account.assert_called_once_with(1, display_name=None)

    def test_strips_whitespace(self, client, mock_db):
        row = _make_account_row()
        updated = _make_account_row(display_name="Work")
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "  Work  "})

        assert resp.status_code == 200
        mock_db.update_account.assert_called_once_with(1, display_name="Work")

    def test_empty_body_returns_400(self, client, mock_db):
        row = _make_account_row()
        mock_db.get_account.return_value = row

        resp = client.patch("/api/auth/accounts/1", json={})

        assert resp.status_code == 400
        assert "No fields to update" in resp.json()["error"]["message"]

    def test_nonexistent_account_returns_404(self, client, mock_db):
        mock_db.get_account.return_value = None

        resp = client.patch("/api/auth/accounts/999", json={"display_name": "X"})

        assert resp.status_code == 404

    def test_too_long_returns_422(self, client, mock_db):
        resp = client.patch(
            "/api/auth/accounts/1",
            json={"display_name": "A" * 51},
        )
        assert resp.status_code == 422

    def test_unicode_emoji(self, client, mock_db):
        label = "Cuenta de Trabajo \U0001f4bc"
        row = _make_account_row()
        updated = _make_account_row(display_name=label)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": label})

        assert resp.status_code == 200
        assert resp.json()["display_name"] == label

    def test_exact_max_length_accepted(self, client, mock_db):
        label = "A" * 50
        row = _make_account_row()
        updated = _make_account_row(display_name=label)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": label})

        assert resp.status_code == 200
        assert resp.json()["display_name"] == label

    def test_update_returns_false_gives_404(self, client, mock_db):
        """TOCTOU: account deleted between existence check and update."""
        row = _make_account_row()
        mock_db.get_account.return_value = row
        mock_db.update_account.return_value = False

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "X"})

        assert resp.status_code == 404

    def test_update_raises_returns_500(self, client, mock_db):
        """DB write fails (e.g., disk full, lock timeout)."""
        import sqlite3

        row = _make_account_row()
        mock_db.get_account.return_value = row
        mock_db.update_account.side_effect = sqlite3.OperationalError("disk full")

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "X"})

        assert resp.status_code == 500

    def test_set_is_active(self, client, mock_db):
        row = _make_account_row(is_active=True)
        updated = _make_account_row(is_active=False)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"is_active": False})

        assert resp.status_code == 200
        assert resp.json()["is_active"] is False
        mock_db.update_account.assert_called_once_with(1, is_active=False)

    def test_combined_label_and_is_active(self, client, mock_db):
        row = _make_account_row()
        updated = _make_account_row(display_name="Work", is_active=False)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.update_account.return_value = True

        resp = client.patch(
            "/api/auth/accounts/1",
            json={"display_name": "Work", "is_active": False},
        )

        assert resp.status_code == 200
        mock_db.update_account.assert_called_once_with(
            1, display_name="Work", is_active=False
        )

    def test_db_unavailable_returns_503(self, client):
        app.state.db = None

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "X"})

        assert resp.status_code == 503
