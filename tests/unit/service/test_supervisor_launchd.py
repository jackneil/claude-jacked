import plistlib
import subprocess
from types import SimpleNamespace

import pytest
from unittest.mock import Mock, patch


from jacked.service.spec import SupervisorKind
from jacked.service.supervisors import (
    install_owned_supervisor,
    restart_owned_supervisor,
)
from tests.unit.service.supervisor_test_support import (
    installed_artifact as _installed_artifact,
)



@pytest.fixture
def _no_drain(monkeypatch):
    """Skip the bootout drain wait for tests that script launchctl replies
    positionally; the drain itself is covered by the simulator tests."""
    from jacked.service.supervisors import launchd as launchd_module

    monkeypatch.setattr(launchd_module, "_await_unloaded", lambda ctx, timeout=None: True)
    monkeypatch.setattr(launchd_module.time, "sleep", lambda _s: None)


def test_restart_refuses_loaded_systemd_definition_from_other_path(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nFragmentPath=/tmp/foreign\nNeedDaemonReload=no\n",
        )
    )

    result = restart_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert result.action == "refused"
    assert runner.call_count == 1


def test_install_launchd_kickstarts_matching_loaded_generation(tmp_path):
    spec, environment, _, path = _installed_artifact(tmp_path, SupervisorKind.LAUNCHD)
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0, stdout=f"{spec.generation} {spec.launcher_path}"
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is True
    assert runner.call_args_list[1].args[0] == [
        "launchctl",
        "enable",
        "gui/501/ai.hank.jacked",
    ]
    assert runner.call_args_list[2].args[0] == [
        "launchctl",
        "kickstart",
        "-k",
        "gui/501/ai.hank.jacked",
    ]


def test_install_launchd_creates_missing_launchagents_directory(tmp_path):
    spec, environment, _, _ = _installed_artifact(tmp_path, SupervisorKind.LAUNCHD)
    path = tmp_path / "fresh" / "LaunchAgents" / "ai.hank.jacked.plist"
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=113, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is True
    assert path.exists()


@pytest.mark.usefixtures("_no_drain")
def test_install_launchd_migrates_exact_known_legacy_definition(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    legacy_args = [
        "/Users/test/.local/bin/jacked",
        "service",
        "start",
        "--port",
        "8321",
    ]
    legacy = plistlib.dumps(
        {"Label": spec.service_id, "ProgramArguments": legacy_args, "RunAtLoad": True}
    )
    path.write_bytes(legacy)
    path.chmod(0o600)
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=" ".join(legacy_args)),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is True
    assert path.read_bytes() == rendered.content
    assert path.with_name(f"{path.name}.pre-v2").read_bytes() == legacy
    assert runner.call_args_list[1].args[0][:2] == ["launchctl", "bootout"]
    assert runner.call_args_list[2].args[0][:2] == ["launchctl", "enable"]
    assert runner.call_args_list[3].args[0][:2] == ["launchctl", "bootstrap"]


def test_install_launchd_resolves_bootstrap_timeout_from_loaded_identity(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    path.unlink()
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=113, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            subprocess.TimeoutExpired("launchctl", 15),
            SimpleNamespace(
                returncode=0, stdout=f"{spec.generation} {spec.launcher_path}"
            ),
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is True
    assert path.read_bytes() == rendered.content


@pytest.mark.usefixtures("_no_drain")
def test_install_launchd_boots_out_proven_old_generation_before_replacing_it(
    tmp_path,
):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    previous_generation = "0" * len(spec.generation)
    previous = rendered.content.replace(
        spec.generation.encode(), previous_generation.encode()
    )
    path.write_bytes(previous)
    path.chmod(0o600)
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=f"{previous_generation} {spec.launcher_path}",
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is True
    assert path.read_bytes() == rendered.content
    assert runner.call_args_list[2].args[0] == [
        "launchctl",
        "bootout",
        "gui/501/ai.hank.jacked",
    ]
    assert runner.call_args_list[3].args[0] == [
        "launchctl",
        "bootstrap",
        "gui/501",
        str(path),
    ]


@pytest.mark.usefixtures("_no_drain")
def test_install_launchd_restores_old_generation_when_new_bootstrap_fails(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    previous_generation = "0" * len(spec.generation)
    previous = rendered.content.replace(
        spec.generation.encode(), previous_generation.encode()
    )
    path.write_bytes(previous)
    path.chmod(0o600)
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=f"{previous_generation} {spec.launcher_path}",
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),  # retried once after the drain
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is False
    assert "previous supervisor restored" in result.reason
    assert path.read_bytes() == previous
    assert runner.call_args_list[5].args[0] == [
        "launchctl",
        "bootstrap",
        "gui/501",
        str(path),
    ]


@pytest.mark.usefixtures("_no_drain")
def test_install_launchd_removes_new_artifact_when_first_bootstrap_fails(tmp_path):
    spec, environment, _, path = _installed_artifact(tmp_path, SupervisorKind.LAUNCHD)
    path.unlink()
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=113, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),  # retried once after the drain
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is False
    assert "previous unloaded state restored" in result.reason
    assert not path.exists()


@pytest.mark.usefixtures("_no_drain")
def test_install_launchd_restores_unloaded_owned_drift_on_failure(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
    path.write_bytes(previous)
    path.chmod(0o600)
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=113, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),  # retried once after the drain
        ]
    )

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is False
    assert path.read_bytes() == previous


@pytest.mark.usefixtures("_no_drain")
def test_install_launchd_reloads_old_job_when_reconcile_raises(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    previous_generation = "0" * len(spec.generation)
    previous = rendered.content.replace(
        spec.generation.encode(), previous_generation.encode()
    )
    path.write_bytes(previous)
    path.chmod(0o600)
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=f"{previous_generation} {spec.launcher_path}",
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    with patch(
        "jacked.service.supervisors.launchd.reconcile_artifact",
        side_effect=OSError("disk full"),
    ):
        result = install_owned_supervisor(
            spec, path, environment=environment, run=runner, uid=501
        )

    assert result.ok is False
    assert "previous supervisor restored" in result.reason
    assert path.read_bytes() == previous
    assert runner.call_args_list[3].args[0][:2] == ["launchctl", "bootstrap"]


def test_install_launchd_refuses_drift_when_loaded_identity_is_not_the_old_artifact(
    tmp_path,
):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
    path.write_bytes(previous)
    path.chmod(0o600)
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="foreign"))

    result = install_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is False
    assert path.read_bytes() == previous
    assert runner.call_count == 1


def test_is_known_legacy_artifact_recognises_the_pre_v2_launchd_plist(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    path = tmp_path / "ai.hank.jacked.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hank.jacked",
                "ProgramArguments": [
                    "/Users/test/.local/bin/jacked",
                    "service",
                    "start",
                    "--port",
                    "8321",
                ],
                "RunAtLoad": True,
            }
        )
    )
    path.chmod(0o600)

    assert is_known_legacy_artifact(path, "ai.hank.jacked", SupervisorKind.LAUNCHD)


def test_is_known_legacy_artifact_rejects_a_foreign_launchd_plist(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    path = tmp_path / "ai.hank.jacked.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "ai.hank.jacked",
                "ProgramArguments": ["/usr/bin/curl", "https://example.invalid"],
            }
        )
    )
    path.chmod(0o600)

    assert not is_known_legacy_artifact(path, "ai.hank.jacked", SupervisorKind.LAUNCHD)


def test_is_known_legacy_artifact_rejects_a_different_label(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    path = tmp_path / "other.plist"
    path.write_bytes(
        plistlib.dumps(
            {
                "Label": "com.other.thing",
                "ProgramArguments": ["/usr/local/bin/jacked", "service", "start"],
            }
        )
    )
    path.chmod(0o600)

    assert not is_known_legacy_artifact(path, "ai.hank.jacked", SupervisorKind.LAUNCHD)


def test_is_known_legacy_artifact_rejects_the_owned_v2_artifact(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    spec, _environment, _rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )

    assert not is_known_legacy_artifact(path, spec.service_id, SupervisorKind.LAUNCHD)


def test_is_known_legacy_artifact_is_false_for_systemd_and_manual(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    spec, _environment, _rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )

    assert not is_known_legacy_artifact(
        path, spec.service_id, SupervisorKind.SYSTEMD_USER
    )
    assert not is_known_legacy_artifact(path, spec.service_id, SupervisorKind.MANUAL)


def test_is_known_legacy_artifact_is_false_for_a_missing_path(tmp_path):
    from jacked.service.supervisors import is_known_legacy_artifact

    assert not is_known_legacy_artifact(
        tmp_path / "absent.plist", "ai.hank.jacked", SupervisorKind.LAUNCHD
    )


def test_launchd_artifact_runs_the_tray_as_an_interactive_job(tmp_path):
    """Background jobs are CPU/IO throttled by launchd; the tray must bind a
    port inside its readiness window even on a loaded machine (2026-09-05)."""
    import hashlib
    import plistlib

    from jacked.service.spec import SupervisorKind
    from jacked.service.supervisors import render_for_spec
    from tests.unit.service.supervisor_test_support import make_spec

    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    spec = make_spec(
        SupervisorKind.LAUNCHD,
        launcher_path=launcher,
        launcher_hash=hashlib.sha256(b"launcher").hexdigest(),
    )
    artifact = render_for_spec(
        spec, environment={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    )
    payload = plistlib.loads(artifact.content)
    assert payload["ProcessType"] == "Interactive"


class _LaunchctlSim:
    """A launchctl that models bootout's asynchronous drain.

    After ``bootout`` the job name stays registered for ``drain_polls`` print
    calls; a bootstrap inside that window fails with EIO (5), exactly as
    launchd behaves (measured 2026-09-05: seconds of drain on a real tray).
    """

    def __init__(self, loaded_stdout: str, drain_polls: int = 2) -> None:
        self.loaded_stdout = loaded_stdout
        self.drain_polls = drain_polls
        self.loaded = True
        self.draining = 0
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        verb = argv[1]
        if verb == "print":
            if self.draining > 0:
                self.draining -= 1
                return SimpleNamespace(returncode=0, stdout=self.loaded_stdout)
            return SimpleNamespace(
                returncode=0 if self.loaded else 113,
                stdout=self.loaded_stdout if self.loaded else "",
            )
        if verb == "enable":
            return SimpleNamespace(returncode=0, stdout="")
        if verb == "bootout":
            self.loaded = False
            self.draining = self.drain_polls
            return SimpleNamespace(returncode=0, stdout="")
        if verb == "bootstrap":
            if self.draining > 0:
                self.draining -= 1
                return SimpleNamespace(returncode=5, stdout="")
            self.loaded = True
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(f"unexpected launchctl verb {verb}")


def _generation_change(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.LAUNCHD
    )
    previous_generation = "0" * len(spec.generation)
    previous = rendered.content.replace(
        spec.generation.encode(), previous_generation.encode()
    )
    path.write_bytes(previous)
    path.chmod(0o600)
    return spec, environment, rendered, path, previous_generation


def test_generation_change_waits_for_the_old_job_to_drain_before_bootstrap(
    tmp_path, monkeypatch
):
    from jacked.service.supervisors import launchd as launchd_module

    monkeypatch.setattr(launchd_module.time, "sleep", lambda _s: None)
    spec, environment, rendered, path, previous_generation = _generation_change(tmp_path)
    sim = _LaunchctlSim(f"{previous_generation} {spec.launcher_path}", drain_polls=3)

    result = install_owned_supervisor(
        spec, path, environment=environment, run=sim, uid=501
    )

    assert result.ok, result.reason
    assert path.read_bytes() == rendered.content
    verbs = [call[1] for call in sim.calls]
    bootout = verbs.index("bootout")
    bootstrap = verbs.index("bootstrap")
    # Every call between bootout and bootstrap is a drain probe.
    assert set(verbs[bootout + 1 : bootstrap]) == {"print"}
    assert verbs.count("bootstrap") == 1


def test_bootstrap_eio_during_drain_is_retried_once_the_name_is_free(
    tmp_path, monkeypatch
):
    from jacked.service.supervisors import launchd as launchd_module

    monkeypatch.setattr(launchd_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(launchd_module, "_await_unloaded", lambda ctx, timeout=None: True)
    spec, environment, rendered, path, previous_generation = _generation_change(tmp_path)
    sim = _LaunchctlSim(f"{previous_generation} {spec.launcher_path}", drain_polls=1)
    # The await is stubbed as "free", so the first bootstrap still hits EIO.
    result = install_owned_supervisor(
        spec, path, environment=environment, run=sim, uid=501
    )

    assert result.ok, result.reason
    assert [c[1] for c in sim.calls].count("bootstrap") == 2


def test_drain_timeout_rolls_back_to_the_previous_generation(tmp_path, monkeypatch):
    from jacked.service.supervisors import launchd as launchd_module

    monkeypatch.setattr(launchd_module.time, "sleep", lambda _s: None)
    monkeypatch.setattr(launchd_module, "BOOTOUT_DRAIN_TIMEOUT_SECONDS", 0.0)
    spec, environment, rendered, path, previous_generation = _generation_change(tmp_path)
    previous = path.read_bytes()
    sim = _LaunchctlSim(f"{previous_generation} {spec.launcher_path}", drain_polls=50)

    result = install_owned_supervisor(
        spec, path, environment=environment, run=sim, uid=501
    )

    assert result.ok is False
    assert "did not unload" in result.reason
    assert path.read_bytes() == previous

