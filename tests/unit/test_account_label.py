"""Tests for account label (display_name) via PATCH /api/auth/accounts/{id}.

Labels are changed exclusively through db.set_account_label() — the
update_account() whitelist deliberately excludes display_name.
"""

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
    """PATCH /api/auth/accounts/{id} — display_name via set_account_label()."""

    def test_set_label(self, client, mock_db):
        row = _make_account_row()
        updated = _make_account_row(display_name="Work Max")
        mock_db.get_account.side_effect = [row, updated]
        mock_db.set_account_label.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "Work Max"})

        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Work Max"
        mock_db.set_account_label.assert_called_once_with(1, "Work Max")
        mock_db.update_account.assert_not_called()

    def test_clear_label_empty_string(self, client, mock_db):
        row = _make_account_row(display_name="Work Max")
        updated = _make_account_row(display_name=None)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.set_account_label.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": ""})

        assert resp.status_code == 200
        assert resp.json()["display_name"] is None
        mock_db.set_account_label.assert_called_once_with(1, None)

    def test_clear_label_null(self, client, mock_db):
        row = _make_account_row(display_name="Work Max")
        updated = _make_account_row(display_name=None)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.set_account_label.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": None})

        assert resp.status_code == 200
        assert resp.json()["display_name"] is None
        mock_db.set_account_label.assert_called_once_with(1, None)

    def test_field_not_sent_preserves_label(self, client, mock_db):
        """PATCH without display_name field should not touch existing label."""
        row = _make_account_row(display_name="Work Max")
        mock_db.get_account.side_effect = [row, row]
        mock_db.update_account.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"is_active": True})

        assert resp.status_code == 200
        mock_db.set_account_label.assert_not_called()
        mock_db.update_account.assert_called_once_with(1, is_active=True)

    def test_whitespace_only_clears(self, client, mock_db):
        row = _make_account_row()
        updated = _make_account_row(display_name=None)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.set_account_label.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "   "})

        assert resp.status_code == 200
        mock_db.set_account_label.assert_called_once_with(1, None)

    def test_strips_whitespace(self, client, mock_db):
        row = _make_account_row()
        updated = _make_account_row(display_name="Work")
        mock_db.get_account.side_effect = [row, updated]
        mock_db.set_account_label.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "  Work  "})

        assert resp.status_code == 200
        mock_db.set_account_label.assert_called_once_with(1, "Work")

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
        mock_db.set_account_label.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": label})

        assert resp.status_code == 200
        assert resp.json()["display_name"] == label

    def test_exact_max_length_accepted(self, client, mock_db):
        label = "A" * 50
        row = _make_account_row()
        updated = _make_account_row(display_name=label)
        mock_db.get_account.side_effect = [row, updated]
        mock_db.set_account_label.return_value = True

        resp = client.patch("/api/auth/accounts/1", json={"display_name": label})

        assert resp.status_code == 200
        assert resp.json()["display_name"] == label

    def test_is_active_update_returns_false_gives_404(self, client, mock_db):
        """TOCTOU: account deleted between existence check and update."""
        row = _make_account_row()
        mock_db.get_account.return_value = row
        mock_db.update_account.return_value = False

        resp = client.patch("/api/auth/accounts/1", json={"is_active": False})

        assert resp.status_code == 404

    def test_label_raises_returns_500(self, client, mock_db):
        """DB write fails (e.g., disk full, lock timeout)."""
        import sqlite3

        row = _make_account_row()
        mock_db.get_account.return_value = row
        mock_db.set_account_label.side_effect = sqlite3.OperationalError("disk full")

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
        mock_db.set_account_label.return_value = True
        mock_db.update_account.return_value = True

        resp = client.patch(
            "/api/auth/accounts/1",
            json={"display_name": "Work", "is_active": False},
        )

        assert resp.status_code == 200
        mock_db.set_account_label.assert_called_once_with(1, "Work")
        mock_db.update_account.assert_called_once_with(1, is_active=False)

    def test_db_unavailable_returns_503(self, client):
        app.state.db = None

        resp = client.patch("/api/auth/accounts/1", json={"display_name": "X"})

        assert resp.status_code == 503


class TestUpdateAccountWhitelist:
    """update_account() must reject display_name."""

    def test_display_name_rejected(self):
        from jacked.web.database import Database

        db = Database(":memory:")
        acct = db.create_account("wl@test.com", "tok", 9999999999)
        with pytest.raises(ValueError, match="display_name"):
            db.update_account(acct["id"], display_name="Sneaky")


class TestSetAccountLabel:
    """Database.set_account_label() unit tests."""

    def test_set_and_read(self):
        from jacked.web.database import Database

        db = Database(":memory:")
        acct = db.create_account("lab@test.com", "tok", 9999999999)
        assert db.set_account_label(acct["id"], "Work") is True
        assert db.get_account(acct["id"])["display_name"] == "Work"

    def test_clear_label(self):
        from jacked.web.database import Database

        db = Database(":memory:")
        acct = db.create_account(
            "lab@test.com", "tok", 9999999999, display_name="Old",
        )
        assert db.set_account_label(acct["id"], None) is True
        assert db.get_account(acct["id"])["display_name"] is None

    def test_nonexistent_returns_false(self):
        from jacked.web.database import Database

        db = Database(":memory:")
        assert db.set_account_label(999, "Nope") is False


class TestLabelAuditLog:
    """display_name_audit trigger + get_label_audit_log()."""

    def test_audit_on_label_change(self):
        from jacked.web.database import Database

        db = Database(":memory:")
        acct = db.create_account("aud@test.com", "tok", 9999999999)
        db.set_account_label(acct["id"], "Alpha")
        db.set_account_label(acct["id"], "Beta")

        log = db.get_label_audit_log()
        # Most recent first
        assert len(log) >= 2
        assert log[0]["old_value"] == "Alpha"
        assert log[0]["new_value"] == "Beta"
        assert log[1]["old_value"] is None
        assert log[1]["new_value"] == "Alpha"
        assert log[0]["email"] == "aud@test.com"

    def test_audit_on_create_with_label(self):
        from jacked.web.database import Database

        db = Database(":memory:")
        db.create_account(
            "cre@test.com", "tok", 9999999999, display_name="Initial",
        )
        log = db.get_label_audit_log()
        assert any(
            e["new_value"] == "Initial" and e["old_value"] is None for e in log
        )


class TestDisplayNameAuditEndpoint:
    """GET /api/auth/display-name-audit endpoint."""

    def test_returns_entries(self, client, mock_db):
        mock_db.get_label_audit_log.return_value = [
            {
                "id": 1,
                "account_id": 1,
                "old_value": None,
                "new_value": "Work",
                "changed_at": "2025-01-01 00:00:00",
                "email": "test@example.com",
            },
        ]

        resp = client.get("/api/auth/display-name-audit")

        assert resp.status_code == 200
        data = resp.json()
        assert len(data["entries"]) == 1
        assert data["entries"][0]["new_value"] == "Work"

    def test_db_unavailable(self, client):
        app.state.db = None

        resp = client.get("/api/auth/display-name-audit")

        assert resp.status_code == 503
