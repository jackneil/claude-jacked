"""Canonical identity contract for the jacked background service.

The generation is content-addressed.  Supervisor code may act on an artifact
only when its owner marker and generation match this object exactly.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class SupervisorKind(str, Enum):
    LAUNCHD = "launchd"
    SYSTEMD_USER = "systemd-user"
    TASK_SCHEDULER = "task-scheduler"
    MANUAL = "manual"


@dataclass(frozen=True)
class ServiceSpec:
    """Everything that must match before lifecycle control is authorized."""

    service_id: str
    protocol_version: int
    build_version: str
    runtime_path: str
    launcher_path: str
    launcher_sha256: str
    supervisor: SupervisorKind
    arguments: tuple[str, ...]
    schema_version: int = 1
    owner: str = "claude-jacked"

    def __post_init__(self) -> None:
        if not self.service_id or not self.build_version:
            raise ValueError("service_id and build_version are required")
        if self.protocol_version < 1 or self.schema_version < 1:
            raise ValueError("protocol and schema versions must be positive")
        self._validate_path(self.runtime_path, "runtime_path", require_resolved=True)
        self._validate_path(self.launcher_path, "launcher_path", require_resolved=False)
        if len(self.launcher_sha256) != 64 or any(
            char not in "0123456789abcdef" for char in self.launcher_sha256
        ):
            raise ValueError("launcher_sha256 must be a lowercase SHA-256 digest")
        if not self.arguments or self.arguments[0] != "-I":
            raise ValueError("service Python must start in isolated mode (-I)")
        if any("\x00" in value for value in self.arguments):
            raise ValueError("arguments cannot contain NUL")

    @staticmethod
    def _validate_path(value: str, name: str, *, require_resolved: bool) -> None:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"{name} must be absolute")
        normalized = os.path.normpath(value)
        if normalized != value:
            raise ValueError(f"{name} must be normalized")
        if require_resolved and os.path.realpath(value) != value:
            raise ValueError(f"{name} must be resolved and not a symlink")

    def constructor_fields(self) -> dict[str, Any]:
        return {
            "service_id": self.service_id,
            "protocol_version": self.protocol_version,
            "build_version": self.build_version,
            "runtime_path": self.runtime_path,
            "launcher_path": self.launcher_path,
            "launcher_sha256": self.launcher_sha256,
            "supervisor": self.supervisor,
            "arguments": self.arguments,
            "schema_version": self.schema_version,
            "owner": self.owner,
        }

    def canonical_payload(self) -> dict[str, Any]:
        payload = self.constructor_fields()
        payload["supervisor"] = self.supervisor.value
        payload["arguments"] = list(self.arguments)
        return payload

    @property
    def generation(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(), sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def artifact_marker(self) -> dict[str, str | int]:
        return {
            "owner": self.owner,
            "service_id": self.service_id,
            "schema_version": self.schema_version,
            "protocol_version": self.protocol_version,
            "generation": self.generation,
        }

    def matches_artifact_marker(self, marker: dict[str, Any]) -> bool:
        return marker == self.artifact_marker()
