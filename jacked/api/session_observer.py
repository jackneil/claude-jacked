"""Bounded service-side refresh for session credential observations.

Hooks write only non-secret observation requests to SQLite.  This worker drains
those requests off the hook path, re-observes the configured credential
authority, and publishes the canonical secret-free resolver snapshot.  It
never writes a credential store and never claims that a running provider
request used the newly observed identity.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Callable, Protocol

from jacked.credentials.file_store import FileCredentialStore
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
    FileResolverSnapshotSink,
    ResolverObservation,
    ResolverSnapshotSink,
    ResolverState,
    SnapshotUpdate,
)

logger = logging.getLogger(__name__)

SESSION_OBSERVER_INTERVAL_SECONDS = 10.0
SESSION_OBSERVER_BATCH_LIMIT = 100


class SessionObservationDatabase(Protocol):
    def drain_session_observations(self, limit: int = 100) -> list[dict]: ...

    def enqueue_session_observation(
        self,
        session_id: str,
        event_kind: str,
        *,
        credential_revision: str | None = None,
        launch_nonce: str | None = None,
        repo_path: str | None = None,
        idempotency_key: str,
        max_rows: int = 1000,
    ) -> None: ...

    def get_account(self, account_id: int) -> dict | None: ...

    def get_setting(self, key: str) -> str | None: ...


def _config_root() -> tuple[Path, str]:
    configured = os.environ.get("CLAUDE_CONFIG_DIR")
    if configured:
        root = Path(configured).expanduser()
        default_root = Path.home() / ".claude"
        return root, "global" if root == default_root else "scoped"
    return Path.home() / ".claude", "global"


def _build_resolver(config_root: Path, scope: str) -> CanonicalCredentialResolver:
    """Build a read-only resolver for the authority used by this config root."""
    executable = ExecutableIdentity(
        resolved_path="service-observer",
        sha256="observation-only",
        build_version="unknown",
        config_mode=scope,
    )
    if scope == "global" and sys.platform == "darwin":
        from jacked.credentials.macos_store import MacOSCredentialStore

        authority = MacOSCredentialStore()
        declaration = StoreDeclaration(
            "macos-keychain", authority.locator, StoreRole.AUTHORITY
        )
        stores = {authority.locator: authority}
    else:
        authority = FileCredentialStore(
            config_root / ".credentials.json", trusted_root=config_root
        )
        declaration = StoreDeclaration(
            "configured-credential-file", authority.locator, StoreRole.AUTHORITY
        )
        stores = {authority.locator: authority}

    capability = CredentialCapability(
        executable=executable,
        mode=(
            CapabilityMode.SCOPED_COOPERATIVE
            if scope == "scoped"
            else CapabilityMode.GLOBAL_UNCOOPERATIVE
        ),
        authority=declaration,
        consumers=("session-observer",),
        capability_epoch=1,
        writer_protocol_epoch=2,
        provenance="read-only-runtime-authority-observation",
        registry_version=1,
    )
    return CanonicalCredentialResolver(capability, stores)


def _account_identity(
    db: SessionObservationDatabase | None, account_id: int | None
) -> CredentialIdentity | None:
    if db is None or account_id is None:
        return None
    account = db.get_account(account_id)
    if not account or account.get("is_deleted"):
        return None
    email = account.get("email")
    if not isinstance(email, str) or not email:
        return None
    organization_id = account.get("organization_uuid") or None
    if organization_id is not None and not isinstance(organization_id, str):
        return None
    return CredentialIdentity(account_id, email, organization_id)


def _desired_identity(
    db: SessionObservationDatabase | None,
) -> CredentialIdentity | None:
    if db is None:
        return None
    raw_id = db.get_setting("desired_account_id") or db.get_setting("active_account_id")
    try:
        account_id = int(raw_id) if raw_id is not None else None
    except (TypeError, ValueError):
        return None
    if account_id is None or account_id <= 0:
        return None
    return _account_identity(db, account_id)


def _qualify_observation(
    db: SessionObservationDatabase | None,
    observation: ResolverObservation,
    desired: CredentialIdentity | None,
) -> tuple[ResolverState, CredentialIdentity | None, tuple[str, ...]]:
    state = observation.state
    evidence = observation.evidence
    observed: CredentialIdentity | None = None
    if state is ResolverState.RESOLVED:
        observed = _account_identity(db, observation.identity.account_id)
        if observed is None:
            state = ResolverState.UNUSABLE
            evidence += ("account-metadata:missing",)
        elif (
            observation.identity.organization_id is not None
            and observation.identity.organization_id != observed.organization_id
        ):
            state = ResolverState.CONFLICT
            evidence += ("account-metadata:organization-conflict",)
        elif desired is not None and desired.account_id != observed.account_id:
            state = ResolverState.CONFLICT
            evidence += ("desired-default:conflict",)
    return state, observed, evidence


def refresh_resolver_snapshot(
    db: SessionObservationDatabase | None,
    *,
    resolver: CanonicalCredentialResolver | None = None,
    sink: ResolverSnapshotSink | None = None,
    config_root: Path | None = None,
    scope: str | None = None,
) -> SnapshotUpdate:
    """Re-observe one authority and atomically publish a secret-free snapshot."""
    if config_root is None or scope is None:
        default_root, default_scope = _config_root()
        config_root = config_root or default_root
        scope = scope or default_scope
    try:
        resolver = resolver or _build_resolver(config_root, scope)
        observation = resolver.resolve()
    except Exception:
        # A locked/unavailable native authority is an observed unusable state,
        # not permission to keep presenting an older identity as fresh.
        observation = ResolverObservation(
            ResolverState.UNUSABLE,
            CredentialIdentity(),
            ("authority:observer:unavailable",),
        )
    desired = _desired_identity(db)
    state, observed, evidence = _qualify_observation(db, observation, desired)
    update = SnapshotUpdate(
        scope=scope,
        state=state,
        evidence=evidence,
        # A revision is an ordering claim. A passive read has no transaction
        # witness, so it intentionally leaves this unknown.
        credential_revision=None,
        desired=desired,
        observed=observed,
    )
    (
        sink or FileResolverSnapshotSink(config_root / "jacked-resolver-snapshot.json")
    ).publish(update)
    return update


def _requeue_observations(db: SessionObservationDatabase, rows: list[dict]) -> None:
    for row in rows:
        db.enqueue_session_observation(
            row["session_id"],
            row["event_kind"],
            credential_revision=row.get("credential_revision"),
            launch_nonce=row.get("launch_nonce"),
            repo_path=row.get("repo_path"),
            idempotency_key=row["idempotency_key"],
        )


def run_session_observation_pass(
    db: SessionObservationDatabase | None,
    *,
    limit: int = SESSION_OBSERVER_BATCH_LIMIT,
    refresh: Callable[[SessionObservationDatabase | None], SnapshotUpdate]
    | None = None,
) -> int:
    """Run one bounded pass, returning the number of acknowledged requests.

    Requests are drained before the authority read, ensuring the published
    snapshot was observed after each acknowledged hook event. On any failure,
    drained requests are requeued with their original idempotency keys.
    """
    rows = db.drain_session_observations(limit) if db is not None else []
    try:
        (refresh or refresh_resolver_snapshot)(db)
    except Exception:
        if db is not None and rows:
            try:
                _requeue_observations(db, rows)
            except Exception:
                logger.exception("Failed to restore session observation requests")
        raise
    return len(rows)


async def session_observation_loop(
    app,
    interval: float = SESSION_OBSERVER_INTERVAL_SECONDS,
    *,
    limit: int = SESSION_OBSERVER_BATCH_LIMIT,
) -> None:
    """Continuously refresh before TTL expiry without blocking the event loop."""
    while True:
        try:
            db = getattr(app.state, "db", None)
            acknowledged = await asyncio.to_thread(
                run_session_observation_pass, db, limit=limit
            )
            if acknowledged:
                logger.debug(
                    "Refreshed resolver snapshot for %d session event(s)",
                    acknowledged,
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Session observation refresh failed", exc_info=True)
        await asyncio.sleep(interval)
