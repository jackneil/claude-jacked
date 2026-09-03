"""Native supervisor artifacts rendered from :class:`ServiceSpec`.

Writing is deliberately separate from loading.  Reconciliation never invokes
launchctl, systemctl, or Task Scheduler, and never overwrites an artifact that
does not carry jacked's exact ownership marker.
"""

from __future__ import annotations

import json
import os
import plistlib
import stat
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from jacked.service.environment import posix_preinterpreter_command
from jacked.service.launcher import verify_launcher
from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors._helpers import (
    atomic_write,
    systemd_quote,
    windows_quote,
)


class ArtifactDisposition(str, Enum):
    MISSING = "missing"
    MATCHING = "matching"
    OWNED_DRIFT = "owned_drift"
    FOREIGN = "foreign"
    INSTALLED = "installed"


@dataclass(frozen=True)
class SupervisorArtifact:
    kind: SupervisorKind
    name: str
    marker: dict[str, str | int]
    content: bytes


@dataclass(frozen=True)
class ArtifactInspection:
    disposition: ArtifactDisposition
    reason: str = ""


@dataclass(frozen=True)
class SupervisorAction:
    ok: bool
    action: str
    reason: str


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
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={escaped}",
        "Restart=on-failure",
        "RestartSec=3",
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
    # The versioned, hash-verified launcher rebuilds this exact allowlist before
    # CreateProcessW.
    arguments = (
        # The script is generated locally, private, and hash-verified against
        # ServiceSpec before task reconciliation. AllSigned would reject this
        # intentionally unsigned per-install artifact on every normal host.
        "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File "
        + windows_quote(spec.launcher_path)
        + " -Generation "
        + spec.generation
    )
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
    actions = ElementTree.SubElement(root, "Actions", {"Context": "CurrentUser"})
    execute = ElementTree.SubElement(actions, "Exec")
    ElementTree.SubElement(execute, "Command").text = "powershell.exe"
    ElementTree.SubElement(execute, "Arguments").text = arguments
    # Do not embed inherited values. The environment is included in the
    # content-addressed launcher inputs only as reviewed key/value arguments.
    if environment:
        ElementTree.SubElement(execute, "WorkingDirectory").text = environment.get(
            "JACKED_APP_DIR", str(Path(spec.launcher_path).parent)
        )
    content = ElementTree.tostring(root, encoding="utf-8", xml_declaration=True)
    return SupervisorArtifact(
        kind=spec.supervisor,
        name=spec.service_id,
        marker=marker,
        content=content,
    )


def inspect_artifact(path: Path, expected: SupervisorArtifact) -> ArtifactInspection:
    if not path.exists() and not path.is_symlink():
        return ArtifactInspection(ArtifactDisposition.MISSING)
    try:
        status = path.lstat()
        if not stat.S_ISREG(status.st_mode) or status.st_nlink != 1:
            return ArtifactInspection(
                ArtifactDisposition.FOREIGN,
                "artifact is not a regular single-link file",
            )
        if os.name == "posix" and (
            status.st_uid != os.getuid() or status.st_mode & 0o022
        ):
            return ArtifactInspection(
                ArtifactDisposition.FOREIGN,
                "artifact is not privately controlled by the current user",
            )
        current = path.read_bytes()
    except OSError as exc:
        return ArtifactInspection(
            ArtifactDisposition.FOREIGN, f"artifact is unreadable: {type(exc).__name__}"
        )
    if current == expected.content:
        return ArtifactInspection(ArtifactDisposition.MATCHING)
    marker = _extract_marker(current, expected.kind)
    if marker is None:
        return ArtifactInspection(
            ArtifactDisposition.FOREIGN, "artifact has no valid jacked ownership marker"
        )
    if (
        marker.get("owner") != expected.marker["owner"]
        or marker.get("service_id") != expected.marker["service_id"]
    ):
        return ArtifactInspection(
            ArtifactDisposition.FOREIGN, "artifact owner or service identity differs"
        )
    return ArtifactInspection(
        ArtifactDisposition.OWNED_DRIFT, "owned artifact generation differs"
    )


def reconcile_artifact(path: Path, expected: SupervisorArtifact) -> ArtifactInspection:
    inspection = inspect_artifact(path, expected)
    if inspection.disposition in {
        ArtifactDisposition.MATCHING,
        ArtifactDisposition.FOREIGN,
    }:
        return inspection
    atomic_write(path, expected.content)
    return ArtifactInspection(
        ArtifactDisposition.INSTALLED, "artifact installed from exact ServiceSpec"
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
    run: Any = subprocess.run,
    uid: int | None = None,
) -> SupervisorAction:
    """Restart only after the on-disk artifact exactly matches ServiceSpec."""

    expected = render_for_spec(spec, environment=environment)
    inspection = inspect_artifact(artifact_path, expected)
    if inspection.disposition is not ArtifactDisposition.MATCHING:
        return SupervisorAction(
            False, "refused", f"supervisor artifact is {inspection.disposition.value}"
        )
    if spec.supervisor is SupervisorKind.LAUNCHD:
        effective_uid = os.getuid() if uid is None else uid
        command = [
            "launchctl",
            "kickstart",
            "-k",
            f"gui/{effective_uid}/{spec.service_id}",
        ]
    elif spec.supervisor is SupervisorKind.SYSTEMD_USER:
        command = ["systemctl", "--user", "restart", expected.name]
    elif spec.supervisor is SupervisorKind.TASK_SCHEDULER:
        command = ["schtasks.exe", "/Run", "/TN", spec.service_id]
    else:
        return SupervisorAction(
            False, "refused", "manual services are controlled over native IPC"
        )
    try:
        result = run(command, capture_output=True, text=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "restart", type(exc).__name__)
    if result.returncode != 0:
        # Supervisor output may contain inherited environment or local paths.
        # Expose only the numeric exit status at this boundary.
        return SupervisorAction(
            False, "restart", f"supervisor exit {result.returncode}"
        )
    return SupervisorAction(
        True, "restart", f"restarted generation {spec.generation[:12]}"
    )


def install_owned_supervisor(
    spec: ServiceSpec,
    artifact_path: Path,
    *,
    environment: dict[str, str],
    run: Any = subprocess.run,
    uid: int | None = None,
) -> SupervisorAction:
    """Reconcile and activate only an owned, exact native definition."""

    expected = render_for_spec(spec, environment=environment)
    reconciled = reconcile_artifact(artifact_path, expected)
    if reconciled.disposition is ArtifactDisposition.FOREIGN:
        return SupervisorAction(False, "refused", reconciled.reason)
    common = {"capture_output": True, "text": True, "timeout": 15, "check": False}
    try:
        if spec.supervisor is SupervisorKind.LAUNCHD:
            effective_uid = os.getuid() if uid is None else uid
            domain = f"gui/{effective_uid}/{spec.service_id}"
            loaded = run(["launchctl", "print", domain], **common)
            if loaded.returncode == 0:
                # launchctl does not return the source plist. Require the
                # content-addressed generation and immutable launcher path in
                # its parsed job description before acting on the label.
                if not all(
                    value in loaded.stdout
                    for value in (spec.generation, spec.launcher_path)
                ):
                    return SupervisorAction(
                        False, "refused", "loaded launchd job identity is foreign"
                    )
                command = ["launchctl", "kickstart", "-k", domain]
            else:
                command = [
                    "launchctl",
                    "bootstrap",
                    f"gui/{effective_uid}",
                    str(artifact_path),
                ]
        elif spec.supervisor is SupervisorKind.SYSTEMD_USER:
            reloaded = run(["systemctl", "--user", "daemon-reload"], **common)
            if reloaded.returncode != 0:
                return SupervisorAction(
                    False, "install", f"supervisor exit {reloaded.returncode}"
                )
            command = ["systemctl", "--user", "enable", "--now", expected.name]
        elif spec.supervisor is SupervisorKind.TASK_SCHEDULER:
            queried = run(
                ["schtasks.exe", "/Query", "/TN", spec.service_id, "/XML"], **common
            )
            if queried.returncode == 0:
                marker = _extract_marker(
                    queried.stdout.encode("utf-8"), spec.supervisor
                )
                if marker is None or (
                    marker.get("owner") != expected.marker["owner"]
                    or marker.get("service_id") != expected.marker["service_id"]
                ):
                    return SupervisorAction(
                        False, "refused", "registered task identity is foreign"
                    )
            command = [
                "schtasks.exe",
                "/Create",
                "/TN",
                spec.service_id,
                "/XML",
                str(artifact_path),
                "/F",
            ]
        else:
            return SupervisorAction(False, "refused", "manual supervisor")
        result = run(command, **common)
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "install", type(exc).__name__)
    if result.returncode != 0:
        return SupervisorAction(
            False, "install", f"supervisor exit {result.returncode}"
        )
    return SupervisorAction(True, "install", f"activated {spec.generation[:12]}")


def _extract_marker(content: bytes, kind: SupervisorKind) -> dict[str, Any] | None:
    try:
        if kind is SupervisorKind.LAUNCHD:
            payload = plistlib.loads(content)
            return {
                "owner": payload["JackedOwner"],
                "service_id": payload["JackedServiceID"],
                "schema_version": payload["JackedSchemaVersion"],
                "protocol_version": payload["JackedProtocolVersion"],
                "generation": payload["JackedGeneration"],
            }
        if kind is SupervisorKind.SYSTEMD_USER:
            values: dict[str, str | int] = {}
            names = {
                "X-Jacked-Owner": "owner",
                "X-Jacked-Service-ID": "service_id",
                "X-Jacked-Schema-Version": "schema_version",
                "X-Jacked-Protocol-Version": "protocol_version",
                "X-Jacked-Generation": "generation",
            }
            for line in content.decode("utf-8").splitlines():
                if not line.startswith("# ") or ": " not in line:
                    continue
                key, value = line[2:].split(": ", 1)
                if key in names:
                    values[names[key]] = (
                        int(value) if names[key].endswith("version") else value
                    )
            return values if set(values) == set(names.values()) else None
        root = ElementTree.fromstring(content)
        description = next(
            (
                item.text or ""
                for item in root.iter()
                if item.tag.rsplit("}", 1)[-1] == "Description"
            ),
            "",
        )
        prefix = "JACKED-MARKER:"
        return (
            json.loads(description[len(prefix) :])
            if description.startswith(prefix)
            else None
        )
    except (
        KeyError,
        ValueError,
        TypeError,
        plistlib.InvalidFileException,
        ElementTree.ParseError,
        json.JSONDecodeError,
    ):
        return None


def _require_kind(spec: ServiceSpec, expected: SupervisorKind) -> None:
    if spec.supervisor is not expected:
        raise ValueError(f"ServiceSpec supervisor must be {expected.value}")


def uninstall_owned_supervisor(
    spec: ServiceSpec,
    artifact_path: Path,
    *,
    environment: dict[str, str],
    run: Any = subprocess.run,
    uid: int | None = None,
) -> SupervisorAction:
    """Load the evidence-qualified uninstall implementation on demand."""
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
    "reconcile_artifact",
    "render_for_spec",
    "render_launchd",
    "render_systemd_user",
    "render_task_scheduler",
    "restart_owned_supervisor",
    "SupervisorAction",
    "uninstall_owned_supervisor",
]
