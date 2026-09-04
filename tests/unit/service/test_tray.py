"""Tests for jacked.service.tray module."""

import socket
import time
from types import SimpleNamespace

import pytest
from unittest.mock import patch, MagicMock


def _skip_if_no_tray():
    """Skip test if pystray/Pillow can't load (not installed, or headless:
    pystray resolves its GUI backend at import and raises non-ImportError
    errors like Xlib.error.DisplayNameError when there is no display)."""
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except Exception:
        pytest.skip("pystray/Pillow unavailable (missing or headless)")


class TestCreateIcon:
    """Tests for create_icon_image()."""

    def test_running_icon_is_64x64(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        img = create_icon_image("running")
        assert img.size == (64, 64)

    def test_stopped_icon_is_64x64(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        img = create_icon_image("stopped")
        assert img.size == (64, 64)

    def test_starting_icon_is_64x64(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        img = create_icon_image("starting")
        assert img.size == (64, 64)

    def test_different_states_produce_different_images(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        running = create_icon_image("running")
        stopped = create_icon_image("stopped")
        assert running.tobytes() != stopped.tobytes()

    def test_unknown_state_defaults_to_stopped(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        unknown = create_icon_image("bogus")
        stopped = create_icon_image("stopped")
        assert unknown.tobytes() == stopped.tobytes()

    def test_update_available_draws_blue_badge_dot(self):
        """update_available=True paints the blue badge dot (parity with the
        macOS menu-bar icon). (51, 13) is the badge dot center."""
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        badged = create_icon_image("running", update_available=True)
        assert badged.getpixel((51, 13)) == (59, 130, 246, 255)

    def test_no_badge_when_update_unavailable(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        plain = create_icon_image("running")
        assert plain.getpixel((51, 13)) != (59, 130, 246, 255)


class TestMarkVisibility:
    """Regression: the tray mark must survive the OS downscale to a 16px
    Windows tray cell.

    History: the icon used to be a font-rendered 'J'. Thin font strokes plus
    font-resolution failures on Windows (bare 'Arial' only resolves on
    macOS) shrank it to an invisible speck. The mark is now a bold shipped
    silhouette (jacked.service.icon) — these tests pin its visibility at
    both the render size and the tray-cell size, independent of any font.
    """

    def _coverage(self, img, size):
        """Fraction of pixels with meaningful alpha after LANCZOS downscale."""
        from PIL import Image
        small = img.convert("RGBA").resize((size, size), Image.LANCZOS)
        data = small.tobytes()
        solid = sum(1 for i in range(0, len(data), 4) if data[i + 3] > 128)
        return solid / (size * size)

    def test_running_icon_is_bold_at_full_size(self):
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        cov = self._coverage(create_icon_image("running"), 64)
        assert cov > 0.15, f"mark covers only {cov:.0%} of the 64px canvas"

    def test_running_icon_survives_16px_tray_downscale(self):
        """The Windows tray renders at 16px — the actual bug surface. The
        old 10px-bitmap 'J' covered ~2% of a 16px cell; the shipped arm
        silhouette measures ~42%."""
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        # Calibration: shipped mark ~42% of a 16px cell, the old font-bug
        # speck ~2%. 15% floor separates them with margin for redesigns.
        cov = self._coverage(create_icon_image("running"), 16)
        assert cov > 0.15, f"mark covers only {cov:.0%} of a 16px tray cell"

    def test_all_three_states_render_distinct_tints(self):
        """running/starting/stopped must each get their own fill — pins the
        amber 'starting' tint, not just running-vs-stopped."""
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        imgs = {s: create_icon_image(s).tobytes() for s in ("running", "starting", "stopped")}
        assert len(set(imgs.values())) == 3, "two service states share a tint"

    def test_icon_renders_without_any_font(self):
        """The mark renderer must not touch font machinery at all — font
        resolution was the Windows failure class. jacked.service.tray no
        longer defines _load_glyph_font."""
        _skip_if_no_tray()
        from jacked.service import tray
        assert not hasattr(tray, "_load_glyph_font")


class TestBuildMenu:
    """Tests for build_menu()."""

    def test_menu_has_expected_items(self):
        _skip_if_no_tray()
        from jacked.service.tray import build_menu
        def noop():
            return None
        menu = build_menu(
            port=8321,
            version="0.39.0",
            autostart_check=lambda: True,
            on_open_dashboard=noop,
            on_restart=noop,
            on_stop=noop,
            on_toggle_autostart=noop,
        )
        items = list(menu)
        texts = [str(item) for item in items]
        assert any("Dashboard" in t for t in texts)
        assert any("Restart" in t for t in texts)
        assert any("Stop" in t for t in texts)
        assert any("Login" in t for t in texts)
        assert any("0.39.0" in t for t in texts)


class TestServiceRunner:
    """Tests for ServiceRunner lifecycle."""

    def test_init_stores_config(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=9999)
        assert runner.host == "127.0.0.1"
        assert runner.port == 9999

    def test_uvicorn_server_initialized_in_init(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        assert hasattr(runner, "_uvicorn_server")
        assert runner._uvicorn_server is None

    def test_resolve_active_legacy_service_pid_ignores_stale_entry(self, monkeypatch):
        from jacked.service.tray import ServiceRunner

        monkeypatch.setattr(
            "jacked.service.process.read_pid",
            lambda *args, **kwargs: {"pid": 4242, "port": 8321},
        )
        monkeypatch.setattr(
            "jacked.service.process.is_process_alive", lambda _pid: False
        )

        assert ServiceRunner._resolve_active_legacy_service_pid() is None

    def test_guard_legacy_pid_or_exit_raises_when_live(self, monkeypatch):
        from jacked.service.tray import ServiceRunner

        monkeypatch.setattr(
            "jacked.service.process.read_pid",
            lambda *args, **kwargs: {"pid": 4242, "port": 8321},
        )
        monkeypatch.setattr(
            "jacked.service.process.is_process_alive", lambda _pid: True
        )
        monkeypatch.setattr(
            "jacked.service.legacy.probe_legacy_health", lambda *_args: True
        )

        with pytest.raises(SystemExit) as exc_info:
            ServiceRunner()._guard_legacy_pid_or_exit()
        assert exc_info.value.code == 0

    def test_run_claims_lease_then_yields_to_live_legacy_pid(self):
        from jacked.service.tray import ServiceRunner

        ownership = MagicMock()
        with (
            patch("jacked.service.lifecycle.provision_service_contract", return_value=(MagicMock(), {})),
            patch("jacked.service.process.read_pid", return_value={"pid": 4242, "port": 8321}),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("jacked.service.legacy.probe_legacy_health", return_value=True),
            patch(
                "jacked.service.lifecycle.claim_service_ownership",
                return_value=ownership,
            ) as mock_claim,
            patch.object(ServiceRunner, "_run") as mock_run,
        ):
            with pytest.raises(SystemExit) as exc_info:
                ServiceRunner().run()
            assert exc_info.value.code == 0
            mock_claim.assert_called_once()
            ownership.close.assert_called_once_with()
            mock_run.assert_not_called()

    def test_run_lease_contention_is_clean_noop_for_every_starter(self):
        from jacked.service.instance import ServiceLeaseBusy
        from jacked.service.tray import ServiceRunner

        with (
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(MagicMock(), {}),
            ),
            patch(
                "jacked.service.lifecycle.claim_service_ownership",
                side_effect=ServiceLeaseBusy(
                    "another process holds the service lease"
                ),
            ),
            patch.object(ServiceRunner, "_run") as mock_run,
        ):
            with pytest.raises(SystemExit) as exc_info:
                ServiceRunner().run()
            assert exc_info.value.code == 0
            mock_run.assert_not_called()

    def test_invalid_ownership_is_nonzero_for_manual_starter(self):
        from jacked.service.instance import ServiceOwnershipInvalid
        from jacked.service.spec import SupervisorKind
        from jacked.service.tray import ServiceRunner

        spec = MagicMock(supervisor=SupervisorKind.MANUAL)
        with (
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(spec, {}),
            ),
            patch(
                "jacked.service.lifecycle.claim_service_ownership",
                side_effect=ServiceOwnershipInvalid("invalid manifest"),
            ),
            patch.object(ServiceRunner, "_run") as mock_run,
            pytest.raises(SystemExit) as exc_info,
        ):
            ServiceRunner().run()
        assert exc_info.value.code != 0
        assert "service recover" in str(exc_info.value)
        mock_run.assert_not_called()

    def test_cold_start_rechecks_legacy_pid_before_binding(self):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._ownership = MagicMock()
        with (
            patch.object(
                runner, "_guard_legacy_pid_or_exit", side_effect=SystemExit(0)
            ) as guard,
            patch("jacked.service.bind.create_sockets") as create_sockets,
            pytest.raises(SystemExit) as exc_info,
        ):
            runner._start_uvicorn(cold_start=True)
        assert exc_info.value.code == 0
        guard.assert_called_once_with()
        create_sockets.assert_not_called()

    def test_bind_race_with_legacy_jacked_listener_yields_instead_of_quarantine(self):
        from jacked.service.bind import BindPlan
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._ownership = MagicMock()
        plan = BindPlan(
            mode="loopback",
            addresses=("127.0.0.1",),
            port=8321,
            primary_host="127.0.0.1",
        )
        with (
            patch.object(runner, "_guard_legacy_pid_or_exit"),
            patch.object(
                runner, "_legacy_jacked_listener_detected", return_value=True
            ) as detected,
            patch("jacked.service.bind.resolve_bind", return_value=plan),
            patch(
                "jacked.service.bind.create_sockets",
                side_effect=OSError("address in use"),
            ) as create_sockets,
            pytest.raises(SystemExit) as exc_info,
        ):
            runner._start_uvicorn(cold_start=True)
        assert exc_info.value.code == 0
        detected.assert_called_once_with("127.0.0.1", 8321)
        assert create_sockets.call_count == 1
        runner._ownership.publish.assert_not_called()

    def test_health_probe_requires_stale_private_pid_evidence(self):
        from jacked.service.legacy import LegacyPidEvidence
        from jacked.service.tray import ServiceRunner

        response = MagicMock()
        response.status = 200
        response.read.return_value = b'{"status":"ok","db":true}'
        response.__enter__.return_value = response
        stale = LegacyPidEvidence(pid=4242, port=8321, alive=False)
        with (
            patch("jacked.service.legacy.inspect_legacy_pid", return_value=stale),
            patch("urllib.request.urlopen", return_value=response),
        ):
            assert ServiceRunner._legacy_jacked_listener_detected(
                "127.0.0.1", 8321
            ) is True

        with patch("jacked.service.legacy.inspect_legacy_pid", return_value=None):
            assert ServiceRunner._legacy_jacked_listener_detected(
                "127.0.0.1", 8321
            ) is False

    def test_pystray_icon_is_not_created_until_bind_preflight_succeeds(self):
        import jacked.service.tray as tray_module
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._ownership = MagicMock()
        fake_pystray = MagicMock()
        with (
            patch.object(tray_module, "pystray", fake_pystray, create=True),
            patch.object(tray_module, "_TRAY_AVAILABLE", True),
            patch.object(tray_module, "_UVICORN_AVAILABLE", True),
            patch.object(tray_module, "_mac_menubar_available", return_value=False),
            patch.object(runner, "_start_uvicorn", side_effect=SystemExit(0)),
            patch("jacked.service.platform.detect_autostart", return_value=True),
            patch("signal.signal"),
            pytest.raises(SystemExit) as exc_info,
        ):
            runner._run()
        assert exc_info.value.code == 0
        fake_pystray.Icon.assert_not_called()

    def test_pystray_icon_is_not_created_when_server_never_becomes_ready(self):
        import jacked.service.tray as tray_module
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        fake_thread = MagicMock()
        fake_pystray = MagicMock()
        with (
            patch.object(tray_module, "pystray", fake_pystray, create=True),
            patch.object(tray_module, "_TRAY_AVAILABLE", True),
            patch.object(tray_module, "_UVICORN_AVAILABLE", True),
            patch.object(tray_module, "_mac_menubar_available", return_value=False),
            patch.object(tray_module, "is_port_available", return_value=True),
            patch.object(runner, "_start_uvicorn", return_value=fake_thread),
            patch.object(runner, "_wait_for_ready", return_value=False),
            patch("jacked.service.platform.detect_autostart", return_value=True),
            patch("signal.signal"),
            pytest.raises(SystemExit),
        ):
            runner._run()
        fake_pystray.Icon.assert_not_called()
        fake_thread.join.assert_called_once_with(timeout=5)

    def test_autostart_toggle_uses_v2_lifecycle_not_legacy_writer(self):
        from jacked.service.autostart import AutostartInspection, AutostartState
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        result = MagicMock(ok=True)
        with (
            patch("jacked.service.lifecycle.default_service_paths") as paths,
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(MagicMock(), {}),
            ),
            patch(
                "jacked.service.lifecycle.install_native_owned",
                return_value=result,
            ) as install_owned,
            patch("jacked.service.lifecycle.uninstall_native_owned") as uninstall_owned,
            patch(
                "jacked.service.platform.inspect_autostart",
                side_effect=[
                    AutostartInspection(AutostartState.ABSENT),
                    AutostartInspection(AutostartState.OWNED_ENABLED),
                ],
            ),
            patch("jacked.service.platform.install_autostart") as legacy_install,
        ):
            runner._on_toggle_autostart()
        install_owned.assert_called_once()
        uninstall_owned.assert_not_called()
        legacy_install.assert_not_called()
        assert install_owned.call_args.kwargs["paths"] is paths.return_value
        assert runner._autostart_enabled is True

    def test_autostart_toggle_refuses_legacy_definition(self):
        from jacked.service.autostart import AutostartInspection, AutostartState
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        before = AutostartInspection(AutostartState.LEGACY, "old plist")
        with (
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(MagicMock(), {}),
            ),
            patch("jacked.service.lifecycle.install_native_owned") as install_owned,
            patch("jacked.service.lifecycle.uninstall_native_owned") as uninstall_owned,
            patch("jacked.service.platform.inspect_autostart", return_value=before),
        ):
            runner._on_toggle_autostart()
        install_owned.assert_not_called()
        uninstall_owned.assert_not_called()

    def test_autostart_toggle_notifies_when_post_state_is_unknown(self):
        from jacked.service.autostart import AutostartInspection, AutostartState
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._icon = MagicMock()
        with (
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(MagicMock(), {}),
            ),
            patch(
                "jacked.service.lifecycle.uninstall_native_owned",
                return_value=MagicMock(ok=True),
            ),
            patch(
                "jacked.service.platform.inspect_autostart",
                side_effect=[
                    AutostartInspection(AutostartState.OWNED_ENABLED),
                    AutostartInspection(AutostartState.UNKNOWN, "manager unavailable"),
                ],
            ),
        ):
            runner._on_toggle_autostart()

        runner._icon.notify.assert_called_once()
        assert "recover" in runner._icon.notify.call_args.args[0]

    # create=True: headless boxes never bind tray.pystray (backend resolution
    # fails without a display), and @patch resolves the target BEFORE the
    # in-body _skip_if_no_tray() can skip.
    @patch("jacked.service.bind.create_sockets", return_value=[])
    @patch("jacked.service.tray.pystray", create=True)
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_thread_is_daemon(self, mock_uvicorn, mock_pystray, mock_socks):
        _skip_if_no_tray()
        from jacked.service.bind import BindPlan
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        plan = BindPlan(
            mode="loopback", addresses=("127.0.0.1",), port=8321,
            primary_host="127.0.0.1",
        )
        with patch("jacked.service.bind.resolve_bind", return_value=plan):
            thread = runner._start_uvicorn()
        assert thread.daemon is True
        thread.join(timeout=0.1)

    @patch("jacked.service.bind.create_sockets", return_value=[])
    @patch("jacked.service.tray.pystray", create=True)
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_reresolves_and_sets_env_from_plan(
        self, mock_uvicorn, mock_pystray, mock_socks, monkeypatch,
    ):
        """_start_uvicorn re-resolves the bind plan on EVERY call (the tray's
        re-read-on-restart guarantee) and feeds JACKED_HOST from the plan's
        primary_host, not self.cli_host."""
        import os

        _skip_if_no_tray()
        from jacked.service.bind import BindPlan
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host=None, port=8321)  # no cli override

        # First start: DB says loopback.
        loop_plan = BindPlan(
            mode="loopback", addresses=("127.0.0.1",), port=8321,
            primary_host="127.0.0.1",
        )
        # Second start (a Restart after a GUI toggle): DB now says tailscale.
        ts_plan = BindPlan(
            mode="tailscale", addresses=("127.0.0.1", "100.64.9.9"), port=8321,
            primary_host="100.64.9.9", tailscale_ip="100.64.9.9",
        )
        plans = [loop_plan, ts_plan]
        calls = []

        def _fake_resolve(cli_host, port):
            calls.append((cli_host, port))
            return plans[len(calls) - 1]

        monkeypatch.setattr("jacked.service.bind.resolve_bind", _fake_resolve)

        t1 = runner._start_uvicorn()
        t1.join(timeout=0.1)
        assert runner.bind_plan is loop_plan
        assert runner.host == "127.0.0.1"
        assert os.environ["JACKED_HOST"] == "127.0.0.1"
        assert os.environ["JACKED_PORT"] == "8321"

        t2 = runner._start_uvicorn()
        t2.join(timeout=0.1)
        # resolve_bind was invoked AGAIN — the re-read-on-restart contract.
        assert len(calls) == 2
        assert calls == [(None, 8321), (None, 8321)]
        assert runner.bind_plan is ts_plan
        assert runner.host == "100.64.9.9"
        assert os.environ["JACKED_HOST"] == "100.64.9.9"

    @patch("jacked.service.tray.pystray", create=True)
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_cold_start_raises_systemexit_on_bind_conflict(
        self, mock_uvicorn, mock_pystray,
    ):
        """At COLD START a create_sockets OSError surfaces as SystemExit with the
        friendly message, so the boot exits cleanly rather than dying silently in
        the daemon thread."""
        _skip_if_no_tray()
        from jacked.service.bind import BindPlan
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        plan = BindPlan(
            mode="loopback", addresses=("127.0.0.1",), port=8321,
            primary_host="127.0.0.1",
        )
        with (
            patch("jacked.service.bind.resolve_bind", return_value=plan),
            patch("jacked.service.bind.create_sockets",
                  side_effect=OSError("Failed to bind 127.0.0.1:8321")),
            pytest.raises(SystemExit) as exc_info,
        ):
            runner._start_uvicorn(cold_start=True)
        assert "already in use" in str(exc_info.value)

    @patch("jacked.service.tray.pystray", create=True)
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_restart_raises_oserror_not_systemexit(
        self, mock_uvicorn, mock_pystray,
    ):
        """On the RESTART path (default), a create_sockets bind failure must
        raise OSError (an Exception), NOT SystemExit (a BaseException), so
        _on_restart's `except Exception` retry loop can catch it and fall back to
        the "stopped" state instead of the exception escaping and stranding a
        dead dashboard."""
        _skip_if_no_tray()
        from jacked.service.bind import BindPlan
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        plan = BindPlan(
            mode="loopback", addresses=("127.0.0.1",), port=8321,
            primary_host="127.0.0.1",
        )
        with (
            patch("jacked.service.bind.resolve_bind", return_value=plan),
            patch("jacked.service.bind.create_sockets",
                  side_effect=OSError("Failed to bind 127.0.0.1:8321")),
        ):
            with pytest.raises(OSError):
                runner._start_uvicorn()  # default: restart mode
            # And prove it is NOT a SystemExit (BaseException) escaping the loop.
            with pytest.raises(Exception) as exc_info:
                runner._start_uvicorn()
            assert not isinstance(exc_info.value, SystemExit)

    def test_on_restart_handles_exception(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        runner._icon = MagicMock()
        with (
            patch.object(runner, "_wait_for_port_free", return_value=True),
            patch.object(runner, "_start_uvicorn", side_effect=OSError("port in use")),
            patch.object(runner, "_shutdown_uvicorn"),
        ):
            runner._on_restart()  # should not raise
        # Icon should show stopped state on failure
        assert runner._icon.icon is not None
        assert runner._service_state == "degraded"
        assert runner._lifecycle_failure == "restart failed after 3 attempts"

    def test_on_restart_aborts_when_port_does_not_free(self):
        """If the old uvicorn won't release the port, abort cleanly
        rather than hanging or repeatedly failing to bind."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        runner._icon = MagicMock()
        with (
            patch.object(runner, "_shutdown_uvicorn"),
            patch.object(runner, "_wait_for_port_free", return_value=False),
            patch.object(runner, "_start_uvicorn") as start,
        ):
            runner._on_restart()
        start.assert_not_called()  # never even tried to bind
        assert runner._icon.icon is not None  # stopped icon shown
        assert runner._service_state == "degraded"
        assert runner._lifecycle_failure == "port did not become available"

    def test_on_restart_retries_on_transient_failure(self):
        """A first-attempt OSError should not give up — try again up to
        3 times before declaring failure."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        runner._icon = MagicMock()
        # First two attempts raise; third attempt succeeds.
        side_effects = [OSError("address in use"), OSError("address in use"),
                        MagicMock()]
        with (
            patch.object(runner, "_shutdown_uvicorn"),
            patch.object(runner, "_wait_for_port_free", return_value=True),
            patch.object(runner, "_start_uvicorn", side_effect=side_effects) as start,
            patch.object(runner, "_wait_for_ready", return_value=True),
        ):
            runner._on_restart()
        assert start.call_count == 3

    def test_shutdown_uvicorn_force_exits_on_graceful_timeout(self):
        """When `should_exit` doesn't release the thread within the
        graceful window, set `force_exit` to drop active connections.
        This is the core fix for "WebSockets keep restart hanging"."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        # Mock server + thread that simulates graceful shutdown hanging
        # (thread stays alive after first join), then exits after force.
        mock_server = MagicMock()
        mock_thread = MagicMock()
        # Simulate: first join returns with thread still alive,
        # second join (after force_exit) returns with thread dead.
        mock_thread.is_alive.side_effect = [True, False]
        runner._uvicorn_server = mock_server
        runner._uvicorn_thread = mock_thread

        runner._shutdown_uvicorn()

        # Verified the canonical shutdown sequence:
        assert mock_server.should_exit is True  # graceful first
        assert mock_server.force_exit is True   # then force
        assert mock_thread.join.call_count == 2  # tried both


class TestApplyIcon:
    """_apply_icon must render the badge whenever an update is available."""

    def test_passes_update_available_true_when_outdated(self):
        _skip_if_no_tray()
        import jacked.service.tray as tray_mod
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "9.9.9", "outdated": True}
        runner._icon = MagicMock()
        with patch.object(tray_mod, "create_icon_image") as mock_create:
            runner._apply_icon("running")
        mock_create.assert_called_once_with("running", update_available=True)
        assert runner._icon_state == "running"

    def test_passes_update_available_false_when_current(self):
        _skip_if_no_tray()
        import jacked.service.tray as tray_mod
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.0.1", "outdated": False}
        runner._icon = MagicMock()
        with patch.object(tray_mod, "create_icon_image") as mock_create:
            runner._apply_icon("running")
        mock_create.assert_called_once_with("running", update_available=False)


class TestStartedTimestamp:
    """Verify the menu shows when the service actually became ready."""

    def test_started_text_default_em_dash(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        assert runner._started_at is None
        assert runner._started_text() == "Started: —"

    def test_started_text_seconds_resolution(self):
        _skip_if_no_tray()
        import time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._started_at = time.time() - 5  # 5s ago
        text = runner._started_text()
        assert text.startswith("Started ")
        assert "5s ago" in text

    def test_started_text_minutes_and_seconds(self):
        _skip_if_no_tray()
        import time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._started_at = time.time() - 125  # 2m 5s ago
        text = runner._started_text()
        assert "2m" in text and "5s ago" in text

    def test_restart_updates_started_at(self):
        """Headline behavior: clicking Restart shifts the timestamp so
        the user can verify the click took effect."""
        _skip_if_no_tray()
        import time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        runner._icon = MagicMock()
        old_started = time.time() - 3600  # an hour ago
        runner._started_at = old_started
        with (
            patch.object(runner, "_shutdown_uvicorn"),
            patch.object(runner, "_wait_for_port_free", return_value=True),
            patch.object(runner, "_start_uvicorn", return_value=MagicMock()),
            patch.object(runner, "_wait_for_ready", return_value=True),
        ):
            runner._on_restart()
        assert runner._started_at is not None
        assert runner._started_at > old_started  # actually moved


class TestCheckDeps:
    """Tests for dependency checking."""

    def test_check_tray_deps_raises_when_missing(self):
        from jacked.service import tray
        with patch.object(tray, "_TRAY_AVAILABLE", False):
            with pytest.raises(SystemExit, match="tray"):
                tray.check_tray_deps()

    def test_check_tray_deps_passes_when_available(self):
        from jacked.service import tray
        with patch.object(tray, "_TRAY_AVAILABLE", True):
            tray.check_tray_deps()  # should not raise


class TestVersionMenu:
    def test_version_text_when_current(self):
        """Not outdated: show the running __version__ (not cached latest)."""
        _skip_if_no_tray()
        from jacked import __version__
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": __version__, "outdated": False}
        assert runner._version_menu_text() == f"v{__version__}"

    def test_manual_check_sets_failed_flag_when_unreachable(self, monkeypatch):
        """The mac menubar reads _last_check_failed to show 'couldn't reach PyPI'
        (its rumps path has no pystray notification)."""
        _skip_if_no_tray()
        import time

        import jacked.service.tray as tray_mod
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        monkeypatch.setattr(tray_mod, "check_version_cached", lambda *a, **k: None)
        runner._on_check_for_updates()
        for _ in range(60):
            if not runner._version_check_in_progress:
                break
            time.sleep(0.05)
        assert runner._version_check_in_progress is False
        assert runner._last_check_failed is True
        assert runner._last_check_at is not None

    def test_manual_check_clears_failed_flag_on_success(self, monkeypatch):
        _skip_if_no_tray()
        import time

        import jacked.service.tray as tray_mod
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        monkeypatch.setattr(
            tray_mod, "check_version_cached",
            lambda *a, **k: {"latest": "9.9.9", "outdated": True},
        )
        runner._on_check_for_updates()
        for _ in range(60):
            if not runner._version_check_in_progress:
                break
            time.sleep(0.05)
        assert runner._last_check_failed is False
        assert runner._version_info == {"latest": "9.9.9", "outdated": True}

    def test_version_text_when_outdated(self):
        _skip_if_no_tray()
        from jacked import __version__
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        text = runner._version_menu_text()
        # Both current and target versions must be visible.
        assert __version__ in text
        assert "0.42.0" in text
        assert "update" in text.lower()

    def test_version_text_when_ahead_of_pypi(self):
        """Running ahead of PyPI cache: show OUR version, not stale cached latest."""
        _skip_if_no_tray()
        from jacked import __version__
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        # Simulate scenario: we're on 0.41.6, PyPI cache still holds 0.41.3
        runner._version_info = {"latest": "0.41.3", "outdated": False, "ahead": True}
        text = runner._version_menu_text()
        assert text == f"v{__version__}"
        assert "0.41.3" not in text  # must NOT show cached latest

    def test_version_text_when_check_not_yet_run(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        from jacked import __version__
        runner = ServiceRunner()
        runner._version_info = None
        assert __version__ in runner._version_menu_text()

    def test_update_enabled_only_when_outdated(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        assert runner._version_is_clickable() is True
        runner._version_info = {"latest": "0.41.0", "outdated": False}
        assert runner._version_is_clickable() is False
        runner._version_info = None
        assert runner._version_is_clickable() is False


class TestOnUpdateClick:
    def test_spawns_updater_then_stops(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")):
            with patch("jacked.service.updater.spawn_updater_from_tray") as mock_spawn:
                with patch.object(runner, "_on_stop") as mock_stop:
                    runner._on_update_click()
        mock_spawn.assert_called_once()
        mock_stop.assert_called_once()

    def test_no_op_when_not_outdated(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.41.0", "outdated": False}
        with patch("jacked.service.updater.spawn_updater_from_tray") as mock_spawn:
            runner._on_update_click()
        mock_spawn.assert_not_called()

    def test_click_releases_lock_on_spawn_failure(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        with patch("jacked.service.updater.spawn_updater_from_tray", side_effect=RuntimeError("boom")):
            with patch.object(runner, "_on_stop"):
                runner._on_update_click()
        # Lock must be released so subsequent clicks work
        assert runner._lifecycle_lock.acquire(blocking=False)
        runner._lifecycle_lock.release()

    def test_double_click_only_spawns_once(self):
        """Rapid double-click spawns only one updater."""
        _skip_if_no_tray()
        import threading as _threading
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        spawn_calls = []
        def slow_spawn(*a, **kw):
            spawn_calls.append(1)
            _time.sleep(0.1)

        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")):
            with patch("jacked.service.updater.spawn_updater_from_tray", side_effect=slow_spawn):
                with patch.object(runner, "_on_stop"):
                    t1 = _threading.Thread(target=runner._on_update_click)
                    t2 = _threading.Thread(target=runner._on_update_click)
                    t1.start()
                    t2.start()
                    t1.join()
                    t2.join()

        assert len(spawn_calls) == 1

    def test_spawns_before_stops(self):
        """Updater is spawned BEFORE _on_stop is called (ordering assertion)."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        parent = MagicMock()
        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")):
            with patch(
                "jacked.service.updater.spawn_updater_from_tray",
                side_effect=lambda *a, **kw: parent.spawn(*a, **kw),
            ):
                with patch.object(runner, "_on_stop", side_effect=lambda: parent.stop()):
                    runner._on_update_click()

        names = [c[0] for c in parent.method_calls]
        assert names[0] == "spawn"
        assert "stop" in names
        assert names.index("spawn") < names.index("stop")


class TestCheckForUpdatesMenu:
    def test_menu_has_check_for_updates_item(self):
        _skip_if_no_tray()
        from jacked.service.tray import build_menu
        def noop():
            return None
        called = []
        menu = build_menu(
            port=8321,
            version="0.41.8",
            autostart_check=lambda: False,
            on_open_dashboard=noop,
            on_restart=noop,
            on_stop=noop,
            on_toggle_autostart=noop,
            version_text_fn=lambda: "v0.41.8",
            version_click_fn=noop,
            version_enabled_fn=lambda: False,
            on_check_for_updates=lambda: called.append(1),
        )
        items = list(menu)
        texts = [str(item) for item in items]
        assert any("Check for updates" in t for t in texts)

    def test_menu_omits_check_for_updates_when_not_provided(self):
        _skip_if_no_tray()
        from jacked.service.tray import build_menu
        def noop():
            return None
        menu = build_menu(
            port=8321,
            version="0.41.8",
            autostart_check=lambda: False,
            on_open_dashboard=noop,
            on_restart=noop,
            on_stop=noop,
            on_toggle_autostart=noop,
        )
        items = list(menu)
        texts = [str(item) for item in items]
        assert not any("Check for updates" in t for t in texts)


class TestOnCheckForUpdates:
    @patch("jacked.service.tray.check_version_cached")
    def test_forces_fresh_pypi_check(self, mock_check):
        _skip_if_no_tray()
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._icon = MagicMock()
        mock_check.return_value = {"latest": "9.9.9", "outdated": True}

        runner._on_check_for_updates()
        # Wait for the one-shot thread
        deadline = _time.monotonic() + 2.0
        while _time.monotonic() < deadline:
            if mock_check.called:
                break
            _time.sleep(0.05)

        assert mock_check.called
        # force=True must have been passed to bypass cache
        assert mock_check.call_args[1].get("force") is True or \
               (len(mock_check.call_args[0]) > 1 and mock_check.call_args[0][1] is True)


class TestLastCheckedMenu:
    def test_never_checked(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = None
        assert runner._last_check_menu_text() == "Last checked: never"

    def test_just_now(self):
        _skip_if_no_tray()
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 5
        assert "just now" in runner._last_check_menu_text()

    def test_minutes_ago(self):
        _skip_if_no_tray()
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 125  # 2m 5s
        assert runner._last_check_menu_text() == "Last checked: 2m ago"

    def test_hours_ago(self):
        _skip_if_no_tray()
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 7200  # 2h
        assert runner._last_check_menu_text() == "Last checked: 2h ago"

    def test_days_ago(self):
        _skip_if_no_tray()
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - (3 * 86400)
        assert runner._last_check_menu_text() == "Last checked: 3d ago"

    def test_checking_now_overrides_timestamp(self):
        _skip_if_no_tray()
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 30
        runner._version_check_in_progress = True
        text = runner._last_check_menu_text()
        assert "Checking" in text

    def test_menu_includes_last_checked_when_fn_provided(self):
        _skip_if_no_tray()
        from jacked.service.tray import build_menu
        def noop():
            return None
        menu = build_menu(
            port=8321,
            version="0.41.13",
            autostart_check=lambda: False,
            on_open_dashboard=noop,
            on_restart=noop,
            on_stop=noop,
            on_toggle_autostart=noop,
            last_check_text_fn=lambda: "Last checked: 1m ago",
        )
        texts = [str(item) for item in menu]
        assert any("Last checked" in t for t in texts)

    def test_check_for_updates_disabled_while_in_progress(self):
        _skip_if_no_tray()
        from jacked.service.tray import build_menu
        def noop():
            return None
        in_progress = {"v": True}
        menu = build_menu(
            port=8321,
            version="0.41.13",
            autostart_check=lambda: False,
            on_open_dashboard=noop,
            on_restart=noop,
            on_stop=noop,
            on_toggle_autostart=noop,
            on_check_for_updates=noop,
            check_in_progress_fn=lambda: in_progress["v"],
        )
        # Find the "Check for updates..." item and probe its enabled callable.
        for item in menu:
            if "Check for updates" in str(item):
                enabled_cb = getattr(item, "_enabled", None) or getattr(item, "enabled", None)
                # Menu item's `enabled` is a property that calls the stored callable
                # with the menu as argument. If it's a plain bool/callable, invoke
                # it; if it's already resolved, compare directly.
                if callable(enabled_cb):
                    assert enabled_cb(None) is False
                break


class TestWindowsConsoleHandler:
    """`jacked service start` on Windows must respond to Ctrl+C / Ctrl+Break."""

    def test_handler_no_op_on_posix(self):
        """On POSIX the method short-circuits and must not raise."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        # No patching needed — on macOS/Linux it should just return.
        runner._install_windows_console_handler()

    def test_handler_installs_on_windows(self):
        """When sys.platform is win32, it installs a SetConsoleCtrlHandler."""
        _skip_if_no_tray()
        import sys as _sys
        from jacked.service.tray import ServiceRunner
        # Skip if ctypes.windll isn't available (non-Windows systems don't have it
        # and there's no meaningful way to fake the Win32 API surface).
        try:
            import ctypes
            ctypes.windll  # noqa: B018
        except (AttributeError, ImportError):
            pytest.skip("ctypes.windll unavailable on this platform")

        runner = ServiceRunner()
        try:
            with patch.object(_sys, "platform", "win32"):
                runner._install_windows_console_handler()
            # Callback must be retained while Windows has it registered.
            assert runner._win_ctrl_handler is not None
        finally:
            runner._uninstall_windows_console_handler()
        assert runner._win_ctrl_handler is None

    @pytest.mark.parametrize("failure", [False, OSError("denied")])
    def test_failed_uninstall_roots_callback_for_process_lifetime(
        self, monkeypatch, failure
    ):
        _skip_if_no_tray()
        import ctypes
        import gc
        import sys as _sys
        import weakref
        from types import SimpleNamespace
        from jacked.service import tray as tray_mod

        def callback():
            return None

        callback_ref = weakref.ref(callback)
        runner = tray_mod.ServiceRunner()
        runner._win_ctrl_handler = callback
        kernel32 = MagicMock()
        if isinstance(failure, Exception):
            kernel32.SetConsoleCtrlHandler.side_effect = failure
        else:
            kernel32.SetConsoleCtrlHandler.return_value = failure
        monkeypatch.setattr(
            ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
        )
        tray_mod._FAILED_WIN_CTRL_HANDLERS.clear()
        try:
            with patch.object(_sys, "platform", "win32"):
                runner._uninstall_windows_console_handler()

            kernel32.SetConsoleCtrlHandler.assert_called_once_with(callback, False)
            del callback
            del runner
            gc.collect()
            assert callback_ref() is tray_mod._FAILED_WIN_CTRL_HANDLERS[0]
        finally:
            tray_mod._FAILED_WIN_CTRL_HANDLERS.clear()


class TestVersionCheckThread:
    def test_exits_on_stop_event(self):
        _skip_if_no_tray()
        import threading as _threading
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._icon = None
        with patch("jacked.service.tray.check_version_cached", return_value=None):
            t = _threading.Thread(target=runner._check_version, daemon=True)
            t.start()
            runner._stop_event.set()
            t.join(timeout=2)
        assert not t.is_alive()


class TestOnUpdateClickRefusal:
    @patch("jacked.service.update_status.clear_status")
    @patch(
        "jacked.install_method.can_auto_upgrade",
        return_value=(False, "editable — run `git pull && uv sync`"),
    )
    @patch("jacked.service.updater.spawn_updater_from_tray")
    def test_refuses_editable_clears_status_no_spawn_no_stop(
        self, mock_spawn, mock_gate, mock_clear,
    ):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        with patch.object(runner, "_on_stop") as mock_stop:
            runner._on_update_click()
        mock_spawn.assert_not_called()
        mock_stop.assert_not_called()
        mock_clear.assert_called_once()
        runner._icon.notify.assert_called_once()


class TestOnUpdateClickBreadcrumbs:
    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.spawn_updater_from_tray")
    @patch("jacked.service.update_status.init_status")
    def test_init_status_called_before_spawn(
        self, mock_init, mock_spawn, mock_gate, monkeypatch, tmp_path,
    ):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        monkeypatch.setattr("jacked.service.CLAUDE_DIR", tmp_path)
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        call_order = []
        mock_init.side_effect = lambda *a, **kw: call_order.append("init")
        mock_spawn.side_effect = lambda *a, **kw: call_order.append("spawn")

        with patch("webbrowser.open"):
            with patch.object(runner, "_on_stop"):
                runner._on_update_click()

        assert "init" in call_order
        assert "spawn" in call_order
        assert call_order.index("init") < call_order.index("spawn")

    @patch("jacked.install_method.can_auto_upgrade", return_value=(True, ""))
    @patch("jacked.service.updater.spawn_updater_from_tray")
    def test_breadcrumb_appended_to_update_log(
        self, mock_spawn, mock_gate, tmp_path, monkeypatch,
    ):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        from jacked.service import updater as updater_mod
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        monkeypatch.setattr(updater_mod, "UPDATE_LOG", tmp_path / "update.log")
        monkeypatch.setattr("jacked.service.CLAUDE_DIR", tmp_path)

        with patch("webbrowser.open"):
            with patch.object(runner, "_on_stop"):
                runner._on_update_click()

        log = (tmp_path / "update.log").read_text()
        assert "tray: update clicked" in log
        assert "PID" in log

    def test_bootstrap_targets_loopback_regardless_of_host(self, monkeypatch, tmp_path):
        """host may be 0.0.0.0 (bind-all), unroutable by clients on Linux. The
        bootstrap the tray writes must always point the browser at 127.0.0.1."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        monkeypatch.setattr("jacked.service.CLAUDE_DIR", tmp_path)
        runner = ServiceRunner(host="0.0.0.0", port=8321)
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")):
            with patch("webbrowser.open") as mock_wb:
                with patch("jacked.service.updater.spawn_updater_from_tray"):
                    with patch.object(runner, "_on_stop"):
                        runner._on_update_click()

        content = (tmp_path / "jacked-update-progress.html").read_text(encoding="utf-8")
        assert "127.0.0.1" in content
        assert "8321" in content
        assert "0.0.0.0" not in content
        assert mock_wb.call_args[0][0].startswith("file:")

    def test_writes_bootstrap_and_opens_file_uri(self, monkeypatch, tmp_path):
        """Happy path: the substituted bootstrap lands in ~/.claude and the
        browser is pointed at it via file:// — never the racing port."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        monkeypatch.setattr("jacked.service.CLAUDE_DIR", tmp_path)
        runner = ServiceRunner(port=8321)
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")):
            with patch("webbrowser.open") as mock_wb:
                with patch("jacked.service.updater.spawn_updater_from_tray"):
                    with patch.object(runner, "_on_stop"):
                        runner._on_update_click()

        progress = tmp_path / "jacked-update-progress.html"
        assert progress.exists()
        content = progress.read_text(encoding="utf-8")
        assert "__JACKED_PORT__" not in content, "port placeholder not substituted"
        assert "const PORT = 8321;" in content
        opened = mock_wb.call_args[0][0]
        assert opened.startswith("file:")
        assert "jacked-update-progress.html" in opened

    def test_falls_back_to_http_url_when_template_unreadable(self, monkeypatch, tmp_path):
        """Template missing/unreadable -> open the port directly (old behavior,
        minus the pre-warm) rather than leaving the user with nothing."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        empty_web = tmp_path / "web"
        empty_web.mkdir()
        monkeypatch.setattr("jacked.service.CLAUDE_DIR", tmp_path)
        monkeypatch.setattr("jacked.api.main.WEB_DIR", empty_web)
        runner = ServiceRunner(port=8321)
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()

        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")):
            with patch("webbrowser.open") as mock_wb:
                with patch("jacked.service.updater.spawn_updater_from_tray"):
                    with patch.object(runner, "_on_stop"):
                        runner._on_update_click()

        assert not (tmp_path / "jacked-update-progress.html").exists()
        assert mock_wb.call_args[0][0] == "http://127.0.0.1:8321/update.html"


class TestRestartHandlerRegistration:
    """ServiceRunner.run() registers its in-process restart handler for the
    whole run lifetime and unregisters it on exit — the seam the settings API's
    POST /remote-access/restart uses to restart in-process (same PID) instead
    of os.execv. The registered handler is _on_settings_restart, which clears
    any launch-time --host pin before restarting so the DB actually wins."""

    def test_run_registers_during_and_unregisters_after(self, monkeypatch):
        _skip_if_no_tray()
        from jacked.service import restart as restart_mod
        from jacked.service.tray import ServiceRunner

        restart_mod.set_restart_handler(None)
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        uninstall_console_handler = MagicMock()
        monkeypatch.setattr(
            runner,
            "_uninstall_windows_console_handler",
            uninstall_console_handler,
        )

        captured = {}

        def _fake_icon_run(setup=None):
            # We're now inside run(): the handler must already be registered.
            captured["during"] = restart_mod.get_restart_handler()

        icon_instance = MagicMock()
        icon_instance.run.side_effect = _fake_icon_run

        # Force the pystray (non-mac) branch and stub out every side-effecting
        # boundary so run() executes without a real server, tray, or signals.
        monkeypatch.setattr(
            "jacked.service.tray.select_menubar_backend", lambda *a, **k: "pystray"
        )
        monkeypatch.setattr(
            "jacked.service.tray.is_port_available", lambda *a, **k: True
        )
        monkeypatch.setattr("jacked.service.tray.write_pid", lambda *a, **k: None)
        monkeypatch.setattr(
            "jacked.service.platform.detect_autostart", lambda: False
        )
        monkeypatch.setattr(
            runner, "_start_uvicorn", lambda cold_start=False: MagicMock()
        )
        monkeypatch.setattr(runner, "_wait_for_ready", lambda timeout=15: True)
        ownership = MagicMock()
        candidate = MagicMock(generation="test-generation")
        monkeypatch.setenv("JACKED_SERVICE_GENERATION", candidate.generation)
        monkeypatch.setattr(
            "jacked.service.lifecycle.provision_service_contract",
            lambda **_kwargs: (candidate, {}),
        )
        monkeypatch.setattr(
            "jacked.service.lifecycle.claim_service_ownership",
            lambda *_args, **_kwargs: ownership,
        )

        import jacked.service.tray as tray_mod
        monkeypatch.setattr(tray_mod.signal, "signal", lambda *a, **k: None)

        fake_pystray = MagicMock()
        fake_pystray.Icon.return_value = icon_instance
        monkeypatch.setattr(tray_mod, "pystray", fake_pystray, raising=False)

        runner.run()

        # Registered before the run loop, and it is the SETTINGS variant that
        # clears the launch-time --host pin — NOT the bare _on_restart the
        # tray menu uses (an operator's launch pin is deliberate there).
        assert captured.get("during") == runner._on_settings_restart
        assert captured.get("during") != runner._on_restart
        # Unregistered on exit (run()'s finally).
        assert restart_mod.get_restart_handler() is None
        uninstall_console_handler.assert_called_once_with()
        ownership.close.assert_called_once()


class TestOnSettingsRestart:
    """_on_settings_restart is the deal-breaker fix: a service still running
    under a STALE launchd in-memory definition (or an old detached argv) was
    launched with --host X, and that pin (cli_host) would beat the DB inside
    resolve_bind on every in-process restart, making the GUI toggle inert."""

    def test_clears_cli_host_before_restarting(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="0.0.0.0", port=8321)
        assert runner.cli_host == "0.0.0.0"

        seen = []
        runner._on_restart = lambda: seen.append(runner.cli_host)

        runner._on_settings_restart()

        # Pin cleared BEFORE _on_restart ran, so the restarted bind resolves
        # from the settings DB.
        assert runner.cli_host is None
        assert seen == [None]

    def test_menu_restart_keeps_the_launch_pin(self, monkeypatch):
        """The tray MENU's Restart calls _on_restart directly and must NOT
        clear the operator's one-shot --host pin."""
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="192.168.1.5", port=8321)
        # Stub the heavy lifecycle so _on_restart returns fast without a server.
        monkeypatch.setattr(runner, "_shutdown_uvicorn", lambda: None)
        monkeypatch.setattr(runner, "_wait_for_port_free", lambda timeout=10: True)
        monkeypatch.setattr(
            runner, "_start_uvicorn", lambda: MagicMock(name="thread")
        )
        monkeypatch.setattr(runner, "_wait_for_ready", lambda timeout=15: True)

        runner._on_restart()

        assert runner.cli_host == "192.168.1.5"


class TestReadinessBudget:
    """Cold-start readiness: long budget, fail fast on a dead server, retryable exit."""

    def test_timing_constants(self):
        from jacked import service as service_pkg

        assert service_pkg.COLD_START_READY_TIMEOUT == 90.0
        assert service_pkg.REPLACEMENT_READY_TIMEOUT == 105.0
        assert service_pkg.EX_TEMPFAIL == 75

    def test_wait_for_ready_returns_true_once_port_answers_and_records_elapsed(self):
        from jacked.service.tray import ServiceRunner

        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            runner = ServiceRunner(port=port)
            runner._uvicorn_thread = SimpleNamespace(is_alive=lambda: True)
            assert runner._wait_for_ready(timeout=5) is True
            assert runner._ready_elapsed is not None and runner._ready_elapsed < 5
        finally:
            listener.close()

    def test_wait_for_ready_fails_fast_when_server_thread_died(self):
        from jacked.service.tray import ServiceRunner

        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()  # nothing listens here now
        runner = ServiceRunner(port=port)
        runner._uvicorn_thread = SimpleNamespace(is_alive=lambda: False)
        started = time.monotonic()
        assert runner._wait_for_ready(timeout=30) is False
        assert time.monotonic() - started < 3

    @pytest.mark.parametrize("kind", ["LAUNCHD", "SYSTEMD_USER", "TASK_SCHEDULER"])
    def test_unready_start_under_native_supervisor_exits_tempfail(self, kind, tmp_path):
        from jacked.service.spec import SupervisorKind
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._service_spec = SimpleNamespace(supervisor=SupervisorKind[kind])
        runner._uvicorn_thread = None
        with (
            patch.object(runner, "_request_stop"),
            patch.object(runner, "_start_failure_path", return_value=tmp_path / "f.json"),
            pytest.raises(SystemExit) as exc_info,
        ):
            runner._abort_unready_start()
        assert exc_info.value.code == 75
        assert runner._service_state == "degraded"

    def test_unready_start_gives_up_cleanly_after_limit(self, tmp_path, caplog):
        from jacked.service import START_FAILURE_LIMIT
        from jacked.service.spec import SupervisorKind
        from jacked.service.start_failures import record_start_failure
        from jacked.service.tray import ServiceRunner

        path = tmp_path / "f.json"
        for _ in range(START_FAILURE_LIMIT - 1):
            record_start_failure(path, time.time())
        runner = ServiceRunner()
        runner._service_spec = SimpleNamespace(supervisor=SupervisorKind.LAUNCHD)
        runner._uvicorn_thread = None
        with (
            patch.object(runner, "_request_stop"),
            patch.object(runner, "_start_failure_path", return_value=path),
            pytest.raises(SystemExit) as exc_info,
        ):
            runner._abort_unready_start()
        assert exc_info.value.code == 0
        assert "giving up" in caplog.text

    def test_unready_start_under_manual_supervisor_keeps_message(self):
        from jacked.service.spec import SupervisorKind
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._service_spec = SimpleNamespace(supervisor=SupervisorKind.MANUAL)
        runner._uvicorn_thread = None
        with patch.object(runner, "_request_stop"), pytest.raises(SystemExit) as exc_info:
            runner._abort_unready_start()
        assert "did not become ready" in str(exc_info.value.code)

    def test_both_ready_sites_record_success_and_elapsed(self):
        """_note_ready is the only thing that clears the breaker; both platform
        ready sites (macOS menu bar and pystray) must call it and log timing."""
        import inspect

        from jacked.service import tray as tray_module

        source = inspect.getsource(tray_module)
        assert source.count("self._note_ready(self._ready_elapsed or 0.0)") == 2
        assert source.count("ready_in=%.1fs") == 2

    def test_successful_start_clears_failure_memory(self, tmp_path):
        from jacked.service.start_failures import record_start_failure
        from jacked.service.tray import ServiceRunner

        path = tmp_path / "f.json"
        record_start_failure(path, time.time())
        runner = ServiceRunner()
        with patch.object(runner, "_start_failure_path", return_value=path):
            runner._note_ready(0.5)
        assert not path.exists()
