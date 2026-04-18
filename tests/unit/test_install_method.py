"""Unit tests for install-method detection and upgrade command construction."""

import sys
from pathlib import Path
from unittest.mock import patch

from jacked.install_method import (
    detect_install_method,
    is_user_site_install,
    upgrade_command,
    upgrade_command_label,
)


class TestDetectInstallMethod:
    def test_detects_uv_from_path(self):
        """sys.executable under uv/tools/<pkg> → uv."""
        fake = Path("/home/u/.local/share/uv/tools/claude-jacked/bin/python3")
        with patch("jacked.install_method.Path.resolve", return_value=fake):
            with patch("sys.executable", str(fake)):
                assert detect_install_method() == "uv"

    def test_detects_uv_from_windows_path(self):
        fake = Path("C:/Users/u/AppData/Roaming/uv/tools/claude-jacked/Scripts/python.exe")
        with patch("jacked.install_method.Path.resolve", return_value=fake):
            with patch("sys.executable", str(fake)):
                assert detect_install_method() == "uv"

    def test_detects_pipx_from_path(self):
        fake = Path("/home/u/.local/share/pipx/venvs/claude-jacked/bin/python3")
        with patch("jacked.install_method.Path.resolve", return_value=fake):
            with patch("sys.executable", str(fake)):
                assert detect_install_method() == "pipx"

    def test_defaults_to_pip_for_unknown_path(self, monkeypatch):
        fake = Path("/usr/bin/python3")
        # Clear sys.path of editable markers + make the site-packages fallback see
        # jacked as installed normally (the test's runtime is a dev clone, but
        # we're simulating a non-editable environment here).
        monkeypatch.setattr(sys, "path", [])
        with patch("jacked.install_method.Path.resolve", return_value=fake):
            with patch("sys.executable", str(fake)):
                with patch("jacked.install_method._is_path_under_any_site_packages", return_value=True):
                    assert detect_install_method() == "pip"

    def test_windows_python_user_install_is_pip(self, monkeypatch):
        """pip install --user on Windows → %APPDATA%\\Python\\Python3XX\\python.exe."""
        fake = Path("C:/Users/u/AppData/Roaming/Python/Python312/python.exe")
        monkeypatch.setattr(sys, "path", [])
        with patch("jacked.install_method.Path.resolve", return_value=fake):
            with patch("sys.executable", str(fake)):
                with patch("jacked.install_method._is_path_under_any_site_packages", return_value=True):
                    assert detect_install_method() == "pip"


class TestUpgradeCommand:
    def test_uv_method_uses_uv_tool_install(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            cmd = upgrade_command(extras="tray")
        assert cmd[0] == "uv"
        assert "tool" in cmd and "install" in cmd
        assert "claude-jacked[tray]" in cmd
        assert "--force" in cmd

    def test_pipx_method_uses_pipx_install_force(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            cmd = upgrade_command(extras="tray")
        assert cmd[0] == "pipx"
        assert "install" in cmd
        assert "--force" in cmd

    def test_pip_method_uses_current_python(self):
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with patch("jacked.install_method.is_user_site_install", return_value=False):
                cmd = upgrade_command(extras="tray")
        assert cmd[0] == sys.executable
        assert cmd[1:4] == ["-m", "pip", "install"]
        assert "--upgrade" in cmd
        assert "claude-jacked[tray]" in cmd
        assert "--user" not in cmd

    def test_pip_method_adds_user_flag_when_user_site(self):
        """If current install is under the user site, the upgrade must
        pass --user so it lands in the same place instead of trying to
        write to a system Python."""
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with patch("jacked.install_method.is_user_site_install", return_value=True):
                cmd = upgrade_command(extras="tray")
        assert "--user" in cmd


class TestUpgradeCommandLabel:
    def test_uv_label_is_readable(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            label = upgrade_command_label(extras="tray")
        assert 'uv tool install "claude-jacked[tray]" --force' == label

    def test_pipx_label_is_readable(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            label = upgrade_command_label(extras="tray")
        assert "pipx install" in label
        assert "--force" in label

    def test_pip_label_includes_user_flag_when_user_site(self):
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with patch("jacked.install_method.is_user_site_install", return_value=True):
                label = upgrade_command_label(extras="tray")
        assert "--user" in label
        assert "claude-jacked[tray]" in label
