"""Exact-build credential capability registry."""

from __future__ import annotations

import hashlib
import platform
from pathlib import Path

from .models import (
    CapabilityMode,
    CapabilityResolution,
    CredentialCapability,
    ExecutableIdentity,
    StoreDeclaration,
    StoreRole,
)


def resolve_executable(
    executable: Path, *, build_version: str, config_mode: str
) -> ExecutableIdentity:
    """Resolve symlinks and identify the exact executable bytes."""
    resolved = executable.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError("Claude executable is not a regular file")
    hasher = hashlib.sha256()
    with resolved.open("rb") as executable_file:
        for chunk in iter(lambda: executable_file.read(1024 * 1024), b""):
            hasher.update(chunk)
    digest = hasher.hexdigest()
    return ExecutableIdentity(
        str(resolved),
        digest,
        build_version,
        config_mode,
        platform.system().lower(),
        platform.machine().lower(),
    )


def _unsupported(identity: ExecutableIdentity, reason: str) -> CapabilityResolution:
    capability = CredentialCapability(
        executable=identity,
        mode=CapabilityMode.UNSUPPORTED,
        authority=StoreDeclaration("unknown", "unknown", StoreRole.AUTHORITY),
        consumers=(),
        capability_epoch=0,
        writer_protocol_epoch=2,
        provenance="fail-closed",
        registry_version=1,
    )
    return CapabilityResolution(capability, False, reason)


class CapabilityRegistry:
    """Conservative registry keyed by artifact, platform, and config mode."""

    def __init__(self, capabilities: tuple[CredentialCapability, ...] = ()) -> None:
        self._records = {
            self._key(record.executable): record for record in capabilities
        }
        self._disabled_reason: str | None = None
        self._fresh_generation = 0
        self._disabled_generation: int | None = None

    def disable_mutation(self, reason: str) -> None:
        """Latch mutation off until a caller starts a fresh resolution."""
        self._disabled_reason = reason
        self._disabled_generation = self._fresh_generation

    def begin_fresh_resolution(self) -> None:
        """Start a new explicit capability-resolution generation."""
        self._fresh_generation += 1
        self._disabled_reason = None
        self._disabled_generation = None

    def resolve(self, identity: ExecutableIdentity) -> CapabilityResolution:
        if self._disabled_reason is not None:
            return _unsupported(
                identity, f"mutation kill switch: {self._disabled_reason}"
            )
        capability = self._records.get(self._key(identity))
        if capability is None:
            return _unsupported(
                identity, "exact build/config capability is not certified"
            )
        capability = CredentialCapability(
            **{**capability.__dict__, "executable": identity}
        )
        return CapabilityResolution(capability, True, "exact capability record matched")

    @staticmethod
    def _key(identity: ExecutableIdentity) -> tuple[str, str, str, str, str]:
        return (
            identity.sha256,
            identity.build_version,
            identity.config_mode,
            identity.platform_system,
            identity.platform_machine,
        )


DEFAULT_REGISTRY = CapabilityRegistry()
