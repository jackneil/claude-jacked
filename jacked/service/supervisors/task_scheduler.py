"""Transactional Windows Task Scheduler installation and rollback."""

from __future__ import annotations

import os
import re
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from jacked.service.spec import ServiceSpec
from jacked.service.supervisors._artifacts import (
    ArtifactDisposition,
    SupervisorAction,
    SupervisorArtifact,
    extract_marker,
    reconcile_artifact,
    restore_artifact,
    snapshot_artifact,
)
from jacked.service.supervisors._helpers import atomic_write

_RUN_OPTIONS = {
    "capture_output": True,
    "text": True,
    "timeout": 15,
    "check": False,
}


@dataclass(frozen=True)
class _InstallRequest:
    spec: ServiceSpec
    path: Path
    expected: SupervisorArtifact
    run: Any


@dataclass(frozen=True)
class _Evidence:
    previous_artifact: bytes | None
    previous_task: bytes | None
    legacy_path: Path
    legacy_content: bytes | None


@dataclass(frozen=True)
class _Transition:
    request: _InstallRequest
    evidence: _Evidence
    task_backup: Path | None
    legacy_backup: Path | None
    prior_running: bool = False


def install_task_supervisor(
    spec: ServiceSpec,
    path: Path,
    expected: SupervisorArtifact,
    *,
    run: Any,
) -> SupervisorAction:
    """Replace one exact owned task and restore all prior state on failure."""

    request = _InstallRequest(spec, path, expected, run)
    inspection, artifact = snapshot_artifact(path, expected)
    if inspection.disposition is ArtifactDisposition.FOREIGN:
        return SupervisorAction(False, "refused", inspection.reason)
    legacy_path = _legacy_windows_startup_path()
    legacy_content = _known_legacy_vbs(legacy_path)
    if (legacy_path.exists() or legacy_path.is_symlink()) and legacy_content is None:
        return SupervisorAction(False, "refused", "Startup VBS identity is foreign")
    previous_task = _inspect_registered_task(request)
    if isinstance(previous_task, SupervisorAction):
        return previous_task
    evidence = _Evidence(artifact, previous_task, legacy_path, legacy_content)
    prepared = _prepare_transition(request, evidence)
    if isinstance(prepared, SupervisorAction):
        return prepared
    return _reconcile_and_activate(prepared)


def _inspect_registered_task(
    request: _InstallRequest,
) -> bytes | None | SupervisorAction:
    try:
        queried = request.run(
            ["schtasks.exe", "/Query", "/TN", request.spec.service_id, "/XML"],
            **_RUN_OPTIONS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "install", type(exc).__name__)
    if queried.returncode == 1:
        return None
    if queried.returncode != 0:
        return SupervisorAction(
            False,
            "refused",
            f"Task Scheduler inspection exit {queried.returncode}",
        )
    content = queried.stdout.encode("utf-8")
    marker = extract_marker(content, request.spec.supervisor)
    if marker is None or any(
        marker.get(key) != request.expected.marker[key]
        for key in ("owner", "service_id")
    ) or task_action(content) is None:
        return SupervisorAction(False, "refused", "registered task is foreign")
    return content


def _prepare_transition(
    request: _InstallRequest, evidence: _Evidence
) -> _Transition | SupervisorAction:
    legacy_backup = (
        _retire_legacy_vbs(evidence.legacy_path, evidence.legacy_content)
        if evidence.legacy_content
        else None
    )
    if evidence.legacy_content and legacy_backup is None:
        return SupervisorAction(False, "refused", "legacy Startup VBS backup failed")
    task_backup = (
        _stage_task_backup(request.path, evidence.previous_task)
        if evidence.previous_task
        else None
    )
    if evidence.previous_task and task_backup is None:
        _restore_legacy_vbs(evidence.legacy_path, legacy_backup)
        return SupervisorAction(False, "refused", "registered task backup failed")
    return _Transition(request, evidence, task_backup, legacy_backup)


def _reconcile_and_activate(transition: _Transition) -> SupervisorAction:
    request = transition.request
    try:
        reconciled = reconcile_artifact(request.path, request.expected)
    except OSError as exc:
        return _failed_transition(transition, type(exc).__name__, False)
    if reconciled.disposition is ArtifactDisposition.FOREIGN:
        is_restored = _restore_legacy_vbs(
            transition.evidence.legacy_path, transition.legacy_backup
        )
        _remove_task_backup(
            transition.task_backup, transition.evidence.previous_task
        )
        reason = reconciled.reason
        if not is_restored:
            reason += "; legacy backup restore failed"
        return SupervisorAction(False, "refused", reason)
    return _activate_task(transition)


def _activate_task(transition: _Transition) -> SupervisorAction:
    request = transition.request
    try:
        created = request.run(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                request.spec.service_id,
                "/XML",
                str(request.path),
                "/F",
            ],
            **_RUN_OPTIONS,
        )
        if created.returncode != 0:
            return _failed_transition(
                transition, f"supervisor exit {created.returncode}", True
            )
        if transition.evidence.previous_task is not None:
            ended = request.run(
                ["schtasks.exe", "/End", "/TN", request.spec.service_id],
                **_RUN_OPTIONS,
            )
            if ended.returncode not in {0, 1}:
                return _failed_transition(
                    transition, f"supervisor exit {ended.returncode}", True
                )
            transition = replace(transition, prior_running=ended.returncode == 0)
        started = request.run(
            ["schtasks.exe", "/Run", "/TN", request.spec.service_id],
            **_RUN_OPTIONS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _failed_transition(transition, type(exc).__name__, True)
    if started.returncode != 0:
        return _failed_transition(
            transition, f"supervisor exit {started.returncode}", True
        )
    _remove_task_backup(transition.task_backup, transition.evidence.previous_task)
    return SupervisorAction(
        True, "install", f"activated {request.spec.generation[:12]}"
    )


def task_action(content: bytes) -> tuple[str, str, str] | None:
    """Extract the executable identity fields from a task definition."""

    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return None
    values = {}
    for item in root.iter():
        name = item.tag.rsplit("}", 1)[-1]
        if name in {"Command", "Arguments", "WorkingDirectory"}:
            values[name] = item.text or ""
    if "Command" not in values or "Arguments" not in values:
        return None
    return (
        values["Command"],
        values["Arguments"],
        values.get("WorkingDirectory", ""),
    )


def _legacy_windows_startup_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "jacked.vbs"
    )


def _known_legacy_vbs(path: Path) -> bytes | None:
    try:
        status = path.lstat()
        content = path.read_bytes()
    except (FileNotFoundError, OSError):
        return None
    if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
        return None
    if os.name == "posix" and status.st_uid != os.getuid():
        return None
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return None
    pattern = re.compile(
        r'^Set WshShell = CreateObject\("WScript\.Shell"\)\r?\n'
        r'^WshShell\.Run """[^\r\n"]+"" (?:-m jacked )?service start'
        r'(?: --(?:host|port) [^\s"&|;]+)*", 0, False\r?\n?$',
        re.MULTILINE | re.IGNORECASE,
    )
    return content if pattern.fullmatch(text) else None


def _retire_legacy_vbs(path: Path, content: bytes) -> Path | None:
    backup = path.with_name(f"{path.name}.pre-v2")
    try:
        if backup.exists() and backup.read_bytes() != content:
            return None
        os.replace(path, backup)
    except OSError:
        return None
    return backup if backup.read_bytes() == content else None


def _restore_legacy_vbs(path: Path, backup: Path | None) -> bool:
    if backup is None:
        return True
    if path.exists() or not backup.exists():
        return False
    try:
        os.replace(backup, path)
    except OSError:
        return False
    return path.exists() and not backup.exists()


def _rollback_new_task(transition: _Transition) -> bool:
    request = transition.request
    try:
        checked = request.run(
            ["schtasks.exe", "/Query", "/TN", request.spec.service_id, "/XML"],
            **_RUN_OPTIONS,
        )
        if checked.returncode in {1, 3}:
            return True
        registered = checked.stdout.encode("utf-8")
        if checked.returncode != 0 or (
            extract_marker(registered, request.spec.supervisor)
            != request.expected.marker
        ) or task_action(registered) != task_action(request.expected.content):
            return False
        deleted = request.run(
            ["schtasks.exe", "/Delete", "/TN", request.spec.service_id, "/F"],
            **_RUN_OPTIONS,
        )
        return deleted.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _stage_task_backup(path: Path, content: bytes) -> Path | None:
    backup = path.with_name(f".{path.name}.transition-backup")
    try:
        if backup.exists():
            return backup if backup.read_bytes() == content else None
        atomic_write(backup, content)
        return backup if backup.read_bytes() == content else None
    except OSError:
        return None


def _restore_previous_task(transition: _Transition) -> bool:
    request = transition.request
    backup = transition.task_backup
    if backup is None or not backup.exists():
        return False
    try:
        restored = request.run(
            [
                "schtasks.exe",
                "/Create",
                "/TN",
                request.spec.service_id,
                "/XML",
                str(backup),
                "/F",
            ],
            **_RUN_OPTIONS,
        )
        if restored.returncode != 0 or not transition.prior_running:
            return restored.returncode == 0
        started = request.run(
            ["schtasks.exe", "/Run", "/TN", request.spec.service_id],
            **_RUN_OPTIONS,
        )
        return started.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _failed_transition(
    transition: _Transition, reason: str, task_replaced: bool
) -> SupervisorAction:
    evidence = transition.evidence
    if evidence.previous_task is None:
        manager_ok = _rollback_new_task(transition) if task_replaced else True
    elif task_replaced:
        manager_ok = _restore_previous_task(transition)
    else:
        manager_ok = True
    artifact_ok = restore_artifact(
        transition.request.path,
        transition.request.expected,
        evidence.previous_artifact,
    )
    legacy_ok = (
        _restore_legacy_vbs(evidence.legacy_path, transition.legacy_backup)
        if manager_ok
        else False
    )
    if manager_ok and artifact_ok and legacy_ok:
        _remove_task_backup(transition.task_backup, evidence.previous_task)
        suffix = "; previous Task Scheduler state restored"
    else:
        retained = transition.task_backup or transition.legacy_backup
        suffix = (
            "; rollback failed, recovery evidence retained at "
            f"{retained or transition.request.path}"
        )
    return SupervisorAction(False, "install", f"{reason}{suffix}")


def _remove_task_backup(path: Path | None, expected: bytes | None) -> None:
    if path is None or expected is None:
        return
    try:
        status = path.lstat()
        if (
            stat.S_ISREG(status.st_mode)
            and status.st_nlink == 1
            and path.read_bytes() == expected
        ):
            path.unlink()
    except OSError:
        return
