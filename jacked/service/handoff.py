"""Authenticated service-generation handoff orchestration."""

from __future__ import annotations

import time
from dataclasses import dataclass

from jacked.service import HANDOFF_EXIT_TIMEOUT, REPLACEMENT_READY_TIMEOUT
from jacked.service.instance_models import InstanceManifest, ServicePaths
from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors import SupervisorAction

_LIVENESS_PROBE_INTERVAL = 1.0


def _await_ready_generation(
    paths: ServicePaths,
    generation: str,
    *,
    previous_instance: str,
    timeout: float,
) -> SupervisorAction:
    from jacked.service.ipc import ControlAction, send_native_control

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            response = send_native_control(paths.manifest, ControlAction.STATUS)
            status = response.get("result", {})
        except (OSError, ValueError):
            time.sleep(0.05)
            continue
        if (
            response.get("ok")
            and isinstance(status, dict)
            and status.get("state") == "running"
            and status.get("generation") == generation
            and status.get("instance_id") != previous_instance
        ):
            return SupervisorAction(True, "handoff", "new generation is ready")
        time.sleep(0.05)
    return SupervisorAction(False, "handoff", "new generation did not become ready")


def _wait_for_handoff_exit(
    paths: ServicePaths,
    old: InstanceManifest,
    spec: ServiceSpec,
    timeout: float,
    ready_timeout: float,
) -> SupervisorAction | None:
    from jacked.service.instance import process_is_stale, read_manifest

    deadline = time.monotonic() + timeout
    next_liveness_probe = time.monotonic()
    while time.monotonic() < deadline:
        try:
            current = read_manifest(paths.manifest)
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return SupervisorAction(
                False, "handoff", "ownership became indeterminate"
            )
        if current.instance_id != old.instance_id:
            if current.generation == spec.generation:
                return _await_ready_generation(
                    paths,
                    spec.generation,
                    previous_instance=old.instance_id,
                    timeout=ready_timeout,
                )
            return SupervisorAction(False, "handoff", "supervisor started stale build")
        # macOS terminates through NSApp without unwinding Python and a
        # crashed service never removes its manifest: a proven-dead owner is
        # an exit. Probe once a second; the macOS probe shells out to ps.
        if time.monotonic() >= next_liveness_probe:
            if process_is_stale(getattr(current, "process", None)):
                return None
            next_liveness_probe = time.monotonic() + _LIVENESS_PROBE_INTERVAL
        time.sleep(0.05)
    return SupervisorAction(False, "handoff", "old ownership did not exit")


@dataclass(frozen=True)
class _HandoffPrevious:
    instance_id: str
    supervisor: str
    timeout: float


def _activate_handoff(
    spec: ServiceSpec,
    environment: dict[str, str],
    paths: ServicePaths,
    previous: _HandoffPrevious,
) -> SupervisorAction:
    from jacked.service.lifecycle import (
        install_owned_supervisor,
        native_artifact_path,
        spawn_exact_service,
    )

    artifact = native_artifact_path(spec, paths=paths)
    native = install_owned_supervisor(spec, artifact, environment=environment)
    if native.ok:
        return _await_ready_generation(
            paths,
            spec.generation,
            previous_instance=previous.instance_id,
            timeout=previous.timeout,
        )
    if previous.supervisor != SupervisorKind.MANUAL.value:
        return SupervisorAction(
            False,
            "refused",
            f"managed supervisor restart refused: {native.reason}",
        )
    manual = ServiceSpec(
        **{**spec.constructor_fields(), "supervisor": SupervisorKind.MANUAL}
    )
    spawned = spawn_exact_service(manual, environment=environment)
    if not spawned.ok:
        return spawned
    return _await_ready_generation(
        paths,
        manual.generation,
        previous_instance=previous.instance_id,
        timeout=previous.timeout,
    )


def handoff_owned_service(
    spec: ServiceSpec,
    *,
    environment: dict[str, str],
    paths: ServicePaths | None = None,
    timeout: float = HANDOFF_EXIT_TIMEOUT,
    ready_timeout: float = REPLACEMENT_READY_TIMEOUT,
) -> SupervisorAction:
    """Authenticate shutdown, await lease release, then start the new build.

    ``timeout`` bounds the old owner's exit (a graceful shutdown alone can
    take ~9 s). ``ready_timeout`` bounds the replacement's cold start and
    must cover the tray's own cold-start budget.
    """

    from jacked.service.instance import read_manifest
    from jacked.service.ipc import ControlAction, send_native_control
    from jacked.service.lifecycle import default_service_paths

    selected_paths = paths or default_service_paths()
    try:
        old = read_manifest(selected_paths.manifest)
        response = send_native_control(
            selected_paths.manifest, ControlAction.RESTART_HANDOFF
        )
    except (OSError, ValueError) as exc:
        return SupervisorAction(False, "handoff", type(exc).__name__)
    if not response.get("ok"):
        return SupervisorAction(False, "handoff", "service rejected shutdown")
    waiting = _wait_for_handoff_exit(selected_paths, old, spec, timeout, ready_timeout)
    if waiting is not None:
        return waiting
    previous = _HandoffPrevious(old.instance_id, old.supervisor, ready_timeout)
    return _activate_handoff(spec, environment, selected_paths, previous)
