"""Tests for jacked.service.platform.native_restart() across macOS + Linux + Windows."""

import os
import subprocess
import sys
from unittest.mock import patch, MagicMock


class TestMacOS:
    def test_kickstart_called_when_plist_exists(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")

        fake_plist = tmp_path / "ai.hank.jacked.plist"
        fake_plist.write_text("<plist/>")
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)
        # os.getuid doesn't exist on Windows; the darwin branch needs it.
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)

        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(plat.subprocess, "run", return_value=mock_result) as mock_run:
            ok, reason = plat.native_restart()

        assert ok is True
        assert "launchctl kickstart" in reason
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "launchctl"
        assert "kickstart" in call_args
        assert "-k" in call_args
        assert any("ai.hank.jacked" in a for a in call_args)

    def test_fallback_when_plist_missing(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            plat, "_get_launchd_plist_path", lambda: tmp_path / "nope.plist",
        )
        ok, reason = plat.native_restart()
        assert ok is False
        assert "not installed" in reason.lower()

    def test_fallback_when_launchctl_nonzero(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "darwin")
        fake_plist = tmp_path / "ai.hank.jacked.plist"
        fake_plist.write_text("<plist/>")
        monkeypatch.setattr(plat, "_get_launchd_plist_path", lambda: fake_plist)
        monkeypatch.setattr(os, "getuid", lambda: 501, raising=False)
        mock_result = MagicMock(returncode=3, stdout="", stderr="not bootstrapped")
        with patch.object(plat.subprocess, "run", return_value=mock_result):
            ok, reason = plat.native_restart()
        assert ok is False
        assert "not bootstrapped" in reason


class TestLinux:
    def test_systemctl_called_when_user_unit_exists(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "linux")

        fake_unit = tmp_path / "jacked.service"
        fake_unit.write_text("[Unit]\n")
        monkeypatch.setattr(plat, "_get_systemd_user_unit_path", lambda: fake_unit)

        mock_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch.object(plat.subprocess, "run", return_value=mock_result) as mock_run:
            ok, reason = plat.native_restart()

        assert ok is True
        assert "systemctl" in reason
        call_args = mock_run.call_args[0][0]
        assert call_args[:4] == ["systemctl", "--user", "restart", "jacked"]

    def test_fallback_when_no_user_unit(self, tmp_path, monkeypatch):
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(
            plat, "_get_systemd_user_unit_path", lambda: tmp_path / "nope.service",
        )
        ok, reason = plat.native_restart()
        assert ok is False
        assert "no user systemd" in reason.lower()


class TestWindows:
    def test_always_returns_false(self, monkeypatch):
        """Windows has no supervising manager — the Startup VBS only runs
        on login. Return False so the caller falls back to manual stop+start."""
        from jacked.service import platform as plat
        monkeypatch.setattr(sys, "platform", "win32")
        ok, reason = plat.native_restart()
        assert ok is False
        assert "no native lifecycle manager" in reason.lower()


class TestServiceRestartCli:
    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(True, "already_installed", "plist installed"))
    @patch("jacked.service.platform.native_restart", return_value=(True, "launchctl kickstart"))
    @patch("subprocess.Popen")
    def test_cli_defers_to_native_when_available(
        self, mock_popen, mock_native, mock_ensure,
    ):
        from click.testing import CliRunner
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        assert "launchctl" in result.output
        mock_native.assert_called_once()
        mock_popen.assert_not_called()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(True, "already_installed", "plist installed"))
    @patch("jacked.service.platform.native_restart", return_value=(False, "no native manager"))
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": False, "died": False, "killed": False},
    )
    @patch("subprocess.Popen")
    def test_cli_falls_back_when_native_restart_fails(
        self, mock_popen, mock_stop, mock_native, mock_ensure,
    ):
        """Native lifecycle is present but its kickstart fails → fall back to
        the manual stop+detached-start path."""
        from click.testing import CliRunner
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_native.assert_called_once()
        mock_popen.assert_called_once()  # manual stop+start path ran

    @patch("jacked.service.platform.native_restart")
    @patch("jacked.service.tray.ServiceRunner")
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": False, "died": False, "killed": False},
    )
    def test_cli_foreground_skips_native_handoff(
        self, mock_stop, mock_runner_cls, mock_native,
    ):
        from click.testing import CliRunner
        from jacked.cli import main
        mock_runner_cls.return_value.run.return_value = None
        result = CliRunner().invoke(main, ["service", "restart", "--foreground"])
        assert result.exit_code == 0
        mock_native.assert_not_called()
        mock_runner_cls.return_value.run.assert_called_once()


class TestUpdaterUsesNativeRestart:
    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(True, "already_installed", "plist installed"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.platform.native_restart", return_value=(True, "launchctl kickstart"))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_starting_service_uses_native_when_available(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_native,
        mock_gate, mock_method, mock_ensure, tmp_path, monkeypatch,
    ):
        """When native_restart succeeds, the updater does NOT spawn its own
        detached `jacked service start`. That's the whole point — no race."""
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.side_effect = [True, True] + [False] * 100

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.22")

        # native_restart was called
        mock_native.assert_called()
        # NO detached Popen for `jacked service start` — launchd is handling it
        for call in mock_popen.call_args_list:
            argv = call[0][0]
            if isinstance(argv, list):
                cmd_str = " ".join(str(a) for a in argv)
                assert "service start" not in cmd_str, (
                    f"expected no detached service start when native restart succeeded, got: {cmd_str}"
                )

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "no native lifecycle manager"))
    @patch("jacked.install_method.detect_install_method", return_value="uv")
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.platform.native_restart", return_value=(False, "no plist"))
    @patch("jacked.service.updater.is_port_available")
    @patch("jacked.service.updater.find_bin")
    @patch("subprocess.run")
    @patch("subprocess.Popen")
    def test_starting_service_falls_back_when_no_native(
        self, mock_popen, mock_run, mock_find, mock_port_avail, mock_native,
        mock_gate, mock_method, mock_ensure, tmp_path, monkeypatch,
    ):
        """When no native manager, updater uses its detached spawn (Windows
        and unmanaged-Linux behavior)."""
        from jacked.service import updater, update_status as us_mod
        monkeypatch.setattr(updater, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr(us_mod, "UPDATE_STATUS_FILE", tmp_path / "status.json")
        mock_find.side_effect = lambda name: {"uv": "/fake/uv", "jacked": "/fake/jacked"}.get(name)
        mock_run.return_value = MagicMock(returncode=0)
        mock_port_avail.side_effect = [True, True] + [False] * 100

        with patch.object(updater, "wait_for_exit", return_value=True):
            updater.run_update(parent_pid=12345, extras="tray", target_version="0.41.22")

        # Detached Popen for `jacked service start` fired (fallback path)
        spawn_calls = [
            c for c in mock_popen.call_args_list
            if isinstance(c[0][0], list)
            and "service" in " ".join(str(a) for a in c[0][0])
            and "start" in " ".join(str(a) for a in c[0][0])
        ]
        assert len(spawn_calls) == 1
