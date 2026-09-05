"""Render and dispatch exact, owned native supervisor definitions."""

from __future__ import annotations

import json
import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from jacked.service.environment import posix_preinterpreter_command
from jacked.service.launcher import verify_launcher
from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors._artifacts import (
    ArtifactDisposition,
    ArtifactInspection,
    SupervisorAction,
    SupervisorArtifact,
    extract_marker,
    inspect_artifact,
    reconcile_artifact,
)
from jacked.service.supervisors._helpers import systemd_quote, windows_quote
from jacked.service.supervisors._transition import (
    SupervisorTransitionLease,
    TransitionBusy,
)


def render_launchd(
    spec: ServiceSpec, *, environment: dict[str, str]
) -> SupervisorArtifact:
    _require_kind(spec, SupervisorKind.LAUNCHD)
    marker = spec.artifact_marker()
    command = posix_preinterpreter_command(
        runtime=spec.runtime_path,
        argv=spec.arguments,
        environment={**environment, "JACKED_SERVICE_GENERATION": spec.generation},
        launcher=spec.launcher_path,
        runtime_target=spec.runtime_target_path,
    )
    payload: dict[str, Any] = {
        "Label": spec.service_id,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Background",
        "JackedOwner": marker["owner"],
        "JackedServiceID": marker["service_id"],
        "JackedSchemaVersion": marker["schema_version"],
        "JackedProtocolVersion": marker["protocol_version"],
        "JackedGeneration": marker["generation"],
    }
    return SupervisorArtifact(
        kind=spec.supervisor,
        name=f"{spec.service_id}.plist",
        marker=marker,
        content=plistlib.dumps(payload, sort_keys=True),
    )


def render_systemd_user(
    spec: ServiceSpec, *, environment: dict[str, str]
) -> SupervisorArtifact:
    _require_kind(spec, SupervisorKind.SYSTEMD_USER)
    marker = spec.artifact_marker()
    command = posix_preinterpreter_command(
        runtime=spec.runtime_path,
        argv=spec.arguments,
        environment={**environment, "JACKED_SERVICE_GENERATION": spec.generation},
        launcher=spec.launcher_path,
        runtime_target=spec.runtime_target_path,
    )
    escaped = (
        " ".join(command[:2])
        + " "
        + " ".join(systemd_quote(value) for value in command[2:])
    )
    lines = [
        f"# X-Jacked-Owner: {marker['owner']}",
        f"# X-Jacked-Service-ID: {marker['service_id']}",
        f"# X-Jacked-Schema-Version: {marker['schema_version']}",
        f"# X-Jacked-Protocol-Version: {marker['protocol_version']}",
        f"# X-Jacked-Generation: {marker['generation']}",
        "[Unit]",
        "Description=Jacked account dashboard",
        "After=network.target",
        "StartLimitIntervalSec=300",
        "StartLimitBurst=5",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={escaped}",
        "Restart=on-failure",
        "RestartSec=5",
        "NoNewPrivileges=true",
        "PrivateTmp=true",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "ReadWritePaths=%h/.claude",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    return SupervisorArtifact(
        kind=spec.supervisor,
        name="jacked.service",
        marker=marker,
        content="\n".join(lines).encode("utf-8"),
    )


def render_task_scheduler(
    spec: ServiceSpec, *, environment: dict[str, str]
) -> SupervisorArtifact:
    _require_kind(spec, SupervisorKind.TASK_SCHEDULER)
    marker = spec.artifact_marker()
    arguments = (
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
        + windows_quote(spec.launcher_path)
        + " -Generation "
        + spec.generation
    )
    root = _task_scheduler_tree(spec, marker, environment, arguments)
    content = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    return SupervisorArtifact(
        kind=spec.supervisor,
        name=spec.service_id,
        marker=marker,
        content=content,
    )


def _task_scheduler_tree(spec, marker, environment, arguments):
    root = ElementTree.Element(
        "Task",
        {
            "version": "1.4",
            "xmlns": "http://schemas.microsoft.com/windows/2004/02/mit/task",
        },
    )
    registration = ElementTree.SubElement(root, "RegistrationInfo")
    ElementTree.SubElement(registration, "Author").text = str(marker["owner"])
    ElementTree.SubElement(registration, "URI").text = f"\\{spec.service_id}"
    ElementTree.SubElement(registration, "Description").text = (
        "JACKED-MARKER:" + json.dumps(marker, sort_keys=True, separators=(",", ":"))
    )
    triggers = ElementTree.SubElement(root, "Triggers")
    logon = ElementTree.SubElement(triggers, "LogonTrigger")
    ElementTree.SubElement(logon, "Enabled").text = "true"
    principals = ElementTree.SubElement(root, "Principals")
    principal = ElementTree.SubElement(principals, "Principal", {"id": "CurrentUser"})
    user_id = environment.get("JACKED_SERVICE_USER", "")
    if not user_id.startswith("sid:S-"):
        raise ValueError("Task Scheduler requires a current-user SID")
    ElementTree.SubElement(principal, "UserId").text = user_id.removeprefix("sid:")
    ElementTree.SubElement(principal, "LogonType").text = "InteractiveToken"
    ElementTree.SubElement(principal, "RunLevel").text = "LeastPrivilege"
    _append_task_settings(root)
    _append_task_action(root, spec, environment, arguments)
    return root


def _append_task_settings(root) -> None:
    settings = ElementTree.SubElement(root, "Settings")
    ElementTree.SubElement(settings, "MultipleInstancesPolicy").text = "IgnoreNew"
    ElementTree.SubElement(settings, "DisallowStartIfOnBatteries").text = "false"
    ElementTree.SubElement(settings, "StopIfGoingOnBatteries").text = "false"
    ElementTree.SubElement(settings, "ExecutionTimeLimit").text = "PT0S"
    ElementTree.SubElement(settings, "AllowHardTerminate").text = "false"
    ElementTree.SubElement(settings, "StartWhenAvailable").text = "true"
    restart = ElementTree.SubElement(settings, "RestartOnFailure")
    ElementTree.SubElement(restart, "Interval").text = "PT1M"
    ElementTree.SubElement(restart, "Count").text = "3"


def _append_task_action(root, spec, environment, arguments) -> None:
    actions = ElementTree.SubElement(root, "Actions", {"Context": "CurrentUser"})
    execute = ElementTree.SubElement(actions, "Exec")
    ElementTree.SubElement(execute, "Command").text = "powershell.exe"
    ElementTree.SubElement(execute, "Arguments").text = arguments
    if environment:
        ElementTree.SubElement(execute, "WorkingDirectory").text = environment.get(
            "JACKED_APP_DIR", str(Path(spec.launcher_path).parent)
        )


def render_for_spec(
    spec: ServiceSpec, *, environment: dict[str, str]
) -> SupervisorArtifact:
    if not verify_launcher(Path(spec.launcher_path), spec.launcher_sha256):
        raise ValueError(
            "ServiceSpec launcher is missing, altered, or not privately owned"
        )
    if spec.supervisor is SupervisorKind.LAUNCHD:
        return render_launchd(spec, environment=environment)
    if spec.supervisor is SupervisorKind.SYSTEMD_USER:
        return render_systemd_user(spec, environment=environment)
    if spec.supervisor is SupervisorKind.TASK_SCHEDULER:
        return render_task_scheduler(spec, environment=environment)
    raise ValueError("manual services have no native supervisor artifact")


def restart_owned_supervisor(
    spec: ServiceSpec,
    artifact_path: Path,
    *,
    environment: dict[str, str],
    run: Any | None = None,
    uid: int | None = None,
) -> SupervisorAction:
    """Restart only after disk and native-manager identities both match."""
    # Resolved at call time so a test-time patch of subprocess.run is honored;
    # a def-time default would bind the real function before any patch.
    run = subprocess.run if run is None else run

    expected = render_for_spec(spec, environment=environment)
    try:
        with SupervisorTransitionLease(artifact_path, spec.service_id):
            return _restart_owned_locked(spec, artifact_path, expected, run, uid)
    except TransitionBusy:
        return SupervisorAction(False, "refused", "another native transition is active")
    except OSError as exc:
        return SupervisorAction(False, "restart", type(exc).__name__)


def _restart_owned_locked(spec, path, expected, run, uid) -> SupervisorAction:
    common = {"capture_output": True, "text": True, "timeout": 15, "check": False}
    inspection = inspect_artifact(path, expected)
    if inspection.disposition is not ArtifactDisposition.MATCHING:
        return SupervisorAction(
            False, "refused", f"supervisor artifact is {inspection.disposition.value}"
        )
    command = _restart_command(spec, path, expected, run, common, uid)
    if isinstance(command, SupervisorAction):
        return command
    try:
        result = run(command, **common)
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "restart", type(exc).__name__)
    if result.returncode != 0:
        return SupervisorAction(False, "restart", f"supervisor exit {result.returncode}")
    return SupervisorAction(True, "restart", f"restarted {spec.generation[:12]}")


def _restart_command(spec, path, expected, run, common, uid):
    if spec.supervisor is SupervisorKind.LAUNCHD:
        effective_uid = os.getuid() if uid is None else uid
        domain = f"gui/{effective_uid}/{spec.service_id}"
        checked = run(["launchctl", "print", domain], **common)
        if checked.returncode != 0 or not all(
            value in checked.stdout for value in (spec.generation, spec.launcher_path)
        ):
            return SupervisorAction(False, "refused", "loaded launchd identity differs")
        return ["launchctl", "kickstart", "-k", domain]
    if spec.supervisor is SupervisorKind.SYSTEMD_USER:
        from jacked.service.supervisors.systemd import parse_properties, same_path

        checked = run(
            [
                "systemctl",
                "--user",
                "show",
                expected.name,
                "--property=LoadState",
                "--property=FragmentPath",
                "--property=NeedDaemonReload",
            ],
            **common,
        )
        properties = parse_properties(checked.stdout)
        if (
            checked.returncode != 0
            or properties.get("LoadState") != "loaded"
            or properties.get("NeedDaemonReload", "no") != "no"
            or not same_path(properties.get("FragmentPath", ""), path)
        ):
            return SupervisorAction(False, "refused", "loaded systemd identity differs")
        return ["systemctl", "--user", "restart", expected.name]
    if spec.supervisor is SupervisorKind.TASK_SCHEDULER:
        from jacked.service.supervisors.task_scheduler import task_action

        checked = run(
            ["schtasks.exe", "/Query", "/TN", spec.service_id, "/XML"], **common
        )
        registered = checked.stdout.encode("utf-8")
        if (
            checked.returncode != 0
            or extract_marker(registered, spec.supervisor) != expected.marker
            or task_action(registered) != task_action(expected.content)
        ):
            return SupervisorAction(False, "refused", "registered task identity differs")
        return ["schtasks.exe", "/Run", "/TN", spec.service_id]
    return SupervisorAction(False, "refused", "manual service has no native manager")


def install_owned_supervisor(
    spec: ServiceSpec,
    artifact_path: Path,
    *,
    environment: dict[str, str],
    run: Any | None = None,
    uid: int | None = None,
) -> SupervisorAction:
    """Reconcile and activate under one service-scoped transition lease."""
    # Resolved at call time so a test-time patch of subprocess.run is honored;
    # a def-time default would bind the real function before any patch.
    run = subprocess.run if run is None else run

    expected = render_for_spec(spec, environment=environment)
    try:
        with SupervisorTransitionLease(artifact_path, spec.service_id):
            return _install_owned_locked(spec, artifact_path, expected, run, uid)
    except TransitionBusy:
        return SupervisorAction(False, "refused", "another native transition is active")
    except OSError as exc:
        return SupervisorAction(False, "install", type(exc).__name__)


def _install_owned_locked(
    spec: ServiceSpec,
    artifact_path: Path,
    expected: SupervisorArtifact,
    run: Any,
    uid: int | None,
) -> SupervisorAction:
    if spec.supervisor is SupervisorKind.LAUNCHD:
        from jacked.service.supervisors.launchd import install_launchd_supervisor

        return install_launchd_supervisor(
            spec, artifact_path, expected, run=run, uid=uid
        )
    if spec.supervisor is SupervisorKind.TASK_SCHEDULER:
        from jacked.service.supervisors.task_scheduler import install_task_supervisor

        return install_task_supervisor(spec, artifact_path, expected, run=run)
    if spec.supervisor is SupervisorKind.SYSTEMD_USER:
        from jacked.service.supervisors.systemd import install_systemd_supervisor

        return install_systemd_supervisor(spec, artifact_path, expected, run=run)
    return SupervisorAction(False, "refused", "manual supervisor")


def is_known_legacy_artifact(
    path: Path, service_id: str, kind: SupervisorKind
) -> bool:
    """Report whether ``path`` holds a pre-v2 definition jacked itself wrote.

    A known legacy definition is not foreign: ``install_owned_supervisor``
    recognises it, backs it up, and replaces it with the owned artifact.
    Callers use this to tell "jacked's own old layout" apart from a genuinely
    foreign artifact, which is never touched.

    LAUNCHD inspects the plist at ``path``. TASK_SCHEDULER inspects the legacy
    Startup VBS script at ``path``; the registered task itself has no pre-v2
    form. SYSTEMD_USER and MANUAL have no recognised legacy layout, so they
    always report False.
    """

    if kind is SupervisorKind.LAUNCHD:
        from jacked.service.supervisors.launchd import _legacy_arguments

        return _legacy_arguments(path, service_id) is not None
    if kind is SupervisorKind.TASK_SCHEDULER:
        from jacked.service.supervisors.task_scheduler import _known_legacy_vbs

        return _known_legacy_vbs(path) is not None
    return False


def _require_kind(spec: ServiceSpec, expected: SupervisorKind) -> None:
    if spec.supervisor is not expected:
        raise ValueError(f"ServiceSpec supervisor must be {expected.value}")


def uninstall_owned_supervisor(
    spec: ServiceSpec,
    artifact_path: Path,
    *,
    environment: dict[str, str],
    run: Any | None = None,
    uid: int | None = None,
) -> SupervisorAction:
    """Load the evidence-qualified uninstall implementation on demand."""
    # Resolved at call time so a test-time patch of subprocess.run is honored;
    # a def-time default would bind the real function before any patch.
    run = subprocess.run if run is None else run
    from jacked.service.supervisors.uninstall import uninstall_owned_supervisor

    return uninstall_owned_supervisor(
        spec, artifact_path, environment=environment, run=run, uid=uid
    )


__all__ = [
    "ArtifactDisposition",
    "ArtifactInspection",
    "SupervisorArtifact",
    "inspect_artifact",
    "install_owned_supervisor",
    "is_known_legacy_artifact",
    "reconcile_artifact",
    "render_for_spec",
    "render_launchd",
    "render_systemd_user",
    "render_task_scheduler",
    "restart_owned_supervisor",
    "SupervisorAction",
    "uninstall_owned_supervisor",
]
