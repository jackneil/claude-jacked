"""Tests for the macOS menu-bar launcher branch (M3).

Covers the pure backend selector, the run() dispatch in both directions
(mocked sys.platform — never actually starting a GUI loop or uvicorn), the
agent module's importability + degraded stub, and the `jacked menubar` CLI
guard. The pill-title-from-summary wiring is covered in test_menubar_summary.
"""
import sys
from unittest import mock

import pytest

from jacked.service import tray
from jacked.service.tray import ServiceRunner, select_menubar_backend


# ---------------------------------------------------------------------------
# Pure selector
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "platform,mac_available,expected",
    [
        ("darwin", True, "mac"),
        ("darwin", False, "pystray"),
        ("win32", True, "pystray"),
        ("win32", False, "pystray"),
        ("linux", True, "pystray"),
        ("linux", False, "pystray"),
    ],
)
def test_select_menubar_backend(platform, mac_available, expected):
    assert select_menubar_backend(platform, mac_available) == expected


# ---------------------------------------------------------------------------
# run() dispatch
# ---------------------------------------------------------------------------


def test_run_dispatches_to_mac_on_darwin():
    runner = ServiceRunner()
    with mock.patch.object(tray.sys, "platform", "darwin"), \
         mock.patch.object(tray, "_mac_menubar_available", return_value=True), \
         mock.patch.object(ServiceRunner, "_install_tray_file_logger"), \
         mock.patch.object(ServiceRunner, "_run_mac_menubar") as mac_run:
        runner.run()
    mac_run.assert_called_once()


def test_run_uses_pystray_off_darwin():
    """On non-darwin, run() must NOT take the mac branch — it falls into the
    pystray precondition path (proven here by the port guard firing)."""
    runner = ServiceRunner()
    with mock.patch.object(tray.sys, "platform", "linux"), \
         mock.patch.object(tray, "_mac_menubar_available", return_value=False), \
         mock.patch.object(ServiceRunner, "_install_tray_file_logger"), \
         mock.patch.object(ServiceRunner, "_run_mac_menubar") as mac_run, \
         mock.patch.object(tray, "is_port_available", return_value=False):
        with pytest.raises(SystemExit):
            runner.run()
    mac_run.assert_not_called()


def test_run_uses_pystray_when_mac_deps_missing_on_darwin():
    """darwin but rumps/pyobjc unavailable → pystray fallback, not mac."""
    runner = ServiceRunner()
    with mock.patch.object(tray.sys, "platform", "darwin"), \
         mock.patch.object(tray, "_mac_menubar_available", return_value=False), \
         mock.patch.object(ServiceRunner, "_install_tray_file_logger"), \
         mock.patch.object(ServiceRunner, "_run_mac_menubar") as mac_run, \
         mock.patch.object(tray, "is_port_available", return_value=False):
        with pytest.raises(SystemExit):
            runner.run()
    mac_run.assert_not_called()


# ---------------------------------------------------------------------------
# Agent module
# ---------------------------------------------------------------------------


def test_menubar_mac_importable_and_loopback():
    from jacked.service import menubar_mac

    # rumps/pyobjc are darwin-only deps; on this darwin dev/CI box they resolve.
    if sys.platform == "darwin":
        assert menubar_mac.RUMPS_AVAILABLE is True
    assert menubar_mac._loopback("0.0.0.0") == "127.0.0.1"
    assert menubar_mac._loopback("127.0.0.1") == "127.0.0.1"
    assert menubar_mac._loopback("192.168.1.5") == "192.168.1.5"


def test_mac_menubar_available_matches_platform():
    avail = tray._mac_menubar_available()
    if sys.platform != "darwin":
        assert avail is False
    else:
        assert avail is True


def test_mac_app_wires_version_update_menu_handlers():
    """Regression guard: the mac agent must keep the version line, click-to-update,
    Check-for-Updates, and Start-on-Login handlers (a prior rebuild dropped them,
    regressing the pystray tray's update UX). Methods only — GUI behavior is
    user-verified."""
    from jacked.service import menubar_mac

    if not menubar_mac.RUMPS_AVAILABLE:
        pytest.skip("rumps/pyobjc unavailable")
    cls = menubar_mac.MacMenuBarApp
    for handler in (
        "_refresh_version_menu",
        "_on_version_click",
        "_on_check_updates_item",
        "_after_check_refresh",
        "_on_toggle_autostart_item",
    ):
        assert hasattr(cls, handler), f"missing menu handler: {handler}"


# ---------------------------------------------------------------------------
# CLI guard
# ---------------------------------------------------------------------------


def test_cli_menubar_rejects_non_darwin():
    from click.testing import CliRunner

    from jacked.cli import main

    with mock.patch("jacked.cli.sys.platform", "linux"):
        result = CliRunner().invoke(main, ["menubar"])
    assert result.exit_code == 1
    assert "macOS-only" in result.output
