"""Canonical credential identity resolution and secret-free snapshots."""

from __future__ import annotations

import json
import os
import stat
import tempfile
import time
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol

from .models import CredentialCapability, CredentialIdentity, StoreStatus
from .store import CredentialStore

if TYPE_CHECKING:
    from .canonical import CredentialPayload

SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_FILENAME = "jacked-resolver-snapshot.json"


class ResolverState(str, Enum):
    RESOLVED = "resolved"
    CONFLICT = "conflict"
    MISSING = "missing"
    UNUSABLE = "unusable"
    STALE = "stale"
    UNSUPPORTED = "unsupported"


@dataclass(frozen=True)
class ResolverObservation:
    state: ResolverState
    identity: CredentialIdentity
    evidence: tuple[str, ...]


@dataclass(frozen=True)
class ResolverSnapshot:
    published_at: float
    fresh_until: float
    scope: str
    state: ResolverState
    evidence: tuple[str, ...]
    credential_revision: str | None
    desired: CredentialIdentity | None
    observed: CredentialIdentity | None


@dataclass(frozen=True)
class SnapshotUpdate:
    scope: str
    state: ResolverState
    evidence: tuple[str, ...]
    credential_revision: str | None
    desired: CredentialIdentity | None
    observed: CredentialIdentity | None


class ResolverSnapshotSink(Protocol):
    def publish(self, update: SnapshotUpdate) -> None: ...


class FileResolverSnapshotSink:
    """Publish resolver updates with a bounded freshness lifetime."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        ttl_seconds: float = 30.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._path = path or default_snapshot_path()
        self._ttl_seconds = ttl_seconds
        self._clock = clock

    def publish(self, update: SnapshotUpdate) -> None:
        published_at = self._clock()
        snapshot = ResolverSnapshot(
            published_at=published_at,
            fresh_until=published_at + self._ttl_seconds,
            scope=update.scope,
            state=update.state,
            evidence=update.evidence,
            credential_revision=update.credential_revision,
            desired=update.desired,
            observed=update.observed,
        )
        publish_snapshot(self._path, snapshot)


class MemoryResolverSnapshotSink:
    def __init__(self) -> None:
        self.updates: list[SnapshotUpdate] = []

    def publish(self, update: SnapshotUpdate) -> None:
        self.updates.append(update)


class CanonicalCredentialResolver:
    """Resolve the active identity without guessing, in one of two modes.

    ``require_mirror_consensus=True`` (the default, used by write
    verification) resolves only when the authority and every required mirror
    describe the same payload. ``False`` (used by observation) lets the
    authority decide and reports required-mirror drift as evidence, because
    Claude Code refreshes the authority alone between jacked writes.
    """

    def __init__(
        self,
        capability: CredentialCapability,
        stores: Mapping[str, CredentialStore],
        *,
        require_mirror_consensus: bool = True,
    ) -> None:
        self._capability = capability
        self._stores = stores
        self._require_mirror_consensus = require_mirror_consensus

    def resolve(self) -> ResolverObservation:
        if self._require_mirror_consensus:
            return self._resolve_by_consensus()
        return self._resolve_from_authority()

    def _resolve_from_authority(self) -> ResolverObservation:
        """Report what the runtime will use: the authority decides, mirrors are evidence.

        Claude Code refreshes the authority alone, so a required mirror can lag
        it between jacked writes. Hiding the identity behind CONFLICT on every
        lag blanked the menu bar. Write verification keeps demanding consensus
        in the transaction engine; only observation takes this path.
        """
        authority = self._capability.authority
        store = self._stores.get(authority.locator)
        if store is None:
            return ResolverObservation(
                ResolverState.UNUSABLE, CredentialIdentity(), (f"missing-adapter:{authority.locator}",)
            )
        result = store.read()
        evidence = [f"{authority.role.value}:{authority.name}:{result.status.value}"]
        if result.status is StoreStatus.MISSING:
            return ResolverObservation(ResolverState.MISSING, CredentialIdentity(), tuple(evidence))
        if result.status is not StoreStatus.OK or result.payload is None:
            return ResolverObservation(ResolverState.UNUSABLE, CredentialIdentity(), tuple(evidence))
        evidence.extend(self._mirror_evidence(result.payload))
        identity = result.payload.identity
        if identity.account_id is None:
            return ResolverObservation(
                ResolverState.UNUSABLE, CredentialIdentity(), (*evidence, "identity:stamp-absent")
            )
        return ResolverObservation(ResolverState.RESOLVED, identity, tuple(evidence))

    def _mirror_evidence(self, authority_payload: CredentialPayload) -> list[str]:
        evidence = []
        for declaration in self._capability.required_mirrors:
            store = self._stores.get(declaration.locator)
            if store is None:
                evidence.append(f"{declaration.role.value}:{declaration.name}:missing-adapter")
                continue
            result = store.read()
            if result.status is StoreStatus.OK and result.payload is not None:
                same = (
                    result.payload.digest == authority_payload.digest
                    and result.payload.identity == authority_payload.identity
                )
                verdict = "ok" if same else "divergent"
            else:
                verdict = result.status.value
            evidence.append(f"{declaration.role.value}:{declaration.name}:{verdict}")
        return evidence

    def _resolve_by_consensus(self) -> ResolverObservation:
        declarations = (self._capability.authority, *self._capability.required_mirrors)
        observations = []
        evidence = []
        for declaration in declarations:
            store = self._stores.get(declaration.locator)
            if store is None:
                return ResolverObservation(
                    ResolverState.UNUSABLE,
                    CredentialIdentity(),
                    (f"missing-adapter:{declaration.locator}",),
                )
            result = store.read()
            evidence.append(
                f"{declaration.role.value}:{declaration.name}:{result.status.value}"
            )
            if result.status is StoreStatus.MISSING:
                return ResolverObservation(
                    ResolverState.MISSING, CredentialIdentity(), tuple(evidence)
                )
            if result.status is not StoreStatus.OK or result.payload is None:
                return ResolverObservation(
                    ResolverState.UNUSABLE, CredentialIdentity(), tuple(evidence)
                )
            observations.append(result.payload)
        digests = {payload.digest for payload in observations}
        identities = {payload.identity for payload in observations}
        if len(digests) != 1 or len(identities) != 1:
            return ResolverObservation(
                ResolverState.CONFLICT, CredentialIdentity(), tuple(evidence)
            )
        identity = observations[0].identity
        if identity.account_id is None:
            # Claude Code wrote this credential itself, or a jacked write was
            # replaced. Name it instead of hiding it inside a bare UNUSABLE.
            return ResolverObservation(
                ResolverState.UNUSABLE,
                CredentialIdentity(),
                (*evidence, "identity:stamp-absent"),
            )
        return ResolverObservation(ResolverState.RESOLVED, identity, tuple(evidence))


def default_snapshot_path(config_dir: Path | None = None) -> Path:
    directory = config_dir
    if directory is None:
        configured = os.environ.get("CLAUDE_CONFIG_DIR")
        directory = Path(configured) if configured else Path.home() / ".claude"
    return directory / SNAPSHOT_FILENAME


def _identity_dict(identity: CredentialIdentity | None) -> dict | None:
    return asdict(identity) if identity is not None else None


def publish_snapshot(path: Path, snapshot: ResolverSnapshot) -> None:
    """Atomically publish the fixed, token-free resolver snapshot schema."""
    value = {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "published_at": snapshot.published_at,
        "fresh_until": snapshot.fresh_until,
        "scope": snapshot.scope,
        "state": snapshot.state.value,
        "evidence": list(snapshot.evidence),
        "credential_revision": snapshot.credential_revision,
        "desired": _identity_dict(snapshot.desired),
        "observed": _identity_dict(snapshot.observed),
    }
    _validate_snapshot_path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _validate_snapshot_path(path)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=".resolver-snapshot-", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(value, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    finally:
        try:
            Path(temp_name).unlink(missing_ok=True)
        except OSError:
            pass


def _validate_snapshot_path(path: Path) -> None:
    """Reject a link-like configured parent or snapshot target.

    The configured directory is the trust boundary. Walking above it would
    reject platform-owned aliases such as macOS ``/var -> /private/var``.
    """
    current = path.parent.absolute()
    trusted_root = current
    while True:
        if current.is_symlink():
            raise OSError("resolver snapshot parent contains a symlink")
        if current.exists():
            current_stat = current.stat(follow_symlinks=False)
            if not stat.S_ISDIR(current_stat.st_mode):
                raise OSError("resolver snapshot parent is not a real directory")
            if getattr(current_stat, "st_file_attributes", 0) & 0x400:
                raise OSError("resolver snapshot parent is a reparse point")
        if current == trusted_root:
            break
        current = current.parent
    if path.is_symlink():
        raise OSError("refusing resolver snapshot symlink")
    if path.exists():
        target_stat = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(target_stat.st_mode):
            raise OSError("resolver snapshot target is not a regular file")
        if getattr(target_stat, "st_file_attributes", 0) & 0x400:
            raise OSError("resolver snapshot target is a reparse point")
        if target_stat.st_nlink != 1:
            raise OSError("refusing hard-linked resolver snapshot")
