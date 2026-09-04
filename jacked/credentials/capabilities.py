"""Topology-keyed credential capability registry."""

from __future__ import annotations

import hashlib
import platform
import re
from dataclasses import dataclass
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


_BUILD_RE = re.compile(r"^(\d+(?:\.\d+)*)")


def parse_build(version: str) -> tuple[int, ...]:
    """Parse the leading dotted integers of a build version ("2.1.260" -> (2, 1, 260))."""
    match = _BUILD_RE.match(version or "")
    if match is None:
        raise ValueError(f"unparseable build version: {version!r}")
    return tuple(int(part) for part in match.group(1).split("."))


@dataclass(frozen=True)
class CapabilityRecord:
    """One certified credential-store topology for a platform and config mode.

    Certification is keyed by where Claude Code keeps its credentials, which
    is stable across builds, rather than by executable bytes, which change on
    every release. ``min_build`` is the oldest build the topology was verified
    against and ``inspected_through`` the newest; a newer build still resolves
    and is flagged in the evidence so mutation can be more conservative.
    """

    platform_system: str
    config_mode: str
    min_build: str
    inspected_through: str
    capability: CredentialCapability


NEWER_THAN_INSPECTED = "build-newer-than-inspected"


class CapabilityRegistry:
    """Conservative registry keyed by platform and config mode."""

    def __init__(self, records: tuple[CapabilityRecord, ...] = ()) -> None:
        self._records = {
            (record.platform_system, record.config_mode): record for record in records
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
        record = self._records.get((identity.platform_system, identity.config_mode))
        if record is None:
            return _unsupported(
                identity,
                "no certified credential-store topology for "
                f"{identity.platform_system or 'unknown'}/{identity.config_mode}",
            )
        try:
            build = parse_build(identity.build_version)
        except ValueError:
            return _unsupported(identity, "Claude build version is not parseable")
        if build < parse_build(record.min_build):
            return _unsupported(
                identity,
                f"Claude build {identity.build_version} predates the certified "
                f"floor {record.min_build}",
            )
        evidence = [
            f"build:{identity.build_version}",
            f"inspected-through:{record.inspected_through}",
        ]
        if build > parse_build(record.inspected_through):
            evidence.append(NEWER_THAN_INSPECTED)
        capability = CredentialCapability(
            **{**record.capability.__dict__, "executable": identity}
        )
        return CapabilityResolution(
            capability, True, "certified topology matched", tuple(evidence)
        )


DEFAULT_REGISTRY = CapabilityRegistry()
