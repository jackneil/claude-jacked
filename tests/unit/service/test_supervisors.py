import os
import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors import (
    ArtifactDisposition,
    inspect_artifact,
    install_owned_supervisor,
    reconcile_artifact,
    render_launchd,
    render_systemd_user,
    render_task_scheduler,
    restart_owned_supervisor,
)
from jacked.service.supervisors.uninstall import uninstall_owned_supervisor


def _spec(kind, *, launcher_path="/opt/jacked/launcher-v2", launcher_hash="c" * 64):
    return ServiceSpec(
        service_id="ai.hank.jacked",
        protocol_version=2,
        build_version="0.99.0",
        runtime_path=os.path.realpath(os.sys.executable),
        launcher_path=str(launcher_path),
        launcher_sha256=launcher_hash,
        supervisor=kind,
        arguments=("-I", "-m", "jacked", "service", "start"),
    )


@pytest.mark.parametrize(
    ("kind", "renderer", "needle"),
    [
        (SupervisorKind.LAUNCHD, render_launchd, b"/usr/bin/env"),
        (
            SupervisorKind.SYSTEMD_USER,
            render_systemd_user,
            b"ExecStart=/usr/bin/env -i",
        ),
        (SupervisorKind.TASK_SCHEDULER, render_task_scheduler, b"IgnoreNew"),
    ],
)
def test_renderers_embed_exact_generation_and_clean_launch(kind, renderer, needle):
    spec = _spec(kind)
    environment = {"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"}
    if kind is SupervisorKind.TASK_SCHEDULER:
        environment["JACKED_SERVICE_USER"] = "sid:S-1-5-21-123"
    artifact = renderer(spec, environment=environment)
    assert spec.generation.encode() in artifact.content
    assert needle in artifact.content
    assert b"OPENAI_API_KEY" not in artifact.content
    if kind is SupervisorKind.TASK_SCHEDULER:
        assert b"ExecutionPolicy Bypass" in artifact.content
        assert b"ExecutionPolicy AllSigned" not in artifact.content
        assert b"windows/2004/02/mit/task" in artifact.content
        root = ElementTree.fromstring(artifact.content)
        namespace = {"t": "http://schemas.microsoft.com/windows/2004/02/mit/task"}
        assert (
            root.findtext("./t:Principals/t:Principal/t:UserId", namespaces=namespace)
            == "S-1-5-21-123"
        )
        assert root.find("./t:Actions", namespace).attrib["Context"] == "CurrentUser"


def test_reconcile_is_idempotent_and_refuses_foreign_artifact(tmp_path):
    spec = _spec(SupervisorKind.SYSTEMD_USER)
    artifact = render_systemd_user(
        spec, environment={"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"}
    )
    path = tmp_path / "jacked.service"
    assert inspect_artifact(path, artifact).disposition is ArtifactDisposition.MISSING
    assert (
        reconcile_artifact(path, artifact).disposition is ArtifactDisposition.INSTALLED
    )
    assert (
        reconcile_artifact(path, artifact).disposition is ArtifactDisposition.MATCHING
    )

    path.write_text("[Unit]\nDescription=someone else's service\n")
    before = path.read_bytes()
    result = reconcile_artifact(path, artifact)
    assert result.disposition is ArtifactDisposition.FOREIGN
    assert path.read_bytes() == before


def test_restart_never_invokes_supervisor_for_foreign_artifact(tmp_path):
    import hashlib
    from unittest.mock import Mock

    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    spec = _spec(
        SupervisorKind.SYSTEMD_USER,
        launcher_path=launcher,
        launcher_hash=hashlib.sha256(b"launcher").hexdigest(),
    )
    path = tmp_path / "jacked.service"
    path.write_text("[Unit]\nDescription=foreign\n")
    runner = Mock()
    result = restart_owned_supervisor(
        spec,
        path,
        environment={"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"},
        run=runner,
    )
    assert result.ok is False
    assert result.action == "refused"
    runner.assert_not_called()


def test_restart_invokes_exact_owned_supervisor(tmp_path):
    import hashlib
    from unittest.mock import MagicMock, Mock

    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    spec = _spec(
        SupervisorKind.SYSTEMD_USER,
        launcher_path=launcher,
        launcher_hash=hashlib.sha256(b"launcher").hexdigest(),
    )
    artifact = render_systemd_user(
        spec, environment={"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"}
    )
    path = tmp_path / "jacked.service"
    reconcile_artifact(path, artifact)
    runner = Mock(return_value=MagicMock(returncode=0))
    result = restart_owned_supervisor(
        spec,
        path,
        environment={"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"},
        run=runner,
    )
    assert result.ok is True
    assert runner.call_args.args[0] == [
        "systemctl",
        "--user",
        "restart",
        "jacked.service",
    ]


def test_install_reconciles_before_systemd_activation(tmp_path):
    import hashlib
    from types import SimpleNamespace
    from unittest.mock import Mock

    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    spec = _spec(
        SupervisorKind.SYSTEMD_USER,
        launcher_path=launcher,
        launcher_hash=hashlib.sha256(b"launcher").hexdigest(),
    )
    artifact = tmp_path / "jacked.service"
    runner = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))

    result = install_owned_supervisor(
        spec,
        artifact,
        environment={"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"},
        run=runner,
    )

    assert result.ok is True
    assert artifact.exists()
    assert runner.call_args_list[0].args[0] == [
        "systemctl",
        "--user",
        "daemon-reload",
    ]
    assert runner.call_args_list[1].args[0] == [
        "systemctl",
        "--user",
        "enable",
        "--now",
        "jacked.service",
    ]


def _installed_artifact(tmp_path, kind):
    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    spec = _spec(
        kind,
        launcher_path=launcher,
        launcher_hash=hashlib.sha256(b"launcher").hexdigest(),
    )
    environment = {"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"}
    if kind is SupervisorKind.TASK_SCHEDULER:
        environment["JACKED_SERVICE_USER"] = "sid:S-1-5-21-123"
        rendered = render_task_scheduler(spec, environment=environment)
        path = tmp_path / "jacked-task.xml"
    elif kind is SupervisorKind.LAUNCHD:
        rendered = render_launchd(spec, environment=environment)
        path = tmp_path / "ai.hank.jacked.plist"
    else:
        rendered = render_systemd_user(spec, environment=environment)
        path = tmp_path / "jacked.service"
    path.write_bytes(rendered.content)
    path.chmod(0o600)
    return spec, environment, rendered, path


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
