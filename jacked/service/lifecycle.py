"""Integration-facing facade for service ownership and native supervision."""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from jacked import __version__
from jacked.service import CLAUDE_DIR, DEFAULT_HOST, DEFAULT_PORT, LAUNCHD_LABEL
from jacked.service.environment import EnvironmentInputs, build_service_environment
from jacked.service.environment import render_windows_launcher
from jacked.service.instance import (
    Discovery,
    Inspection,
    InstanceManifest,
    ServiceInstance,
    ServiceOwnership,
    ServicePaths,
    discover_endpoint,
    inspect_instance,
    load_or_create_machine_id,
)
from jacked.service.launcher import POSIX_LAUNCHER_SOURCE, install_versioned_launcher
from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors import (
    ArtifactInspection,
    SupervisorAction,
    inspect_artifact,
    render_for_spec,
    restart_owned_supervisor,
    install_owned_supervisor,
    uninstall_owned_supervisor,
)


PROTOCOL_VERSION = 2


def default_service_paths() -> ServicePaths:
    paths = ServicePaths.in_directory(CLAUDE_DIR / "jacked-service-v2")
    return ServicePaths(
        root=paths.root,
        lease=paths.lease,
        manifest=paths.manifest,
        control=paths.control,
        legacy_pid=CLAUDE_DIR / "jacked-service.pid",
    )


def supervisor_for_platform(platform: str | None = None) -> SupervisorKind:
    current = sys.platform if platform is None else platform
    if current == "darwin":
        return SupervisorKind.LAUNCHD
    if current.startswith("linux"):
        return SupervisorKind.SYSTEMD_USER
    if current == "win32":
        return SupervisorKind.TASK_SCHEDULER
    return SupervisorKind.MANUAL


def build_service_spec(
    *,
    runtime_path: str,
    launcher_path: str,
    launcher_content: bytes,
    supervisor: SupervisorKind | None = None,
    build_version: str = __version__,
    arguments: tuple[str, ...] = ("-I", "-m", "jacked", "service", "start"),
) -> ServiceSpec:
    """Create the exact contract used by manifests and native artifacts."""

    return ServiceSpec(
        service_id=LAUNCHD_LABEL,
        protocol_version=PROTOCOL_VERSION,
        build_version=build_version,
        runtime_path=os.path.realpath(runtime_path),
        launcher_path=os.path.normpath(launcher_path),
        launcher_sha256=hashlib.sha256(launcher_content).hexdigest(),
        supervisor=supervisor or supervisor_for_platform(),
        arguments=arguments,
    )


def default_environment(
    *,
    home: str,
    user_id: str,
    inherited_allowlisted: dict[str, str] | None = None,
    platform: str | None = None,
) -> dict[str, str]:
    return build_service_environment(
        EnvironmentInputs(
            home=home,
            user_id=user_id,
            platform=sys.platform if platform is None else platform,
            app_dir=str(CLAUDE_DIR),
        ),
        inherited=inherited_allowlisted,
    )


def provision_service_contract(
    *,
    paths: ServicePaths | None = None,
    platform: str | None = None,
    supervisor: SupervisorKind | None = None,
) -> tuple[ServiceSpec, dict[str, str]]:
    """Install/verify the immutable launcher and build the current ServiceSpec."""

    from jacked.service.instance import current_user_identity

    selected_platform = sys.platform if platform is None else platform
    selected_paths = paths or default_service_paths()
    allowed_names = {
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "DISPLAY",
        "WAYLAND_DISPLAY",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
    }
    inherited = {key: os.environ[key] for key in allowed_names if key in os.environ}
    environment = default_environment(
        home=str(Path.home()),
        user_id=current_user_identity(),
        inherited_allowlisted=inherited,
        platform=selected_platform,
    )
    runtime = os.path.realpath(sys.executable)
    arguments = ("-I", "-m", "jacked", "service", "start")
    if selected_platform == "win32":
        launcher_content = render_windows_launcher(
            runtime=runtime, argv=arguments, environment=environment
        ).encode("utf-8")
        launcher_name = "jacked-service.ps1"
        executable = False
    else:
        launcher_content = POSIX_LAUNCHER_SOURCE
        launcher_name = "jacked-service-launch"
        executable = True
    digest = hashlib.sha256(launcher_content).hexdigest()
    launcher = install_versioned_launcher(
        selected_paths.root / "launchers",
        version=f"v2-{digest[:16]}",
        name=launcher_name,
        content=launcher_content,
        expected_sha256=digest,
        executable=executable,
    )
    spec = build_service_spec(
        runtime_path=runtime,
        launcher_path=str(launcher),
        launcher_content=launcher_content,
        supervisor=supervisor or supervisor_for_platform(selected_platform),
        arguments=arguments,
    )
    return spec, environment


def start_owned_instance(
    spec: ServiceSpec,
    *,
    machine_id: str,
    paths: ServicePaths | None = None,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    login_sessions: tuple[str, ...] = (),
) -> ServiceInstance:
    """Acquire singleton ownership and reserve the normal/quarantine socket."""

    return ServiceInstance.bootstrap(
        spec=spec,
        paths=paths or default_service_paths(),
        machine_id=machine_id,
        host=host,
        preferred_port=port,
        login_sessions=login_sessions,
    )


def claim_service_ownership(
    spec: ServiceSpec,
    *,
    machine_id: str | None = None,
    paths: ServicePaths | None = None,
) -> ServiceOwnership:
    selected_paths = paths or default_service_paths()
    return ServiceOwnership.acquire(
        spec=spec,
        paths=selected_paths,
        machine_id=machine_id
        or load_or_create_machine_id(selected_paths.root / "machine-id"),
    )


def inspect_service(
    spec: ServiceSpec,
    *,
    paths: ServicePaths | None = None,
    supervisor_loaded: bool = False,
    supervisor_crash_loop: bool = False,
    health_check: Callable[[InstanceManifest], bool] | None = None,
    fixed_port_occupied: bool = False,
) -> Inspection:
    return inspect_instance(
        paths or default_service_paths(),
        spec,
        supervisor_loaded=supervisor_loaded,
        supervisor_crash_loop=supervisor_crash_loop,
        health_check=health_check,
        fixed_port_occupied=fixed_port_occupied,
    )


def discover_service(paths: ServicePaths | None = None) -> Discovery:
    return discover_endpoint(paths or default_service_paths())


def quarantine_invalid_ownership(paths: ServicePaths) -> Path | None:
    """Move a safely-owned invalid manifest aside while holding the v2 lease."""
    from jacked.service.instance import ServiceLease, read_manifest

    lease = ServiceLease(paths.lease)
    lease.acquire()
    try:
        try:
            read_manifest(paths.manifest)
            return None
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            pass

        status = paths.manifest.lstat()
        unsafe = (
            not stat.S_ISREG(status.st_mode)
            or status.st_nlink != 1
            or bool(getattr(status, "st_file_attributes", 0) & 0x400)
            or (
                os.name == "posix"
                and (status.st_uid != os.getuid() or status.st_mode & 0o077)
            )
        )
        if unsafe:
            raise ValueError(f"unsafe manifest requires manual backup: {paths.manifest}")

        control_exists = paths.control.exists() or paths.control.is_symlink()
        if control_exists:
            control_status = paths.control.lstat()
            if os.name == "posix" and (
                not stat.S_ISSOCK(control_status.st_mode)
                or control_status.st_uid != os.getuid()
            ):
                raise ValueError(
                    f"unsafe control path requires manual backup: {paths.control}"
                )

        backup = paths.manifest.with_name(
            f"{paths.manifest.name}.invalid-{time.time_ns()}"
        )
        os.replace(paths.manifest, backup)
        if control_exists:
            paths.control.unlink()
        return backup
    finally:
        lease.release()


@dataclass(frozen=True)
class Reconciliation:
    artifact: ArtifactInspection
    path: Path


def inspect_native_artifact(
    spec: ServiceSpec,
    path: Path,
    *,
    environment: dict[str, str],
) -> Reconciliation:
    """Inspect an artifact without mutating outside a supervisor transition."""
    expected = render_for_spec(spec, environment=environment)
    return Reconciliation(inspect_artifact(path, expected), path)


def reconcile_native_artifact(
    spec: ServiceSpec,
    path: Path,
    *,
    environment: dict[str, str],
) -> Reconciliation:
    """Compatibility name for the now read-only artifact inspection.

    Native mutation must remain inside ``install_native_owned`` so every
    artifact and manager change shares the cross-process transition lease.
    """

    return inspect_native_artifact(spec, path, environment=environment)


def restart_native_owned(
    spec: ServiceSpec,
    path: Path,
    *,
    environment: dict[str, str],
) -> SupervisorAction:
    return restart_owned_supervisor(spec, path, environment=environment)


def native_artifact_path(
    spec: ServiceSpec, *, paths: ServicePaths | None = None
) -> Path:
    if spec.supervisor is SupervisorKind.LAUNCHD:
        return Path.home() / "Library" / "LaunchAgents" / f"{spec.service_id}.plist"
    if spec.supervisor is SupervisorKind.SYSTEMD_USER:
        return Path.home() / ".config" / "systemd" / "user" / "jacked.service"
    if spec.supervisor is SupervisorKind.TASK_SCHEDULER:
        return (
            (paths or default_service_paths()).root / "supervisors" / "jacked-task.xml"
        )
    return (paths or default_service_paths()).root / "supervisors" / "manual"


def install_native_owned(
    spec: ServiceSpec,
    *,
    environment: dict[str, str],
    paths: ServicePaths | None = None,
) -> SupervisorAction:
    return install_owned_supervisor(
        spec,
        native_artifact_path(spec, paths=paths),
        environment=environment,
    )


def uninstall_native_owned(
    spec: ServiceSpec,
    *,
    environment: dict[str, str],
    paths: ServicePaths | None = None,
) -> SupervisorAction:
    """Remove only the exact, evidence-qualified native supervisor."""

    return uninstall_owned_supervisor(
        spec,
        native_artifact_path(spec, paths=paths),
        environment=environment,
    )


def spawn_exact_service(
    spec: ServiceSpec, *, environment: dict[str, str]
) -> SupervisorAction:
    """Spawn the exact interpreter/spec with a secret-negative environment."""

    child_environment = {
        **environment,
        "JACKED_SERVICE_GENERATION": spec.generation,
    }
    kwargs: dict[str, object] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
        "env": child_environment,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen([spec.runtime_path, *spec.arguments], **kwargs)
    except OSError as exc:
        return SupervisorAction(False, "spawn", type(exc).__name__)
    return SupervisorAction(True, "spawn", f"started {spec.generation[:12]}")


def handoff_owned_service(
    spec: ServiceSpec,
    *,
    environment: dict[str, str],
    paths: ServicePaths | None = None,
    timeout: float = 10,
) -> SupervisorAction:
    """Authenticate shutdown, await lease release, then start the new build."""

    from jacked.service.instance import read_manifest
    from jacked.service.ipc import ControlAction, send_native_control

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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            current = read_manifest(selected_paths.manifest)
        except FileNotFoundError:
            break
        except (OSError, ValueError):
            return SupervisorAction(False, "handoff", "ownership became indeterminate")
        if current.instance_id != old.instance_id:
            if current.generation == spec.generation:
                return _await_ready_generation(
                    selected_paths,
                    spec.generation,
                    previous_instance=old.instance_id,
                    timeout=timeout,
                )
            return SupervisorAction(False, "handoff", "supervisor started stale build")
        time.sleep(0.05)
    else:
        return SupervisorAction(False, "handoff", "old ownership did not exit")

    artifact = native_artifact_path(spec, paths=selected_paths)
    native = install_owned_supervisor(
        spec,
        artifact,
        environment=environment,
    )
    if native.ok:
        return _await_ready_generation(
            selected_paths,
            spec.generation,
            previous_instance=old.instance_id,
            timeout=timeout,
        )
    if old.supervisor != SupervisorKind.MANUAL.value:
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
        selected_paths,
        manual.generation,
        previous_instance=old.instance_id,
        timeout=timeout,
    )


def _await_ready_generation(
    paths: ServicePaths,
    generation: str,
    *,
    previous_instance: str,
    timeout: float,
) -> SupervisorAction:
    """Require authenticated ready state from a new exact generation."""
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


__all__ = [
    "PROTOCOL_VERSION",
    "Reconciliation",
    "build_service_spec",
    "claim_service_ownership",
    "default_environment",
    "default_service_paths",
    "discover_service",
    "inspect_service",
    "handoff_owned_service",
    "quarantine_invalid_ownership",
    "install_native_owned",
    "native_artifact_path",
    "provision_service_contract",
    "inspect_native_artifact",
    "reconcile_native_artifact",
    "restart_native_owned",
    "spawn_exact_service",
    "start_owned_instance",
    "supervisor_for_platform",
    "uninstall_native_owned",
]
