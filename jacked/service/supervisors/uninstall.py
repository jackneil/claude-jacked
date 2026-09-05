"""Evidence-qualified native supervisor removal."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from pathlib import Path
from typing import Any

from jacked.service.spec import ServiceSpec, SupervisorKind

from ._artifacts import (
    ArtifactDisposition,
    SupervisorAction,
    extract_marker,
    inspect_artifact,
)
from ._transition import SupervisorTransitionLease, TransitionBusy
from . import render_for_spec


def uninstall_owned_supervisor(
    spec: ServiceSpec,
    artifact_path: Path,
    *,
    environment: dict[str, str],
    run: Any | None = None,
    uid: int | None = None,
) -> SupervisorAction:
    """Unregister under the same service-scoped lease used by install/restart."""
    run = subprocess.run if run is None else run

    expected = render_for_spec(spec, environment=environment)
    try:
        with SupervisorTransitionLease(artifact_path, spec.service_id):
            return _uninstall_locked(spec, artifact_path, expected, run, uid)
    except TransitionBusy:
        return _refused(artifact_path, "another native transition is active")
    except OSError as exc:
        return _refused(artifact_path, f"transition failed: {type(exc).__name__}")


def _uninstall_locked(
    spec: ServiceSpec,
    artifact_path: Path,
    expected,
    run: Any,
    uid: int | None,
) -> SupervisorAction:
    inspection = inspect_artifact(artifact_path, expected)
    if inspection.disposition not in {
        ArtifactDisposition.MATCHING,
        ArtifactDisposition.OWNED_DRIFT,
        ArtifactDisposition.MISSING,
    }:
        return _refused(
            artifact_path,
            f"supervisor artifact is {inspection.disposition.value}",
        )

    original = (
        None
        if inspection.disposition is ArtifactDisposition.MISSING
        else artifact_path.read_bytes()
    )
    marker = (
        expected.marker
        if original is None
        else extract_marker(original, spec.supervisor)
    )
    if marker is None:
        return _refused(artifact_path, "supervisor ownership marker is invalid")
    common = {"capture_output": True, "text": True, "timeout": 15, "check": False}
    try:
        if spec.supervisor is SupervisorKind.LAUNCHD:
            action = _uninstall_launchd(
                spec, artifact_path, original, marker, run, common, uid
            )
        elif spec.supervisor is SupervisorKind.SYSTEMD_USER:
            action = _uninstall_systemd(spec, artifact_path, original, run, common)
        elif spec.supervisor is SupervisorKind.TASK_SCHEDULER:
            action = _uninstall_task(spec, artifact_path, marker, run, common)
        else:
            return _refused(artifact_path, "manual services have no native artifact")
    except (OSError, subprocess.SubprocessError) as exc:
        return _refused(artifact_path, f"supervisor check failed: {type(exc).__name__}")
    if not action.ok:
        return action
    removed = _remove_exact_artifact(artifact_path, original, action.reason)
    if not removed.ok or spec.supervisor is not SupervisorKind.SYSTEMD_USER:
        return removed
    try:
        reloaded = run(["systemctl", "--user", "daemon-reload"], **common)
    except (OSError, subprocess.SubprocessError) as exc:
        return _refused(artifact_path, f"systemd reload failed: {type(exc).__name__}")
    if reloaded.returncode != 0:
        return _refused(
            artifact_path, f"systemd reload exit {reloaded.returncode} after removal"
        )
    return removed


def _uninstall_launchd(
    spec, path, original, marker, run, common, uid
) -> SupervisorAction:
    effective_uid = os.getuid() if uid is None else uid
    domain = f"gui/{effective_uid}/{spec.service_id}"
    loaded = run(["launchctl", "print", domain], **common)
    if loaded.returncode == 113:
        return SupervisorAction(True, "uninstall", "launchd job was not loaded")
    if loaded.returncode != 0:
        return _refused(path, f"launchctl inspection exit {loaded.returncode}")
    launcher = (
        spec.launcher_path
        if original is None
        else _launchd_launcher(original, len(spec.arguments))
    )
    if not launcher or not all(
        str(value) in loaded.stdout for value in (marker["generation"], launcher)
    ):
        return _refused(path, "loaded launchd job identity is foreign")
    stopped = run(["launchctl", "bootout", domain], **common)
    if stopped.returncode != 0:
        return _refused(path, f"launchctl exit {stopped.returncode}")
    return SupervisorAction(True, "uninstall", "stopped and removed launchd job")


def _uninstall_systemd(spec, path, original, run, common) -> SupervisorAction:
    loaded = run(
        [
            "systemctl",
            "--user",
            "show",
            "jacked.service",
            "--property=LoadState",
            "--property=FragmentPath",
        ],
        **common,
    )
    if loaded.returncode != 0:
        return _refused(path, f"systemctl inspection exit {loaded.returncode}")
    properties = dict(
        line.split("=", 1) for line in loaded.stdout.splitlines() if "=" in line
    )
    fragment = properties.get("FragmentPath", "")
    load_state = properties.get("LoadState", "")
    if fragment and Path(fragment).resolve() != path.resolve():
        return _refused(path, f"loaded systemd unit resolves to {fragment}")
    if not fragment and load_state == "not-found":
        return SupervisorAction(True, "uninstall", "systemd unit was not loaded")
    if original is None:
        return _refused(path, "loaded systemd unit has no owned on-disk artifact")
    if not fragment:
        return _refused(path, "systemd unit origin is indeterminate")
    stopped = run(
        ["systemctl", "--user", "disable", "--now", "jacked.service"], **common
    )
    if stopped.returncode != 0:
        return _refused(path, f"systemctl exit {stopped.returncode}")
    return SupervisorAction(True, "uninstall", "disabled and stopped systemd unit")


def _uninstall_task(spec, path, marker, run, common) -> SupervisorAction:
    queried = run(_task_query_command(spec.service_id), **common)
    if queried.returncode == 3:
        return SupervisorAction(True, "uninstall", "Task Scheduler task was absent")
    if queried.returncode != 0:
        return _refused(path, f"Task Scheduler inspection exit {queried.returncode}")
    registered = extract_marker(queried.stdout.encode("utf-8"), spec.supervisor)
    if registered != marker:
        return _refused(path, "registered task identity is foreign")
    # /End returns 1 when no instance is running. Deletion is still required
    # and is the authoritative unregister operation for this exact task.
    ended = run(["schtasks.exe", "/End", "/TN", spec.service_id], **common)
    if ended.returncode not in {0, 1}:
        return _refused(path, f"Task Scheduler stop exit {ended.returncode}")
    deleted = run(["schtasks.exe", "/Delete", "/TN", spec.service_id, "/F"], **common)
    if deleted.returncode != 0:
        return _refused(path, f"Task Scheduler exit {deleted.returncode}")
    return SupervisorAction(True, "uninstall", "unregistered Task Scheduler task")


def _task_query_command(service_id: str) -> list[str]:
    task_name = service_id.replace("'", "''")
    script = (
        "$ErrorActionPreference='Stop';"
        "$service=New-Object -ComObject 'Schedule.Service';"
        "$service.Connect();$folder=$service.GetFolder('\\');"
        f"try{{$task=$folder.GetTask('{task_name}')}}"
        "catch{if($_.Exception.HResult -eq -2147024894){exit 3}else{exit 4}};"
        "[Console]::Out.Write($task.Xml)"
    )
    return [
        "powershell.exe",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]


def _remove_exact_artifact(
    path: Path, original: bytes | None, reason: str
) -> SupervisorAction:
    if original is None and not path.exists() and not path.is_symlink():
        return SupervisorAction(True, "uninstall", reason)
    try:
        status = path.lstat()
        current = path.read_bytes()
    except OSError:
        return _refused(path, "artifact changed during uninstall")
    if (
        original is None
        or current != original
        or not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or (
            os.name == "posix"
            and (status.st_uid != os.getuid() or status.st_mode & 0o022)
        )
    ):
        return _refused(path, "artifact changed during uninstall")
    try:
        path.unlink()
    except OSError as exc:
        return _refused(path, f"artifact removal failed: {type(exc).__name__}")
    return SupervisorAction(True, "uninstall", f"{reason}; removed {path}")


def _launchd_launcher(content: bytes, argument_count: int) -> str | None:
    try:
        arguments = plistlib.loads(content)["ProgramArguments"]
        # The immutable launcher is followed by the exact runtime, its bound
        # target, and the Python argument vector.
        return arguments[-argument_count - 3]
    except (
        KeyError,
        IndexError,
        TypeError,
        plistlib.InvalidFileException,
    ):
        return None


def _refused(path: Path, reason: str) -> SupervisorAction:
    return SupervisorAction(
        False,
        "refused",
        f"{reason}. Inspect and back up {path}, then run `jacked service recover`.",
    )
