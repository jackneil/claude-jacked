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

    @patch("jacked.service.tray.pystray")
    @patch("jacked.service.tray.uvicorn")
    def test_start_uvicorn_thread_is_daemon(self, mock_uvicorn, mock_pystray):
        _skip_if_no_tray()
        from jacked.service.tray import ServiceRunner
        runner = ServiceRunner(host="127.0.0.1", port=8321)
        thread = runner._start_uvicorn()
        assert thread.daemon is True
        thread.join(timeout=0.1)
