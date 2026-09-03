"""Tests for jacked service CLI commands."""

from unittest.mock import MagicMock, patch
from click.testing import CliRunner
import pytest


def _ready_status(*_args, **_kwargs):
    return {"state": "running", "port": 8321}


def _legacy_plist(host: str, port: int = 8321) -> str:
    """A pre-M5 launchd plist with a baked --host, as installs used to write."""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "    <key>Label</key>\n"
        "    <string>ai.hank.jacked</string>\n"
        "    <key>ProgramArguments</key>\n"
        "    <array>\n"
        "        <string>/usr/local/bin/jacked</string>\n"
        "        <string>service</string>\n"
        "        <string>start</string>\n"
        f"        <string>--host</string>\n"
        f"        <string>{host}</string>\n"
        "        <string>--port</string>\n"
        f"        <string>{port}</string>\n"
        "    </array>\n"
        "</dict>\n"
        "</plist>\n"
    )


def _mem_db(monkeypatch):
    """One shared in-memory settings DB behind every Database() construction."""
    from jacked.web.database import Database

    db = Database(":memory:")
    monkeypatch.setattr("jacked.web.database.Database", lambda *a, **k: db)
    return db


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
            mode="tailscale",
            addresses=("127.0.0.1", "100.64.1.2"),
            port=8321,
            primary_host="100.64.1.2",
            tailscale_ip="100.64.1.2",
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
        from types import SimpleNamespace
        from jacked.cli import main

        runner = CliRunner()
        with (
            patch(
                "jacked.service.lifecycle.discover_service",
                return_value=SimpleNamespace(source="manifest", reason=""),
            ),
            patch(
                "jacked.service.ipc.send_native_control",
                return_value={
                    "ok": True,
                    "result": {
                        "state": "running",
                        "quarantine": False,
                        "build_version": "test",
                        "protocol_version": 2,
                        "generation": "abc",
                        "port": 8321,
                    },
                },
            ),
        ):
            result = runner.invoke(main, ["service", "status"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()
        assert "8321" in result.output

    def test_status_reports_starting_without_dashboard_claim(self):
        from types import SimpleNamespace

        from jacked.cli import main

        with (
            patch(
                "jacked.service.lifecycle.discover_service",
                return_value=SimpleNamespace(source="manifest", reason=""),
            ),
            patch(
                "jacked.service.ipc.send_native_control",
                return_value={
                    "ok": True,
                    "result": {
                        "state": "starting",
                        "build_version": "test",
                        "protocol_version": 2,
                        "generation": "abc",
                        "port": 8321,
                    },
                },
            ),
        ):
            result = CliRunner().invoke(main, ["service", "status"])

        assert result.exit_code == 0
        assert "starting" in result.output.lower()
        assert "Dashboard:" not in result.output

    def test_status_preserves_unknown_autostart_truth(self):
        from types import SimpleNamespace

        from jacked.cli import main
        from jacked.service.autostart import AutostartInspection, AutostartState

        with (
            patch(
                "jacked.service.lifecycle.discover_service",
                return_value=SimpleNamespace(source="default", reason="", port=None),
            ),
            patch(
                "jacked.service.platform.inspect_autostart",
                return_value=AutostartInspection(
                    AutostartState.UNKNOWN, "launchd unavailable"
                ),
            ),
        ):
            result = CliRunner().invoke(main, ["service", "status"])

        assert "Autostart: unknown" in result.output
        assert "Autostart: disabled" not in result.output

    def test_status_reports_healthy_legacy_without_claiming_control(self):
        from types import SimpleNamespace

        from jacked.cli import main

        with patch(
            "jacked.service.lifecycle.discover_service",
            return_value=SimpleNamespace(
                source="legacy", reason="", host="127.0.0.1", port=8321
            ),
        ):
            result = CliRunner().invoke(main, ["service", "status"])

        assert result.exit_code == 0, result.output
        assert "legacy service running" in result.output.lower()
        assert "control unavailable" in result.output.lower()


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
        "jacked.service.ipc.send_native_control",
        return_value={"ok": True, "result": {"accepted": True}},
    )
    def test_stop_reports_ok_on_clean_stop(self, mock_control):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("{}")
        runner = CliRunner()
        result = runner.invoke(main, ["service", "stop"])
        assert result.exit_code == 0
        assert "ok" in result.output.lower()
        mock_control.assert_called_once()

    def test_stop_refuses_legacy_pid_only_evidence(self):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.legacy_pid.write_text("123\n8321")
        runner = CliRunner()
        with patch(
            "jacked.service.legacy.resolve_active_legacy_service",
            return_value=MagicMock(pid=123, port=8321),
        ):
            result = runner.invoke(main, ["service", "stop"])
        assert result.exit_code != 0
        assert "refusing pid-only" in result.output.lower()

    def test_stop_ignores_stale_v2_compatibility_pid(self):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.legacy_pid.write_text("123\n8321\njacked-v2\n")

        result = CliRunner().invoke(main, ["service", "stop"])

        assert result.exit_code == 0, result.output
        assert "not running" in result.output.lower()

    def test_stop_ignores_reused_unhealthy_legacy_pid_without_signal(self):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.legacy_pid.write_text("4242\n8321\n")
        with (
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.service.legacy.probe_legacy_health", return_value=False),
            patch("jacked.service.ipc.send_native_control") as control,
        ):
            result = CliRunner().invoke(main, ["service", "stop"])

        assert result.exit_code == 0, result.output
        assert "not running" in result.output.lower()
        control.assert_not_called()

    @patch(
        "jacked.service.ipc.send_native_control",
        side_effect=OSError("unavailable"),
    )
    def test_stop_exits_nonzero_when_control_fails(self, mock_control):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("{}")
        runner = CliRunner()
        result = runner.invoke(main, ["service", "stop"])
        assert result.exit_code != 0
        assert "no process was" in result.output.lower()
        assert "signalled" in result.output.lower()


class TestServiceInstall:
    @pytest.fixture(autouse=True)
    def _service_becomes_ready(self, monkeypatch):
        monkeypatch.setattr("jacked.cli._wait_owned_service_ready", _ready_status)

    @patch("jacked.service.lifecycle.provision_service_contract")
    @patch("jacked.service.lifecycle.install_native_owned")
    def test_install_calls_platform(self, mock_install, mock_provision):
        from jacked.cli import main
        from jacked.service.spec import SupervisorKind

        spec = MagicMock(supervisor=SupervisorKind.LAUNCHD)
        mock_provision.return_value = (spec, {})
        mock_install.return_value = MagicMock(ok=True, reason="installed")
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert result.exit_code == 0
        mock_install.assert_called_once()
        assert "Autostart registered" in result.output


class TestServiceUninstall:
    @patch("jacked.service.lifecycle.provision_service_contract")
    @patch("jacked.service.lifecycle.uninstall_native_owned")
    def test_uninstall_calls_owned_lifecycle(self, mock_uninstall, mock_provision):
        from jacked.cli import main

        mock_provision.return_value = (MagicMock(), {})
        mock_uninstall.return_value = MagicMock(ok=True, reason="removed exact task")
        runner = CliRunner()
        result = runner.invoke(main, ["service", "uninstall"])
        assert result.exit_code == 0
        mock_uninstall.assert_called_once()
        assert "removed exact task" in result.output

    @patch("jacked.service.lifecycle.provision_service_contract")
    @patch("jacked.service.lifecycle.uninstall_native_owned")
    def test_uninstall_refuses_foreign_artifact(self, mock_uninstall, mock_provision):
        from jacked.cli import main

        mock_provision.return_value = (MagicMock(), {})
        mock_uninstall.return_value = MagicMock(
            ok=False,
            reason=(
                "foreign artifact at /test/jacked.service; run `jacked service recover`"
            ),
        )
        runner = CliRunner()
        result = runner.invoke(main, ["service", "uninstall"])

        assert result.exit_code != 0
        assert "/test/jacked.service" in result.output
        assert "recover" in result.output
        assert "pid or port" in result.output.lower()


class TestServiceInstallError:
    @pytest.fixture(autouse=True)
    def _service_becomes_ready(self, monkeypatch):
        monkeypatch.setattr("jacked.cli._wait_owned_service_ready", _ready_status)

    @patch(
        "jacked.service.lifecycle.provision_service_contract",
        side_effect=ValueError("bad"),
    )
    def test_install_shows_error_when_binary_not_found(self, mock_install):
        from jacked.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert result.exit_code == 0
        assert "Error" in result.output
        assert "safe service install failed" in result.output

    @patch("jacked.service.lifecycle.provision_service_contract")
    @patch("jacked.service.lifecycle.install_native_owned")
    def test_install_shows_ok_on_success(self, mock_install, mock_provision):
        from jacked.cli import main
        from jacked.service.spec import SupervisorKind

        mock_provision.return_value = (MagicMock(supervisor=SupervisorKind.LAUNCHD), {})
        mock_install.return_value = MagicMock(ok=True, reason="installed")
        runner = CliRunner()
        result = runner.invoke(main, ["service", "install"])
        assert "OK" in result.output


class TestServiceRecover:
    def test_private_invalid_manifest_is_quarantined_then_recovered(self):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("not-json", encoding="utf-8")
        paths.manifest.chmod(0o600)
        spec = MagicMock(generation="a" * 64)
        with (
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(spec, {}),
            ),
            patch(
                "jacked.service.lifecycle.native_artifact_path",
                return_value=paths.root / "supervisor",
            ),
            patch(
                "jacked.service.lifecycle.install_native_owned",
                return_value=MagicMock(ok=True, reason="installed"),
            ),
            patch("jacked.cli._wait_owned_service_ready", return_value={"state": "running"}),
        ):
            result = CliRunner().invoke(main, ["service", "recover"])

        assert result.exit_code == 0, result.output
        assert "Quarantined invalid ownership evidence" in result.output
        assert not paths.manifest.exists()
        assert list(paths.root.glob("api-v2.instance.json.invalid-*"))

    def test_active_v2_lease_is_reported_as_recovery_refusal(self):
        from jacked.cli import main
        from jacked.service.instance import ServiceLeaseBusy

        with patch(
            "jacked.service.lifecycle.quarantine_invalid_ownership",
            side_effect=ServiceLeaseBusy("already owned"),
        ):
            result = CliRunner().invoke(main, ["service", "recover"])

        assert result.exit_code != 0
        assert "could not establish ownership" in result.output.lower()
    def test_foreign_artifact_is_left_untouched_with_actionable_guidance(self):
        from jacked.cli import main
        with (
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(MagicMock(), {}),
            ),
            patch(
                "jacked.service.lifecycle.native_artifact_path",
                return_value="/owned/path/jacked.plist",
            ),
            patch(
                "jacked.service.lifecycle.install_native_owned",
                return_value=MagicMock(
                    ok=False,
                    reason="foreign artifact. Inspect and back up the path.",
                ),
            ) as install,
        ):
            result = CliRunner().invoke(main, ["service", "recover"])

        assert result.exit_code != 0
        assert "foreign artifact" in result.output
        assert "/owned/path/jacked.plist" in result.output
        install.assert_called_once()


class TestServiceRestart:
    """All tests mock ensure_native_lifecycle to avoid hitting real launchd.

    Unavailable-native path → exercises the manual stop+start fallback
    (used on Windows, bare POSIX, and --foreground)."""

    @pytest.fixture(autouse=True)
    def _service_becomes_ready(self, monkeypatch):
        monkeypatch.setattr("jacked.cli._wait_owned_service_ready", _ready_status)

    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(False, "unavailable", "no native lifecycle manager"),
    )
    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": False, "died": False, "killed": False},
    )
    def test_restart_when_not_running_starts_fresh_detached(
        self,
        mock_stop,
        mock_popen,
        mock_find,
        mock_ensure,
    ):
        """Unavailable-native path: spawns a detached child and returns quickly."""
        from jacked.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_stop.assert_not_called()
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

    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(False, "unavailable", "test"),
    )
    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch("jacked.service.process.wait_for_port_free", return_value=True)
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": True, "died": True, "killed": False},
    )
    def test_restart_waits_for_pid_and_port(
        self,
        mock_stop,
        mock_wait_port,
        mock_popen,
        mock_find,
        mock_ensure,
    ):
        """After stop, must wait for port release before start."""
        from jacked.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_stop.assert_not_called()
        mock_wait_port.assert_not_called()
        mock_popen.assert_called_once()

    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(False, "unavailable", "test"),
    )
    @patch("subprocess.Popen")
    @patch("jacked.service.process.wait_for_port_free", return_value=False)
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": True, "died": True, "killed": False},
    )
    def test_restart_aborts_when_port_stays_bound(
        self,
        mock_stop,
        mock_wait_port,
        mock_popen,
        mock_ensure,
    ):
        """Port ambiguity is handled by the child service's quarantine bind."""
        from jacked.cli import main

        runner = CliRunner()
        result = runner.invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_stop.assert_not_called()
        mock_wait_port.assert_not_called()
        mock_popen.assert_called_once()

    @patch("jacked.service.tray.ServiceRunner")
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": False, "died": False, "killed": False},
    )
    def test_restart_foreground_blocks_on_ServiceRunner(
        self,
        mock_stop,
        mock_runner_cls,
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
    """Restart no longer trusts legacy native artifacts without ServiceSpec."""

    @pytest.fixture(autouse=True)
    def _service_becomes_ready(self, monkeypatch):
        monkeypatch.setattr("jacked.cli._wait_owned_service_ready", _ready_status)

    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(True, "already_installed", "plist at ~/Library/..."),
    )
    @patch(
        "jacked.service.platform.native_restart",
        return_value=(True, "launchctl kickstart succeeded"),
    )
    def test_legacy_artifact_does_not_authorize_kickstart(
        self, mock_native, mock_ensure, mock_popen, mock_find
    ):
        from jacked.cli import main

        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_ensure.assert_not_called()
        mock_native.assert_not_called()
        mock_popen.assert_called_once()

    @patch("jacked.findbin.find_bin", return_value="/fake/jacked")
    @patch("subprocess.Popen")
    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(True, "just_installed", "launchd agent installed and loaded"),
    )
    @patch("jacked.service.platform.native_restart")
    def test_unverified_just_installed_artifact_is_not_used(
        self, mock_native, mock_ensure, mock_popen, mock_find
    ):
        from jacked.cli import main

        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_ensure.assert_not_called()
        mock_native.assert_not_called()
        mock_popen.assert_called_once()

    @patch(
        "jacked.service.platform.ensure_native_lifecycle",
        return_value=(False, "unavailable", "no native lifecycle manager"),
    )
    @patch(
        "jacked.service.process.stop_process_graceful",
        return_value={"was_running": False, "died": False, "killed": False},
    )
    @patch("subprocess.Popen")
    def test_unavailable_falls_back_to_manual(self, mock_popen, mock_stop, mock_ensure):
        from jacked.cli import main

        result = CliRunner().invoke(main, ["service", "restart"])
        assert result.exit_code == 0
        mock_popen.assert_called_once()


class TestStartCommand:
    """`jacked start` uses authenticated v2 control and refusal-only legacy evidence."""

    def test_noop_when_authenticated_v2_service_is_running(self):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("{}", encoding="utf-8")
        with (
            patch(
                "jacked.service.ipc.send_native_control",
                return_value={
                    "ok": True,
                    "result": {"state": "running", "port": 8421},
                },
            ) as control,
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0, result.output
        assert "authenticated v2 service" in result.output.lower()
        assert "8421" in result.output
        control.assert_called_once()
        stop.assert_not_called()
        remove.assert_not_called()
        spawn.assert_not_called()

    def test_restart_uses_authenticated_owned_handoff(self):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("{}", encoding="utf-8")
        spec = MagicMock()
        environment = {"PATH": "/safe"}
        with (
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(spec, environment),
            ),
            patch(
                "jacked.service.lifecycle.handoff_owned_service",
                return_value=MagicMock(ok=True, reason="new build started"),
            ) as handoff,
            patch("jacked.cli._manifest_is_proven_stale", return_value=False),
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            result = CliRunner().invoke(main, ["start", "--restart"])
        assert result.exit_code == 0, result.output
        handoff.assert_called_once_with(spec, environment=environment, paths=paths)
        stop.assert_not_called()
        remove.assert_not_called()
        spawn.assert_not_called()

    def test_degraded_v2_control_refuses_without_legacy_signal(self):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("{}", encoding="utf-8")
        with (
            patch(
                "jacked.service.ipc.send_native_control",
                side_effect=OSError("unreachable"),
            ),
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code != 0
        assert "service manifest is invalid" in result.output.lower()
        assert "jacked service" in result.output.lower()
        assert "recover" in result.output.lower()
        assert "no process was signalled" in result.output.lower()
        stop.assert_not_called()
        remove.assert_not_called()
        spawn.assert_not_called()

    def test_proven_stale_v2_manifest_self_heals_without_signalling(self, tmp_path):
        from jacked.cli import main
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
        paths.root.mkdir(parents=True, exist_ok=True)
        paths.manifest.write_text("{}", encoding="utf-8")
        with (
            patch(
                "jacked.service.ipc.send_native_control",
                side_effect=OSError("stale endpoint"),
            ),
            patch("jacked.cli._manifest_is_proven_stale", return_value=True),
            patch("jacked.service.process.read_pid", return_value=None),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch(
                "jacked.cli._spawn_service_detached", return_value=tmp_path / "svc.log"
            ) as spawn,
            patch(
                "jacked.cli._wait_owned_service_ready",
                return_value={"state": "running", "port": 8432},
            ),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0, result.output
        assert "stale" in result.output.lower()
        assert "8432" in result.output
        spawn.assert_called_once()

    def test_cold_start_when_down(self, tmp_path):
        from jacked.cli import main

        with (
            patch("jacked.service.process.read_pid", return_value=None),
            patch("jacked.service.process.is_process_alive", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.service.legacy.probe_legacy_health", return_value=False),
            patch(
                "jacked.cli._spawn_service_detached", return_value=tmp_path / "svc.log"
            ) as spawn,
            patch(
                "jacked.cli._wait_owned_service_ready",
                return_value={"state": "running", "port": 8321},
            ),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0
        assert "running" in result.output.lower()
        spawn.assert_called_once()

    def test_ignores_stale_pid_without_unlinking_then_starts(self, tmp_path):
        """A dead PID is non-authoritative and remains untouched."""
        from jacked.cli import main

        with (
            patch(
                "jacked.service.process.read_pid",
                return_value={"pid": 99, "port": 8321},
            ),
            patch("jacked.service.process.is_process_alive", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.service.legacy.probe_legacy_health", return_value=False),
            patch(
                "jacked.cli._spawn_service_detached", return_value=tmp_path / "svc.log"
            ) as spawn,
            patch(
                "jacked.cli._wait_owned_service_ready",
                return_value={"state": "running", "port": 8321},
            ),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0
        remove.assert_not_called()
        spawn.assert_called_once()

    def test_reused_live_pid_without_jacked_health_does_not_block_start(
        self, tmp_path
    ):
        """An unrelated process that reused a stale legacy PID is ignored."""
        from jacked.cli import main

        with (
            patch(
                "jacked.service.process.read_pid",
                return_value={"pid": 77, "port": 8321},
            ),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.service.legacy.probe_legacy_health", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch(
                "jacked.cli._spawn_service_detached", return_value=tmp_path / "svc.log"
            ) as spawn,
            patch(
                "jacked.cli._wait_owned_service_ready",
                return_value={"state": "running", "port": 8321},
            ),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0, result.output
        stop.assert_not_called()
        remove.assert_not_called()
        spawn.assert_called_once()

    def test_restart_refuses_healthy_legacy_pid_without_signal(self):
        """Even a healthy legacy PID cannot authorize --restart."""
        from jacked.cli import main

        with (
            patch(
                "jacked.service.process.read_pid",
                return_value={"pid": 77, "port": 8321},
            ),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.service.legacy.probe_legacy_health", return_value=True),
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            result = CliRunner().invoke(main, ["start", "--restart"])
        assert result.exit_code != 0
        assert "quit the old tray" in result.output.lower()
        stop.assert_not_called()
        remove.assert_not_called()
        spawn.assert_not_called()

    def test_healthy_legacy_service_is_reported_without_control(self):
        from jacked.cli import main

        with (
            patch(
                "jacked.service.process.read_pid",
                return_value={"pid": 77, "port": 8321},
            ),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.service.legacy.probe_legacy_health", return_value=True),
            patch("jacked.service.process.stop_process_graceful") as stop,
            patch("jacked.service.process.remove_pid") as remove,
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code == 0, result.output
        assert "legacy jacked service" in result.output.lower()
        stop.assert_not_called()
        remove.assert_not_called()
        spawn.assert_not_called()

    def test_aborts_when_port_held_by_other(self):
        """Cold start but the port is squatted by a non-jacked process → abort."""
        from jacked.cli import main

        with (
            patch("jacked.service.process.read_pid", return_value=None),
            patch("jacked.service.process.is_process_alive", return_value=False),
            patch("jacked.service.process.is_port_available", return_value=False),
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
            patch("jacked.service.legacy.probe_legacy_health", return_value=False),
            patch(
                "jacked.cli._spawn_service_detached", return_value=tmp_path / "svc.log"
            ),
            patch("jacked.cli._wait_owned_service_ready", return_value=None),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code != 0
        assert "didn't answer" in result.output.lower()

    def test_foreign_http_listener_never_satisfies_owned_readiness(self, tmp_path):
        from jacked.cli import main

        with (
            patch("jacked.service.process.read_pid", return_value=None),
            patch("jacked.service.process.is_port_available", return_value=True),
            patch(
                "jacked.cli._spawn_service_detached", return_value=tmp_path / "svc.log"
            ) as spawn,
            patch("jacked.cli._wait_owned_service_ready", return_value=None),
        ):
            result = CliRunner().invoke(main, ["start"])
        assert result.exit_code != 0
        spawn.assert_called_once()


class TestServiceStartBootMigration:
    """`service start` boot-time migration + argv neutralization: a pre-M5
    artifact's baked --host is captured into the DB, the artifact is rewritten
    host-free FILE-ONLY (we may BE the launchd job it describes — no bootout),
    and a typed --host that exactly equals the baked host is recognized as the
    artifact's own respawn and neutralized so the DB decides the bind."""

    def _invoke(self, monkeypatch, tmp_path, *, baked, argv_host):
        import jacked.cli as cli

        monkeypatch.setattr(cli.sys, "platform", "darwin")
        plist = tmp_path / "ai.hank.jacked.plist"
        if baked is not None:
            plist.write_text(_legacy_plist(baked), encoding="utf-8")
        db = _mem_db(monkeypatch)

        args = ["service", "start"]
        if argv_host is not None:
            args += ["--host", argv_host]
        with (
            patch(
                "jacked.service.platform._get_launchd_plist_path", return_value=plist
            ),
            patch("jacked.service.tray.ServiceRunner") as mock_runner_cls,
        ):
            mock_runner_cls.return_value.run.return_value = None
            result = CliRunner().invoke(cli.main, args)
        return result, mock_runner_cls, db, plist

    def test_respawn_argv_is_neutralized_and_db_migrated(
        self,
        monkeypatch,
        tmp_path,
    ):
        """Baked 0.0.0.0 + `--host 0.0.0.0` argv: this IS the artifact's own
        respawn — runner gets host None AND the DB captured enabled+all."""
        result, runner_cls, db, plist = self._invoke(
            monkeypatch,
            tmp_path,
            baked="0.0.0.0",
            argv_host="0.0.0.0",
        )
        assert result.exit_code == 0, result.output
        assert runner_cls.call_args.kwargs["host"] is None
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "all"
        # Artifact rewritten host-free, file only.
        assert "--host" not in plist.read_text()

    def test_different_typed_host_is_honored_as_one_shot(
        self,
        monkeypatch,
        tmp_path,
    ):
        """Baked 0.0.0.0 + `--host 192.168.1.5` argv: the typed host differs,
        so it is a deliberate one-shot and passes through untouched."""
        result, runner_cls, db, plist = self._invoke(
            monkeypatch,
            tmp_path,
            baked="0.0.0.0",
            argv_host="192.168.1.5",
        )
        assert result.exit_code == 0, result.output
        assert runner_cls.call_args.kwargs["host"] == "192.168.1.5"
        # The baked host still got migrated + the artifact still rewritten.
        assert db.get_setting("remote_access_enabled") == "true"
        assert "--host" not in plist.read_text()

    def test_no_artifact_honors_typed_host(self, monkeypatch, tmp_path):
        result, runner_cls, db, _plist = self._invoke(
            monkeypatch,
            tmp_path,
            baked=None,
            argv_host="192.168.1.5",
        )
        assert result.exit_code == 0, result.output
        assert runner_cls.call_args.kwargs["host"] == "192.168.1.5"
        assert db.get_setting("remote_access_enabled") is None

    def test_hostfree_artifact_leaves_argv_alone(self, monkeypatch, tmp_path):
        """An already-migrated (host-free) artifact: nothing to do."""
        import jacked.cli as cli

        monkeypatch.setattr(cli.sys, "platform", "darwin")
        plist = tmp_path / "ai.hank.jacked.plist"
        hostfree = _legacy_plist("PLACEHOLDER").replace(
            "        <string>--host</string>\n        <string>PLACEHOLDER</string>\n",
            "",
        )
        plist.write_text(hostfree, encoding="utf-8")
        before = plist.read_text()
        db = _mem_db(monkeypatch)
        with (
            patch(
                "jacked.service.platform._get_launchd_plist_path", return_value=plist
            ),
            patch("jacked.service.tray.ServiceRunner") as mock_runner_cls,
        ):
            mock_runner_cls.return_value.run.return_value = None
            result = CliRunner().invoke(cli.main, ["service", "start"])
        assert result.exit_code == 0, result.output
        assert mock_runner_cls.call_args.kwargs["host"] is None
        assert plist.read_text() == before  # untouched
        assert db.get_setting("remote_access_enabled") is None

    def test_migration_guard_never_clobbers_gui_choice(
        self,
        monkeypatch,
        tmp_path,
    ):
        """Stale baked 0.0.0.0 vs a GUI that already turned remote access OFF:
        the boot migration must leave the GUI's choice alone (argv is still
        neutralized — the DB decides, and the DB says off)."""
        import jacked.cli as cli

        monkeypatch.setattr(cli.sys, "platform", "darwin")
        plist = tmp_path / "ai.hank.jacked.plist"
        plist.write_text(_legacy_plist("0.0.0.0"), encoding="utf-8")
        db = _mem_db(monkeypatch)
        db.set_setting("remote_access_enabled", "false")
        with (
            patch(
                "jacked.service.platform._get_launchd_plist_path", return_value=plist
            ),
            patch("jacked.service.tray.ServiceRunner") as mock_runner_cls,
        ):
            mock_runner_cls.return_value.run.return_value = None
            result = CliRunner().invoke(
                cli.main, ["service", "start", "--host", "0.0.0.0"]
            )
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "false"
        assert mock_runner_cls.call_args.kwargs["host"] is None
        assert "--host" not in plist.read_text()

    def test_stale_launchd_replay_is_ignored_when_db_configured(
        self,
        monkeypatch,
        tmp_path,
    ):
        """CRITICAL regression: the disk artifact is already host-free (this
        version stripped it), but launchd replays a STALE in-memory `--host
        0.0.0.0` on a crash-respawn/kickstart. With remote access configured in
        the DB as OFF, the stale host must be IGNORED so the dashboard is not
        silently re-exposed on 0.0.0.0 against the user's saved choice."""
        import jacked.cli as cli

        monkeypatch.setattr(cli.sys, "platform", "darwin")
        plist = tmp_path / "ai.hank.jacked.plist"
        # Host-free artifact (already migrated) — the stale --host is NOT here.
        hostfree = _legacy_plist("PLACEHOLDER").replace(
            "        <string>--host</string>\n        <string>PLACEHOLDER</string>\n",
            "",
        )
        plist.write_text(hostfree, encoding="utf-8")
        db = _mem_db(monkeypatch)
        db.set_setting("remote_access_enabled", "false")  # user turned it OFF
        with (
            patch(
                "jacked.service.platform._get_launchd_plist_path", return_value=plist
            ),
            patch("jacked.service.tray.ServiceRunner") as mock_runner_cls,
        ):
            mock_runner_cls.return_value.run.return_value = None
            result = CliRunner().invoke(
                cli.main, ["service", "start", "--host", "0.0.0.0"]
            )
        assert result.exit_code == 0, result.output
        # Stale --host ignored -> runner gets None -> resolve_bind reads the DB.
        assert mock_runner_cls.call_args.kwargs["host"] is None
        assert db.get_setting("remote_access_enabled") == "false"

    def test_hostfree_artifact_honors_typed_host_when_db_unconfigured(
        self,
        monkeypatch,
        tmp_path,
    ):
        """Backward compat: host-free artifact, a typed --host, and NO DB
        remote-access setting (legacy pure-CLI user who never used the GUI) ->
        the typed host is honored as a one-shot override."""
        import jacked.cli as cli

        monkeypatch.setattr(cli.sys, "platform", "darwin")
        plist = tmp_path / "ai.hank.jacked.plist"
        hostfree = _legacy_plist("PLACEHOLDER").replace(
            "        <string>--host</string>\n        <string>PLACEHOLDER</string>\n",
            "",
        )
        plist.write_text(hostfree, encoding="utf-8")
        db = _mem_db(monkeypatch)
        assert db.get_setting("remote_access_enabled") is None  # precondition
        with (
            patch(
                "jacked.service.platform._get_launchd_plist_path", return_value=plist
            ),
            patch("jacked.service.tray.ServiceRunner") as mock_runner_cls,
        ):
            mock_runner_cls.return_value.run.return_value = None
            result = CliRunner().invoke(
                cli.main, ["service", "start", "--host", "192.168.1.5"]
            )
        assert result.exit_code == 0, result.output
        assert mock_runner_cls.call_args.kwargs["host"] == "192.168.1.5"


class TestServiceInstallRemoteAccessParity:
    """`service install --host X` maps X onto the dashboard Remote access
    setting (OVERWRITING existing keys — the command expresses intent), then
    installs host-free. Unmapped IPs stay a one-shot for the immediate spawn."""

    @pytest.fixture(autouse=True)
    def _service_becomes_ready(self, monkeypatch):
        monkeypatch.setattr("jacked.cli._wait_owned_service_ready", _ready_status)

    def _invoke(self, monkeypatch, host, platform="win32"):
        import jacked.cli as cli

        db = _mem_db(monkeypatch)
        monkeypatch.setattr(cli.sys, "platform", platform)
        with (
            patch("jacked.service.lifecycle.provision_service_contract") as provision,
            patch(
                "jacked.service.lifecycle.install_native_owned",
                return_value=MagicMock(ok=True, reason="installed"),
            ),
            patch("jacked.cli._spawn_service_detached") as spawn,
        ):
            from jacked.service.spec import SupervisorKind

            provision.return_value = (
                MagicMock(supervisor=SupervisorKind.TASK_SCHEDULER),
                {},
            )
            result = CliRunner().invoke(
                cli.main, ["service", "install", "--host", host]
            )
        return result, db, spawn

    def test_all_interfaces_writes_enabled_all(self, monkeypatch):
        result, db, spawn = self._invoke(monkeypatch, "0.0.0.0")
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "all"
        assert "Remote access setting updated" in result.output
        # Host consumed -> the immediate spawn is host-free (DB decides).
        spawn.assert_not_called()

    def test_tailscale_ip_writes_enabled_tailscale(self, monkeypatch):
        result, db, _spawn = self._invoke(monkeypatch, "100.77.1.2")
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "tailscale"
        assert "Remote access setting updated" in result.output

    def test_loopback_writes_disabled(self, monkeypatch):
        result, db, _spawn = self._invoke(monkeypatch, "127.0.0.1")
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "false"
        assert "Remote access setting updated" in result.output

    def test_overwrites_existing_setting(self, monkeypatch):
        """Unlike the artifact migration, an explicit --host OVERWRITES: the
        command is the user expressing intent right now."""
        import jacked.cli as cli

        db = _mem_db(monkeypatch)
        db.set_setting("remote_access_enabled", "false")
        db.set_setting("remote_access_scope", "tailscale")
        monkeypatch.setattr(cli.sys, "platform", "win32")
        with (
            patch("jacked.service.lifecycle.provision_service_contract") as provision,
            patch(
                "jacked.service.lifecycle.install_native_owned",
                return_value=MagicMock(ok=True, reason="installed"),
            ),
            patch("jacked.cli._spawn_service_detached"),
        ):
            from jacked.service.spec import SupervisorKind

            provision.return_value = (
                MagicMock(supervisor=SupervisorKind.TASK_SCHEDULER),
                {},
            )
            result = CliRunner().invoke(
                cli.main, ["service", "install", "--host", "0.0.0.0"]
            )
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "all"

    def test_unmapped_ip_is_one_shot_pass_through(self, monkeypatch):
        result, db, spawn = self._invoke(monkeypatch, "192.168.1.5")
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") is None
        assert db.get_setting("remote_access_scope") is None
        assert "not applied" in result.output
        assert "host-free" in result.output
        spawn.assert_not_called()


class TestServiceRestartRemoteAccessParity:
    """`service restart --host X` maps X onto the Remote access setting before
    restarting, so the restarted service (native kickstart or detached spawn)
    resolves the new mode from the DB — this is what makes the command finally
    work reliably on macOS, where kickstart reuses launchd's in-memory argv."""

    @pytest.fixture(autouse=True)
    def _service_becomes_ready(self, monkeypatch):
        monkeypatch.setattr("jacked.cli._wait_owned_service_ready", _ready_status)

    def test_all_interfaces_writes_db_then_safe_detached_start(self, monkeypatch):
        import jacked.cli as cli

        db = _mem_db(monkeypatch)
        with patch("jacked.cli._spawn_service_detached") as mock_spawn:
            result = CliRunner().invoke(
                cli.main, ["service", "restart", "--host", "0.0.0.0"]
            )
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "true"
        assert db.get_setting("remote_access_scope") == "all"
        assert "Remote access setting updated" in result.output
        mock_spawn.assert_called_once_with(None, 8321)

    def test_loopback_disables_then_restarts(self, monkeypatch):
        import jacked.cli as cli

        db = _mem_db(monkeypatch)
        db.set_setting("remote_access_enabled", "true")
        db.set_setting("remote_access_scope", "all")
        with (
            patch(
                "jacked.service.platform.ensure_native_lifecycle",
                return_value=(True, "already_installed", "plist installed"),
            ),
            patch(
                "jacked.service.platform.native_restart",
                return_value=(True, "launchctl kickstart"),
            ),
        ):
            result = CliRunner().invoke(
                cli.main, ["service", "restart", "--host", "127.0.0.1"]
            )
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "false"
        assert "Remote access setting updated" in result.output

    def test_mapped_host_spawn_argv_is_hostfree(self, monkeypatch):
        """On the manual fallback path a MAPPED host must not leak into the
        detached child's argv — the DB carries the intent now."""
        import jacked.cli as cli

        db = _mem_db(monkeypatch)
        with (
            patch(
                "jacked.service.platform.ensure_native_lifecycle",
                return_value=(False, "unavailable", "no native manager"),
            ),
            patch("jacked.findbin.find_bin", return_value="/fake/jacked"),
            patch("subprocess.Popen") as mock_popen,
            patch(
                "jacked.service.process.stop_process_graceful",
                return_value={"was_running": False, "died": False, "killed": False},
            ),
        ):
            result = CliRunner().invoke(
                cli.main, ["service", "restart", "--host", "0.0.0.0"]
            )
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") == "true"
        args = mock_popen.call_args[0][0]
        assert "--host" not in args

    def test_unmapped_ip_one_shot_passes_through_spawn(self, monkeypatch):
        import jacked.cli as cli

        db = _mem_db(monkeypatch)
        with (
            patch(
                "jacked.service.platform.ensure_native_lifecycle",
                return_value=(False, "unavailable", "no native manager"),
            ),
            patch("jacked.findbin.find_bin", return_value="/fake/jacked"),
            patch("subprocess.Popen") as mock_popen,
            patch(
                "jacked.service.process.stop_process_graceful",
                return_value={"was_running": False, "died": False, "killed": False},
            ),
        ):
            result = CliRunner().invoke(
                cli.main, ["service", "restart", "--host", "192.168.1.5"]
            )
        assert result.exit_code == 0, result.output
        assert db.get_setting("remote_access_enabled") is None
        assert "one-shot" in result.output
        args = mock_popen.call_args[0][0]
        assert "--host" in args
        assert args[args.index("--host") + 1] == "192.168.1.5"
