import hashlib
from types import SimpleNamespace
from unittest.mock import Mock
from xml.etree import ElementTree

import pytest

from jacked.service.spec import SupervisorKind
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
from jacked.service.supervisors._transition import SupervisorTransitionLease
from tests.unit.service.supervisor_test_support import (
    installed_artifact as _installed_artifact,
)
from tests.unit.service.supervisor_test_support import make_spec as _spec


def _missing_systemd_runner():
    return Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=(
                    "LoadState=not-found\nFragmentPath=\n"
                    "ActiveState=inactive\nUnitFileState=disabled\n"
                ),
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
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


@pytest.mark.parametrize("operation", ["install", "restart", "uninstall"])
def test_all_native_mutators_share_one_transition_lease(tmp_path, operation):
    spec, environment, _, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    runner = Mock()

    with SupervisorTransitionLease(path, spec.service_id):
        if operation == "install":
            result = install_owned_supervisor(
                spec, path, environment=environment, run=runner
            )
        elif operation == "restart":
            result = restart_owned_supervisor(
                spec, path, environment=environment, run=runner
            )
        else:
            result = uninstall_owned_supervisor(
                spec, path, environment=environment, run=runner
            )

    assert result.ok is False
    assert result.action == "refused"
    assert "transition" in result.reason
    runner.assert_not_called()


def test_restart_invokes_exact_owned_supervisor(tmp_path):
    from unittest.mock import Mock

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
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=(
                    f"LoadState=loaded\nFragmentPath={path}\nNeedDaemonReload=no\n"
                ),
            ),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )
    result = restart_owned_supervisor(
        spec,
        path,
        environment={"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"},
        run=runner,
    )
    assert result.ok is True
    assert runner.call_args_list[1].args[0] == [
        "systemctl",
        "--user",
        "restart",
        "jacked.service",
    ]


def test_install_reconciles_before_systemd_activation(tmp_path):
    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    spec = _spec(
        SupervisorKind.SYSTEMD_USER,
        launcher_path=launcher,
        launcher_hash=hashlib.sha256(b"launcher").hexdigest(),
    )
    artifact = tmp_path / "jacked.service"
    runner = _missing_systemd_runner()

    result = install_owned_supervisor(
        spec,
        artifact,
        environment={"HOME": "/tmp/user", "PATH": "/usr/bin:/bin"},
        run=runner,
    )

    assert result.ok is True
    assert artifact.exists()
    assert runner.call_args_list[1].args[0] == [
        "systemctl",
        "--user",
        "daemon-reload",
    ]
    assert runner.call_args_list[2].args[0] == [
        "systemctl",
        "--user",
        "enable",
        "jacked.service",
    ]
    assert runner.call_args_list[3].args[0] == [
        "systemctl",
        "--user",
        "restart",
        "jacked.service",
    ]


def test_install_systemd_refuses_foreign_loaded_fragment_before_write(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
    path.write_bytes(previous)
    runner = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout=(
                "LoadState=loaded\nFragmentPath=/tmp/foreign.service\n"
                "ActiveState=active\nUnitFileState=enabled\n"
            ),
        )
    )

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert "identity differs" in result.reason
    assert path.read_bytes() == previous
    assert runner.call_count == 1


def test_install_systemd_restores_old_generation_after_restart_failure(tmp_path):
    spec, environment, rendered, path = _installed_artifact(
        tmp_path, SupervisorKind.SYSTEMD_USER
    )
    previous = rendered.content.replace(
        spec.generation.encode(), b"0" * len(spec.generation)
    )
    path.write_bytes(previous)
    runner = Mock(
        side_effect=[
            SimpleNamespace(
                returncode=0,
                stdout=(
                    f"LoadState=loaded\nFragmentPath={path}\n"
                    "ActiveState=active\nUnitFileState=enabled\n"
                ),
            ),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=5, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
    )

    result = install_owned_supervisor(spec, path, environment=environment, run=runner)

    assert result.ok is False
    assert "previous systemd state restored" in result.reason
    assert path.read_bytes() == previous
    assert runner.call_args_list[4].args[0][-1] == "daemon-reload"
    assert runner.call_args_list[5].args[0][-2:] == ["enable", "jacked.service"]
    assert runner.call_args_list[6].args[0][-2:] == ["restart", "jacked.service"]
