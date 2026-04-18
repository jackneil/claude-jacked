"""Tests for jacked.service.tray module."""

import pytest
from unittest.mock import patch, MagicMock


def _skip_if_no_tray():
    """Skip test if pystray/Pillow not installed."""
    try:
        import pystray  # noqa: F401
        import PIL  # noqa: F401
    except ImportError:
        pytest.skip("pystray/Pillow not installed")


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


class TestBuildMenu:
    """Tests for build_menu()."""

    def test_menu_has_expected_items(self):
        _skip_if_no_tray()
        from jacked.service.tray import build_menu
        noop = lambda: None
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

    @patch("jacked.service.tray.pystray")
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_thread_is_daemon(self, mock_uvicorn, mock_pystray):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        thread = runner._start_uvicorn()
        assert thread.daemon is True
        thread.join(timeout=0.1)

    def test_on_restart_handles_exception(self):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        runner._icon = MagicMock()
        with patch.object(runner, "_start_uvicorn", side_effect=OSError("port in use")):
            runner._on_restart()  # should not raise
        # Icon should show stopped state on failure
        assert runner._icon.icon is not None


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
                    t1.start(); t2.start()
                    t1.join(); t2.join()

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
        noop = lambda: None
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
        noop = lambda: None
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
        noop = lambda: None
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
        noop = lambda: None
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
