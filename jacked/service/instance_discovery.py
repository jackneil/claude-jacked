"""Evidence-qualified service inspection, discovery, and bind reservation."""

from __future__ import annotations

import json
import socket
from collections.abc import Callable

from jacked.service.instance_models import (
    BindIdentity,
    Discovery,
    InspectState,
    Inspection,
    InstanceManifest,
    ServicePaths,
)
from jacked.service.instance_storage import (
    current_user_identity,
    process_identity,
    process_user_identity,
    read_manifest,
)
from jacked.service.spec import ServiceSpec


def inspect_instance(
    paths: ServicePaths,
    spec: ServiceSpec,
    *,
    supervisor_loaded: bool = False,
    supervisor_crash_loop: bool = False,
    health_check: Callable[[InstanceManifest], bool] | None = None,
    fixed_port_occupied: bool = False,
) -> Inspection:
    if not paths.manifest.exists():
        if supervisor_crash_loop:
            return Inspection(
                InspectState.SUPERVISOR_CRASH_LOOP,
                reason="native supervisor is crash-looping",
            )
        if paths.legacy_pid.exists():
            return Inspection(
                InspectState.LEGACY_JACKED,
                reason="legacy PID evidence exists but is not controllable",
            )
        if fixed_port_occupied:
            return Inspection(
                InspectState.FOREIGN_LISTENER,
                reason="the fixed port is occupied without ownership proof",
            )
        return Inspection(InspectState.STOPPED)
    try:
        manifest = read_manifest(paths.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return Inspection(
            InspectState.STALE_MANIFEST,
            reason=f"manifest validation failed: {type(exc).__name__}",
        )
    if (
        manifest.service_id != spec.service_id
        or manifest.protocol_version != spec.protocol_version
        or manifest.generation != spec.generation
        or manifest.user_id != current_user_identity()
    ):
        return Inspection(
            InspectState.STALE_MANIFEST,
            manifest,
            "manifest does not match this ServiceSpec/user",
        )
    try:
        observed = process_identity(manifest.process.pid)
        observed_user = process_user_identity(manifest.process.pid)
    except (OSError, ProcessLookupError, ValueError):
        return Inspection(
            InspectState.STALE_MANIFEST, manifest, "manifest process no longer exists"
        )
    if observed != manifest.process:
        return Inspection(
            InspectState.STALE_MANIFEST,
            manifest,
            "process creation identity or executable changed",
        )
    if observed_user != manifest.user_id:
        return Inspection(
            InspectState.STALE_MANIFEST, manifest, "process user identity changed"
        )
    if manifest.bind.quarantine:
        return Inspection(
            InspectState.QUARANTINED,
            manifest,
            "owned service is isolated from an ambiguous listener",
        )
    healthy = health_check(manifest) if health_check is not None else True
    if supervisor_loaded:
        state = (
            InspectState.MANAGED_HEALTHY if healthy else InspectState.MANAGED_DEGRADED
        )
    else:
        state = (
            InspectState.VERIFIED_UNMANAGED
            if healthy
            else InspectState.MANAGED_DEGRADED
        )
    return Inspection(state, manifest)


def discover_endpoint(
    paths: ServicePaths, *, default_host: str = "127.0.0.1", default_port: int = 8321
) -> Discovery:
    """Discover the API endpoint without unsafe 8321 fallback.

    The mere presence of a v2 manifest suppresses legacy fallback.  A corrupt
    manifest is an ownership conflict, not permission to contact an arbitrary
    listener on the historical port.
    """

    if paths.manifest.exists():
        try:
            manifest = read_manifest(paths.manifest)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            return Discovery(None, None, "manifest-invalid", type(exc).__name__)
        return Discovery(manifest.bind.host, manifest.bind.port, "manifest")
    if paths.legacy_pid.exists():
        return Discovery(
            None,
            None,
            "legacy-ambiguous",
            "legacy PID evidence suppresses fixed-port fallback",
        )
    return Discovery(default_host, default_port, "default")


def choose_quarantine_port(host: str, preferred_port: int = 8321) -> BindIdentity:
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, preferred_port))
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as dynamic:
            dynamic.bind((host, 0))
            return BindIdentity(
                host=host, port=dynamic.getsockname()[1], quarantine=True
            )
    finally:
        probe.close()
    return BindIdentity(host=host, port=preferred_port, quarantine=False)


def reserve_service_bind(
    host: str, preferred_port: int = 8321
) -> tuple[socket.socket, BindIdentity]:
    """Reserve the preferred port or an owned quarantine port without a race."""

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind((host, preferred_port))
        listener.listen()
        return listener, BindIdentity(host=host, port=preferred_port, quarantine=False)
    except OSError:
        listener.close()
    quarantine = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        quarantine.bind((host, 0))
        quarantine.listen()
        port = quarantine.getsockname()[1]
        return quarantine, BindIdentity(host=host, port=port, quarantine=True)
    except BaseException:
        quarantine.close()
        raise
