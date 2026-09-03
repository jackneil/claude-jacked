"""Lifetime lease and instance ownership resources."""

from __future__ import annotations

import json
import os
import secrets
import socket
import stat
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jacked.service.instance_discovery import reserve_service_bind
from jacked.service.instance_models import BindIdentity, InstanceManifest, ServicePaths
from jacked.service.instance_storage import (
    _ensure_private_directory,
    _secure_windows_path,
    current_process_identity,
    current_user_identity,
    publish_manifest,
    read_manifest,
    remove_manifest_if_current,
    process_identity,
)
from jacked.service.spec import ServiceSpec


class ServiceLeaseBusy(RuntimeError):
    """The singleton lease is already held by another live starter."""


class ServiceOwnershipInvalid(RuntimeError):
    """Persistent ownership evidence is unsafe to recover automatically."""


class ServiceLease:
    """A non-blocking lock retained for the entire API process lifetime."""

    _held_paths: set[str] = set()
    _held_lock = threading.Lock()

    def __init__(self, path: Path):
        self.path = path
        self._file: Any | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        key = os.path.realpath(self.path)
        with self._held_lock:
            if key in self._held_paths:
                raise ServiceLeaseBusy("this process already holds the service lease")
            _ensure_private_directory(self.path.parent)
            handle = open(self.path, "a+b")
            try:
                if os.name == "nt":
                    import msvcrt

                    _secure_windows_path(self.path)
                    handle.seek(0)
                    if handle.tell() == 0:
                        handle.write(b"\0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    os.fchmod(handle.fileno(), 0o600)
            except (OSError, BlockingIOError) as exc:
                handle.close()
                raise ServiceLeaseBusy(
                    "another process holds the service lease"
                ) from exc
            self._file = handle
            self._held_paths.add(key)

    def release(self) -> None:
        if self._file is None:
            return
        key = os.path.realpath(self.path)
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._file.close()
            self._file = None
            with self._held_lock:
                self._held_paths.discard(key)

    def __enter__(self) -> "ServiceLease":
        self.acquire()
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


@dataclass
class ServiceOwnership:
    """Lease-backed ownership that can publish changing bind plans."""

    spec: ServiceSpec
    paths: ServicePaths
    machine_id: str
    lease: ServiceLease
    instance_id: str
    control_nonce: str
    manifest: InstanceManifest | None = None

    @classmethod
    def acquire(
        cls, *, spec: ServiceSpec, paths: ServicePaths, machine_id: str
    ) -> "ServiceOwnership":
        lease = ServiceLease(paths.lease)
        try:
            lease.acquire()
        except ServiceLeaseBusy:
            raise
        except (OSError, ValueError) as exc:
            raise ServiceOwnershipInvalid(
                "the private service lease is unsafe or unavailable"
            ) from exc
        try:
            _clear_proven_stale_manifest(paths)
        except Exception as exc:
            lease.release()
            if isinstance(exc, ServiceOwnershipInvalid):
                raise
            raise ServiceOwnershipInvalid(
                "stale ownership state could not be recovered safely"
            ) from exc
        except BaseException:
            lease.release()
            raise
        return cls(
            spec=spec,
            paths=paths,
            machine_id=machine_id,
            lease=lease,
            instance_id=secrets.token_urlsafe(24),
            control_nonce=secrets.token_urlsafe(32),
        )

    def publish(
        self, bind: BindIdentity, *, login_sessions: tuple[str, ...] = ()
    ) -> InstanceManifest:
        if not self.lease.held:
            raise RuntimeError("cannot publish without the lifetime lease")
        from jacked.service.ipc import native_control_address

        control = native_control_address(
            self.paths.control, current_user_identity(), platform=sys.platform
        ).address
        manifest = InstanceManifest.create(
            spec=self.spec,
            process=current_process_identity(),
            user_id=current_user_identity(),
            machine_id=self.machine_id,
            bind=bind,
            control_address=control,
            instance_id=self.instance_id,
            control_nonce=self.control_nonce,
            login_sessions=login_sessions,
        )
        publish_manifest(self.paths.manifest, manifest)
        self.manifest = manifest
        return manifest

    def close(self) -> None:
        remove_manifest_if_current(self.paths.manifest, self.instance_id)
        self.lease.release()


def _clear_proven_stale_manifest(paths: ServicePaths) -> None:
    if not paths.manifest.exists() and not paths.manifest.is_symlink():
        return
    try:
        stale = read_manifest(paths.manifest)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ServiceOwnershipInvalid(
            "an invalid instance manifest requires explicit recovery"
        ) from exc
    try:
        observed = process_identity(stale.process.pid)
    except (OSError, ProcessLookupError, ValueError):
        _remove_owned_stale_control(paths.control)
        if not remove_manifest_if_current(paths.manifest, stale.instance_id):
            raise ServiceOwnershipInvalid("stale manifest changed during recovery")
        return
    if observed != stale.process:
        _remove_owned_stale_control(paths.control)
        if not remove_manifest_if_current(paths.manifest, stale.instance_id):
            raise ServiceOwnershipInvalid("stale manifest changed during recovery")
        return
    raise ServiceOwnershipInvalid(
        "an existing instance manifest still names a live process"
    )


def _remove_owned_stale_control(path: Path) -> None:
    try:
        status = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(status.st_mode):
        raise ServiceOwnershipInvalid("stale control path is not a socket")
    if os.name == "posix" and status.st_uid != os.getuid():
        raise ServiceOwnershipInvalid("stale control socket has the wrong owner")
    path.unlink()


@dataclass
class ServiceInstance:
    """Resources retained by one bootstrapped API service."""

    spec: ServiceSpec
    paths: ServicePaths
    lease: ServiceLease
    manifest: InstanceManifest
    bound_socket: socket.socket

    @classmethod
    def bootstrap(
        cls,
        *,
        spec: ServiceSpec,
        paths: ServicePaths,
        machine_id: str,
        host: str = "127.0.0.1",
        preferred_port: int = 8321,
        login_sessions: tuple[str, ...] = (),
    ) -> "ServiceInstance":
        lease = ServiceLease(paths.lease)
        lease.acquire()
        listener: socket.socket | None = None
        try:
            _clear_proven_stale_manifest(paths)
            listener, bind = reserve_service_bind(host, preferred_port)
            from jacked.service.ipc import native_control_address

            control = native_control_address(
                paths.control, current_user_identity(), platform=sys.platform
            ).address
            manifest = InstanceManifest.create(
                spec=spec,
                process=current_process_identity(),
                user_id=current_user_identity(),
                machine_id=machine_id,
                bind=bind,
                control_address=control,
                login_sessions=login_sessions,
            )
            publish_manifest(paths.manifest, manifest)
            return cls(spec, paths, lease, manifest, listener)
        except BaseException:
            if listener is not None:
                listener.close()
            lease.release()
            raise

    def repair_manifest(self) -> None:
        """Republish only while this exact in-memory instance owns the lease."""

        if not self.lease.held:
            raise RuntimeError("cannot repair a manifest without the lifetime lease")
        if self.paths.manifest.exists() or self.paths.manifest.is_symlink():
            try:
                current = read_manifest(self.paths.manifest)
            except (OSError, ValueError, json.JSONDecodeError):
                current = None
            if current is not None and current.instance_id != self.manifest.instance_id:
                raise RuntimeError("refusing to replace another instance manifest")
        publish_manifest(self.paths.manifest, self.manifest)

    def close(self) -> None:
        remove_manifest_if_current(self.paths.manifest, self.manifest.instance_id)
        self.bound_socket.close()
        self.lease.release()

    def __enter__(self) -> "ServiceInstance":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
