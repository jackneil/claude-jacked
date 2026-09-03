"""Value objects for the owned service lifecycle."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from jacked.service.spec import ServiceSpec


class InspectState(str, Enum):
    MANAGED_HEALTHY = "managed_healthy"
    MANAGED_DEGRADED = "managed_degraded"
    VERIFIED_UNMANAGED = "verified_unmanaged"
    STALE_MANIFEST = "stale_manifest"
    LEGACY_JACKED = "legacy_jacked"
    FOREIGN_LISTENER = "foreign_listener"
    SUPERVISOR_CRASH_LOOP = "supervisor_crash_loop"
    QUARANTINED = "quarantined"
    STOPPED = "stopped"


@dataclass(frozen=True)
class ServicePaths:
    root: Path
    lease: Path
    manifest: Path
    control: Path
    legacy_pid: Path

    @classmethod
    def in_directory(cls, root: Path) -> "ServicePaths":
        return cls(
            root=root,
            lease=root / "api-v2.lease",
            manifest=root / "api-v2.instance.json",
            control=root / "api-v2.control.sock",
            legacy_pid=root / "jacked-service.pid",
        )


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    creation_id: str
    executable: str


@dataclass(frozen=True)
class BindIdentity:
    host: str
    port: int
    quarantine: bool = False

    def __post_init__(self) -> None:
        if not (1 <= self.port <= 65535):
            raise ValueError("port must be between 1 and 65535")


@dataclass(frozen=True)
class InstanceManifest:
    schema_version: int
    instance_id: str
    machine_id: str
    user_id: str
    process: ProcessIdentity
    service_id: str
    protocol_version: int
    build_version: str
    generation: str
    supervisor: str
    bind: BindIdentity
    control_address: str
    control_nonce: str
    login_sessions: tuple[str, ...]
    signature: str

    @classmethod
    def create(
        cls,
        *,
        spec: ServiceSpec,
        process: ProcessIdentity,
        user_id: str,
        machine_id: str,
        bind: BindIdentity,
        control_address: str,
        instance_id: str | None = None,
        control_nonce: str | None = None,
        login_sessions: tuple[str, ...] = (),
    ) -> "InstanceManifest":
        manifest = cls(
            schema_version=1,
            instance_id=instance_id or secrets.token_urlsafe(24),
            machine_id=machine_id,
            user_id=user_id,
            process=process,
            service_id=spec.service_id,
            protocol_version=spec.protocol_version,
            build_version=spec.build_version,
            generation=spec.generation,
            supervisor=spec.supervisor.value,
            bind=bind,
            control_address=control_address,
            control_nonce=control_nonce or secrets.token_urlsafe(32),
            login_sessions=tuple(sorted(set(login_sessions))),
            signature="",
        )
        return replace(manifest, signature=manifest._calculate_signature())

    def _unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("signature", None)
        payload["login_sessions"] = list(self.login_sessions)
        return payload

    def _calculate_signature(self) -> str:
        encoded = json.dumps(
            self._unsigned_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hmac.new(
            self.control_nonce.encode("utf-8"), encoded, hashlib.sha256
        ).hexdigest()

    def verify_signature(self) -> bool:
        return hmac.compare_digest(self.signature, self._calculate_signature())

    def to_dict(self) -> dict[str, Any]:
        payload = self._unsigned_payload()
        payload["signature"] = self.signature
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "InstanceManifest":
        required = {
            "schema_version",
            "instance_id",
            "machine_id",
            "user_id",
            "process",
            "service_id",
            "protocol_version",
            "build_version",
            "generation",
            "supervisor",
            "bind",
            "control_address",
            "control_nonce",
            "login_sessions",
            "signature",
        }
        if set(payload) != required:
            raise ValueError("manifest has missing or unknown fields")
        try:
            manifest = cls(
                **{
                    **payload,
                    "process": ProcessIdentity(**payload["process"]),
                    "bind": BindIdentity(**payload["bind"]),
                    "login_sessions": tuple(payload["login_sessions"]),
                }
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("manifest structure is invalid") from exc
        if not manifest.verify_signature():
            raise ValueError("manifest signature is invalid")
        return manifest

    def replace_process(self, **changes: Any) -> "InstanceManifest":
        changed = replace(self, process=replace(self.process, **changes), signature="")
        return replace(changed, signature=changed._calculate_signature())


@dataclass(frozen=True)
class Inspection:
    state: InspectState
    manifest: InstanceManifest | None = None
    reason: str = ""

    @property
    def controllable(self) -> bool:
        return self.state in {
            InspectState.MANAGED_HEALTHY,
            InspectState.MANAGED_DEGRADED,
            InspectState.VERIFIED_UNMANAGED,
            InspectState.QUARANTINED,
        }


@dataclass(frozen=True)
class Discovery:
    host: str | None
    port: int | None
    source: str
    reason: str = ""
