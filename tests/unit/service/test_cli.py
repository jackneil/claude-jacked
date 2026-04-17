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
    @patch("socket.socket")
    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("jacked.service.process.stop_process", return_value=False)
    def test_restart_when_not_running_starts_fresh_detached(
        self, mock_stop, mock_read_pid, mock_popen, mock_find, mock_sock,
    ):
        """Default `service restart` spawns a detached child and returns quickly."""
        from jacked.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_stop.assert_called_once()
        # Detached start via Popen
        assert mock_popen.call_count == 1
        args = mock_popen.call_args[0][0]
        assert "/fake/jacked" in args
        assert "service" in args and "start" in args
        kwargs = mock_popen.call_args[1]
        # POSIX detach (we're on darwin/linux in tests)
        import sys as _sys
        if _sys.platform != "win32":
            assert kwargs.get("start_new_session") is True

    @patch("socket.socket")
    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch("jacked.service.process.is_process_alive", return_value=False)
    @patch("jacked.service.process.read_pid", return_value={"pid": 99999, "port": 8321})
    @patch("jacked.service.process.stop_process", return_value=True)
    def test_restart_waits_for_pid_and_port(
        self, mock_stop, mock_read_pid, mock_alive, mock_popen, mock_find, mock_sock,
    ):
        """After stop, must wait for PID death + port release before start."""
        from jacked.cli import main
        # Socket bind "succeeds" immediately so port-wait exits fast.
        mock_sock.return_value.bind = MagicMock()

        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        # is_process_alive was polled (at least the initial check)
        assert mock_alive.call_count >= 1
        # Port was tested before detaching
        assert mock_sock.return_value.bind.call_count >= 1
        # Then detached Popen fired
        mock_popen.assert_called_once()

    @patch("jacked.service.tray.ServiceRunner")
    @patch("jacked.service.process.stop_process", return_value=False)
    def test_restart_foreground_blocks_on_ServiceRunner(
        self, mock_stop, mock_runner_cls,
    ):
        """--foreground runs the tray in-process (blocks on pystray event loop)."""
        from jacked.cli import main
        runner_instance = mock_runner_cls.return_value
        runner_instance.run.return_value = None

        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart", "--foreground"])
        assert result.exit_code == 0
        runner_instance.run.assert_called_once()
