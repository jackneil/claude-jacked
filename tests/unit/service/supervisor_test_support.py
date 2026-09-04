"""Shared builders for native supervisor tests."""

import hashlib
import os
import tempfile
from pathlib import Path

from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors import (
    render_launchd,
    render_systemd_user,
    render_task_scheduler,
)


def _secure_test_path(path: Path) -> None:
    if os.name == "nt":
        from jacked.service.windows_security import secure_windows_path

        secure_windows_path(path)


def make_spec(kind, *, launcher_path=None, launcher_hash="c" * 64):
    if launcher_path is None:
        launcher_path = os.path.join(tempfile.gettempdir(), "jacked", "launcher-v2")
    elif Path(launcher_path).exists():
        _secure_test_path(Path(launcher_path))
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


def installed_artifact(tmp_path, kind):
    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    spec = make_spec(
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
    _secure_test_path(path)
    return spec, environment, rendered, path
