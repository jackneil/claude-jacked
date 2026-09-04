"""Unit tests for install-method detection and upgrade command construction."""

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jacked.install_method import (
    detect_install_method,
    rollback_command,
    rollback_command_label,
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
        # --refresh bypasses uv's package-index cache so a `--force` reinstall
        # actually picks up newly-published versions (tray "Update" silently
        # no-op'd between releases without this flag).
        assert "--refresh" in cmd

    def test_pipx_method_uses_pipx_install_force(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            cmd = upgrade_command(extras="tray")
        assert cmd[0] == "pipx"
        assert "install" in cmd
        assert "--force" in cmd

    def test_pip_method_raises_valueerror(self):
        """0.41.24: pip auto-upgrade is refused upstream in can_auto_upgrade.
        upgrade_command must raise if reached anyway (old code shipped a
        `python -m pip install` command that crashed with 'No module named
        pip' in uv-managed venvs — the 0.41.17 bug)."""
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with pytest.raises(ValueError, match="pip auto-upgrade is not supported"):
                upgrade_command(extras="tray")

    def test_editable_method_raises_valueerror(self):
        with patch("jacked.install_method.detect_install_method", return_value="editable"):
            with pytest.raises(ValueError, match="pip auto-upgrade is not supported"):
                upgrade_command(extras="tray")


class TestUpgradeCommandLabel:
    def test_uv_label_is_readable(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            label = upgrade_command_label(extras="tray")
        assert 'uv tool install "claude-jacked[tray]" --force --refresh' == label

    def test_pipx_label_is_readable(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            label = upgrade_command_label(extras="tray")
        assert "pipx install" in label
        assert "--force" in label

    def test_pip_label_raises_valueerror(self):
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with pytest.raises(ValueError, match="pip auto-upgrade is not supported"):
                upgrade_command_label(extras="tray")


class TestRollbackCommand:
    """A failed upgrade must be able to pin the exact build that was running."""

    def test_uv_pins_the_exact_version(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            cmd = rollback_command(extras="tray", version="0.95.0")
        assert cmd[0] == "uv"
        assert cmd[1:3] == ["tool", "install"]
        assert "claude-jacked[tray]==0.95.0" in cmd
        assert "--force" in cmd
        # Same reason upgrade_command needs it: a cached index entry must not
        # decide which artifact we pin back to.
        assert "--refresh" in cmd

    def test_pipx_pins_the_exact_version(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            cmd = rollback_command(extras="tray", version="0.95.0")
        assert cmd[0] == "pipx"
        assert "install" in cmd
        assert "claude-jacked[tray]==0.95.0" in cmd
        assert "--force" in cmd

    def test_pip_method_raises_valueerror(self):
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with pytest.raises(ValueError, match="pip rollback is not supported"):
                rollback_command(extras="tray", version="0.95.0")

    def test_editable_method_raises_valueerror(self):
        with patch("jacked.install_method.detect_install_method", return_value="editable"):
            with pytest.raises(ValueError, match="pip rollback is not supported"):
                rollback_command(extras="tray", version="0.95.0")

    @pytest.mark.parametrize(
        "version",
        [
            "",
            "0.95.0 && rm -rf /",
            '0.95.0"',
            "0.95.0; whoami",
            "$(id)",
            "%PATH%",
            "../0.95.0",
            "0.95.0\n1.0.0",
        ],
    )
    def test_refuses_a_version_that_is_not_a_version(self, version):
        """The version reaches us from a running build and lands in a batch
        line, so anything but a PEP 440 release is refused outright."""
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            with pytest.raises(ValueError, match="refusing to build a rollback"):
                rollback_command(extras="tray", version=version)

    def test_refuses_a_bad_version_before_detecting_the_method(self):
        """Validation runs first, so even a pip install reports the real fault."""
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with pytest.raises(ValueError, match="refusing to build a rollback"):
                rollback_command(extras="tray", version="not a version")

    @pytest.mark.parametrize(
        "version", ["1.2.3", "0.100.0", "1.0.0rc1", "1.0.0.post1", "1!2.0.0", "1.0.0+local"]
    )
    def test_accepts_real_pep440_versions(self, version):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            cmd = rollback_command(extras="tray", version=version)
        assert f"claude-jacked[tray]=={version}" in cmd


class TestRollbackCommandLabel:
    def test_uv_label_is_readable(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            label = rollback_command_label(extras="tray", version="0.95.0")
        assert (
            label == 'uv tool install "claude-jacked[tray]==0.95.0" --force --refresh'
        )

    def test_pipx_label_is_readable(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            label = rollback_command_label(extras="tray", version="0.95.0")
        assert label == 'pipx install "claude-jacked[tray]==0.95.0" --force'

    def test_pip_label_raises_valueerror(self):
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            with pytest.raises(ValueError, match="pip rollback is not supported"):
                rollback_command_label(extras="tray", version="0.95.0")

    def test_label_refuses_a_bad_version(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            with pytest.raises(ValueError, match="refusing to build a rollback"):
                rollback_command_label(extras="tray", version="0.95.0 && id")
