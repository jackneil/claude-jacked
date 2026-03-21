"""Tests for jacked.api.routes.profiles — REST API endpoints."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from jacked import __version__
from jacked.api.main import app


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db(**overrides):
    """Create a mock DB with get_setting / set_setting."""
    db = MagicMock()
    settings = {
        "gatekeeper.model": json.dumps("haiku"),
        "gatekeeper.eval_method": json.dumps("api_first"),
        "gatekeeper.command_categories": json.dumps({"network": "evaluate"}),
        "gatekeeper.path_safety": json.dumps({"enabled": True}),
        "gatekeeper.tools": json.dumps({"Bash": True}),
    }
    settings.update(overrides)
    db.get_setting.side_effect = lambda k: settings.get(k)
    db.set_setting = MagicMock()
    return db


def _valid_profile_dict(**overrides):
    """Return a minimal valid profile dict."""
    base = {
        "name": "test-profile",
        "description": "A test profile",
        "author": "tester",
        "jacked_version": __version__,
        "created_at": "2025-01-01T00:00:00+00:00",
        "rules": {"allow": ["Read(*:*)"], "deny": [], "ask": []},
        "gatekeeper_config": {"model": "haiku"},
    }
    base.update(overrides)
    return base


@pytest.fixture()
def mock_db():
    db = _make_db()
    app.state.db = db
    yield db
    app.state.db = None


@pytest.fixture()
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture()
def profiles_dir(tmp_path):
    d = tmp_path / "profiles"
    d.mkdir()
    return d


@pytest.fixture()
def backup_dir(tmp_path):
    d = tmp_path / "profiles" / ".backups"
    d.mkdir(parents=True)
    return d


@pytest.fixture()
def patch_dirs(profiles_dir, backup_dir):
    """Patch _get_profiles_dir and _get_backup_dir to use tmp_path fixtures."""
    with patch(
        "jacked.api.routes.profiles._get_profiles_dir", return_value=profiles_dir
    ), patch(
        "jacked.api.routes.profiles._get_backup_dir", return_value=backup_dir
    ):
        yield profiles_dir, backup_dir


@pytest.fixture()
def patch_settings(tmp_path):
    """Patch _read_settings_json and _write_settings_json to use a temp file."""
    settings_file = tmp_path / "settings.json"
    settings_file.write_text(
        json.dumps({"permissions": {"allow": ["Read(*:*)"], "deny": [], "ask": []}}),
        encoding="utf-8",
    )

    def _read():
        if settings_file.exists():
            return json.loads(settings_file.read_text(encoding="utf-8"))
        return {}

    def _write(data):
        settings_file.write_text(json.dumps(data, indent=2), encoding="utf-8")

    with patch(
        "jacked.api.routes.profiles._read_settings_json", side_effect=_read
    ), patch(
        "jacked.api.routes.profiles._write_settings_json", side_effect=_write
    ):
        yield settings_file


# ---------------------------------------------------------------------------
# GET / — list profiles
# ---------------------------------------------------------------------------


class TestListProfiles:
    def test_empty(self, client, patch_dirs):
        resp = client.get("/api/profiles/")
        assert resp.status_code == 200
        assert resp.json()["profiles"] == []

    def test_with_profiles(self, client, patch_dirs):
        profiles_dir, _ = patch_dirs
        profile = _valid_profile_dict()
        (profiles_dir / "test-profile.json").write_text(
            json.dumps(profile), encoding="utf-8"
        )
        resp = client.get("/api/profiles/")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["profiles"]) == 1
        assert data["profiles"][0]["name"] == "test-profile"

    def test_multiple_profiles(self, client, patch_dirs):
        profiles_dir, _ = patch_dirs
        for name in ("alpha", "beta", "gamma"):
            p = _valid_profile_dict(name=name)
            (profiles_dir / f"{name}.json").write_text(
                json.dumps(p), encoding="utf-8"
            )
        resp = client.get("/api/profiles/")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()["profiles"]]
        assert sorted(names) == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# POST /export
# ---------------------------------------------------------------------------


class TestExportProfile:
    def test_export_success(self, client, mock_db, patch_dirs, patch_settings):
        resp = client.post(
            "/api/profiles/export",
            json={"name": "my-export", "description": "Exported", "author": "me"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["name"] == "my-export"

        # Verify file was created
        profiles_dir, _ = patch_dirs
        assert (profiles_dir / "my-export.json").exists()

    def test_export_no_db(self, client, patch_dirs, patch_settings):
        app.state.db = None
        resp = client.post(
            "/api/profiles/export",
            json={"name": "x", "description": "", "author": ""},
        )
        assert resp.status_code == 503

    def test_export_invalid_name(self, client, mock_db, patch_dirs, patch_settings):
        resp = client.post(
            "/api/profiles/export",
            json={"name": "../evil", "description": "", "author": ""},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /validate
# ---------------------------------------------------------------------------


class TestValidateProfile:
    def test_valid_profile(self, client):
        resp = client.post(
            "/api/profiles/validate",
            json={"profile": _valid_profile_dict()},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["warnings"] == []

    def test_profile_with_warnings(self, client):
        profile = _valid_profile_dict(
            rules={"allow": ["Bash(*:*)"], "deny": [], "ask": []}
        )
        resp = client.post(
            "/api/profiles/validate",
            json={"profile": profile},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert len(data["warnings"]) > 0

    def test_invalid_schema(self, client):
        resp = client.post(
            "/api/profiles/validate",
            json={"profile": {"name": ""}},
        )
        assert resp.status_code == 400

    def test_invalid_extra_field(self, client):
        profile = _valid_profile_dict()
        profile["evil_field"] = "injected"
        resp = client.post(
            "/api/profiles/validate",
            json={"profile": profile},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# POST /import
# ---------------------------------------------------------------------------


class TestImportProfile:
    def test_import_success(self, client, mock_db, patch_dirs, patch_settings):
        profile = _valid_profile_dict()
        resp = client.post(
            "/api/profiles/import",
            json={"profile": profile},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "backup_path" in data
        assert data["name"] == "test-profile"

    def test_import_creates_backup(self, client, mock_db, patch_dirs, patch_settings):
        _, backup_dir = patch_dirs
        profile = _valid_profile_dict()
        client.post("/api/profiles/import", json={"profile": profile})
        backups = list(backup_dir.glob("backup-*.json"))
        assert len(backups) == 1

    def test_import_no_db(self, client, patch_dirs, patch_settings):
        app.state.db = None
        resp = client.post(
            "/api/profiles/import",
            json={"profile": _valid_profile_dict()},
        )
        assert resp.status_code == 503

    def test_import_invalid_profile(self, client, mock_db, patch_dirs, patch_settings):
        resp = client.post(
            "/api/profiles/import",
            json={"profile": {"name": ""}},
        )
        assert resp.status_code == 400

    def test_import_applies_rules(self, client, mock_db, patch_dirs, patch_settings):
        settings_file = patch_settings
        profile = _valid_profile_dict(
            rules={"allow": ["Bash(git:*)"], "deny": ["Bash(rm:*)"], "ask": []}
        )
        client.post("/api/profiles/import", json={"profile": profile})
        saved = json.loads(settings_file.read_text(encoding="utf-8"))
        assert saved["permissions"]["allow"] == ["Bash(git:*)"]
        assert saved["permissions"]["deny"] == ["Bash(rm:*)"]

    def test_import_applies_gatekeeper_config(
        self, client, mock_db, patch_dirs, patch_settings
    ):
        profile = _valid_profile_dict(
            gatekeeper_config={"model": "sonnet", "eval_method": "cli_first"}
        )
        client.post("/api/profiles/import", json={"profile": profile})
        calls = mock_db.set_setting.call_args_list
        keys_set = {c.args[0] for c in calls}
        assert "gatekeeper.model" in keys_set
        assert "gatekeeper.eval_method" in keys_set


# ---------------------------------------------------------------------------
# POST /restore
# ---------------------------------------------------------------------------


class TestRestoreBackup:
    def test_restore_success(self, client, mock_db, patch_dirs, patch_settings):
        # First import to create a backup
        profile = _valid_profile_dict()
        client.post("/api/profiles/import", json={"profile": profile})

        # Now restore
        resp = client.post("/api/profiles/restore")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert "backup" in data

    def test_restore_no_backup(self, client, mock_db, patch_dirs, patch_settings):
        resp = client.post("/api/profiles/restore")
        assert resp.status_code == 404

    def test_restore_no_db(self, client, patch_dirs, patch_settings):
        app.state.db = None
        resp = client.post("/api/profiles/restore")
        assert resp.status_code == 503


# ---------------------------------------------------------------------------
# DELETE /{profile_name}
# ---------------------------------------------------------------------------


class TestDeleteProfile:
    def test_delete_success(self, client, patch_dirs):
        profiles_dir, _ = patch_dirs
        profile = _valid_profile_dict()
        (profiles_dir / "test-profile.json").write_text(
            json.dumps(profile), encoding="utf-8"
        )
        resp = client.delete("/api/profiles/test-profile")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert not (profiles_dir / "test-profile.json").exists()

    def test_delete_not_found(self, client, patch_dirs):
        resp = client.delete("/api/profiles/nonexistent")
        assert resp.status_code == 404

    def test_delete_traversal(self, client, patch_dirs):
        resp = client.delete("/api/profiles/..%2Fevil")
        # Starlette routing may intercept encoded slashes as 405/400
        assert resp.status_code in (400, 405)

    def test_delete_slash(self, client, patch_dirs):
        resp = client.delete("/api/profiles/a/b")
        # FastAPI routing doesn't match two-segment DELETE — 405 is expected
        assert resp.status_code in (400, 404, 405, 422)
