from __future__ import annotations

import hashlib
from pathlib import Path

from jacked.credentials.capabilities import CapabilityRegistry, resolve_executable
from jacked.credentials.models import (
    CapabilityMode,
    CredentialCapability,
    ExecutableIdentity,
    StoreDeclaration,
    StoreRole,
)


def _capability(identity: ExecutableIdentity) -> CredentialCapability:
    return CredentialCapability(
        executable=identity,
        mode=CapabilityMode.GLOBAL_UNCOOPERATIVE,
        authority=StoreDeclaration("file", "global", StoreRole.AUTHORITY),
        consumers=("claude",),
        capability_epoch=2,
        writer_protocol_epoch=2,
        provenance="shipped:test",
        registry_version=1,
    )


def test_unknown_exact_build_disables_mutation(tmp_path: Path) -> None:
    executable = tmp_path / "claude"
    executable.write_bytes(b"unknown-build")
    identity = resolve_executable(
        executable, build_version="1.0.0", config_mode="global"
    )

    resolution = CapabilityRegistry().resolve(identity)

    assert resolution.capability.mode is CapabilityMode.UNSUPPORTED
    assert resolution.can_mutate is False
    assert "exact build" in resolution.reason


def test_registry_requires_digest_version_and_config_match(tmp_path: Path) -> None:
    executable = tmp_path / "claude"
    executable.write_bytes(b"known-build")
    identity = resolve_executable(
        executable, build_version="1.0.0", config_mode="global"
    )
    registry = CapabilityRegistry((_capability(identity),))

    assert registry.resolve(identity).can_mutate is True
    changed = ExecutableIdentity(
        resolved_path=identity.resolved_path,
        sha256=hashlib.sha256(b"other").hexdigest(),
        build_version="1.0.0",
        config_mode="global",
    )
    assert registry.resolve(changed).can_mutate is False

    relocated = ExecutableIdentity(
        resolved_path="/another/non-symlink/location/claude",
        sha256=identity.sha256,
        build_version=identity.build_version,
        config_mode=identity.config_mode,
        platform_system=identity.platform_system,
        platform_machine=identity.platform_machine,
    )
    assert registry.resolve(relocated).can_mutate is True


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


def test_kill_switch_needs_fresh_resolution_to_reenable(tmp_path: Path) -> None:
    executable = tmp_path / "claude"
    executable.write_bytes(b"known-build")
    identity = resolve_executable(
        executable, build_version="1.0.0", config_mode="global"
    )
    registry = CapabilityRegistry((_capability(identity),))

    registry.disable_mutation("consumer contract drift")
    assert registry.resolve(identity).can_mutate is False
    registry.begin_fresh_resolution()
    assert registry.resolve(identity).can_mutate is True
