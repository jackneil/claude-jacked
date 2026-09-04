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


def test_unstamped_payload_is_unusable_with_named_evidence() -> None:
    payload = CredentialPayload.from_mapping({"claudeAiOauth": {"accessToken": "a"}})
    resolver = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", payload),
            "file": MemoryCredentialStore("file", payload),
        },
    )

    observation = resolver.resolve()

    assert observation.state is ResolverState.UNUSABLE
    assert "identity:stamp-absent" in observation.evidence


def test_observation_resolves_from_the_authority_when_the_mirror_diverges() -> None:
    resolver = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", _payload(1)),
            "file": MemoryCredentialStore("file", _payload(2)),
        },
        require_mirror_consensus=False,
    )

    observation = resolver.resolve()

    assert observation.state is ResolverState.RESOLVED
    assert observation.identity.account_id == 1
    assert "authority:keychain:ok" in observation.evidence
    assert "required_mirror:file:divergent" in observation.evidence


def test_observation_names_a_missing_mirror_but_still_resolves() -> None:
    resolver = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", _payload(1)),
            "file": MemoryCredentialStore("file", None),
        },
        require_mirror_consensus=False,
    )

    observation = resolver.resolve()

    assert observation.state is ResolverState.RESOLVED
    assert observation.identity.account_id == 1
    assert "required_mirror:file:missing" in observation.evidence


def test_observation_agreeing_mirror_is_marked_ok() -> None:
    resolver = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", _payload(1)),
            "file": MemoryCredentialStore("file", _payload(1)),
        },
        require_mirror_consensus=False,
    )

    observation = resolver.resolve()

    assert observation.state is ResolverState.RESOLVED
    assert "required_mirror:file:ok" in observation.evidence


def test_observation_still_fails_closed_on_the_authority() -> None:
    missing = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", None),
            "file": MemoryCredentialStore("file", _payload(2)),
        },
        require_mirror_consensus=False,
    ).resolve()
    assert missing.state is ResolverState.MISSING

    unstamped = CredentialPayload.from_mapping({"claudeAiOauth": {"accessToken": "secret"}})
    unusable = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", unstamped),
            "file": MemoryCredentialStore("file", _payload(2)),
        },
        require_mirror_consensus=False,
    ).resolve()
    assert unusable.state is ResolverState.UNUSABLE
    assert "identity:stamp-absent" in unusable.evidence


def test_consensus_default_is_unchanged() -> None:
    resolver = CanonicalCredentialResolver(
        _capability(),
        {
            "keychain": MemoryCredentialStore("keychain", _payload(1)),
            "file": MemoryCredentialStore("file", _payload(2)),
        },
    )

    assert resolver.resolve().state is ResolverState.CONFLICT
