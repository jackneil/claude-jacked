"""Tests for jacked doctor — real health probes, not just port availability."""
import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from jacked.service.platform import launchd_job_never_ran as _real_launchd_job_never_ran


@pytest.fixture(autouse=True)
def _no_real_supervisor_probes():
    """doctor's NOT RUNNING path asks launchctl whether the job ever ran and
    asks the supervisor for the autostart state. On a CI runner the port is
    free, so both reach the real-supervisor tripwire; on a developer machine
    the live service holds the port and hides that. Tests that care about
    either answer patch it again inside the test."""
    from jacked.service.autostart import AutostartInspection, AutostartState

    with patch("jacked.service.platform.launchd_job_never_ran", return_value=False), \
         patch("jacked.service.platform.inspect_autostart",
               return_value=AutostartInspection(AutostartState.ABSENT)):
        yield


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


# #178i: doctor's recovery and the launchd job that never ran


def _autostart(state_name):
    from jacked.service.autostart import AutostartState

    return MagicMock(state=getattr(AutostartState, state_name), reason="")


@pytest.mark.parametrize("state, expected", [
    ("OWNED_ENABLED", "jacked service restart"),
    ("ABSENT", "jacked service start"),
])
def test_doctor_recovery_matches_the_autostart_state(state, expected):
    """With autostart on, `service start` runs in the foreground and never
    returns; `service restart` hands the start to the supervisor."""
    from jacked.cli import main

    with patch("jacked.service.process.is_port_available", return_value=True), \
         patch("jacked.service.platform.inspect_autostart", return_value=_autostart(state)), \
         patch("jacked.service.platform.launchd_job_never_ran", return_value=False):
        result = CliRunner().invoke(main, ["doctor"])
    assert f"Recovery: {expected}" in result.output


def test_doctor_names_a_launchd_job_that_never_ran():
    from jacked.cli import main

    with patch("jacked.service.process.is_port_available", return_value=True), \
         patch("jacked.service.platform.inspect_autostart", return_value=_autostart("OWNED_ENABLED")), \
         patch("jacked.service.platform.launchd_job_never_ran", return_value=True):
        result = CliRunner().invoke(main, ["doctor"])
    assert "loaded but never ran" in result.output
    assert "Recovery: jacked service restart" in result.output


@pytest.mark.parametrize("stdout, expected", [
    ("gui/501/ai.hank.jacked = {\n\tstate = not running\n\truns = 0\n\t\tstate = active\n}", True),
    ("gui/501/ai.hank.jacked = {\n\tstate = running\n\truns = 1\n\tpid = 66875\n}", False),
    ("gui/501/ai.hank.jacked = {\n\tstate = not running\n\truns = 3\n}", False),
])
def test_launchd_never_ran_reads_only_the_top_level_fields(monkeypatch, stdout, expected):
    from jacked.service import platform

    monkeypatch.setattr(platform.sys, "platform", "darwin")
    probe = MagicMock(returncode=0, stdout=stdout)
    assert _real_launchd_job_never_ran(run=lambda *a, **k: probe, uid=501) is expected


def test_launchd_never_ran_is_false_off_macos_and_on_a_probe_failure(monkeypatch):
    from jacked.service import platform

    monkeypatch.setattr(platform.sys, "platform", "linux")
    assert _real_launchd_job_never_ran(run=lambda *a, **k: 1 / 0) is False
    monkeypatch.setattr(platform.sys, "platform", "darwin")
    assert _real_launchd_job_never_ran(run=lambda *a, **k: MagicMock(returncode=113, stdout="")) is False
    # A platform without os.getuid (Windows) never raises.
    monkeypatch.delattr(platform.os, "getuid", raising=False)
    assert _real_launchd_job_never_ran(run=lambda *a, **k: MagicMock(returncode=0, stdout="")) is False
