"""Tests for jacked service CLI commands."""

from unittest.mock import patch
from click.testing import CliRunner


class TestSpawnServiceDetachedArgv:
    """argv honesty: `--host` appears ONLY when the caller passed an explicit
    host. Omitting it lets the detached child re-resolve its bind from the DB,
    which is what makes upgrade/autostart restarts honor the GUI toggle."""

    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    def test_no_host_omits_host_flag(self, mock_popen, _mock_find):
        from jacked.cli import _spawn_service_detached
        _spawn_service_detached(None, 8321)
        args = mock_popen.call_args[0][0]
        assert "--host" not in args
        assert "service" in args and "start" in args
        assert "--port" in args
        assert "8321" in args

    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    def test_explicit_host_includes_host_flag(self, mock_popen, _mock_find):
        from jacked.cli import _spawn_service_detached
        _spawn_service_detached("0.0.0.0", 8321)
        args = mock_popen.call_args[0][0]
        assert "--host" in args
        assert args[args.index("--host") + 1] == "0.0.0.0"
        assert "--port" in args
        assert "8321" in args


class TestWebuxHostResolution:
    """webux resolves its bind via resolve_bind and feeds JACKED_HOST from the
    plan's primary_host; --reload stays single-host and ignores the DB."""

    def test_webux_sets_jacked_host_from_plan_and_passes_sockets(self, monkeypatch):
        import os
        import uvicorn
        from jacked.cli import main
        from jacked.service.bind import BindPlan

        plan = BindPlan(
            mode="tailscale", addresses=("127.0.0.1", "100.64.1.2"), port=8321,
            primary_host="100.64.1.2", tailscale_ip="100.64.1.2",
        )
        captured = {}

        class _FakeServer:
            def __init__(self, config):
                captured["config"] = config

            def run(self, sockets=None):
                captured["sockets"] = sockets

        monkeypatch.setattr("jacked.service.bind.resolve_bind", lambda h, p: plan)
        monkeypatch.setattr(
            "jacked.service.bind.create_sockets", lambda _plan: ["S1", "S2"]
        )
        monkeypatch.setattr(uvicorn, "Config", lambda *a, **k: {"a": a, "k": k})
        monkeypatch.setattr(uvicorn, "Server", _FakeServer)
        # setenv so monkeypatch restores JACKED_HOST at teardown (webux mutates
        # os.environ directly, which would otherwise leak into other tests).
        monkeypatch.setenv("JACKED_HOST", "sentinel")

        result = CliRunner().invoke(main, ["webux", "--no-browser"])
        assert result.exit_code == 0, result.output
        assert os.environ["JACKED_HOST"] == "100.64.1.2"
        assert captured["sockets"] == ["S1", "S2"]
        # The Tailscale URL is surfaced to the user.
        assert "100.64.1.2" in result.output

    def test_webux_reload_is_single_host_and_skips_resolve_bind(self, monkeypatch):
        import os
        import uvicorn
        from jacked.cli import main

        captured = {}
        monkeypatch.setattr(
            uvicorn, "run", lambda *a, **k: captured.update(args=a, kwargs=k)
        )

        def _boom(*a, **k):
            raise AssertionError("resolve_bind must not run on the --reload path")

        monkeypatch.setattr("jacked.service.bind.resolve_bind", _boom)
        monkeypatch.setenv("JACKED_HOST", "sentinel")

        result = CliRunner().invoke(main, ["webux", "--no-browser", "--reload"])
        assert result.exit_code == 0, result.output
        assert os.environ["JACKED_HOST"] == "127.0.0.1"
        assert captured["kwargs"]["reload"] is True



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

    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": True, "died": True, "killed": False},
    )
    def test_stop_reports_ok_on_clean_stop(self, mock_stop):
        from jacked.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["service", "stop"])
        assert result.exit_code == 0
        assert "ok" in result.output.lower()
        assert "force-killed" not in result.output.lower()

    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": True, "died": True, "killed": True},
    )
    def test_stop_reports_when_sigkill_needed(self, mock_stop):
        """If the tray ignored SIGTERM, user should see the force-kill message."""
        from jacked.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["service", "stop"])
        assert result.exit_code == 0
        assert "force-killed" in result.output.lower()

    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": True, "died": False, "killed": True},
    )
    def test_stop_exits_nonzero_when_kill_fails(self, mock_stop):
        from jacked.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["service", "stop"])
        assert result.exit_code != 0


class TestServiceInstall:
    @patch("jacked.cli._spawn_service_detached")
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("jacked.service.platform.install_autostart")
    def test_install_calls_platform(self, mock_install, _mock_read, _mock_spawn):
        from jacked.cli import main
        mock_install.return_value = "Installed startup script: /test/path"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert result.exit_code == 0
        mock_install.assert_called_once()
        assert "Autostart registered" in result.output


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

    @patch("jacked.cli._spawn_service_detached")
    @patch("jacked.service.process.read_pid", return_value=None)
    @patch("jacked.service.platform.install_autostart")
    def test_install_shows_ok_on_success(self, mock_install, _mock_read, _mock_spawn):
        from jacked.cli import main
        mock_install.return_value = "Installed startup script: /test/path"
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert "OK" in result.output


class TestServiceRestart:
    """All tests mock ensure_native_lifecycle to avoid hitting real launchd.

    Unavailable-native path → exercises the manual stop+start fallback
    (used on Windows, bare POSIX, and --foreground)."""

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "no native lifecycle manager"))
    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": False, "died": False, "killed": False},
    )
    def test_restart_when_not_running_starts_fresh_detached(
        self, mock_stop, mock_popen, mock_find, mock_ensure,
    ):
        """Unavailable-native path: spawns a detached child and returns quickly."""
        from jacked.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_stop.assert_called_once()
        assert mock_popen.call_count == 1
        args = mock_popen.call_args[0][0]
        assert "service" in args and "start" in args
        kwargs = mock_popen.call_args[1]
        import sys as _sys
        if _sys.platform == "win32":
            # Windows launches `pythonw.exe -m jacked` to dodge the uv
            # console-trampoline window; jacked.exe is only the fallback.
            assert str(args[0]).lower().endswith("pythonw.exe")
            assert "-m" in args and "jacked" in args
        else:
            assert "/fake/jacked" in args
            assert kwargs.get("start_new_session") is True

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test"))
    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch("jacked.service.process.wait_for_port_free", return_value=True)
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": True, "died": True, "killed": False},
    )
    def test_restart_waits_for_pid_and_port(
        self, mock_stop, mock_wait_port, mock_popen, mock_find, mock_ensure,
    ):
        """After stop, must wait for port release before start."""
        from jacked.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_stop.assert_called_once()
        mock_wait_port.assert_called_once()
        mock_popen.assert_called_once()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "test"))
    @patch("subprocess.Popen")
    @patch("jacked.service.process.wait_for_port_free", return_value=False)
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": True, "died": True, "killed": False},
    )
    def test_restart_aborts_when_port_stays_bound(
        self, mock_stop, mock_wait_port, mock_popen, mock_ensure,
    ):
        """If port never frees, we must NOT spawn the start."""
        from jacked.cli import main
        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code != 0
        mock_popen.assert_not_called()

    @patch("jacked.service.tray.ServiceRunner")
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": False, "died": False, "killed": False},
    )
    def test_restart_foreground_blocks_on_ServiceRunner(
        self, mock_stop, mock_runner_cls,
    ):
        """--foreground runs the tray in-process (skips native handoff)."""
        from jacked.cli import main
        runner_instance = mock_runner_cls.return_value
        runner_instance.run.return_value = None

        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart", "--foreground"])
        assert result.exit_code == 0
        runner_instance.run.assert_called_once()


class TestServiceRestartAutoInstall:
    """0.41.24: service_restart handles missing plist via ensure_native_lifecycle."""

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(True, "already_installed", "plist at ~/Library/..."))
    @patch("jacked.service.platform.native_restart",
           return_value=(True, "launchctl kickstart succeeded"))
    def test_already_installed_runs_kickstart(self, mock_native, mock_ensure):
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_ensure.assert_called_once()
        mock_native.assert_called_once()
        assert "kickstart" in result.output.lower()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(True, "just_installed", "launchd agent installed and loaded"))
    @patch("jacked.service.platform.native_restart")
    def test_just_installed_skips_kickstart(self, mock_native, mock_ensure):
        """just_installed means launchd already loaded and RunAtLoad started
        the service fresh. Calling native_restart would race the boot."""
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_ensure.assert_called_once()
        mock_native.assert_not_called()
        assert "installed" in result.output.lower() or "ok" in result.output.lower()

    @patch("jacked.service.platform.ensure_native_lifecycle",
           return_value=(False, "unavailable", "no native lifecycle manager"))
    @patch("jacked.service.process.stop_process_graceful",
           return_value={"was_running": False, "died": False, "killed": False})
    @patch("subprocess.Popen")
    def test_unavailable_falls_back_to_manual(self, mock_popen, mock_stop, mock_ensure):
        from jacked.cli import main
        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_popen.assert_called_once()


class TestStartCommand:
    """`jacked start` — idempotent "make sure it's running" command.

    If the service is healthy it no-ops; if it's dead/stale/hung it
    (re)starts it detached and verifies it came up. This is the command a
    user runs when the tray disappears, without needing to know whether
    the service is actually down.
    """

    def test_noop_when_already_healthy(self):
        from jacked.cli import main
        with (
            patch("jacked.service.process.read_pid",
                  return_value={"pid": 4242, "port": 8321}),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.cli._service_http_ok", return_value=True),
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0
        assert "already running" in result.output.lower()
        spawn.assert_not_called()

    def test_cold_start_when_down(self, tmp_path):
        from jacked.cli import main
        with (
            patch("jacked.service.process.read_pid", return_value=None),
            patch("jacked.service.process.is_process_alive", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.cli._service_http_ok", return_value=False),
            patch("jacked.cli._spawn_service_detached",
                  return_value=tmp_path / "svc.log") as spawn,
            patch("jacked.cli._wait_service_ready", return_value=True),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()
        spawn.assert_called_once()

    def test_clears_stale_pid_then_starts(self, tmp_path):
        """A dead PID from a crash must be cleared, then a fresh start spawned."""
        from jacked.cli import main
        with (
            patch("jacked.service.process.read_pid",
                  return_value={"pid": 99, "port": 8321}),
            patch("jacked.service.process.is_process_alive", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.cli._service_http_ok", return_value=False),
            patch("jacked.cli._spawn_service_detached",
                  return_value=tmp_path / "svc.log") as spawn,
            patch("jacked.cli._wait_service_ready", return_value=True),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0
        remove.assert_called_once()
        spawn.assert_called_once()

    def test_restarts_when_alive_but_not_responding(self, tmp_path):
        """Process alive but dashboard hung → stop it and restart."""
        from jacked.cli import main
        with (
            patch("jacked.service.process.read_pid",
                  return_value={"pid": 77, "port": 8321}),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.service.process.stop_process_graceful",
                  return_value={"was_running": True, "died": True, "killed": False}) as stop,
            patch("jacked.service.process.wait_for_port_free", return_value=True),
            patch("jacked.cli._service_http_ok", return_value=False),
            patch("jacked.cli._spawn_service_detached",
                  return_value=tmp_path / "svc.log") as spawn,
            patch("jacked.cli._wait_service_ready", return_value=True),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0
        stop.assert_called_once()
        spawn.assert_called_once()

    def test_force_restart_when_healthy(self, tmp_path):
        """--restart tears down a healthy service and starts fresh."""
        from jacked.cli import main
        with (
            patch("jacked.service.process.read_pid",
                  return_value={"pid": 77, "port": 8321}),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.service.process.stop_process_graceful",
                  return_value={"was_running": True, "died": True, "killed": False}) as stop,
            patch("jacked.service.process.wait_for_port_free", return_value=True),
            patch("jacked.cli._service_http_ok", return_value=True),
            patch("jacked.cli._spawn_service_detached",
                  return_value=tmp_path / "svc.log") as spawn,
            patch("jacked.cli._wait_service_ready", return_value=True),
        ):
            result = CliRunner().invoke(main, ["start", "--restart"])
        assert result.exit_code == 0
        stop.assert_called_once()
        spawn.assert_called_once()

    def test_aborts_when_port_held_by_other(self):
        """Cold start but the port is squatted by a non-jacked process → abort."""
        from jacked.cli import main
        with (
            patch("jacked.service.process.read_pid", return_value=None),
            patch("jacked.service.process.is_process_alive", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=False),
            patch("jacked.cli._service_http_ok", return_value=False),
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code != 0
        spawn.assert_not_called()

    def test_exits_nonzero_when_never_ready(self, tmp_path):
        """Spawned, but the dashboard never answers → non-zero exit + hint."""
        from jacked.cli import main
        with (
            patch("jacked.service.process.read_pid", return_value=None),
            patch("jacked.service.process.is_process_alive", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.cli._service_http_ok", return_value=False),
            patch("jacked.cli._spawn_service_detached",
                  return_value=tmp_path / "svc.log"),
            patch("jacked.cli._wait_service_ready", return_value=False),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code != 0
        assert "didn't answer" in result.output.lower()
