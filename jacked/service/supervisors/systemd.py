"""Transactional systemd user-service installation and rollback."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jacked.service.spec import ServiceSpec
from jacked.service.supervisors._artifacts import (
    ArtifactDisposition,
    SupervisorAction,
    SupervisorArtifact,
    reconcile_artifact,
    restore_artifact,
    snapshot_artifact,
)

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
class _PriorState:
    artifact: bytes | None
    is_enabled: bool
    is_active: bool


@dataclass(frozen=True)
class _Transition:
    request: _InstallRequest
    prior: _PriorState


def install_systemd_supervisor(
    spec: ServiceSpec,
    path: Path,
    expected: SupervisorArtifact,
    *,
    run: Any,
) -> SupervisorAction:
    """Reconcile and activate one exact user unit with coherent rollback."""

    request = _InstallRequest(spec, path, expected, run)
    inspection, artifact = snapshot_artifact(path, expected)
    if inspection.disposition is ArtifactDisposition.FOREIGN:
        return SupervisorAction(False, "refused", inspection.reason)
    prior = _inspect_manager(request, artifact)
    if isinstance(prior, SupervisorAction):
        return prior
    return _activate(_Transition(request, prior))


def _inspect_manager(
    request: _InstallRequest, artifact: bytes | None
) -> _PriorState | SupervisorAction:
    try:
        queried = request.run(
            [
                "systemctl",
                "--user",
                "show",
                request.expected.name,
                "--property=LoadState",
                "--property=FragmentPath",
                "--property=ActiveState",
                "--property=UnitFileState",
            ],
            **_RUN_OPTIONS,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "install", type(exc).__name__)
    properties = parse_properties(queried.stdout)
    load_state = properties.get("LoadState")
    if queried.returncode != 0 or load_state not in {"loaded", "not-found"}:
        return SupervisorAction(False, "refused", "systemd state is indeterminate")
    if load_state == "loaded" and not same_path(
        properties.get("FragmentPath", ""), request.path
    ):
        return SupervisorAction(False, "refused", "loaded systemd identity differs")
    if load_state == "loaded" and artifact is None:
        return SupervisorAction(False, "refused", "loaded unit has no owned artifact")
    return _PriorState(
        artifact=artifact,
        is_enabled=properties.get("UnitFileState") in {"enabled", "enabled-runtime"},
        is_active=properties.get("ActiveState") == "active",
    )


def _activate(transition: _Transition) -> SupervisorAction:
    request = transition.request
    try:
        reconciled = reconcile_artifact(request.path, request.expected)
        if reconciled.disposition is ArtifactDisposition.FOREIGN:
            return SupervisorAction(False, "refused", reconciled.reason)
        commands = (
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", request.expected.name],
            ["systemctl", "--user", "restart", request.expected.name],
        )
        for command in commands:
            result = request.run(command, **_RUN_OPTIONS)
            if result.returncode != 0:
                return _rollback(
                    transition, f"supervisor exit {result.returncode}"
                )
    except (OSError, subprocess.SubprocessError) as exc:
        return _rollback(transition, type(exc).__name__)
    return SupervisorAction(
        True, "install", f"activated {request.spec.generation[:12]}"
    )


def _rollback(transition: _Transition, reason: str) -> SupervisorAction:
    request, prior = transition.request, transition.prior
    is_restored = True
    try:
        if prior.artifact is None:
            disabled = request.run(
                ["systemctl", "--user", "disable", "--now", request.expected.name],
                **_RUN_OPTIONS,
            )
            is_restored = disabled.returncode == 0
        is_restored = (
            restore_artifact(request.path, request.expected, prior.artifact)
            and is_restored
        )
        reloaded = request.run(
            ["systemctl", "--user", "daemon-reload"], **_RUN_OPTIONS
        )
        is_restored = reloaded.returncode == 0 and is_restored
        if prior.artifact is not None:
            is_restored = _restore_manager(transition) and is_restored
    except (OSError, subprocess.SubprocessError):
        is_restored = False
    suffix = (
        "; previous systemd state restored"
        if is_restored
        else f"; rollback failed, inspect {request.path}"
    )
    return SupervisorAction(False, "install", f"{reason}{suffix}")


def _restore_manager(transition: _Transition) -> bool:
    request, prior = transition.request, transition.prior
    enabled_command = "enable" if prior.is_enabled else "disable"
    restored_enabled = request.run(
        ["systemctl", "--user", enabled_command, request.expected.name],
        **_RUN_OPTIONS,
    )
    restored_active = request.run(
        [
            "systemctl",
            "--user",
            "restart" if prior.is_active else "stop",
            request.expected.name,
        ],
        **_RUN_OPTIONS,
    )
    return restored_enabled.returncode == 0 and restored_active.returncode == 0


def parse_properties(output: str) -> dict[str, str]:
    """Parse the stable ``systemctl show`` key/value format."""

    return dict(line.split("=", 1) for line in output.splitlines() if "=" in line)


def same_path(observed: str, expected: Path) -> bool:
    """Compare manager and artifact paths after canonicalization."""

    return bool(observed) and os.path.realpath(observed) == os.path.realpath(expected)
