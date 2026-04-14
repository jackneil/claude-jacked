"""Tests for jacked service CLI commands."""

from unittest.mock import patch, MagicMock
from click.testing import CliRunner

import pytest


class TestServiceStatus:
    def test_status_when_not_running(self, tmp_path):
        from jacked.cli import main
        runner = CliRunner()
        pid_file = tmp_path / "nope.pid"
        with patch("jacked.service.PID_FILE", pid_file):
            result = runner.invoke(main, ["service", "status"])
        assert result.exit_code == 0
        assert "stopped" in result.output.lower()

    def test_status_when_running(self, tmp_path):
        import os
        from jacked.cli import main
        runner = CliRunner()
        pid_file = tmp_path / "test.pid"
        pid_file.write_text(f"{os.getpid()}\n8321")
        with patch("jacked.service.PID_FILE", pid_file):
            result = runner.invoke(main, ["service", "status"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()
        assert "8321" in result.output


class TestServiceStop:
    def test_stop_when_not_running(self, tmp_path):
        from jacked.cli import main
        runner = CliRunner()
        pid_file = tmp_path / "nope.pid"
        with patch("jacked.service.PID_FILE", pid_file):
            result = runner.invoke(main, ["service", "stop"])
        assert result.exit_code == 0
        assert "not running" in result.output.lower()


class TestServiceInstall:
    @patch("jacked.service.platform.install_autostart")
    def test_install_calls_platform(self, mock_install):
        from jacked.cli import main
        mock_install.return_value = "Installed launchd agent: /test/path"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert result.exit_code == 0
        mock_install.assert_called_once()


class TestServiceUninstall:
    @patch("jacked.service.platform.uninstall_autostart")
    def test_uninstall_calls_platform(self, mock_uninstall):
        from jacked.cli import main
        mock_uninstall.return_value = "Removed launchd agent: /test/path"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "uninstall"])
        assert result.exit_code == 0
        mock_uninstall.assert_called_once()


class TestServiceInstallError:
    @patch("jacked.service.platform.install_autostart")
    def test_install_shows_error_when_binary_not_found(self, mock_install):
        from jacked.cli import main
        mock_install.return_value = "Could not find 'jacked' binary on PATH. Is it installed?"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert result.exit_code == 0
        assert "Error" in result.output
        assert "Could not find" in result.output

    @patch("jacked.service.platform.install_autostart")
    def test_install_shows_ok_on_success(self, mock_install):
        from jacked.cli import main
        mock_install.return_value = "Installed launchd agent: /test/path"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert "OK" in result.output


class TestServiceRestart:
    @patch("jacked.service.process.stop_process", return_value=False)
    def test_restart_when_not_running_starts_fresh(self, mock_stop):
        from jacked.cli import main
        runner = CliRunner()
        with patch("jacked.service.tray.ServiceRunner.run", side_effect=SystemExit(0)):
            result = runner.invoke(main, ["service", "restart"])
        mock_stop.assert_called_once()
