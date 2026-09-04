from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from jacked.credentials.capabilities import (
    CapabilityRecord,
    CapabilityRegistry,
    parse_build,
    resolve_executable,
)
from jacked.credentials.models import (
    CapabilityMode,
    CredentialCapability,
    ExecutableIdentity,
    StoreDeclaration,
    StoreRole,
)


def _template() -> CredentialCapability:
    return CredentialCapability(
        executable=ExecutableIdentity("<template>", "<template>", "1.0.0", "global", "linux", ""),
        mode=CapabilityMode.GLOBAL_UNCOOPERATIVE,
        authority=StoreDeclaration("file", "global-credential-file", StoreRole.AUTHORITY),
        consumers=("claude",),
        capability_epoch=2,
        writer_protocol_epoch=2,
        provenance="shipped:test",
        registry_version=2,
    )


def _record(**changes) -> CapabilityRecord:
    values = {
        "platform_system": "linux",
        "config_mode": "global",
        "min_build": "1.0.0",
        "inspected_through": "1.2.0",
        "capability": _template(),
    }
    values.update(changes)
    return CapabilityRecord(**values)


def _identity(**changes) -> ExecutableIdentity:
    values = {
        "resolved_path": "/opt/claude",
        "sha256": hashlib.sha256(b"any-build").hexdigest(),
        "build_version": "1.1.0",
        "config_mode": "global",
        "platform_system": "linux",
        "platform_machine": "x86_64",
    }
    values.update(changes)
    return ExecutableIdentity(**values)


def test_parse_build_reads_leading_dotted_integers() -> None:
    assert parse_build("2.1.260") == (2, 1, 260)
    assert parse_build("2.1.260-beta (Claude Code)") == (2, 1, 260)
    with pytest.raises(ValueError):
        parse_build("nightly")


def test_empty_registry_disables_mutation(tmp_path: Path) -> None:
    executable = tmp_path / "claude"
    executable.write_bytes(b"unknown-build")
    identity = resolve_executable(executable, build_version="1.0.0", config_mode="global")

    resolution = CapabilityRegistry().resolve(identity)

    assert resolution.capability.mode is CapabilityMode.UNSUPPORTED
    assert resolution.can_mutate is False
    assert "topology" in resolution.reason


def test_topology_match_ignores_hash_and_machine_but_keeps_them_as_provenance() -> None:
    registry = CapabilityRegistry((_record(),))

    for identity in (
        _identity(),
        _identity(sha256="0" * 64),
        _identity(platform_machine="aarch64"),
        _identity(resolved_path="/elsewhere/claude"),
    ):
        resolution = registry.resolve(identity)
        assert resolution.can_mutate is True
        assert resolution.capability.executable == identity
        assert resolution.capability.mode is CapabilityMode.GLOBAL_UNCOOPERATIVE
        assert "build:1.1.0" in resolution.evidence
        assert "inspected-through:1.2.0" in resolution.evidence
        assert "build-newer-than-inspected" not in resolution.evidence


def test_topology_requires_platform_and_config_mode() -> None:
    registry = CapabilityRegistry((_record(),))

    assert registry.resolve(_identity(platform_system="darwin")).can_mutate is False
    assert registry.resolve(_identity(config_mode="scoped")).can_mutate is False


def test_build_floor_and_newer_than_inspected_marker() -> None:
    registry = CapabilityRegistry((_record(),))

    below = registry.resolve(_identity(build_version="0.9.9"))
    assert below.can_mutate is False
    assert "predates" in below.reason

    newer = registry.resolve(_identity(build_version="1.3.0"))
    assert newer.can_mutate is True
    assert "build-newer-than-inspected" in newer.evidence

    assert registry.resolve(_identity(build_version="nightly")).can_mutate is False


def test_executable_resolution_follows_symlink_and_hashes_target(
    tmp_path: Path,
) -> None:
    target = tmp_path / "real-claude"
    target.write_bytes(b"binary")
    wrapper = tmp_path / "claude"
    wrapper.symlink_to(target)

    identity = resolve_executable(wrapper, build_version="1.2.3", config_mode="scoped")

    assert identity.resolved_path == str(target.resolve())
    assert identity.sha256 == hashlib.sha256(b"binary").hexdigest()


def test_kill_switch_needs_fresh_resolution_to_reenable() -> None:
    registry = CapabilityRegistry((_record(),))

    registry.disable_mutation("consumer contract drift")
    assert registry.resolve(_identity()).can_mutate is False
    registry.begin_fresh_resolution()
    assert registry.resolve(_identity()).can_mutate is True
