"""Tests for jacked doctor — real health probes, not just port availability."""
import sys
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from jacked.service.platform import launchd_job_never_ran as _real_launchd_job_never_ran
from jacked.service.platform import launchd_job_probe as _real_launchd_job_probe
from jacked.service.platform import supervision_drift as _real_supervision_drift


@pytest.fixture(autouse=True)
def _no_real_supervisor_probes():
    """doctor's NOT RUNNING path asks launchctl whether the job ever ran and
    asks the supervisor for the autostart state. On a CI runner the port is
    free, so both reach the real-supervisor tripwire; on a developer machine
    the live service holds the port and hides that. The supervision-drift
    probe reads the service manifest and asks launchctl too, and doctor asks
    launchctl once for both (launchd_job_probe). Tests that care about an
    answer patch it again inside the test."""
    from jacked.service.autostart import AutostartInspection, AutostartState

    with patch("jacked.service.platform.launchd_job_probe", return_value=None), \
         patch("jacked.service.platform.launchd_job_never_ran", return_value=False), \
         patch("jacked.service.platform.supervision_drift", return_value=None), \
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


@pytest.mark.parametrize("method", ["pip", "unknown (no metadata)"])
def test_doctor_install_recovery_prints_the_tray_extra(monkeypatch, method):
    """Rich reads "[tray]" as a style tag. The literal must be escaped, or the
    printed recovery command loses the extra."""
    from jacked import install_method
    monkeypatch.setattr(install_method, "detect_install_method", lambda: method)
    from jacked.cli import main
    result = CliRunner().invoke(main, ["doctor"])
    output = " ".join(result.output.split())
    assert 'uv tool install "claude-jacked[tray]" --force' in output


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


def _real_probe():
    """Undo the autouse stub of the shared probe for one block.

    A ``patch`` context nests inside the autouse fixture's own patch, so the
    stub comes back at the block's end and the original at the fixture's
    end. ``monkeypatch.setattr`` would undo last and restore the stub for
    every later test in the process.
    """
    return patch("jacked.service.platform.launchd_job_probe", _real_launchd_job_probe)


@pytest.mark.parametrize("stdout, expected", [
    ("gui/501/ai.hank.jacked = {\n\tstate = not running\n\truns = 0\n\t\tstate = active\n}", True),
    ("gui/501/ai.hank.jacked = {\n\tstate = running\n\truns = 1\n\tpid = 66875\n}", False),
    ("gui/501/ai.hank.jacked = {\n\tstate = not running\n\truns = 3\n}", False),
])
def test_launchd_never_ran_reads_only_the_top_level_fields(monkeypatch, stdout, expected):
    from jacked.service import platform

    monkeypatch.setattr(platform.sys, "platform", "darwin")
    probe = MagicMock(returncode=0, stdout=stdout)
    with _real_probe():
        assert _real_launchd_job_never_ran(run=lambda *a, **k: probe, uid=501) is expected


def test_launchd_never_ran_is_false_off_macos_and_on_a_probe_failure(monkeypatch):
    from jacked.service import platform

    with _real_probe():
        monkeypatch.setattr(platform.sys, "platform", "linux")
        assert _real_launchd_job_never_ran(run=lambda *a, **k: 1 / 0) is False
        monkeypatch.setattr(platform.sys, "platform", "darwin")
        assert _real_launchd_job_never_ran(
            run=lambda *a, **k: MagicMock(returncode=113, stdout="")
        ) is False
        # A platform without os.getuid (Windows) never raises.
        monkeypatch.delattr(platform.os, "getuid", raising=False)
        assert _real_launchd_job_never_ran(
            run=lambda *a, **k: MagicMock(returncode=0, stdout="")
        ) is False


# #128i: doctor names an autostart definition that does not supervise the
# running service, whatever the port probe shows.


def _healthy_service_probes():
    """Port held, HTTP 200 and a live PID file: doctor reports HEALTHY."""
    return (
        patch("jacked.service.process.is_port_available", return_value=False),
        patch("jacked.service.process.read_pid", return_value={"pid": 4242, "port": 8321}),
        patch("jacked.service.process.is_process_alive", return_value=True),
        patch("httpx.get", return_value=MagicMock(status_code=200)),
    )


def _doctor(*patches):
    from contextlib import ExitStack

    from jacked.cli import main

    with ExitStack() as stack:
        for item in patches:
            stack.enter_context(item)
        result = CliRunner().invoke(main, ["doctor"])
    assert result.exit_code == 0, result.output
    return " ".join(result.output.split())  # Rich wraps at the console width


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS only")
def test_doctor_says_an_unloaded_launchd_job_does_not_supervise_the_service(
    tmp_path, monkeypatch
):
    """The #128i state: launchctl print exits 113 while a manual service
    runs. Doctor gives the same reason as `jacked service status`."""
    import subprocess

    from jacked.service import instance_storage
    from jacked.service.autostart import AutostartInspection, AutostartState
    from jacked.service.instance import (
        BindIdentity,
        InstanceManifest,
        ProcessIdentity,
        ServicePaths,
        current_user_identity,
        publish_manifest,
    )
    from jacked.service.platform import _get_launchd_plist_path
    from jacked.service.spec import SupervisorKind
    from tests.unit.service.supervisor_test_support import installed_artifact, make_spec

    render = tmp_path / "render"
    render.mkdir()
    _spec, _environment, rendered, _path = installed_artifact(
        render, SupervisorKind.LAUNCHD
    )
    plist = _get_launchd_plist_path()
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_bytes(rendered.content)
    paths = ServicePaths.in_directory(tmp_path / "jacked-service-v2")
    monkeypatch.setattr("jacked.service.lifecycle.default_service_paths", lambda: paths)
    process = ProcessIdentity(
        pid=4242, creation_id="test-creation", executable="/opt/jacked/python"
    )
    manifest = InstanceManifest.create(
        spec=make_spec(SupervisorKind.MANUAL),
        process=process,
        user_id=current_user_identity(),
        machine_id="machine-test",
        bind=BindIdentity(host="127.0.0.1", port=8321),
        control_address=str(paths.root / "control.sock"),
    )
    publish_manifest(paths.manifest, manifest)
    monkeypatch.setattr(instance_storage, "process_identity", lambda pid: process)
    guarded_run = subprocess.run
    asked = []

    def launchctl(argv, **kwargs):
        if list(argv[:2]) == ["launchctl", "print"]:
            asked.append(list(argv))
            return MagicMock(returncode=113, stdout="", stderr="Could not find service")
        return guarded_run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", launchctl)
    owned = AutostartInspection(
        AutostartState.OWNED_ENABLED, generation=manifest.generation
    )

    text = _doctor(
        *_healthy_service_probes(),
        patch("jacked.service.platform.launchd_job_probe", _real_launchd_job_probe),
        patch("jacked.service.platform.supervision_drift", _real_supervision_drift),
        patch("jacked.service.platform.inspect_autostart", return_value=owned),
    )

    assert "Launchd plist: OK" in text
    assert "Service: HEALTHY" in text
    assert (
        "Autostart: not supervising the running service: launchd job not loaded "
        "Recovery: jacked service restart"
    ) in text
    assert asked


def test_doctor_reports_supervision_drift_beside_a_healthy_service():
    text = _doctor(
        *_healthy_service_probes(),
        patch(
            "jacked.service.platform.supervision_drift",
            return_value="the running service is under a manual supervisor",
        ),
    )

    assert "Service: HEALTHY" in text
    assert (
        "Autostart: not supervising the running service: the running service is "
        "under a manual supervisor Recovery: jacked service restart"
    ) in text


def test_doctor_is_silent_about_supervision_when_nothing_drifted():
    text = _doctor(*_healthy_service_probes())

    assert "Service: HEALTHY" in text
    assert "not supervising" not in text


_STOPPED_UNLOADED = (
    "Autostart: will not start the service until the next login: launchd job "
    "not loaded Recovery: jacked service restart"
)


def test_doctor_shows_the_drift_reason_instead_of_the_never_ran_hint():
    text = _doctor(
        patch("jacked.service.process.is_port_available", return_value=True),
        patch(
            "jacked.service.platform.inspect_autostart",
            return_value=_autostart("OWNED_ENABLED"),
        ),
        patch("jacked.service.platform.launchd_job_never_ran", return_value=True),
        patch(
            "jacked.service.platform.supervision_drift",
            return_value="launchd job not loaded",
        ),
    )

    assert "Service: NOT RUNNING" in text
    assert _STOPPED_UNLOADED in text
    assert "never ran" not in text


@pytest.mark.parametrize(
    "verdict",
    ["not running", "dashboard without a tray service", "healthy", "alive but unreachable"],
)
def test_doctor_words_an_unloaded_job_by_its_verdict(verdict):
    """A held port and a live PID name a running service; every other
    verdict gets the words that are true either way: launchd loads the plist
    again at the next login, and until then nothing starts the service."""
    import httpx

    from jacked.service.platform import LAUNCHD_JOB_NOT_LOADED

    probes = {
        "not running": (
            patch("jacked.service.process.is_port_available", return_value=True),
        ),
        "dashboard without a tray service": (
            patch("jacked.service.process.is_port_available", return_value=False),
            patch("jacked.service.process.read_pid", return_value=None),
            patch("httpx.get", return_value=MagicMock(status_code=200)),
        ),
        "healthy": _healthy_service_probes(),
        "alive but unreachable": (
            patch("jacked.service.process.is_port_available", return_value=False),
            patch("jacked.service.process.read_pid", return_value={"pid": 4242}),
            patch("jacked.service.process.is_process_alive", return_value=True),
            patch("httpx.get", side_effect=httpx.ConnectError("refused")),
        ),
    }[verdict]

    text = _doctor(
        *probes,
        patch(
            "jacked.service.platform.supervision_drift",
            return_value=LAUNCHD_JOB_NOT_LOADED,
        ),
    )

    if verdict in {"healthy", "alive but unreachable"}:
        assert (
            "Autostart: not supervising the running service: launchd job not "
            "loaded Recovery: jacked service restart"
        ) in text
    else:
        assert _STOPPED_UNLOADED in text


def test_doctor_keeps_the_running_service_words_for_a_manifest_reason():
    """A manual-supervisor reason comes from a live manifest, so it names the
    running service even when doctor's port 8321 check missed that service
    (a quarantine start listens on another port)."""
    text = _doctor(
        patch("jacked.service.process.is_port_available", return_value=True),
        patch(
            "jacked.service.platform.supervision_drift",
            return_value="the running service is under a manual supervisor",
        ),
    )

    assert "Service: NOT RUNNING" in text
    assert (
        "Autostart: not supervising the running service: the running service is "
        "under a manual supervisor"
    ) in text


@pytest.mark.skipif(sys.platform != "darwin", reason="launchd is macOS only")
@pytest.mark.parametrize("answer", ["loaded, never ran", "timed out"])
def test_doctor_asks_launchctl_once_for_both_launchd_checks(
    monkeypatch, caplog, answer
):
    """The drift check and the never-ran check asked launchctl the same
    question, so a launchctl that could not answer logged two WARNINGs."""
    import logging
    import subprocess
    from types import SimpleNamespace

    from jacked.service.autostart import AutostartInspection, AutostartState

    calls = []

    def launchctl(argv, **_kwargs):
        argv = list(argv)
        calls.append(argv)
        if argv[:2] != ["launchctl", "print"]:
            raise AssertionError(f"unexpected command: {argv}")
        if answer == "timed out":
            raise subprocess.TimeoutExpired(argv, 10)
        return SimpleNamespace(
            returncode=0,
            stdout="gui/501/ai.hank.jacked = {\n\tstate = not running\n\truns = 0\n}\n",
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", launchctl)
    owned = AutostartInspection(AutostartState.OWNED_ENABLED)
    with caplog.at_level(logging.WARNING, logger="jacked.service.platform"):
        text = _doctor(
            patch("jacked.service.process.is_port_available", return_value=True),
            patch("jacked.service.platform.inspect_autostart", return_value=owned),
            patch("jacked.service.platform.launchd_job_probe", _real_launchd_job_probe),
            patch("jacked.service.platform.supervision_drift", _real_supervision_drift),
            patch(
                "jacked.service.platform.launchd_job_never_ran",
                _real_launchd_job_never_ran,
            ),
        )

    assert len(calls) == 1, calls
    warnings = [
        record.getMessage()
        for record in caplog.records
        if record.name == "jacked.service.platform" and record.levelno == logging.WARNING
    ]
    if answer == "timed out":
        assert warnings == [
            "launchctl could not be asked about the jacked job (TimeoutExpired); "
            "autostart supervision was not checked"
        ]
        assert "never ran" not in text
    else:
        assert warnings == []
        assert "The launchd job is loaded but never ran." in text


# --- hook and settings integrity line (#99i) ---------------------------------

@pytest.fixture
def integrity_home(tmp_path, monkeypatch):
    """A scratch jacked home, and a free port so doctor never probes a live service."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("JACKED_HOME", str(home))
    monkeypatch.setattr("jacked.service.process.is_port_available", lambda *a, **k: True)
    return home


def test_doctor_prints_the_integrity_ok_line(integrity_home):
    from jacked import integrity
    from jacked.cli import main
    from jacked.memory import settings_io

    path = settings_io.settings_path(integrity_home)
    settings_io.write_settings(path, {})
    time = integrity.load_baseline(path)["log"][-1]["time"]

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert f"Hook/settings integrity: OK (baseline {time})" in result.output


def test_doctor_prints_each_integrity_finding_unwrapped(integrity_home):
    import json

    from jacked.cli import main
    from jacked.memory import settings_io

    path = settings_io.settings_path(integrity_home)
    settings_io.write_settings(path, {})
    path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
        {"hooks": [{"type": "command", "command": "python3 /tmp/payload.py"}]},
    ]}}), encoding="utf-8")

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Hook/settings integrity: 1 change outside jacked" in result.output
    assert ("  jacked integrity: settings.json changed outside jacked; "
            "UserPromptSubmit hook added: python3 /tmp/payload.py") in result.output
    assert "  Review each change. If you made it, run: jacked integrity accept" in result.output


def test_doctor_without_a_baseline_says_so(integrity_home):
    from jacked.cli import main

    result = CliRunner().invoke(main, ["doctor"])

    assert result.exit_code == 0, result.output
    assert "Hook/settings integrity: no baseline yet" in result.output
