"""Tests for `jacked upgrade` one-shot upgrade command."""

import sys
from unittest.mock import patch, MagicMock
from click.testing import CliRunner


class TestUpgradeCommand:
    @patch("sys.platform", "darwin")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.is_process_alive", return_value=False)
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_starts_detached_service_even_when_not_running(
        self, mock_run, mock_popen, mock_read_pid, mock_alive, mock_find,
    ):
        """Service wasn't running → still start it detached (user ran `upgrade` expecting it)."""
        from jacked.cli import main

        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 0
        # Two blocking subprocess.run calls: uv install + jacked install
        assert mock_run.call_count == 2
        uv_args = mock_run.call_args_list[0][0][0]
        assert "/fake/uv" in uv_args
        assert "claude-jacked[tray]" in uv_args
        install_args = mock_run.call_args_list[1][0][0]
        assert "/fake/jacked" in install_args
        assert "install" in install_args

        # One Popen call: detached `jacked service start`
        assert mock_popen.call_count == 1
        popen_args = mock_popen.call_args[0][0]
        assert "/fake/jacked" in popen_args
        assert "service" in popen_args and "start" in popen_args
        # Must be detached
        kwargs = mock_popen.call_args[1]
        assert kwargs.get("start_new_session") is True
        assert kwargs.get("stdin") is __import__("subprocess").DEVNULL

    @patch("jacked.findbin.find_bin")
    @patch("subprocess.run")
    def test_upgrade_aborts_if_uv_not_found(self, mock_run, mock_find):
        from jacked.cli import main
        mock_find.return_value = None

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code != 0
        assert "uv" in result.output.lower()
        mock_run.assert_not_called()

    @patch("jacked.findbin.find_bin")
    @patch("subprocess.run")
    def test_upgrade_aborts_if_uv_install_fails(self, mock_run, mock_find):
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": "/fake/uv"}.get(name)
        mock_run.return_value = MagicMock(returncode=1)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 1
        # Only one subprocess call — aborts after uv install fails
        assert mock_run.call_count == 1

    @patch("sys.platform", "darwin")
    @patch("socket.socket")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.is_process_alive", return_value=True)
    @patch("jacked.service.process.read_pid", return_value={"pid": 99999, "port": 8321})
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_stops_then_starts_detached_when_running(
        self, mock_run, mock_popen, mock_read_pid, mock_alive, mock_find, mock_sock,
    ):
        """When service is running: stop (blocking), wait for port, start detached."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        # Socket bind "succeeds" immediately so the port-wait loop exits fast.
        mock_sock.return_value.bind = MagicMock()

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 0
        # 3 blocking runs: uv install, jacked install, jacked service stop
        assert mock_run.call_count == 3
        stop_args = mock_run.call_args_list[2][0][0]
        assert "/fake/jacked" in stop_args
        assert "service" in stop_args and "stop" in stop_args
        # 1 detached Popen: jacked service start
        assert mock_popen.call_count == 1
        start_args = mock_popen.call_args[0][0]
        assert "start" in start_args
        assert mock_popen.call_args[1].get("start_new_session") is True

    @patch("sys.platform", "darwin")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.is_process_alive", return_value=True)
    @patch("jacked.service.process.read_pid", return_value={"pid": 99999, "port": 8321})
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_skip_service_flag_honored(
        self, mock_run, mock_popen, mock_read_pid, mock_alive, mock_find,
    ):
        from jacked.cli import main
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        mock_run.return_value = MagicMock(returncode=0)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade", "--skip-service"])

        assert result.exit_code == 0
        # Only 2 blocking runs — service untouched. No detached Popen either.
        assert mock_run.call_count == 2
        mock_popen.assert_not_called()

    @patch("sys.platform", "darwin")
    @patch("jacked.findbin.find_bin")
    @patch("jacked.service.process.is_process_alive", return_value=False)
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("subprocess.Popen")
    @patch("subprocess.run")
    def test_upgrade_continues_if_jacked_install_fails(
        self, mock_run, mock_popen, mock_read_pid, mock_alive, mock_find,
    ):
        """jacked install failure is non-fatal — package is still upgraded."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {
            "uv": "/fake/uv",
            "jacked": "/fake/jacked",
        }.get(name)
        # uv install succeeds, jacked install fails
        mock_run.side_effect = [
            MagicMock(returncode=0),
            MagicMock(returncode=1),
        ]

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 0
        # 2 blocking runs (uv + failing jacked install), still attempts detached start
        assert mock_run.call_count == 2
        assert mock_popen.call_count == 1


class TestUpgradeWindows:
    @patch("sys.platform", "win32")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    def test_windows_spawns_detached_helper_and_exits(
        self, mock_popen, mock_find,
    ):
        """Windows must spawn a detached cmd.exe helper, never try inline."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": r"C:\uv\uv.exe"}.get(name)

        runner = CliRunner()
        result = runner.invoke(main, ["upgrade"])

        assert result.exit_code == 0
        # Helper was spawned via cmd.exe
        mock_popen.assert_called_once()
        args = mock_popen.call_args[0][0]
        assert "cmd.exe" in args[0]
        assert args[1] == "/c"
        assert args[2].endswith(".bat")
        # DETACHED_PROCESS flag set
        kwargs = mock_popen.call_args[1]
        flags = kwargs.get("creationflags", 0)
        assert flags & 0x00000008  # DETACHED_PROCESS

    @patch("sys.platform", "win32")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    def test_windows_batch_contains_uv_and_jacked_commands(
        self, mock_popen, mock_find, tmp_path, monkeypatch,
    ):
        """Batch file must embed the full upgrade sequence."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": r"C:\uv\uv.exe"}.get(name)

        # Redirect tempfile to a place we control so we can read the batch file.
        import tempfile as _tempfile
        real_mkstemp = _tempfile.mkstemp
        created = []
        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
            created.append(path)
            return fd, path
        monkeypatch.setattr(_tempfile, "mkstemp", fake_mkstemp)

        runner = CliRunner()
        runner.invoke(main, ["upgrade", "--extras", "all"])

        assert len(created) == 1
        batch = open(created[0]).read()
        assert "tasklist" in batch  # waits for parent exit
        assert "uv.exe" in batch
        assert "claude-jacked[all]" in batch
        assert "--force" in batch
        assert "jacked install --force" in batch
        assert "service restart" in batch

    @patch("sys.platform", "win32")
    @patch("jacked.findbin.find_bin")
    @patch("subprocess.Popen")
    def test_windows_skip_service_flag(
        self, mock_popen, mock_find, tmp_path, monkeypatch,
    ):
        """--skip-service should result in SKIP_SERVICE=1 in the batch."""
        from jacked.cli import main
        mock_find.side_effect = lambda name: {"uv": r"C:\uv\uv.exe"}.get(name)

        import tempfile as _tempfile
        real_mkstemp = _tempfile.mkstemp
        created = []
        def fake_mkstemp(*args, **kwargs):
            fd, path = real_mkstemp(*args, dir=str(tmp_path), **{k: v for k, v in kwargs.items() if k != "dir"})
            created.append(path)
            return fd, path
        monkeypatch.setattr(_tempfile, "mkstemp", fake_mkstemp)

        runner = CliRunner()
        runner.invoke(main, ["upgrade", "--skip-service"])

        batch = open(created[0]).read()
        assert "set SKIP_SERVICE=1" in batch
