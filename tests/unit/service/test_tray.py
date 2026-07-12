"""Tests for jacked.service.tray module."""

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


class TestGlyphFont:
    """Regression: the tray 'J' must render at full size, not the ~10px
    bitmap default.

    Pillow only resolves the bare family name 'Arial' on macOS; on Windows
    and Linux it raises OSError, so the old code fell back to
    ``ImageFont.load_default()`` (a ~10px bitmap face) and the 'J' shrank to
    an invisible speck in the system tray. See _load_glyph_font.
    """

    def test_glyph_font_is_requested_size_not_tiny_default(self):
        _skip_if_no_tray()
        from jacked.service.tray import _load_glyph_font
        font = _load_glyph_font(36)
        # The legacy argless load_default() is a ~10px bitmap face — the bug.
        assert getattr(font, "size", 0) == 36

    def test_running_icon_has_visible_white_glyph(self):
        """The white 'J' must occupy a real chunk of the 64x64 icon, not a
        2px speck. Counts near-white glyph pixels over the purple bg.

        Calibration on Windows: old 10px default -> 10 white px;
        fixed 36px arial -> 107 white px. Threshold of 60 separates them
        with margin for DejaVu's slightly different glyph on Linux CI.
        """
        _skip_if_no_tray()
        from jacked.service.tray import create_icon_image
        img = create_icon_image("running").convert("RGBA")
        # Iterate raw RGBA bytes — avoids getdata() (deprecated in Pillow 14)
        # and get_flattened_data() (too new for our >=9.0 floor).
        data = img.tobytes()
        white = sum(
            1
            for i in range(0, len(data), 4)
            if data[i + 3] > 200
            and data[i] > 220 and data[i + 1] > 220 and data[i + 2] > 220
        )
        assert white > 60, (
            f"glyph too small ({white} white px) — font fell back to the "
            "bitmap default instead of a scalable 36px face"
        )


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
    def test_start_uvicorn_raises_systemexit_on_bind_conflict(
        self, mock_uvicorn, mock_pystray,
    ):
        """A create_sockets OSError (port in use) surfaces synchronously as
        SystemExit with the friendly message, not a silent daemon-thread death."""
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
            runner._start_uvicorn()
        assert "already in use" in str(exc_info.value)

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
        with patch.object(_sys, "platform", "win32"):
            runner._install_windows_console_handler()
        # Callback must be retained (otherwise GC would invalidate the handler).
        assert getattr(runner, "_win_ctrl_handler", None) is not None


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
        monkeypatch.setattr("jacked.service.tray.remove_pid", lambda *a, **k: None)
        monkeypatch.setattr(
            "jacked.service.platform.detect_autostart", lambda: False
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
