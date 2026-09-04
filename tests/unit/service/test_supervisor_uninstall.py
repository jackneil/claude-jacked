from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from jacked.service.spec import SupervisorKind
from jacked.service.supervisors import (
    render_task_scheduler,
)
from jacked.service.supervisors.uninstall import uninstall_owned_supervisor
from tests.unit.service.supervisor_test_support import (
    installed_artifact as _installed_artifact,
)
from tests.unit.service.supervisor_test_support import make_spec as _spec


def test_uninstall_launchd_stops_exact_loaded_job_then_deletes_artifact(tmp_path):
    spec, environment, _, path = _installed_artifact(tmp_path, SupervisorKind.LAUNCHD)
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0, stdout=f"{spec.generation} {spec.launcher_path}"
            ),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = uninstall_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is True
    assert not path.exists()
    assert runner.call_args_list[1].args[0] == [
        "launchctl",
        "bootout",
        "gui/501/ai.hank.jacked",
    ]


def test_uninstall_launchd_refuses_foreign_loaded_job(tmp_path):
    spec, environment, _, path = _installed_artifact(tmp_path, SupervisorKind.LAUNCHD)
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout="foreign"))

    result = uninstall_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is False
    assert str(path) in result.reason
    assert path.exists()
    assert runner.call_count == 1


def test_uninstall_launchd_is_idempotent_when_job_and_artifact_absent(tmp_path):
    spec, environment, _, path = _installed_artifact(tmp_path, SupervisorKind.LAUNCHD)
    path.unlink()
    runner = Mock(return_value=SimpleNamespace(returncode=113, stdout=""))

    result = uninstall_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is True
    assert runner.call_count == 1


def test_uninstall_launchd_refuses_ambiguous_inspection_failure(tmp_path):
    spec, environment, _, path = _installed_artifact(tmp_path, SupervisorKind.LAUNCHD)
    runner = Mock(return_value=SimpleNamespace(returncode=5, stdout=""))

    result = uninstall_owned_supervisor(
        spec, path, environment=environment, run=runner, uid=501
    )

    assert result.ok is False
    assert path.exists()
    assert "inspection exit 5" in result.reason


def test_uninstall_systemd_disables_exact_fragment_then_reloads(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=f"LoadState=loaded\nFragmentPath={path}\n",
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is True
    assert not path.exists()
    assert runner.call_args_list[1].args[0] == [
        "systemctl",
        "--user",
        "disable",
        "--now",
        "jacked.service",
    ]
    assert runner.call_args_list[2].args[0] == [
        "systemctl",
        "--user",
        "daemon-reload",
    ]


def test_uninstall_systemd_refuses_different_loaded_fragment(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout="LoadState=loaded\nFragmentPath=/tmp/foreign.service\n",
        )
    )

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert path.exists()
    assert runner.call_count == 1


def test_uninstall_systemd_is_idempotent_when_unit_and_artifact_absent(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    path.unlink()
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0, stdout="LoadState=not-found\nFragmentPath=\n"
            ),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is True
    assert runner.call_count == 2
    assert runner.call_args_list[1].args[0] == [
        "systemctl",
        "--user",
        "daemon-reload",
    ]


def test_uninstall_accepts_proven_owned_previous_systemd_generation(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
    path.write_bytes(previous)
    path.chmod(0o600)
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=f"LoadState=loaded\nFragmentPath={path}\n",
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is True
    assert not path.exists()


def test_uninstall_task_stops_and_deletes_exact_registered_definition(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    runner = Mock(
        side_effect=[
            SimpleNamespace(returncode=0, stdout=rendered.content.decode()),
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is True
    assert not path.exists()
    assert runner.call_args_list[1].args[0] == [
        "schtasks.exe",
        "/End",
        "/TN",
        "ai.hank.jacked",
    ]
    assert runner.call_args_list[2].args[0] == [
        "schtasks.exe",
        "/Delete",
        "/TN",
        "ai.hank.jacked",
        "/F",
    ]


def test_uninstall_task_refuses_foreign_registered_definition(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    foreign = render_task_scheduler(
        _spec(SupervisorKind.TASK_SCHEDULER),
        environment={
            "HOME": "/tmp/user",
            "PATH": "/usr/bin:/bin",
            "JACKED_SERVICE_USER": "sid:S-1-5-21-123",
        },
    )
    runner = Mock(
        return_value=SimpleNamespace(returncode=0, stdout=foreign.content.decode())
    )

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert path.exists()
    assert runner.call_count == 1


def test_uninstall_task_is_idempotent_when_task_and_artifact_absent(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    path.unlink()
    runner = Mock(return_value=SimpleNamespace(returncode=3, stdout=""))

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is True
    assert runner.call_count == 1
    query = runner.call_args.args[0]
    assert query[:4] == [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
    ]


def test_uninstall_task_refuses_ambiguous_inspection_failure_when_absent(tmp_path):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.TASK_SCHEDULER
    )
    path.unlink()
    runner = Mock(return_value=SimpleNamespace(returncode=4, stdout=""))

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert "inspection exit 4" in result.reason
    assert str(path) in result.reason


@pytest.mark.parametrize(
    "kind",
    [
        SupervisorKind.LAUNCHD,
        SupervisorKind.SYSTEMD_USER,
        SupervisorKind.TASK_SCHEDULER,
    ],
)
def test_uninstall_never_invokes_supervisor_for_unmarked_legacy_artifact(
    tmp_path, kind
):
    spec, environment, _, path = _installed_artifact(tmp_path, kind)
    path.write_text("legacy or foreign definition")
    path.chmod(0o600)
    runner = Mock()

    result = uninstall_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert "recover" in result.reason
    assert str(path) in result.reason
    assert path.exists()
    runner.assert_not_called()
