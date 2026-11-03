"""Tests for jacked.service.tray module."""

import logging
import signal
import socket
import time
from types import SimpleNamespace

import pytest
from unittest.mock import call, patch, MagicMock


@pytest.fixture(autouse=True)
def _tray(tray_module):
    """Run every test in this module on the real pystray, or on the fake.

    pystray cannot load on a Linux runner with no display. Then the
    ``tray_module`` fixture (conftest.py) binds ``tray.pystray`` to the fake,
    so these tests run there too instead of skipping. Pillow is a core
    dependency, so the icon tests always have it.
    """
    return tray_module


class TestCreateIcon:
    """Tests for create_icon_image()."""

    def test_running_icon_is_64x64(self):
        from jacked.service.tray import create_icon_image
        img = create_icon_image("running")
        assert img.size == (64, 64)

    def test_stopped_icon_is_64x64(self):
        from jacked.service.tray import create_icon_image
        img = create_icon_image("stopped")
        assert img.size == (64, 64)

    def test_starting_icon_is_64x64(self):
        from jacked.service.tray import create_icon_image
        img = create_icon_image("starting")
        assert img.size == (64, 64)

    def test_different_states_produce_different_images(self):
        from jacked.service.tray import create_icon_image
        running = create_icon_image("running")
        stopped = create_icon_image("stopped")
        assert running.tobytes() != stopped.tobytes()

    def test_unknown_state_defaults_to_stopped(self):
        from jacked.service.tray import create_icon_image
        unknown = create_icon_image("bogus")
        stopped = create_icon_image("stopped")
        assert unknown.tobytes() == stopped.tobytes()

    def test_update_available_draws_blue_badge_dot(self):
        """update_available=True paints the blue badge dot (parity with the
        macOS menu-bar icon). (51, 13) is the badge dot center."""
        from jacked.service.tray import create_icon_image
        badged = create_icon_image("running", update_available=True)
        assert badged.getpixel((51, 13)) == (59, 130, 246, 255)

    def test_no_badge_when_update_unavailable(self):
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
        from jacked.service.tray import create_icon_image
        cov = self._coverage(create_icon_image("running"), 64)
        assert cov > 0.15, f"mark covers only {cov:.0%} of the 64px canvas"

    def test_running_icon_survives_16px_tray_downscale(self):
        """The Windows tray renders at 16px — the actual bug surface. The
        old 10px-bitmap 'J' covered ~2% of a 16px cell; the shipped arm
        silhouette measures ~42%."""
        from jacked.service.tray import create_icon_image
        # Calibration: shipped mark ~42% of a 16px cell, the old font-bug
        # speck ~2%. 15% floor separates them with margin for redesigns.
        cov = self._coverage(create_icon_image("running"), 16)
        assert cov > 0.15, f"mark covers only {cov:.0%} of a 16px tray cell"

    def test_all_three_states_render_distinct_tints(self):
        """running/starting/stopped must each get their own fill — pins the
        amber 'starting' tint, not just running-vs-stopped."""
        from jacked.service.tray import create_icon_image
        imgs = {s: create_icon_image(s).tobytes() for s in ("running", "starting", "stopped")}
        assert len(set(imgs.values())) == 3, "two service states share a tint"

    def test_icon_renders_without_any_font(self):
        """The mark renderer must not touch font machinery at all — font
        resolution was the Windows failure class. jacked.service.tray no
        longer defines _load_glyph_font."""
        from jacked.service import tray
        assert not hasattr(tray, "_load_glyph_font")


class TestBuildMenu:
    """Tests for build_menu()."""

    def test_menu_has_expected_items(self):
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

    def test_open_dashboard_is_the_one_default_action(self, tray_module):
        """#232i: Open Dashboard is the default item. On a left click, the
        backends with a click action (Windows, GTK, the Xorg fallback) run
        ``menu(icon)``, and that call runs the default item. No other item
        is a default, so the click cannot do anything else. The test uses
        the real pystray when it loads, else the fake (``tray_module``)."""

        def noop():
            return None

        opened = []
        menu = tray_module.build_menu(
            port=8321,
            version="0.39.0",
            autostart_check=lambda: True,
            on_open_dashboard=lambda: opened.append("dashboard"),
            on_restart=noop,
            on_stop=noop,
            on_toggle_autostart=noop,
            version_text_fn=lambda: "v0.39.0",
            version_click_fn=noop,
            version_enabled_fn=lambda: False,
            on_check_for_updates=noop,
            last_check_text_fn=lambda: "Last checked: never",
            check_in_progress_fn=lambda: False,
            started_at_text_fn=lambda: "Started: -",
            usage_text_fn=lambda: "Claude · 7d 74%",
        )
        for item in menu.items:
            assert item.default is (item.text == "Open Dashboard"), item.text
        menu(MagicMock())  # pystray's Icon.__call__ runs this on a left click
        assert opened == ["dashboard"]


class _SetupIcon:
    """The parts of a pystray icon that ``ServiceRunner._setup`` uses.

    It declares no capability attribute (``HAS_MENU``,
    ``HAS_NOTIFICATION``), like a backend that does not declare them.
    """

    def __init__(self):
        self.visible = False
        self.notices = []
        self.menu_updates = 0

    def notify(self, message, title=None):
        self.notices.append((message, title))

    def update_menu(self):
        self.menu_updates += 1


class _XorgIcon(_SetupIcon):
    """pystray's Xorg backend: no menu (only the left click works) and no
    notifications. Its notify raises, as the real backend's does."""

    HAS_MENU = False
    HAS_NOTIFICATION = False

    def notify(self, message, title=None):
        raise NotImplementedError()  # pystray _base.Icon._notify on Xorg


class _MenuIcon(_SetupIcon):
    HAS_MENU = True  # pystray on Windows, GTK and AppIndicator
    HAS_NOTIFICATION = True


class _RefusingIcon(_MenuIcon):
    """Declares notifications and then refuses one, as GTK can when no
    notification service runs on the session bus."""

    def notify(self, message, title=None):
        raise RuntimeError("no notification service")


_NO_MENU_WARNING = (
    "Tray: this desktop has no tray menu (pystray Xorg backend). Left-click "
    "the icon to open the dashboard. For the other actions, use `jacked "
    "service restart`, `jacked service stop` and `jacked upgrade`; `jacked "
    "service install` / `uninstall` set start on login. The full menu needs "
    "PyGObject; see the README Linux tray notes."
)


def _run_setup(icon, *, started=True):
    """Run ``ServiceRunner._setup`` on *icon* with no threads and no render.

    *started* False models a first start that did not become ready.
    """
    from jacked.service import tray

    runner = tray.ServiceRunner(host="127.0.0.1", port=9999)
    runner._started_at = time.time() if started else None

    class _Thread:
        def __init__(self, target=None, name=None, daemon=None):
            self.target, self.name = target, name

        def start(self):
            return None

    with patch.object(tray.threading, "Thread", _Thread), patch.object(
        runner, "_apply_icon"
    ):
        runner._setup(icon)
    return runner


class TestTrayMenuCapability:
    """#232i: on a pystray backend with no menu (Xorg), only the left click
    works. The tray probes the backend once at setup, logs one WARNING, and
    the native STATUS payload reports the result."""

    @pytest.mark.parametrize(
        "icon_cls, available",
        [(_XorgIcon, False), (_MenuIcon, True), (_SetupIcon, True)],
        ids=["xorg-no-menu", "menu", "no-capability-attribute"],
    )
    def test_setup_probes_the_menu_and_status_reports_it(
        self, icon_cls, available, caplog
    ):
        from jacked.service.ipc import ControlAction

        with caplog.at_level(logging.WARNING):
            runner = _run_setup(icon_cls())

        warnings = [
            (record.name, record.getMessage())
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        # The "jacked" logger writes to ~/.claude/jacked-tray.log.
        expected = [] if available else [("jacked.service.tray", _NO_MENU_WARNING)]
        assert warnings == expected
        status = runner._handle_control_action(ControlAction.STATUS)
        assert status["tray_menu_available"] is available

    def test_a_runner_without_the_pystray_setup_reports_a_menu(self):
        """The macOS menu-bar agent never runs ``_setup``, and STATUS can
        arrive before it runs. Neither case reports a missing menu."""
        from jacked.service.ipc import ControlAction
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=9999)
        status = runner._handle_control_action(ControlAction.STATUS)
        assert status["tray_menu_available"] is True

    def test_the_shared_probe_reads_the_declared_capability(self):
        from jacked.service.usage_tooltip import tray_backend_has_menu

        assert tray_backend_has_menu(_XorgIcon()) is False
        assert tray_backend_has_menu(_MenuIcon()) is True
        assert tray_backend_has_menu(_SetupIcon()) is True
        # A real bool, because the STATUS payload is sent as JSON.
        assert tray_backend_has_menu(MagicMock()) is True


class TestTrayNotifications:
    """#232i, the same class for notifications: pystray's Xorg backend
    cannot show one (HAS_NOTIFICATION is False and its notify raises).
    ``ServiceRunner._notify`` shows a notification where the backend can
    and logs it at WARNING where it cannot, so no message is lost."""

    FAILED_START = ("Jacked Service", "Jacked failed to start")
    RECOVERY = (
        "Jacked Update Failed Earlier",
        "Previous update failed. See ~/.claude/jacked-update-failed.txt for "
        "recovery steps.",
    )

    @staticmethod
    def _write_recovery_file():
        from jacked.service import updater

        # The service conftest points RECOVERY_FILE into the test's tmp dir.
        updater.RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        updater.RECOVERY_FILE.write_text("update failed\n", encoding="utf-8")

    def test_the_shared_probe_reads_the_notification_capability(self):
        from jacked.service.usage_tooltip import tray_backend_can_notify

        assert tray_backend_can_notify(_XorgIcon()) is False
        assert tray_backend_can_notify(_MenuIcon()) is True
        assert tray_backend_can_notify(_SetupIcon()) is True  # not declared
        assert tray_backend_can_notify(MagicMock()) is True  # a real bool

    @pytest.mark.parametrize(
        "recovery", [False, True], ids=["failed-start", "and-recovery-file"]
    )
    def test_an_xorg_tray_logs_each_notification_and_setup_does_not_raise(
        self, caplog, recovery
    ):
        if recovery:
            self._write_recovery_file()
        with caplog.at_level(logging.WARNING):
            _run_setup(_XorgIcon(), started=False)  # must not raise

        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        expected = [self.RECOVERY, self.FAILED_START] if recovery else [self.FAILED_START]
        for title, message in expected:
            assert any(
                "cannot show notifications" in w and title in w and message in w
                for w in warnings
            ), (title, warnings)
        # The capability probe routed each one, so the backend refused none,
        # and a notification that the backend cannot show is not an error.
        assert not any("refused" in w for w in warnings), warnings
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []

    def test_a_notifying_tray_shows_the_failed_start_once(self, caplog):
        icon = _MenuIcon()
        with caplog.at_level(logging.WARNING):
            _run_setup(icon, started=False)

        title, message = self.FAILED_START
        assert icon.notices == [(message, title)]
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_a_refused_recovery_notification_names_the_notification(self, caplog):
        """A backend that declares notifications can still refuse one. The
        existing handler logs it, and the text names the notification."""
        self._write_recovery_file()
        with caplog.at_level(logging.ERROR):
            _run_setup(_RefusingIcon())  # must not raise

        errors = [r.getMessage() for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [
            "Could not check the update recovery file or show its notification"
        ]

    def test_a_refused_failed_start_notification_is_logged_by_class(self, caplog):
        """The failed-start notice is the last step of the setup thread. A
        refusal is logged at WARNING with the error class and the message,
        and the thread does not end on it (no traceback on stderr)."""
        with caplog.at_level(logging.WARNING):
            _run_setup(_RefusingIcon(), started=False)  # must not raise

        title, message = self.FAILED_START
        warnings = [
            r.getMessage() for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "RuntimeError" in w and title in w and message in w for w in warnings
        ), warnings
        assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []


class TestOpenDashboardGuard:
    """#232i: Windows sends down, up, double-click, up for one double-click,
    and pystray runs the default item (Open Dashboard) on each button-up.
    The tray ignores a second activation within 0.5 s of an open, so a
    double-click opens one browser tab."""

    URL = "http://127.0.0.1:9999"

    @staticmethod
    def _runner(monkeypatch, open_seconds=0.0):
        """A runner with a fake clock and a fake browser launch.

        The clock starts at 0.0, so the first activation must open at any
        clock value. A browser launch moves the clock by *open_seconds*.
        """
        from jacked.service import tray

        runner = tray.ServiceRunner(host="127.0.0.1", port=9999)
        clock = [0.0]
        opened = []

        def _open(url):
            opened.append(url)
            clock[0] += open_seconds

        monkeypatch.setattr(tray, "webbrowser", SimpleNamespace(open=_open))
        monkeypatch.setattr(tray, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        return runner, opened, clock

    @pytest.mark.parametrize(
        "gap, tabs", [(0.010, 1), (1.0, 2)], ids=["10-ms-apart", "1-s-apart"]
    )
    def test_a_second_activation_within_half_a_second_opens_no_tab(
        self, monkeypatch, caplog, gap, tabs
    ):
        runner, opened, clock = self._runner(monkeypatch)
        with caplog.at_level(logging.DEBUG, logger="jacked.service.tray"):
            runner._on_open_dashboard()
            clock[0] += gap
            runner._on_open_dashboard()

        assert opened == [self.URL] * tabs
        # An ignored activation is logged at DEBUG, never dropped in silence.
        ignored = [
            r
            for r in caplog.records
            if "ignored a second" in r.getMessage() and r.levelno == logging.DEBUG
        ]
        assert len(ignored) == 2 - tabs

    def test_the_interval_counts_from_the_last_open(self, monkeypatch):
        """An ignored activation does not make the interval longer."""
        runner, opened, clock = self._runner(monkeypatch)
        for at in (0.0, 0.3, 0.6):  # open, ignored, open
            clock[0] = at
            runner._on_open_dashboard()
        assert opened == [self.URL] * 2

    def test_a_slow_browser_start_does_not_let_the_second_click_through(
        self, monkeypatch
    ):
        """The second button-up waits in the message queue until the first
        launch returns. The interval starts when the launch returns, so a
        browser that takes 0.7 s to start still opens one tab."""
        runner, opened, _clock = self._runner(monkeypatch, open_seconds=0.7)
        runner._on_open_dashboard()
        runner._on_open_dashboard()  # queued during the launch, runs at once
        assert opened == [self.URL]


class TestServiceRunner:
    """Tests for ServiceRunner lifecycle."""

    def test_init_stores_config(self):
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=9999)
        assert runner.host == "127.0.0.1"
        assert runner.port == 9999

    def test_uvicorn_server_initialized_in_init(self):
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

    # create=True: a headless box binds no real tray.pystray (backend
    # resolution fails without a display). The autouse _tray fixture binds
    # the fake there, and create=True keeps the patch valid without it.
    @patch("jacked.service.bind.create_sockets", return_value=[])
    @patch("jacked.service.tray.pystray", create=True)
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_thread_is_daemon(self, mock_uvicorn, mock_pystray, mock_socks):
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
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        assert runner._started_at is None
        assert runner._started_text() == "Started: -"

    def test_started_text_seconds_resolution(self):
        import time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._started_at = time.time() - 5  # 5s ago
        text = runner._started_text()
        assert text.startswith("Started ")
        assert "5s ago" in text

    def test_started_text_minutes_and_seconds(self):
        import time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._started_at = time.time() - 125  # 2m 5s ago
        text = runner._started_text()
        assert "2m" in text and "5s ago" in text

    def test_restart_updates_started_at(self):
        """Headline behavior: clicking Restart shifts the timestamp so
        the user can verify the click took effect."""
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


class TestPystrayLoadFailure:
    """pystray 0.19.5 selects its backend at import, and its selector
    catches only ImportError. On Linux, when PyGObject loads but neither
    AppIndicator typelib is installed, the AppIndicator backend raises
    ValueError. Then ``import pystray`` fails and the service does not start.
    The import guard keeps the error, and ``check_tray_deps`` logs it at
    WARNING before the start stops, so the tray log tells the reason. The
    abort text names the error too, because a reinstall of the empty
    ``[tray]`` extra cannot fix a missing typelib or a missing display."""

    TYPELIB_ERROR = "Namespace AyatanaAppIndicator3 not available"
    WARNING = (
        "Tray: pystray could not load (ValueError: Namespace "
        "AyatanaAppIndicator3 not available), so the service does not start. "
        "See the README Linux tray notes."
    )
    BACKEND_ABORT = (
        "The jacked service cannot start: pystray could not load (ValueError: "
        "Namespace AyatanaAppIndicator3 not available).\n"
        'See "Linux tray notes" in the jacked README: '
        "https://github.com/jackneil/claude-jacked#linux-tray-notes"
    )
    INSTALL_ABORT = (
        "Service mode requires the [tray] extra.\n"
        'Install it with: uv tool install "claude-jacked[tray]" --force'
    )

    @classmethod
    def _fake_site(cls, root):
        """A directory with a pystray package whose import raises ValueError,
        as the real one does when the AppIndicator typelibs are missing."""
        package = root / "fake_site" / "pystray"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text(
            f"raise ValueError({cls.TYPELIB_ERROR!r})\n", encoding="utf-8"
        )
        return package.parent

    def test_a_backend_error_is_kept_with_its_class_and_message(
        self, tmp_path, monkeypatch
    ):
        import sys

        from jacked.service import tray

        monkeypatch.syspath_prepend(str(self._fake_site(tmp_path)))
        monkeypatch.delitem(sys.modules, "pystray", raising=False)

        assert tray._import_pystray() == (None, f"ValueError: {self.TYPELIB_ERROR}")

    def test_a_missing_package_gives_no_failure_text(self, monkeypatch):
        import sys

        from jacked.service import tray

        monkeypatch.setitem(sys.modules, "pystray", None)  # import raises ImportError
        assert tray._import_pystray() == (None, None)

    def test_a_loaded_pystray_is_returned(self, monkeypatch):
        import sys
        import types

        from jacked.service import tray

        module = types.ModuleType("pystray")
        monkeypatch.setitem(sys.modules, "pystray", module)
        assert tray._import_pystray() == (module, None)

    def test_a_backend_error_is_logged_and_named_in_the_abort(
        self, monkeypatch, caplog
    ):
        from jacked.service import tray

        monkeypatch.setattr(tray, "_TRAY_AVAILABLE", False)
        monkeypatch.setattr(
            tray, "_PYSTRAY_LOAD_FAILURE", f"ValueError: {self.TYPELIB_ERROR}"
        )
        with caplog.at_level(logging.WARNING), pytest.raises(SystemExit) as stopped:
            tray.check_tray_deps()

        assert stopped.value.code == self.BACKEND_ABORT
        warnings = [
            (record.name, record.getMessage())
            for record in caplog.records
            if record.levelno >= logging.WARNING
        ]
        assert warnings == [("jacked.service.tray", self.WARNING)]

    def test_a_missing_package_keeps_the_install_abort_with_no_warning(
        self, monkeypatch, caplog
    ):
        from jacked.service import tray

        monkeypatch.setattr(tray, "_TRAY_AVAILABLE", False)
        monkeypatch.setattr(tray, "_PYSTRAY_LOAD_FAILURE", None)
        with caplog.at_level(logging.WARNING), pytest.raises(SystemExit) as stopped:
            tray.check_tray_deps()

        assert stopped.value.code == self.INSTALL_ABORT
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_the_tray_log_names_the_error_when_the_start_stops(self, tmp_path):
        """The real artifact, in a fresh interpreter: the tray module imports
        a pystray that raises ValueError, the tray log is installed as
        ``ServiceRunner.run`` installs it first, and the check stops the
        start. A line logged at import time cannot reach the tray log,
        because the module is imported before ``run`` installs the log."""
        import os
        import subprocess
        import sys

        home = tmp_path / "home"
        home.mkdir()
        script = tmp_path / "stops_on_import_error.py"
        script.write_text(
            "from jacked.service import tray\n"
            "assert tray._TRAY_AVAILABLE is False\n"
            "tray.ServiceRunner(host='127.0.0.1', port=9999)._install_tray_file_logger()\n"
            "tray.check_tray_deps()\n",
            encoding="utf-8",
        )
        env = dict(os.environ)
        env.update(HOME=str(home), USERPROFILE=str(home), PYTHONDONTWRITEBYTECODE="1")
        env["PYTHONPATH"] = os.pathsep.join(
            path for path in (str(self._fake_site(tmp_path)), env.get("PYTHONPATH")) if path
        )

        result = subprocess.run(
            [sys.executable, str(script)],
            env=env, capture_output=True, text=True, timeout=120,
        )

        assert result.returncode == 1, result.stderr
        assert self.BACKEND_ABORT in result.stderr, result.stderr
        assert "[tray] extra" not in result.stderr, result.stderr
        log = (home / ".claude" / "jacked-tray.log").read_text(encoding="utf-8")
        assert f"jacked.service.tray WARNING {self.WARNING}" in log, log


class TestSetupFunctionLength:
    """JACKED_GUARDRAILS: 50 lines max per function. ``_setup`` grew to 75
    lines, so its capability report and its failed-start notice are named
    steps. The measure is the one ``test_updater.TestUpdaterFunctionLength``
    uses."""

    FUNCTIONS = {"_setup", "_report_tray_capabilities", "_notify_failed_start"}

    def test_setup_and_its_steps_are_at_most_50_lines(self):
        import ast
        import inspect

        from jacked.service import tray

        tree = ast.parse(inspect.getsource(tray.ServiceRunner))
        lengths = {
            node.name: node.end_lineno - node.lineno + 1
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in self.FUNCTIONS
        }
        assert set(lengths) == self.FUNCTIONS, f"missing: {self.FUNCTIONS - set(lengths)}"
        too_long = {name: n for name, n in lengths.items() if n > 50}
        assert too_long == {}, f"functions over 50 lines: {too_long}"


class TestVersionMenu:
    def test_version_text_when_current(self):
        """Not outdated: show the running __version__ (not cached latest)."""
        from jacked import __version__
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": __version__, "outdated": False}
        assert runner._version_menu_text() == f"v{__version__}"

    def test_manual_check_sets_failed_flag_when_unreachable(self, monkeypatch):
        """The mac menubar reads _last_check_failed to show 'couldn't reach PyPI'
        (its rumps path has no pystray notification)."""
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
        from jacked import __version__
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        # Simulate scenario: we're on 0.41.6, PyPI cache still holds 0.41.3
        runner._version_info = {"latest": "0.41.3", "outdated": False, "ahead": True}
        text = runner._version_menu_text()
        assert text == f"v{__version__}"
        assert "0.41.3" not in text  # must NOT show cached latest

    def test_version_text_when_check_not_yet_run(self):
        from jacked.service.tray import ServiceRunner
        from jacked import __version__
        runner = ServiceRunner()
        runner._version_info = None
        assert __version__ in runner._version_menu_text()

    def test_update_enabled_only_when_outdated(self):
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
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._version_info = {"latest": "0.41.0", "outdated": False}
        with patch("jacked.service.updater.spawn_updater_from_tray") as mock_spawn:
            runner._on_update_click()
        mock_spawn.assert_not_called()

    def test_click_releases_lock_on_spawn_failure(self):
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
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = None
        assert runner._last_check_menu_text() == "Last checked: never"

    def test_just_now(self):
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 5
        assert "just now" in runner._last_check_menu_text()

    def test_minutes_ago(self):
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 125  # 2m 5s
        assert runner._last_check_menu_text() == "Last checked: 2m ago"

    def test_hours_ago(self):
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 7200  # 2h
        assert runner._last_check_menu_text() == "Last checked: 2h ago"

    def test_days_ago(self):
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - (3 * 86400)
        assert runner._last_check_menu_text() == "Last checked: 3d ago"

    def test_checking_now_overrides_timestamp(self):
        import time as _time
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        runner._last_check_at = _time.time() - 30
        runner._version_check_in_progress = True
        text = runner._last_check_menu_text()
        assert "Checking" in text

    def test_menu_includes_last_checked_when_fn_provided(self):
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
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner()
        # No patching needed — on macOS/Linux it should just return.
        runner._install_windows_console_handler()

    def test_handler_installs_on_windows(self):
        """When sys.platform is win32, it installs a SetConsoleCtrlHandler."""
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
        # run() reaches check_tray_deps, which reads _TRAY_AVAILABLE. A
        # headless runner has no real pystray, so the test sets it.
        monkeypatch.setattr(tray_mod, "_TRAY_AVAILABLE", True)

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

    def test_refused_contract_records_the_reason_and_exits_non_zero(
        self, tmp_path, monkeypatch, caplog
    ):
        """The boot refusal that only reached the tray log now names itself."""
        from jacked.service.start_failures import read_start_failures
        from jacked.service.tray import ServiceRunner

        path = tmp_path / "f.json"
        monkeypatch.delenv("JACKED_SERVICE_GENERATION", raising=False)
        runner = ServiceRunner()
        monkeypatch.setattr(runner, "_install_tray_file_logger", lambda: None)
        monkeypatch.setattr(
            "jacked.service.lifecycle.provision_service_contract",
            lambda **_kwargs: (_ for _ in ()).throw(
                ValueError("runtime_path has an untrusted writable directory")
            ),
        )
        with (
            patch.object(runner, "_start_failure_path", return_value=path),
            pytest.raises(SystemExit) as exc_info,
        ):
            runner.run()

        message = str(exc_info.value.code)
        assert "contract refused" in message
        assert "untrusted writable directory" in message
        assert "jacked service status" in message
        assert exc_info.value.code != 0
        assert runner._service_state == "degraded"
        recorded = read_start_failures(path, time.time())
        assert len(recorded) == 1
        assert recorded[0].reason == (
            "ValueError: runtime_path has an untrusted writable directory"
        )
        assert "Service contract refused" in caplog.text

    def test_refused_contract_gives_up_cleanly_for_a_spawned_service(
        self, tmp_path, monkeypatch, caplog
    ):
        """A supervisor relaunches a non-zero exit; the limit stops the loop."""
        from jacked.service import START_FAILURE_LIMIT
        from jacked.service.start_failures import record_start_failure
        from jacked.service.tray import ServiceRunner

        path = tmp_path / "f.json"
        for _ in range(START_FAILURE_LIMIT - 1):
            record_start_failure(path, time.time(), reason="OSError: nope")
        monkeypatch.setenv("JACKED_SERVICE_GENERATION", "spawned-generation")
        runner = ServiceRunner()
        monkeypatch.setattr(runner, "_install_tray_file_logger", lambda: None)
        monkeypatch.setattr(
            "jacked.service.lifecycle.provision_service_contract",
            lambda **_kwargs: (_ for _ in ()).throw(OSError("service root is unreadable")),
        )
        with (
            patch.object(runner, "_start_failure_path", return_value=path),
            pytest.raises(SystemExit) as exc_info,
        ):
            runner.run()

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


class TestReleaseOwnership:
    def test_release_closes_control_then_ownership_once(self):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        parent = MagicMock()
        runner._control_server = parent.control
        runner._ownership = parent.ownership

        runner.release_ownership()
        runner.release_ownership()  # idempotent

        assert parent.mock_calls == [call.control.close(), call.ownership.close()]
        assert runner._control_server is None
        assert runner._ownership is None

    def test_release_still_retires_manifest_when_control_close_raises(self):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._control_server = MagicMock(close=MagicMock(side_effect=OSError("boom")))
        ownership = MagicMock()
        runner._ownership = ownership

        runner.release_ownership()

        ownership.close.assert_called_once()
        assert runner._ownership is None


class TestAbandonedUpdateRecovery:
    """An update interrupted after the old tray exited must be surfaced at boot.

    `update_status.read_status` hides a stale in_progress file so the dashboard
    shows no zombie banner; before this the tray never told the user either.
    """

    def _write_status(self, path, phase, age_seconds):
        import json
        import os
        import time

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"overall": "in_progress", "current_phase": phase}),
            encoding="utf-8",
        )
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def test_stale_in_progress_status_writes_recovery_file_and_fails_status(self):
        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        self._write_status(
            us.UPDATE_STATUS_FILE, "migrating_settings", us.STALE_IN_PROGRESS_SECONDS + 60
        )
        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        text = updater.RECOVERY_FILE.read_text(encoding="utf-8")
        assert "interrupted at phase migrating_settings" in text
        assert "jacked upgrade" in text
        import json

        record = json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        assert record["overall"] == "failed"
        assert "migrating_settings" in (record.get("error") or "")

    def test_fresh_in_progress_status_is_left_alone(self):
        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        self._write_status(us.UPDATE_STATUS_FILE, "preflight", 5)
        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        assert not updater.RECOVERY_FILE.exists()
        import json

        assert json.loads(us.UPDATE_STATUS_FILE.read_text())["overall"] == "in_progress"

    def test_missing_status_is_a_no_op(self):
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()
        assert not updater.RECOVERY_FILE.exists()

    def _write_status_with_pid(self, path, phase, age_seconds, pid):
        import json
        import os
        import time

        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "overall": "in_progress",
                    "current_phase": phase,
                    "updater_pid": pid,
                }
            ),
            encoding="utf-8",
        )
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    def test_a_live_updater_is_never_reported_as_abandoned(self):
        """A slow phase is not an abandoned update.

        A supervisor that relaunches the tray while an update is still running
        used to mark that LIVE update failed and write a recovery file the
        user never needed, because one phase (a package install on a slow
        link) outran STALE_IN_PROGRESS_SECONDS.
        """
        import json
        import os as _os

        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        self._write_status_with_pid(
            us.UPDATE_STATUS_FILE,
            "installing_package",
            us.STALE_IN_PROGRESS_SECONDS + 600,
            _os.getpid(),
        )
        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        assert not updater.RECOVERY_FILE.exists(), (
            "the tray wrote a recovery file for an update that is still running"
        )
        record = json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        assert record["overall"] == "in_progress"

    def test_an_updater_that_died_is_still_reported(self):
        """The pid check must not hide a genuinely abandoned update."""
        import json
        import subprocess
        import sys

        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        dead = subprocess.Popen([sys.executable, "-c", ""])
        dead.wait()
        self._write_status_with_pid(
            us.UPDATE_STATUS_FILE,
            "migrating_settings",
            us.STALE_IN_PROGRESS_SECONDS + 60,
            dead.pid,
        )
        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        assert "interrupted at phase migrating_settings" in (
            updater.RECOVERY_FILE.read_text(encoding="utf-8")
        )
        record = json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        assert record["overall"] == "failed"

    def test_a_held_update_lock_writes_no_recovery_file(self, monkeypatch):
        """A held lock means that an updater runs now: the record stays open,
        and no recovery file tells the user about a failure."""
        import json

        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        self._write_status(
            us.UPDATE_STATUS_FILE, "migrating_settings", us.STALE_IN_PROGRESS_SECONDS + 60
        )
        monkeypatch.setattr(us, "acquire_update_lock", lambda _path: None)

        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        assert not updater.RECOVERY_FILE.exists()
        record = json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        assert record["overall"] == "in_progress"

    def test_a_record_replaced_during_the_check_writes_no_recovery_file(
        self, monkeypatch
    ):
        """Updater B started a new attempt between the boot check's read and
        its close. B's live attempt is not marked failed, and no recovery
        file names a phase that B never had."""
        import json

        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        self._write_status(
            us.UPDATE_STATUS_FILE, "migrating_settings", us.STALE_IN_PROGRESS_SECONDS + 60
        )
        attempt_b = {
            "overall": "in_progress",
            "current_phase": "installing_package",
            "started_at": "2026-11-03T08:00:00+00:00",
        }

        class _Lock:
            def close(self):
                pass

        def b_started_then_granted(_path):
            us.UPDATE_STATUS_FILE.write_text(json.dumps(attempt_b), encoding="utf-8")
            return _Lock()

        monkeypatch.setattr(us, "acquire_update_lock", b_started_then_granted)

        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        assert not updater.RECOVERY_FILE.exists()
        assert json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8")) == attempt_b

    def test_run_checks_for_an_abandoned_update_before_provisioning(self):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        order = []
        with (
            patch.object(runner, "_install_tray_file_logger"),
            patch.object(
                runner, "_record_abandoned_update", side_effect=lambda: order.append("abandoned")
            ),
            patch.object(
                runner, "clear_stale_recovery", side_effect=lambda: order.append("stale")
            ),
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                side_effect=lambda *a, **k: (order.append("provision"), (_ for _ in ()).throw(ValueError("stop here")))[1],
            ),
            patch.object(runner, "_abort_refused_contract", side_effect=SystemExit(0)),
        ):
            with pytest.raises(SystemExit):
                runner.run()
        # The stale-file check follows the interrupted-update check, which
        # can close a record as succeeded that the stale check then reads.
        assert order[:3] == ["abandoned", "stale", "provision"]

    def test_a_stale_check_that_raises_does_not_stop_the_boot(self, caplog):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        order = []
        with (
            caplog.at_level("ERROR", logger="jacked.service.tray"),
            patch.object(runner, "_install_tray_file_logger"),
            patch.object(runner, "_record_abandoned_update"),
            patch.object(runner, "clear_stale_recovery", side_effect=OSError("disk")),
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                side_effect=lambda *a, **k: (order.append("provision"), (_ for _ in ()).throw(ValueError("stop here")))[1],
            ),
            patch.object(runner, "_abort_refused_contract", side_effect=SystemExit(0)),
        ):
            with pytest.raises(SystemExit):
                runner.run()
        assert order == ["provision"]
        assert "Could not check for a stale update recovery file" in caplog.text



class TestTrayProxyHeaderTrustIsPinned:
    """Same pinning as the CLI path: the credential gate trusts
    request.client.host, so the set of peers whose X-Forwarded-For is honored
    must be fixed in code, not inherited from FORWARDED_ALLOW_IPS."""

    @patch("jacked.service.bind.create_sockets", return_value=[])
    @patch("jacked.service.tray.pystray", create=True)
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_pins_proxy_headers(
        self, mock_uvicorn, mock_pystray, mock_socks, monkeypatch
    ):
        from jacked.service.bind import BindPlan
        from jacked.service.tray import ServiceRunner

        monkeypatch.setenv("FORWARDED_ALLOW_IPS", "*")
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        plan = BindPlan(
            mode="loopback", addresses=("127.0.0.1",), port=8321,
            primary_host="127.0.0.1",
        )
        with patch("jacked.service.bind.resolve_bind", return_value=plan):
            thread = runner._start_uvicorn()
        thread.join(timeout=0.1)

        kwargs = mock_uvicorn.Config.call_args.kwargs
        assert kwargs["proxy_headers"] is True
        assert kwargs["forwarded_allow_ips"] == "127.0.0.1"


class TestUpdateClickSurvivesCodeSkew:
    """#178i: a tray still running the old build imported the NEW updater
    module (already on disk), which needs a name the old loaded modules lack;
    "Failed to spawn updater" followed. The spawn now runs in a fresh
    interpreter when the in-process import fails."""

    def _runner(self):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner()
        runner._version_info = {"latest": "0.42.0", "outdated": True}
        runner._icon = MagicMock()
        return runner

    def test_an_import_failure_spawns_from_a_fresh_interpreter_then_stops(self):
        import sys as _sys

        runner = self._runner()
        ok = MagicMock(returncode=0)
        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")), \
             patch("jacked.service.updater.spawn_updater_from_tray",
                   side_effect=ImportError("cannot import name 'is_port_listening'")), \
             patch("subprocess.run", return_value=ok) as run, \
             patch.object(runner, "_on_stop") as stop:
            runner._on_update_click()
        argv = run.call_args.args[0]
        assert argv[:5] == [_sys.executable, "-I", "-m", "jacked", "_tray_spawn_updater"]
        assert "--target" in argv and "0.42.0" in argv
        stop.assert_called_once()

    def test_a_failing_fresh_spawn_keeps_the_tray_running(self):
        runner = self._runner()
        with patch("jacked.install_method.can_auto_upgrade", return_value=(True, "")), \
             patch("jacked.service.updater.spawn_updater_from_tray", side_effect=ImportError("skew")), \
             patch("subprocess.run", return_value=MagicMock(returncode=1)), \
             patch.object(runner, "_on_stop") as stop:
            runner._on_update_click()
        stop.assert_not_called()


def test_a_failing_fresh_spawn_names_its_cause():
    from jacked.service.tray import _spawn_updater_in_fresh_interpreter

    failed = MagicMock(returncode=1, stderr=(
        "Traceback (most recent call last):\n  File \"x\", line 1\n"
        "ModuleNotFoundError: No module named 'pystray'\n"))
    with patch("subprocess.run", return_value=failed), pytest.raises(RuntimeError) as err:
        _spawn_updater_in_fresh_interpreter(42, 8321, None)
    assert str(err.value) == (
        "fresh-interpreter updater spawn exited 1: "
        "ModuleNotFoundError: No module named 'pystray'"
    )

def test_the_hidden_spawn_command_calls_the_updater():
    from click.testing import CliRunner

    from jacked.cli import main

    with patch("jacked.service.updater.spawn_updater_from_tray") as spawn:
        result = CliRunner().invoke(main, ["_tray_spawn_updater", "--parent-pid", "42",
                                           "--port", "8321", "--target", "0.42.0"])
    assert result.exit_code == 0, result.output
    spawn.assert_called_once_with(parent_pid=42, extras="tray", target_version="0.42.0", port=8321)


# ---------------------------------------------------------------------------
# #138i: the stop-signal handlers belong to one run, not to the process
# ---------------------------------------------------------------------------


def _watched_signals():
    """SIGINT and SIGTERM, plus every stop signal this platform routes."""
    from jacked.service import tray

    return {signal.SIGINT, signal.SIGTERM, *tray._stop_signal_numbers()}


def _handlers(signums):
    return {signum: signal.getsignal(signum) for signum in signums}


class TestStopSignalsAreScoped:
    """run() routes the stop signals to its runner only while it runs (#138i).

    run() and the macOS agent replaced the process-wide SIGINT and SIGTERM
    handlers (SIGINT and SIGBREAK on Windows) with lambdas bound to the
    runner and never put the previous handlers back. After run() returned,
    in a test or an in-process restart, Ctrl+C called _request_stop on a dead
    runner instead of raising KeyboardInterrupt.
    """

    @staticmethod
    def _run_with(runner, fake_run):
        """Drive the real run() with its side effects stubbed. ``_run`` is
        replaced by ``fake_run``, so only run() itself can set a handler."""
        from jacked.service.tray import ServiceRunner

        with (
            patch.object(runner, "_install_tray_file_logger"),
            patch.object(runner, "_record_abandoned_update"),
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(MagicMock(), {}),
            ),
            patch(
                "jacked.service.lifecycle.claim_service_ownership",
                return_value=MagicMock(),
            ),
            patch.object(runner, "_guard_legacy_pid_or_exit"),
            patch.object(ServiceRunner, "_run", side_effect=fake_run),
        ):
            return runner.run()

    def test_run_restores_the_previous_handlers_when_it_returns(self):
        from jacked.service import tray
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        watched = _watched_signals()
        before = _handlers(watched)
        during = {}

        def fake_run():
            during.update(_handlers(tray._stop_signal_numbers()))
            # The routed handler is the runner's stop: Ctrl+C asks it to stop.
            signal.getsignal(signal.SIGINT)(signal.SIGINT, None)

        self._run_with(runner, fake_run)

        assert during and all(
            handler == runner._on_stop_signal for handler in during.values()
        ), "run() did not route the stop signals to its runner"
        assert runner._stop_event.is_set()
        after = _handlers(watched)
        for signum, handler in before.items():
            assert after[signum] is handler, (
                f"{signal.Signals(signum).name} still points at the finished runner"
            )

    def test_run_restores_the_previous_handlers_when_it_raises(self):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        watched = _watched_signals()
        before = _handlers(watched)
        during = []

        def fake_run():
            during.append(signal.getsignal(signal.SIGINT))
            raise RuntimeError("the backend failed")

        with pytest.raises(RuntimeError, match="the backend failed"):
            self._run_with(runner, fake_run)

        assert during == [runner._on_stop_signal]
        after = _handlers(watched)
        for signum, handler in before.items():
            assert after[signum] is handler, (
                f"{signal.Signals(signum).name} still points at the finished runner"
            )

    @pytest.mark.skipif(
        not hasattr(signal, "SIGBREAK"),
        reason="SIGBREAK exists only on Windows; the Windows CI leg runs this test",
    )
    def test_run_routes_and_restores_sigbreak_on_windows(self):
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        before = signal.getsignal(signal.SIGBREAK)
        during = []

        def fake_run():
            during.append(signal.getsignal(signal.SIGBREAK))

        self._run_with(runner, fake_run)

        assert during == [runner._on_stop_signal]
        assert signal.getsignal(signal.SIGBREAK) is before

    def test_mac_menubar_restores_the_previous_handlers_when_its_body_returns(self):
        from jacked.service import tray
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        runner._ownership = MagicMock()  # an owned start skips the port probe
        watched = _watched_signals()
        before = _handlers(watched)
        during = {}

        class _Agent:
            """Stands in for the AppKit agent; its run loop returns at once."""

            def __init__(self, owner):
                assert owner is runner

            def run(self):
                during.update(_handlers(tray._stop_signal_numbers()))

        with (
            patch("jacked.service.menubar_mac.MacMenuBarApp", _Agent),
            patch.object(tray, "_UVICORN_AVAILABLE", True),
            patch("jacked.service.platform.detect_autostart", return_value=False),
            patch.object(runner, "_start_uvicorn", return_value=MagicMock()),
            patch.object(runner, "_wait_for_ready", return_value=True),
            patch.object(runner, "_note_ready"),
        ):
            runner._run_mac_menubar()

        assert during and all(
            handler == runner._on_stop_signal for handler in during.values()
        ), "the macOS agent did not route the stop signals to its runner"
        after = _handlers(watched)
        for signum, handler in before.items():
            assert after[signum] is handler, (
                f"{signal.Signals(signum).name} still points at the finished runner"
            )

    @pytest.mark.parametrize("platform", ["darwin", "linux"])
    def test_posix_routes_sigterm_and_sigint(self, platform):
        from jacked.service import tray

        with patch.object(tray.sys, "platform", platform):
            assert tray._stop_signal_numbers() == (signal.SIGTERM, signal.SIGINT)

    def test_windows_routes_sigint_and_sigbreak_when_the_platform_has_it(self):
        from jacked.service import tray

        with patch.object(tray.sys, "platform", "win32"):
            numbers = tray._stop_signal_numbers()
        expected = (signal.SIGINT,)
        if hasattr(signal, "SIGBREAK"):
            expected += (signal.SIGBREAK,)
        assert numbers == expected

    def test_off_the_main_thread_the_handlers_stay_unchanged(self, caplog):
        """Python lets only the main thread set a handler, so another thread
        is a quiet no-op: no attempt, and no warning per signal."""
        import threading

        from jacked.service import tray

        watched = _watched_signals()
        before = _handlers(watched)
        seen = {}

        def worker():
            try:
                with tray._stop_signals(lambda *_: None):
                    seen["inside"] = _handlers(watched)
            except BaseException as exc:  # reported on the main thread below
                seen["error"] = exc

        with caplog.at_level("DEBUG", logger="jacked.service.tray"):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=10)
        assert not thread.is_alive()
        assert "error" not in seen, seen.get("error")
        assert seen["inside"] == before
        assert not [r for r in caplog.records if r.name == "jacked.service.tray"]

    def test_a_signal_that_cannot_be_routed_is_logged_and_the_rest_still_are(
        self, caplog
    ):
        from jacked.service import tray

        numbers = tray._stop_signal_numbers()
        refused = numbers[0]
        real_signal = signal.signal
        before = _handlers(_watched_signals())

        def handler(*_):
            return None

        def flaky_signal(signum, action):
            if signum == refused and action is handler:
                raise OSError("refused")
            return real_signal(signum, action)

        with (
            caplog.at_level("WARNING", logger="jacked.service.tray"),
            patch.object(tray.signal, "signal", flaky_signal),
        ):
            with tray._stop_signals(handler):
                inside = _handlers(numbers)

        assert inside[refused] is before[refused]
        assert all(inside[s] is handler for s in numbers if s != refused)
        after = _handlers(before)
        assert all(after[s] is before[s] for s in before)
        assert any(
            signal.Signals(refused).name in record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING"
        )

    def test_a_handler_that_cannot_be_put_back_is_logged_and_the_rest_are(
        self, caplog
    ):
        from jacked.service import tray

        numbers = tray._stop_signal_numbers()
        stuck = numbers[0]
        real_signal = signal.signal
        before = _handlers(numbers)

        def handler(*_):
            return None

        def flaky_signal(signum, action):
            if signum == stuck and action is before[stuck]:
                raise OSError("refused")
            return real_signal(signum, action)

        try:
            with (
                caplog.at_level("WARNING", logger="jacked.service.tray"),
                patch.object(tray.signal, "signal", flaky_signal),
            ):
                with tray._stop_signals(handler):
                    pass
            after = _handlers(numbers)
            assert after[stuck] is handler
            assert all(after[s] is before[s] for s in numbers if s != stuck)
            assert any(
                signal.Signals(stuck).name in record.getMessage()
                for record in caplog.records
                if record.levelname == "WARNING"
            )
        finally:
            real_signal(stuck, before[stuck])


# ---------------------------------------------------------------------------
# #151i: a completed install closes its update; a stale recovery file goes
# ---------------------------------------------------------------------------


def _iso(when):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(when, tz=timezone.utc).isoformat()


class TestCompletedInstallIsNotAFailedUpdate:
    """#151i: the tray judged an update "abandoned" or "failed" from the
    status record's age alone. It ignored the independent proof that the
    install completed: the last-install record and the running version. So
    an update whose batch died after a good install wrote a recovery file,
    and an old recovery file toasted "Previous update failed" on every start.
    """

    RUNNING = "0.110.2"
    TOAST = (
        "Previous update failed. See "
        "~/.claude/jacked-update-failed.txt for recovery steps."
    )

    @pytest.fixture(autouse=True)
    def _running_version(self, monkeypatch):
        import jacked

        monkeypatch.setattr(jacked, "__version__", self.RUNNING)

    @staticmethod
    def _write_status(record, age_seconds):
        import json
        import os

        from jacked.service import update_status as us

        path = us.UPDATE_STATUS_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding="utf-8")
        stamp = time.time() - age_seconds
        os.utime(path, (stamp, stamp))

    @staticmethod
    def _write_last_install(at, to_version):
        """Write the record where `jacked install` does: the default path
        under this test's home, which is where the tray reads it."""
        from jacked import install_summary as isum

        isum.write_last_install(
            {
                "at": at,
                "from_version": "0.110.1",
                "to_version": to_version,
                "changes": {},
                "unchanged_count": 0,
            }
        )

    @staticmethod
    def _write_recovery_file():
        from jacked.service import updater

        updater.RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        updater.RECOVERY_FILE.write_text(
            "Jacked update interrupted: An update was interrupted.\n",
            encoding="utf-8",
        )

    @staticmethod
    def _boot(backend):
        """Drive the real ``run()`` on one status-bar backend.

        No server, GUI or thread starts. The boot checks run for real. On
        the pystray backend a fake icon runs the real ``_setup`` callback,
        so its toast is visible: the icon is returned. The macOS menu-bar
        agent shows no toast, so that backend returns None.
        """
        from types import SimpleNamespace

        from jacked.service import tray
        from jacked.service.tray import ServiceRunner

        runner = ServiceRunner(host="127.0.0.1", port=8321)
        icon = MagicMock()
        started = []

        class _NoThread:
            def __init__(self, target=None, name=None, daemon=None):
                pass

            def start(self):
                pass

        class _Icon:
            """pystray.Icon: its run loop calls the setup callback once."""

            def __init__(self, **_kwargs):
                pass

            def run(self, setup):
                started.append("pystray")
                with patch.object(tray.threading, "Thread", _NoThread):
                    setup(icon)

        with (
            patch.object(runner, "_install_tray_file_logger"),
            patch(
                "jacked.service.lifecycle.provision_service_contract",
                return_value=(MagicMock(), {}),
            ),
            patch(
                "jacked.service.lifecycle.claim_service_ownership",
                return_value=MagicMock(),
            ),
            patch.object(runner, "_guard_legacy_pid_or_exit"),
            patch.object(tray, "select_menubar_backend", return_value=backend),
            patch.object(
                runner, "_run_mac_menubar", side_effect=lambda: started.append("mac")
            ),
            patch.object(tray, "check_tray_deps"),
            patch.object(tray, "_UVICORN_AVAILABLE", True),
            patch.object(runner, "_install_windows_console_handler"),
            patch("jacked.service.platform.detect_autostart", return_value=False),
            patch.object(runner, "_start_uvicorn", return_value=MagicMock()),
            patch.object(runner, "_wait_for_ready", return_value=True),
            patch.object(runner, "_note_ready"),
            patch.object(runner, "_apply_icon"),
            patch.object(tray, "build_menu", return_value=MagicMock()),
            patch.object(tray, "create_icon_image", return_value=MagicMock()),
            patch.object(tray, "pystray", SimpleNamespace(Icon=_Icon), create=True),
        ):
            runner.run()
        assert started == [backend], "run() did not reach the backend under test"
        return icon if backend == "pystray" else None

    def _assert_toast(self, icon, shown):
        """The pystray toast for a recovery file (the macOS agent has none)."""
        if icon is None:
            return
        if shown:
            icon.notify.assert_called_once_with(self.TOAST, "Jacked Update Failed Earlier")
        else:
            icon.notify.assert_not_called()

    def _stale_in_progress(self):
        """An in_progress record whose updater stopped writing long ago."""
        from jacked.service import update_status as us

        started = time.time() - us.STALE_IN_PROGRESS_SECONDS - 120
        self._write_status(
            {
                "overall": "in_progress",
                "current_phase": "restarting_service",
                "started_at": _iso(started),
                "from_version": "0.110.1",
                "to_version": self.RUNNING,
                "phases": [],
            },
            us.STALE_IN_PROGRESS_SECONDS + 60,
        )
        return started

    def _failed(self, from_version, started, failed_at=None):
        record = {
            "overall": "failed",
            "current_phase": None,
            "started_at": _iso(started),
            "from_version": from_version,
            "to_version": "0.104.0",
            "phases": [],
            "error": "An update was interrupted at phase migrating_settings.",
            "recovery": "jacked upgrade",
        }
        if failed_at is not None:
            record["failed_at"] = _iso(failed_at)
        self._write_status(record, time.time() - started)

    # (1) a completed install is not an abandoned update

    def test_a_stale_update_whose_install_completed_is_closed_not_reported(self):
        import json

        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        started = self._stale_in_progress()
        self._write_last_install(_iso(started + 25), self.RUNNING)

        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        assert not updater.RECOVERY_FILE.exists(), (
            "the tray wrote a recovery file for an install that completed"
        )
        record = json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        assert record["overall"] == "succeeded"

    # (2) without a later install, the update is still reported

    @pytest.mark.parametrize("install_offset", [None, -60])
    def test_a_stale_update_without_a_later_install_is_still_reported(
        self, install_offset
    ):
        import json

        from jacked.service import update_status as us
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        started = self._stale_in_progress()
        if install_offset is not None:
            self._write_last_install(_iso(started + install_offset), self.RUNNING)

        ServiceRunner(host="127.0.0.1", port=8321)._record_abandoned_update()

        assert "interrupted at phase restarting_service" in (
            updater.RECOVERY_FILE.read_text(encoding="utf-8")
        )
        record = json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        assert record["overall"] == "failed"

    # (3) a later install makes an old failure stale. The check runs in
    # run(), the path both backends share: it ran only in the pystray setup
    # before, so the macOS menu-bar agent never removed a stale file.

    @pytest.mark.parametrize("backend", ["mac", "pystray"])
    def test_boot_clears_a_recovery_file_a_later_install_made_stale(
        self, caplog, backend
    ):
        from jacked.service import updater

        started = time.time() - 3 * 86400
        self._failed("0.103.0", started)
        self._write_last_install(_iso(time.time() - 3600), self.RUNNING)
        self._write_recovery_file()

        with caplog.at_level("INFO", logger="jacked.service.tray"):
            icon = self._boot(backend)

        assert not updater.RECOVERY_FILE.exists()
        assert (
            "cleared stale update recovery file (from 0.103.0, running 0.110.2)"
            in caplog.text
        )
        self._assert_toast(icon, shown=False)

    # (4) a genuine failure keeps its file and its toast

    @pytest.mark.parametrize("backend", ["mac", "pystray"])
    def test_boot_keeps_a_recovery_file_no_later_install_followed(self, backend):
        from jacked.service import updater

        started = time.time() - 3600
        self._failed("0.103.0", started)
        self._write_last_install(_iso(started - 86400), self.RUNNING)
        self._write_recovery_file()

        icon = self._boot(backend)

        assert updater.RECOVERY_FILE.exists()
        self._assert_toast(icon, shown=True)

    @pytest.mark.parametrize("backend", ["mac", "pystray"])
    def test_boot_keeps_the_recovery_file_while_the_failed_build_still_runs(
        self, backend
    ):
        """The update started from the build that still runs, so its failure
        still describes this process, whatever a later install wrote."""
        from jacked.service import updater

        started = time.time() - 3600
        self._failed(self.RUNNING, started)
        self._write_last_install(_iso(started + 60), "0.110.3")
        self._write_recovery_file()

        icon = self._boot(backend)

        assert updater.RECOVERY_FILE.exists()
        self._assert_toast(icon, shown=True)

    @pytest.mark.parametrize("backend", ["mac", "pystray"])
    def test_boot_keeps_a_recovery_file_without_a_record(self, backend):
        """A recovery file that no status record explains (the tray's own
        refusal, a CLI upgrade) is never cleared on a guess."""
        from jacked.service import updater

        self._write_last_install(_iso(time.time() - 60), self.RUNNING)
        self._write_recovery_file()

        icon = self._boot(backend)

        assert updater.RECOVERY_FILE.exists()
        self._assert_toast(icon, shown=True)

    @pytest.mark.parametrize("backend", ["mac", "pystray"])
    def test_boot_keeps_the_file_of_a_failure_that_followed_its_own_install(
        self, backend
    ):
        """Reviewer B / pre-mortem 3: an update installed 0.110.2 (its own
        last-install record), then failed and could not roll back, so
        0.110.2 runs. Nothing repaired the failure after it closed."""
        from jacked.service import updater

        started = time.time() - 3600
        self._failed("0.110.1", started, failed_at=started + 300)
        self._write_last_install(_iso(started + 60), self.RUNNING)
        self._write_recovery_file()

        icon = self._boot(backend)

        assert updater.RECOVERY_FILE.exists()
        self._assert_toast(icon, shown=True)

    # (5) a completed update retires the file of an EARLIER failure

    def _reconciled_update(self, recovery_written):
        """An earlier failure's recovery file, then a later update whose
        install completed while its record stayed in_progress."""
        import os

        from jacked.service import updater

        self._write_recovery_file()
        started = self._stale_in_progress()
        os.utime(updater.RECOVERY_FILE, (started + recovery_written,) * 2)
        self._write_last_install(_iso(started + 25), self.RUNNING)

    @pytest.mark.parametrize("backend", ["mac", "pystray"])
    def test_boot_clears_an_earlier_failures_file_after_a_completed_update(
        self, caplog, backend
    ):
        """Reviewer D: abandoned_status closed the later update as succeeded,
        but only a failed record could clear the file, so the old file
        stayed and toasted on every start."""
        import json

        from jacked.service import update_status as us
        from jacked.service import updater

        self._reconciled_update(recovery_written=-600)

        with caplog.at_level("INFO", logger="jacked.service.tray"):
            icon = self._boot(backend)

        record = json.loads(us.UPDATE_STATUS_FILE.read_text(encoding="utf-8"))
        assert record["overall"] == "succeeded"
        assert not updater.RECOVERY_FILE.exists()
        assert (
            "cleared stale update recovery file (from 0.110.1, running 0.110.2)"
            in caplog.text
        )
        self._assert_toast(icon, shown=False)

    def test_boot_clears_the_file_after_an_old_completed_update(self, caplog):
        """read_status hides a succeeded record after STALE_SUCCEEDED_SECONDS,
        but the record is still evidence: a boot an hour later still finds
        that the file predates the completed update."""
        import os

        from jacked.service import update_status as us
        from jacked.service import updater

        started = time.time() - 3600
        self._write_recovery_file()
        os.utime(updater.RECOVERY_FILE, (started - 3600,) * 2)
        self._write_status(
            {
                "overall": "succeeded",
                "current_phase": None,
                "started_at": _iso(started),
                "from_version": "0.110.1",
                "to_version": self.RUNNING,
                "phases": [],
            },
            us.STALE_SUCCEEDED_SECONDS + 60,
        )
        assert us.read_status(us.UPDATE_STATUS_FILE) is None

        with caplog.at_level("INFO", logger="jacked.service.tray"):
            self._boot("mac")

        assert not updater.RECOVERY_FILE.exists()
        assert "cleared stale update recovery file" in caplog.text

    @pytest.mark.parametrize("backend", ["mac", "pystray"])
    def test_boot_keeps_a_file_written_after_the_completed_update_started(
        self, backend
    ):
        from jacked.service import updater

        self._reconciled_update(recovery_written=+30)

        icon = self._boot(backend)

        assert updater.RECOVERY_FILE.exists()
        self._assert_toast(icon, shown=True)

    def test_clear_stale_recovery_without_a_recovery_file_does_nothing(self):
        from jacked.service import updater
        from jacked.service.tray import ServiceRunner

        started = time.time() - 3 * 86400
        self._failed("0.103.0", started)
        self._write_last_install(_iso(time.time() - 3600), self.RUNNING)

        assert ServiceRunner(host="127.0.0.1", port=8321).clear_stale_recovery() is False
        assert not updater.RECOVERY_FILE.exists()


@pytest.mark.parametrize("payload", ["[]", '"in_progress"'], ids=["list", "string"])
def test_a_status_file_that_is_not_an_object_reads_as_missing(payload, caplog):
    """A status file that holds valid JSON but no object (a ``[]`` or a
    string) made every status reader raise AttributeError on ``data.get``:
    /api/update/status failed, and the tray's boot check logged an
    exception on every start. It now reads as missing, and the boot check
    stays quiet."""
    import logging

    from jacked.service import update_status as us
    from jacked.service.tray import ServiceRunner

    us.UPDATE_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    us.UPDATE_STATUS_FILE.write_text(payload, encoding="utf-8")
    assert us.read_status(us.UPDATE_STATUS_FILE) is None

    runner = ServiceRunner(host="127.0.0.1", port=8321)
    with (
        caplog.at_level(logging.DEBUG, logger="jacked"),
        patch.object(runner, "_install_tray_file_logger"),
        patch(
            "jacked.service.lifecycle.provision_service_contract",
            return_value=(MagicMock(), {}),
        ),
        patch(
            "jacked.service.lifecycle.claim_service_ownership",
            return_value=MagicMock(),
        ),
        patch.object(runner, "_guard_legacy_pid_or_exit"),
        patch.object(ServiceRunner, "_run"),
    ):
        runner.run()  # the real boot check: _record_abandoned_update

    loud = [r for r in caplog.records if r.levelno > logging.DEBUG]
    assert not loud, [r.getMessage() for r in loud]
    assert us.UPDATE_STATUS_FILE.read_text(encoding="utf-8") == payload


def test_the_readme_names_the_same_commands_as_the_no_menu_warning():
    """The Xorg tray has no Update item and no Start on Login item, so the
    warning and the README both give the commands that replace them."""
    from pathlib import Path

    readme = (Path(__file__).resolve().parents[3] / "README.md").read_text(encoding="utf-8")
    line = next(row for row in readme.splitlines() if "**The Xorg tray has no menu.**" in row)
    for command in (
        "jacked service restart",
        "jacked service stop",
        "jacked upgrade",
        "jacked service install",
        "jacked service uninstall",
    ):
        assert command in line, command
    for command in ("jacked upgrade", "jacked service install"):
        assert command in " ".join(_NO_MENU_WARNING.split()), command
