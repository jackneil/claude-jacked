"""Tests for jacked doctor — real health probes, not just port availability."""
import sys
from unittest.mock import MagicMock, patch

from click.testing import CliRunner


def test_doctor_reports_version_and_method():
    from jacked.cli import main
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "Version:" in result.output
    assert "Install method:" in result.output


def test_doctor_plist_missing_recommends_install(tmp_path, monkeypatch):
    if sys.platform != "darwin":
        import pytest
        pytest.skip("macOS-specific")
    from jacked.cli import main
    from jacked.service import platform as plat
    monkeypatch.setattr(plat, "_get_launchd_plist_path",
                        lambda: tmp_path / "nope.plist")
    result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0
    assert "plist" in result.output.lower()
    assert "MISSING" in result.output or "missing" in result.output
    assert "service install" in result.output or "jacked install" in result.output


def test_doctor_service_healthy_via_http_probe(monkeypatch):
    """HTTP 200 *and* a live PID file → HEALTHY.

    Both halves are patched deliberately: before the webux fix this test
    passed on any machine whose PID file happened to be live, which made it
    silently environment-dependent.
    """
    from jacked.cli import main
    fake_resp = MagicMock(status_code=200)
    fake_resp.json.return_value = {"current": "0.41.24"}
    with patch("jacked.service.process.is_port_available", return_value=False), \
         patch("jacked.service.process.read_pid",
               return_value={"pid": 4321, "port": 8321}), \
         patch("jacked.service.process.is_process_alive", return_value=True), \
         patch("httpx.get", return_value=fake_resp):
        result = CliRunner().invoke(main, ["doctor"])
    assert "healthy" in result.output.lower()


def test_doctor_http_200_without_pid_is_not_healthy():
    """Regression: a stray `jacked webux` must not be reported HEALTHY.

    webux serves the same dashboard (so the HTTP probe returns 200) but runs
    no tray icon and writes no PID file. Reporting HEALTHY here hid a dead
    tray for 8 days while webux squatted port 8321.
    """
    from jacked.cli import main
    fake_resp = MagicMock(status_code=200)
    with patch("jacked.service.process.is_port_available", return_value=False), \
         patch("jacked.service.process.read_pid", return_value=None), \
         patch("httpx.get", return_value=fake_resp):
        result = CliRunner().invoke(main, ["doctor"])
    out = result.output.lower()
    assert "healthy" not in out
    assert "no tray service" in out
    assert "webux" in out
    assert "jacked service start" in out


def test_doctor_http_200_with_dead_pid_reports_stale():
    """HTTP 200 but the PID file points at a dead process → not healthy."""
    from jacked.cli import main
    fake_resp = MagicMock(status_code=200)
    with patch("jacked.service.process.is_port_available", return_value=False), \
         patch("jacked.service.process.read_pid",
               return_value={"pid": 999999, "port": 8321}), \
         patch("jacked.service.process.is_process_alive", return_value=False), \
         patch("httpx.get", return_value=fake_resp):
        result = CliRunner().invoke(main, ["doctor"])
    out = result.output.lower()
    assert "healthy" not in out
    assert "no tray service" in out
    assert "stale pid file" in out
    assert "999999" in result.output


def test_port_owner_hint_is_platform_correct(monkeypatch):
    """Windows users get netstat, not a macOS-only lsof they cannot run."""
    import sys as _sys

    from jacked.cli import _port_owner_hint

    monkeypatch.setattr(_sys, "platform", "win32")
    assert _port_owner_hint(8321) == "netstat -ano | findstr :8321"

    monkeypatch.setattr(_sys, "platform", "darwin")
    assert _port_owner_hint(8321) == "lsof -iTCP:8321 -sTCP:LISTEN"


def test_doctor_port_held_but_http_dead():
    """Port in use but HTTP probe fails → report 'unhealthy'/'unreachable'."""
    import httpx
    from jacked.cli import main
    with patch("jacked.service.process.is_port_available", return_value=False), \
         patch("httpx.get", side_effect=httpx.ConnectError("refused")):
        result = CliRunner().invoke(main, ["doctor"])
    assert ("not healthy" in result.output.lower()
            or "unreachable" in result.output.lower()
            or "unhealthy" in result.output.lower())


def test_doctor_service_not_running():
    """Port free → service not running, recommend jacked service start."""
    from jacked.cli import main
    with patch("jacked.service.process.is_port_available", return_value=True):
        result = CliRunner().invoke(main, ["doctor"])
    assert "not running" in result.output.lower()
    assert "jacked service" in result.output


def test_doctor_editable_install_recovery(monkeypatch):
    from jacked import install_method
    monkeypatch.setattr(install_method, "detect_install_method", lambda: "editable")
    from jacked.cli import main
    result = CliRunner().invoke(main, ["doctor"])
    assert "git pull" in result.output or "uv sync" in result.output


def test_doctor_pip_install_recovery(monkeypatch):
    from jacked import install_method
    monkeypatch.setattr(install_method, "detect_install_method", lambda: "pip")
    from jacked.cli import main
    result = CliRunner().invoke(main, ["doctor"])
    assert "uv tool install" in result.output
