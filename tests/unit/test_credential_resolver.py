from __future__ import annotations

import json
from pathlib import Path

import pytest

from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.models import (
    CapabilityMode,
    CredentialCapability,
    CredentialIdentity,
    ExecutableIdentity,
    StoreDeclaration,
    StoreRole,
)
from jacked.credentials.resolver import (
    CanonicalCredentialResolver,
    ResolverSnapshot,
    ResolverState,
    publish_snapshot,
)
from jacked.credentials.store import MemoryCredentialStore


def _payload(account_id: int) -> CredentialPayload:
    return CredentialPayload.from_mapping(
        {"_jackedAccountId": account_id, "claudeAiOauth": {"accessToken": "secret"}}
    )


def _capability() -> CredentialCapability:
    return CredentialCapability(
        executable=ExecutableIdentity("/claude", "a" * 64, "1", "global"),
        mode=CapabilityMode.GLOBAL_COOPERATIVE,
        authority=StoreDeclaration("keychain", "keychain", StoreRole.AUTHORITY),
        required_mirrors=(StoreDeclaration("file", "file", StoreRole.REQUIRED_MIRROR),),
        consumers=("claude",),
        capability_epoch=2,
        writer_protocol_epoch=2,
        provenance="test",
        registry_version=1,
    )


def test_resolver_reports_conflict_instead_of_precedence_guess() -> None:
    resolver = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", _payload(1)),
            "file": MemoryCredentialStore("file", _payload(2)),
        },
    )

    observation = resolver.resolve()

    assert observation.state is ResolverState.CONFLICT
    assert observation.identity == CredentialIdentity()


def test_snapshot_contains_no_secrets_or_unkeyed_digests(tmp_path: Path) -> None:
    path = tmp_path / "jacked-resolver-snapshot.json"
    snapshot = ResolverSnapshot(
        published_at=100.0,
        fresh_until=130.0,
        scope="global",
        state=ResolverState.RESOLVED,
        evidence=("authority:keychain",),
        credential_revision="switch:op-1",
        desired=CredentialIdentity(account_id=2, email="two@example.com"),
        observed=CredentialIdentity(account_id=2, email="two@example.com"),
    )

    publish_snapshot(path, snapshot)
    raw = path.read_text(encoding="utf-8")
    value = json.loads(raw)

    assert set(value) == {
        "schema_version",
        "published_at",
        "fresh_until",
        "scope",
        "state",
        "evidence",
        "credential_revision",
        "desired",
        "observed",
    }
    assert "accessToken" not in raw
    assert "sha256" not in raw


def test_snapshot_rejects_symlink_target(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "jacked-resolver-snapshot.json"
    link.symlink_to(real)
    snapshot = ResolverSnapshot(
        1.0, 2.0, "global", ResolverState.MISSING, (), None, None, None
    )

    with pytest.raises(OSError, match="symlink"):
        publish_snapshot(link, snapshot)

    assert real.read_text(encoding="utf-8") == "{}"


def test_snapshot_rejects_hard_link_target(tmp_path: Path) -> None:
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "jacked-resolver-snapshot.json"
    link.hardlink_to(real)
    snapshot = ResolverSnapshot(
        1.0, 2.0, "global", ResolverState.MISSING, (), None, None, None
    )

    with pytest.raises(OSError, match="hard-linked"):
        publish_snapshot(link, snapshot)
