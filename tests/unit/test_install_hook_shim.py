"""Tests for install writing _hook shim form and settings backup."""

import json
from unittest.mock import patch


class TestSettingsBackup:
    def test_creates_backup_before_mutation(self, tmp_path):
        from jacked.cli import _snapshot_settings

        settings_path = tmp_path / "settings.json"
        settings_path.write_text('{"test": 1}')

        backup = _snapshot_settings(settings_path)
        assert backup is not None
        assert backup.exists()
        assert backup.read_text() == '{"test": 1}'
        assert "settings.json.bak-" in str(backup)

    def test_no_backup_if_source_missing(self, tmp_path):
        from jacked.cli import _snapshot_settings

        settings_path = tmp_path / "does-not-exist.json"
        assert _snapshot_settings(settings_path) is None

    def test_rotates_old_backups(self, tmp_path):
        """Keeps only last 5 backups."""
        from jacked.cli import _snapshot_settings, _rotate_backups

        settings_path = tmp_path / "settings.json"
        settings_path.write_text("{}")
        for _ in range(7):
            _snapshot_settings(settings_path)
        _rotate_backups(tmp_path, prefix="settings.json.bak-", keep=5)
        backups = sorted(tmp_path.glob("settings.json.bak-*"))
        assert len(backups) == 5


class TestIsJackedManagedHook:
    def test_uv_tool_site_packages_path_is_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path

        p = "/Users/x/.local/share/uv/tools/claude-jacked/lib/python3.12/site-packages/jacked/data/hooks/security_gatekeeper.py"
        assert _is_jacked_managed_hook_path(p) is True

    def test_editable_install_path_is_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path

        p = "/Users/y/Github/claude-jacked/jacked/data/hooks/session_account_tracker.py"
        assert _is_jacked_managed_hook_path(p) is True

    def test_user_custom_hook_not_jacked_managed(self):
        """A user's personal security_gatekeeper.py in their own dir is NOT ours."""
        from jacked.cli import _is_jacked_managed_hook_path

        p = "/Users/alice/my-scripts/security_gatekeeper.py"
        assert _is_jacked_managed_hook_path(p) is False

    def test_shim_form_is_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path

        assert _is_jacked_managed_hook_path('"/Users/x/.local/bin/jacked" _hook security_gatekeeper') is True

    def test_dev_fallback_form_is_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path

        assert _is_jacked_managed_hook_path('"/usr/bin/python3" -m jacked _hook security_gatekeeper') is True

    def test_empty_command_not_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path

        assert _is_jacked_managed_hook_path("") is False

    def test_unrelated_command_not_jacked_managed(self):
        from jacked.cli import _is_jacked_managed_hook_path

        assert _is_jacked_managed_hook_path("/usr/local/bin/my-tool run") is False


class TestBuildHookCommand:
    @patch("jacked.findbin.find_bin")
    def test_prefers_jacked_shim_when_on_path(self, mock_find):
        from jacked.cli import _build_hook_command

        mock_find.return_value = "/fake/bin/jacked"
        cmd = _build_hook_command("security_gatekeeper")
        assert "/fake/bin/jacked" in cmd
        assert "_hook security_gatekeeper" in cmd

    @patch("jacked.findbin.find_bin")
    def test_falls_back_to_python_m_when_jacked_missing(self, mock_find):
        from jacked.cli import _build_hook_command

        mock_find.return_value = None
        cmd = _build_hook_command("security_gatekeeper")
        assert "-m jacked _hook security_gatekeeper" in cmd


class TestAtomicWrite:
    def test_writes_json(self, tmp_path):
        from jacked.cli import _write_settings_atomic

        settings_path = tmp_path / "settings.json"
        _write_settings_atomic(settings_path, {"a": 1, "hooks": {"PreToolUse": []}})
        assert settings_path.exists()
        content = json.loads(settings_path.read_text())
        assert content["a"] == 1

    def test_overwrites_existing(self, tmp_path):
        from jacked.cli import _write_settings_atomic

        settings_path = tmp_path / "settings.json"
        settings_path.write_text('{"old": true}')
        _write_settings_atomic(settings_path, {"new": True})
        content = json.loads(settings_path.read_text())
        assert content == {"new": True}

    def test_no_leftover_tempfile_on_success(self, tmp_path):
        from jacked.cli import _write_settings_atomic

        settings_path = tmp_path / "settings.json"
        _write_settings_atomic(settings_path, {"x": 1})
        tmpfiles = list(tmp_path.glob(".settings-*.tmp"))
        assert tmpfiles == []
