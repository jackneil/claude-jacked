"""Tests for jacked.profiles — core profile logic."""

import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from jacked import __version__
from jacked.profiles import (
    BACKUP_DIR_NAME,
    PROFILE_DIR_NAME,
    ProfileSchema,
    _create_backup,
    _name_to_filename,
    delete_profile,
    export_profile,
    get_latest_backup,
    import_profile,
    list_profiles,
    restore_backup,
    validate_profile,
)


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


def _make_settings(**overrides):
    """Create a sample settings.json dict."""
    base = {
        "permissions": {
            "allow": ["Read(*:*)"],
            "deny": ["Bash(rm -rf /:/*)"],
            "ask": [],
        }
    }
    base.update(overrides)
    return base


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


# ---------------------------------------------------------------------------
# Schema Validation
# ---------------------------------------------------------------------------


class TestSchemaValidation:
    def test_valid_profile(self):
        p = ProfileSchema(**_valid_profile_dict())
        assert p.name == "test-profile"
        assert p.gatekeeper_config.model == "haiku"

    def test_rejects_unknown_keys(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(unknown_field="bad"))

    def test_rejects_unknown_keys_in_gatekeeper(self):
        with pytest.raises(ValidationError):
            ProfileSchema(
                **_valid_profile_dict(
                    gatekeeper_config={"model": "haiku", "secret_field": "x"}
                )
            )

    def test_rejects_unknown_keys_in_rules(self):
        with pytest.raises(ValidationError):
            ProfileSchema(
                **_valid_profile_dict(
                    rules={"allow": [], "deny": [], "ask": [], "extra": []}
                )
            )

    def test_rejects_long_name(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(name="a" * 65))

    def test_rejects_empty_name(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(name=""))

    def test_rejects_path_traversal_dots(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(name="../etc/passwd"))

    def test_rejects_path_traversal_slash(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(name="foo/bar"))

    def test_rejects_path_traversal_backslash(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(name="foo\\bar"))

    def test_rejects_long_description(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(description="x" * 501))

    def test_rejects_long_author(self):
        with pytest.raises(ValidationError):
            ProfileSchema(**_valid_profile_dict(author="x" * 101))

    def test_rejects_invalid_model(self):
        with pytest.raises(ValidationError):
            ProfileSchema(
                **_valid_profile_dict(gatekeeper_config={"model": "gpt4"})
            )

    def test_accepts_valid_models(self):
        for model in ("haiku", "sonnet", "opus"):
            p = ProfileSchema(
                **_valid_profile_dict(gatekeeper_config={"model": model})
            )
            assert p.gatekeeper_config.model == model

    def test_rejects_invalid_eval_method(self):
        with pytest.raises(ValidationError):
            ProfileSchema(
                **_valid_profile_dict(
                    gatekeeper_config={"eval_method": "magic"}
                )
            )

    def test_rejects_invalid_category_mode(self):
        with pytest.raises(ValidationError):
            ProfileSchema(
                **_valid_profile_dict(
                    gatekeeper_config={
                        "command_categories": {"network": "yolo"}
                    }
                )
            )

    def test_name_with_spaces_and_dashes(self):
        p = ProfileSchema(**_valid_profile_dict(name="My Cool Profile-v2"))
        assert p.name == "My Cool Profile-v2"


# ---------------------------------------------------------------------------
# Validation Warnings
# ---------------------------------------------------------------------------


class TestValidateProfile:
    def test_flags_dangerous_allow_patterns(self):
        data = _valid_profile_dict(
            rules={"allow": ["Bash(*:*)"], "deny": [], "ask": []}
        )
        warnings = validate_profile(data)
        assert any("Dangerous pattern" in w and "Bash(*:*)" in w for w in warnings)

    def test_flags_dangerous_deny_patterns(self):
        data = _valid_profile_dict(
            rules={"allow": [], "deny": ["Bash(sudo:*)"], "ask": []}
        )
        warnings = validate_profile(data)
        assert any("Dangerous pattern" in w and "Bash(sudo:*)" in w for w in warnings)

    def test_flags_path_safety_disabled(self):
        data = _valid_profile_dict(
            gatekeeper_config={"path_safety": {"enabled": False}}
        )
        warnings = validate_profile(data)
        assert any("Path safety is disabled" in w for w in warnings)

    def test_flags_root_allowed_paths(self):
        data = _valid_profile_dict(
            gatekeeper_config={
                "path_safety": {"enabled": True, "allowed_paths": ["/", "/home"]}
            }
        )
        warnings = validate_profile(data)
        assert any("Root allowed_path '/'" in w for w in warnings)

    def test_flags_root_wildcard_allowed_paths(self):
        data = _valid_profile_dict(
            gatekeeper_config={
                "path_safety": {"enabled": True, "allowed_paths": ["/*"]}
            }
        )
        warnings = validate_profile(data)
        assert any("Root allowed_path '/*'" in w for w in warnings)

    def test_flags_dangerous_category_modes(self):
        data = _valid_profile_dict(
            gatekeeper_config={"command_categories": {"network": "allow"}}
        )
        warnings = validate_profile(data)
        assert any("'network' set to 'allow'" in w for w in warnings)

    def test_rejects_higher_version(self):
        data = _valid_profile_dict(jacked_version="99.0.0")
        with pytest.raises(ValueError, match="Profile requires jacked 99.0.0"):
            validate_profile(data)

    def test_accepts_same_version(self):
        data = _valid_profile_dict(jacked_version=__version__)
        warnings = validate_profile(data)
        # Should not raise
        assert isinstance(warnings, list)

    def test_clean_profile_no_warnings(self):
        data = _valid_profile_dict()
        warnings = validate_profile(data)
        assert warnings == []


# ---------------------------------------------------------------------------
# Name → Filename
# ---------------------------------------------------------------------------


class TestNameToFilename:
    def test_simple(self):
        assert _name_to_filename("simple") == "simple.json"

    def test_spaces_to_dashes(self):
        assert _name_to_filename("My Profile") == "my-profile.json"

    def test_uppercase_to_lower(self):
        assert _name_to_filename("LOUD") == "loud.json"


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


class TestExport:
    def test_creates_file(self, tmp_path):
        db = _make_db()
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        settings = _make_settings()

        path = export_profile(
            name="my-export",
            description="test export",
            author="tester",
            db=db,
            settings_json=settings,
            profiles_dir=profiles_dir,
        )

        assert path.exists()
        data = json.loads(path.read_text())
        assert data["name"] == "my-export"
        assert data["jacked_version"] == __version__

    def test_strips_api_key(self, tmp_path):
        """api_key should NEVER appear in exported profiles."""
        db = _make_db(**{"gatekeeper.api_key": "sk-secret-key-123"})
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        settings = _make_settings()

        path = export_profile(
            name="no-key",
            description="",
            author="",
            db=db,
            settings_json=settings,
            profiles_dir=profiles_dir,
        )

        content = path.read_text()
        assert "sk-secret-key-123" not in content
        assert "api_key" not in content

    def test_roundtrip_equivalence(self, tmp_path):
        """Export then import should preserve all config values."""
        db = _make_db()
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        backup_dir = tmp_path / BACKUP_DIR_NAME
        settings = _make_settings()

        # Export
        path = export_profile(
            name="roundtrip",
            description="round trip test",
            author="me",
            db=db,
            settings_json=settings,
            profiles_dir=profiles_dir,
        )

        # Read exported data
        exported = json.loads(path.read_text())

        # Import into fresh DB/settings
        db2 = _make_db()
        written_settings = {}

        def write_fn(data):
            written_settings.update(data)

        import_profile(
            profile_data=exported,
            db=db2,
            settings_json={},
            write_settings_fn=write_fn,
            profiles_dir=profiles_dir,
            backup_dir=backup_dir,
        )

        # Verify rules were applied
        assert written_settings["permissions"]["allow"] == settings["permissions"]["allow"]
        assert written_settings["permissions"]["deny"] == settings["permissions"]["deny"]

        # Verify gatekeeper config was applied
        db2.set_setting.assert_any_call("gatekeeper.model", json.dumps("haiku"))


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


class TestImport:
    def test_creates_backup(self, tmp_path):
        db = _make_db()
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        backup_dir = tmp_path / BACKUP_DIR_NAME
        settings = _make_settings()

        backup_path, warnings = import_profile(
            profile_data=_valid_profile_dict(),
            db=db,
            settings_json=settings,
            write_settings_fn=lambda d: None,
            profiles_dir=profiles_dir,
            backup_dir=backup_dir,
        )

        assert backup_path.exists()
        backup_data = json.loads(backup_path.read_text())
        assert "settings_json" in backup_data
        assert "gatekeeper_db" in backup_data

    def test_applies_rules_and_config(self, tmp_path):
        db = _make_db()
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        backup_dir = tmp_path / BACKUP_DIR_NAME
        written = {}

        profile_data = _valid_profile_dict(
            rules={"allow": ["NewRule(*)"], "deny": ["Bash(rm:*)"], "ask": []},
            gatekeeper_config={"model": "sonnet", "eval_method": "cli_only"},
        )

        import_profile(
            profile_data=profile_data,
            db=db,
            settings_json={},
            write_settings_fn=lambda d: written.update(d),
            profiles_dir=profiles_dir,
            backup_dir=backup_dir,
        )

        assert written["permissions"]["allow"] == ["NewRule(*)"]
        assert written["permissions"]["deny"] == ["Bash(rm:*)"]
        db.set_setting.assert_any_call("gatekeeper.model", json.dumps("sonnet"))
        db.set_setting.assert_any_call("gatekeeper.eval_method", json.dumps("cli_only"))

    def test_import_rejects_invalid_schema(self, tmp_path):
        db = _make_db()
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        backup_dir = tmp_path / BACKUP_DIR_NAME

        with pytest.raises(ValidationError):
            import_profile(
                profile_data={"name": "", "unknown": True},
                db=db,
                settings_json={},
                write_settings_fn=lambda d: None,
                profiles_dir=profiles_dir,
                backup_dir=backup_dir,
            )


# ---------------------------------------------------------------------------
# List / Delete
# ---------------------------------------------------------------------------


class TestListDelete:
    def test_list_empty(self, tmp_path):
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        assert list_profiles(profiles_dir) == []

    def test_list_after_export(self, tmp_path):
        db = _make_db()
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        settings = _make_settings()

        export_profile("prof-a", "desc a", "author", db, settings, profiles_dir)
        export_profile("prof-b", "desc b", "author", db, settings, profiles_dir)

        results = list_profiles(profiles_dir)
        names = [r["name"] for r in results]
        assert "prof-a" in names
        assert "prof-b" in names
        assert len(results) == 2

    def test_delete(self, tmp_path):
        db = _make_db()
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        settings = _make_settings()

        export_profile("to-delete", "", "", db, settings, profiles_dir)
        assert len(list_profiles(profiles_dir)) == 1

        assert delete_profile("to-delete", profiles_dir) is True
        assert len(list_profiles(profiles_dir)) == 0

    def test_delete_nonexistent(self, tmp_path):
        profiles_dir = tmp_path / PROFILE_DIR_NAME
        profiles_dir.mkdir()
        assert delete_profile("nope", profiles_dir) is False


# ---------------------------------------------------------------------------
# Backup / Restore
# ---------------------------------------------------------------------------


class TestBackupRestore:
    def test_get_latest_backup_none(self, tmp_path):
        assert get_latest_backup(tmp_path / "no-such-dir") is None

    def test_get_latest_backup(self, tmp_path):
        backup_dir = tmp_path / BACKUP_DIR_NAME
        db = _make_db()
        settings = _make_settings()

        _create_backup(db, settings, backup_dir)
        b2 = _create_backup(db, {"permissions": {"allow": ["X"]}}, backup_dir)

        latest = get_latest_backup(backup_dir)
        assert latest is not None
        # Latest should be the second backup (alphabetically later timestamp)
        assert latest == b2

    def test_restore_produces_original_config(self, tmp_path):
        backup_dir = tmp_path / BACKUP_DIR_NAME
        db = _make_db()
        original_settings = _make_settings()

        # Create backup
        backup_path = _create_backup(db, original_settings, backup_dir)

        # Modify state
        db2 = MagicMock()
        db2.set_setting = MagicMock()
        restored_settings = {}

        def write_fn(data):
            restored_settings.update(data)

        restore_backup(backup_path, db2, write_fn)

        # Verify settings.json was restored
        assert restored_settings["permissions"]["allow"] == original_settings["permissions"]["allow"]
        assert restored_settings["permissions"]["deny"] == original_settings["permissions"]["deny"]

        # Verify DB settings were restored
        db2.set_setting.assert_any_call("gatekeeper.model", json.dumps("haiku"))
