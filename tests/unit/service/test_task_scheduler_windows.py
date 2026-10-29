"""Real Task Scheduler round trip (#165i). Windows only.

The unit tests mock schtasks, so none of them has seen its real XML parser.
This test registers a throwaway task from the rendered artifact, reads it
back through the same query helper the installer uses, registers the
read-back bytes again (the rollback path), and deletes the task.
"""

import subprocess
import sys
import uuid

import pytest

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="Task Scheduler is Windows only"),
    # Runs the real schtasks.exe on purpose, against a throwaway task name.
    pytest.mark.real_process,
]


def _schtasks(*args):
    return subprocess.run(
        ["schtasks.exe", *args],
        capture_output=True,
        text=True,
        errors="replace",
        timeout=60,
        check=False,
    )


def test_rendered_task_registers_and_reads_back_as_owned(tmp_path):
    from jacked.service.instance_storage import current_user_identity
    from jacked.service.spec import ServiceSpec, SupervisorKind
    from jacked.service.supervisors import extract_marker, render_task_scheduler
    from jacked.service.supervisors._task_xml import query_task_xml
    from jacked.service.supervisors.task_scheduler import task_action

    name = f"jacked-test-{uuid.uuid4().hex[:12]}"
    # A non-ASCII path (a user name such as "Jos\u00e9") must survive the
    # read-back exactly: the Arguments comparison decides ownership.
    launcher = tmp_path / "launcher-Jos\u00e9.ps1"
    launcher.write_text("exit 0\n", encoding="utf-8")
    spec = ServiceSpec(
        service_id=name,
        protocol_version=2,
        build_version="0.0.0",
        runtime_path=sys.executable,
        launcher_path=str(launcher),
        launcher_sha256="c" * 64,
        supervisor=SupervisorKind.TASK_SCHEDULER,
        arguments=("-I", "-m", "jacked", "service", "start"),
    )
    environment = {
        "HOME": str(tmp_path),
        "PATH": "C:\\Windows\\System32",
        "JACKED_SERVICE_USER": current_user_identity(),
    }
    rendered = render_task_scheduler(spec, environment=environment)
    artifact = tmp_path / "task.xml"
    artifact.write_bytes(rendered.content)
    try:
        created = _schtasks("/Create", "/TN", name, "/XML", str(artifact), "/F")
        assert created.returncode == 0, created.stderr

        # The read-back goes through the COM query (UTF-8): the first CI run
        # proved schtasks /Query /XML pipes 8-bit code-page text that cannot
        # carry this non-ASCII path exactly.
        queried = query_task_xml(subprocess.run, name, timeout=60)
        assert queried.returncode == 0, queried.stderr
        assert queried.xml is not None
        marker = extract_marker(queried.xml, SupervisorKind.TASK_SCHEDULER)
        assert marker == rendered.marker
        assert task_action(queried.xml) == task_action(rendered.content)
        assert "Jos\u00e9" in task_action(queried.xml)[1]

        # The installer keeps the read-back bytes as its rollback backup.
        backup = tmp_path / "backup.xml"
        backup.write_bytes(queried.xml)
        restored = _schtasks("/Create", "/TN", name, "/XML", str(backup), "/F")
        assert restored.returncode == 0, restored.stderr
    finally:
        _schtasks("/Delete", "/TN", name, "/F")

    gone = query_task_xml(subprocess.run, name, timeout=60)
    assert gone.absent and gone.xml is None, (gone.returncode, gone.stderr)
