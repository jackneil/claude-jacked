"""Evidence-qualified, rollback-safe launchd generation transitions."""

from __future__ import annotations

import os
import plistlib
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors._artifacts import (
    ArtifactDisposition,
    SupervisorAction,
    SupervisorArtifact,
    extract_marker,
    inspect_artifact,
    reconcile_artifact,
)
from jacked.service.supervisors._helpers import atomic_write


@dataclass(frozen=True)
class LaunchdTransition:
    spec: ServiceSpec
    path: Path
    expected: SupervisorArtifact
    run: Any
    uid: int

    @property
    def domain(self) -> str:
        return f"gui/{self.uid}/{self.spec.service_id}"

    @property
    def common(self) -> dict[str, Any]:
        return {"capture_output": True, "text": True, "timeout": 15, "check": False}


@dataclass(frozen=True)
class RollbackState:
    content: bytes | None
    was_loaded: bool


def install_launchd_supervisor(
    spec: ServiceSpec,
    artifact_path: Path,
    expected: SupervisorArtifact,
    *,
    run: Any,
    uid: int | None,
) -> SupervisorAction:
    """Transition launchd while the caller holds the native transition lease."""

    context = LaunchdTransition(
        spec, artifact_path, expected, run, os.getuid() if uid is None else uid
    )
    return _install_locked(context)


def _install_locked(context: LaunchdTransition) -> SupervisorAction:
    inspection = inspect_artifact(context.path, context.expected)
    legacy = (
        _legacy_arguments(context.path, context.spec.service_id)
        if inspection.disposition is ArtifactDisposition.FOREIGN
        else None
    )
    if inspection.disposition is ArtifactDisposition.FOREIGN and legacy is None:
        return SupervisorAction(False, "refused", inspection.reason)
    try:
        loaded = context.run(
            ["launchctl", "print", context.domain], **context.common
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "install", type(exc).__name__)
    if legacy is not None:
        return _replace_legacy(context, legacy, loaded)
    if loaded.returncode == 0:
        return _replace_loaded(context, inspection.disposition, loaded.stdout)
    if loaded.returncode == 113:
        return _install_unloaded(context, inspection.disposition)
    return SupervisorAction(
        False, "install", f"supervisor inspection exit {loaded.returncode}"
    )


def _replace_legacy(
    context: LaunchdTransition, arguments: tuple[str, ...], loaded
) -> SupervisorAction:
    previous = _read_artifact(context.path)
    if previous is None or not _backup_legacy(context.path, previous):
        return SupervisorAction(False, "refused", "legacy backup could not be proven")
    if loaded.returncode == 0:
        if not all(argument in loaded.stdout for argument in arguments):
            return SupervisorAction(False, "refused", "loaded legacy identity differs")
        stopped = _invoke(
            context, ["launchctl", "bootout", context.domain]
        )
        if not stopped.ok:
            return stopped
        was_loaded = True
    elif loaded.returncode == 113:
        was_loaded = False
    else:
        return SupervisorAction(
            False, "install", f"supervisor inspection exit {loaded.returncode}"
        )
    state = RollbackState(previous, was_loaded=was_loaded)
    enabled = _enable(context)
    if not enabled.ok:
        return _failed_with_rollback(context, state, enabled.reason)
    if _read_artifact(context.path) != previous:
        return _failed_with_rollback(context, state, "legacy artifact changed")
    atomic_write(context.path, context.expected.content)
    result = _bootstrap(context)
    if result.ok or result.action == "ambiguous":
        return result
    return _failed_with_rollback(context, state, result.reason)


def _replace_loaded(
    context: LaunchdTransition,
    disposition: ArtifactDisposition,
    loaded_description: str,
) -> SupervisorAction:
    if disposition is ArtifactDisposition.MISSING:
        return SupervisorAction(False, "refused", "loaded job has no owned artifact")
    previous = _read_artifact(context.path)
    if previous is None or not _loaded_matches(context, previous, loaded_description):
        return SupervisorAction(False, "refused", "loaded launchd identity is foreign")
    enabled = _enable(context)
    if not enabled.ok:
        return enabled
    if disposition is ArtifactDisposition.MATCHING:
        return _invoke(context, ["launchctl", "kickstart", "-k", context.domain])
    state = RollbackState(previous, was_loaded=True)
    stopped = _invoke(context, ["launchctl", "bootout", context.domain])
    if not stopped.ok:
        return stopped
    return _reconcile_bootstrap(context, state)


def _install_unloaded(
    context: LaunchdTransition, disposition: ArtifactDisposition
) -> SupervisorAction:
    previous = None if disposition is ArtifactDisposition.MISSING else _read_artifact(
        context.path
    )
    if disposition is not ArtifactDisposition.MISSING and previous is None:
        return SupervisorAction(False, "install", "artifact became unreadable")
    state = RollbackState(previous, was_loaded=False)
    enabled = _enable(context)
    if not enabled.ok:
        return enabled
    if disposition is ArtifactDisposition.MATCHING:
        return _bootstrap(context)
    return _reconcile_bootstrap(context, state)


def _reconcile_bootstrap(
    context: LaunchdTransition, state: RollbackState
) -> SupervisorAction:
    try:
        reconciled = reconcile_artifact(context.path, context.expected)
    except OSError as exc:
        return _failed_with_rollback(context, state, type(exc).__name__)
    if reconciled.disposition is not ArtifactDisposition.INSTALLED:
        return _failed_with_rollback(context, state, "artifact changed during update")
    result = _bootstrap(context)
    if result.ok:
        return result
    if result.action == "ambiguous":
        return result
    return _failed_with_rollback(context, state, result.reason)


def _bootstrap(context: LaunchdTransition) -> SupervisorAction:
    command = ["launchctl", "bootstrap", f"gui/{context.uid}", str(context.path)]
    try:
        result = context.run(command, **context.common)
    except (OSError, subprocess.SubprocessError) as exc:
        return _resolve_ambiguous_bootstrap(context, type(exc).__name__)
    if result.returncode != 0:
        return SupervisorAction(False, "install", f"supervisor exit {result.returncode}")
    return SupervisorAction(
        True, "install", f"activated {context.spec.generation[:12]}"
    )


def _resolve_ambiguous_bootstrap(
    context: LaunchdTransition, failure: str
) -> SupervisorAction:
    """Preserve coherent disk state when bootstrap completion is unknown."""
    try:
        loaded = context.run(
            ["launchctl", "print", context.domain], **context.common
        )
    except (OSError, subprocess.SubprocessError):
        return SupervisorAction(False, "ambiguous", failure)
    if loaded.returncode == 0 and all(
        value in loaded.stdout
        for value in (context.spec.generation, context.spec.launcher_path)
    ):
        return SupervisorAction(
            True, "install", f"activated {context.spec.generation[:12]}"
        )
    if loaded.returncode == 113:
        return SupervisorAction(False, "install", failure)
    return SupervisorAction(False, "ambiguous", f"{failure}; loaded state unknown")


def _invoke(context: LaunchdTransition, command: list[str]) -> SupervisorAction:
    try:
        result = context.run(command, **context.common)
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "install", type(exc).__name__)
    if result.returncode != 0:
        return SupervisorAction(False, "install", f"supervisor exit {result.returncode}")
    return SupervisorAction(
        True, "install", f"activated {context.spec.generation[:12]}"
    )


def _enable(context: LaunchdTransition) -> SupervisorAction:
    """Clear launchd's persistent disabled override before activation."""
    try:
        result = context.run(
            ["launchctl", "enable", context.domain], **context.common
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SupervisorAction(False, "install", type(exc).__name__)
    if result.returncode != 0:
        return SupervisorAction(False, "install", f"supervisor exit {result.returncode}")
    return SupervisorAction(True, "enable", "launchd service enabled")


def _failed_with_rollback(
    context: LaunchdTransition, state: RollbackState, reason: str
) -> SupervisorAction:
    suffix = _rollback(context, state)
    return SupervisorAction(False, "install", f"{reason}{suffix}")


def _rollback(context: LaunchdTransition, state: RollbackState) -> str:
    try:
        current = _read_artifact(context.path)
        if current == state.content:
            pass
        elif current == context.expected.content:
            if state.content is None:
                _unlink_exact(context.path, context.expected.content)
            else:
                atomic_write(context.path, state.content)
        else:
            return "; rollback refused because the artifact changed"
        if not state.was_loaded:
            return "; previous unloaded state restored"
        restored = context.run(
            ["launchctl", "bootstrap", f"gui/{context.uid}", str(context.path)],
            **context.common,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"; rollback failed with {type(exc).__name__}"
    if restored.returncode != 0:
        return f"; rollback supervisor exit {restored.returncode}"
    return "; previous supervisor restored"


def _read_artifact(path: Path) -> bytes | None:
    try:
        status = path.lstat()
        if (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or (
                os.name == "posix"
                and (status.st_uid != os.getuid() or status.st_mode & 0o022)
            )
        ):
            return None
        return path.read_bytes()
    except OSError:
        return None


def _unlink_exact(path: Path, expected: bytes) -> None:
    status = path.lstat()
    if (
        not stat.S_ISREG(status.st_mode)
        or status.st_nlink != 1
        or path.read_bytes() != expected
    ):
        raise OSError("artifact changed")
    path.unlink()


def _loaded_matches(
    context: LaunchdTransition, content: bytes, description: str
) -> bool:
    identity = _artifact_identity(content, len(context.spec.arguments))
    return identity is not None and all(value in description for value in identity)


def _artifact_identity(content: bytes, argument_count: int) -> tuple[str, str] | None:
    marker = extract_marker(content, SupervisorKind.LAUNCHD)
    if marker is None:
        return None
    try:
        arguments = plistlib.loads(content)["ProgramArguments"]
        launcher = arguments[-argument_count - 2]
        generation = marker["generation"]
        if not isinstance(launcher, str) or not isinstance(generation, str):
            return None
        return generation, launcher
    except (KeyError, IndexError, TypeError, plistlib.InvalidFileException):
        return None


def _legacy_arguments(path: Path, service_id: str) -> tuple[str, ...] | None:
    content = _read_artifact(path)
    if content is None or extract_marker(content, SupervisorKind.LAUNCHD) is not None:
        return None
    try:
        payload = plistlib.loads(content)
        arguments = payload["ProgramArguments"]
    except (KeyError, TypeError, plistlib.InvalidFileException):
        return None
    if payload.get("Label") != service_id or not _known_legacy_argv(arguments):
        return None
    return tuple(arguments)


def _known_legacy_argv(arguments: object) -> bool:
    if not isinstance(arguments, list) or len(arguments) < 3:
        return False
    if not all(isinstance(item, str) and item for item in arguments):
        return False
    executable = Path(arguments[0]).name.lower()
    if executable not in {"jacked", "jacked.exe"} or arguments[1:3] != [
        "service",
        "start",
    ]:
        return False
    tail = arguments[3:]
    if len(tail) % 2:
        return False
    return all(flag in {"--host", "--port"} for flag in tail[::2])


def _backup_legacy(path: Path, content: bytes) -> bool:
    backup = path.with_name(f"{path.name}.pre-v2")
    current = _read_artifact(backup)
    if current is not None:
        return current == content
    try:
        atomic_write(backup, content)
    except OSError:
        return False
    return _read_artifact(backup) == content
