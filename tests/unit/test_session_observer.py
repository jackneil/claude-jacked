"""Tests for the bounded service-side session observation worker."""

from pathlib import Path

import pytest

from jacked.api.session_observer import (
    refresh_resolver_snapshot,
    run_session_observation_pass,
)
from jacked.credentials.models import CredentialIdentity
from jacked.credentials.resolver import (
    MemoryResolverSnapshotSink,
    ResolverObservation,
    ResolverState,
)
from jacked.web.database import Database


class _Resolver:
    def __init__(self, observation: ResolverObservation) -> None:
        self.observation = observation
        self.calls = 0

    def resolve(self) -> ResolverObservation:
        self.calls += 1
        return self.observation


def _account(db: Database, account_id: int, email: str, organization_id: str):
    with db._writer() as conn:
        conn.execute(
            """INSERT INTO accounts
               (id, email, organization_uuid, provider, access_token,
                expires_at, is_active, is_deleted)
               VALUES (?, ?, ?, 'claude', 'access', 1999999999, 1, 0)""",
            (account_id, email, organization_id),
        )


def test_refresh_publishes_db_qualified_identity_without_revision(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 7, "jack@example.com", "org-7")
    db.set_setting("desired_account_id", 7)
    resolver = _Resolver(
        ResolverObservation(
            ResolverState.RESOLVED,
            CredentialIdentity(7, organization_id="org-7"),
            ("authority:test:ok",),
        )
    )
    sink = MemoryResolverSnapshotSink()

    update = refresh_resolver_snapshot(
        db,
        resolver=resolver,
        sink=sink,
        config_root=Path(tmp_path),
        scope="global",
    )

    assert update.state is ResolverState.RESOLVED
    assert update.observed == CredentialIdentity(7, "jack@example.com", "org-7")
    assert update.desired == update.observed
    assert update.credential_revision is None
    assert sink.updates == [update]
    assert resolver.calls == 1


def test_refresh_reports_desired_observed_conflict(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 7, "jack@example.com", "org-7")
    _account(db, 8, "other@example.com", "org-8")
    db.set_setting("desired_account_id", 8)
    resolver = _Resolver(
        ResolverObservation(
            ResolverState.RESOLVED,
            CredentialIdentity(7, organization_id="org-7"),
            ("authority:test:ok",),
        )
    )

    update = refresh_resolver_snapshot(
        db,
        resolver=resolver,
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path),
        scope="global",
    )

    assert update.state is ResolverState.CONFLICT
    assert update.desired == CredentialIdentity(8, "other@example.com", "org-8")
    assert update.observed == CredentialIdentity(7, "jack@example.com", "org-7")
    assert "desired-default:conflict" in update.evidence


def test_pass_drains_bounded_requests_only_after_refresh(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    db.enqueue_session_observation(
        "session-1",
        "UserPromptSubmit",
        idempotency_key="one",
    )
    calls = []

    def refresh(database):
        calls.append(database)
        return object()

    assert run_session_observation_pass(db, limit=1, refresh=refresh) == 1
    assert calls == [db]
    assert db.drain_session_observations() == []


def test_failed_refresh_restores_drained_requests(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    db.enqueue_session_observation(
        "session-1",
        "Stop",
        credential_revision="revision-1",
        launch_nonce="launch-1",
        repo_path="/repo",
        idempotency_key="one",
    )

    def fail(_database):
        raise OSError("snapshot unavailable")

    with pytest.raises(OSError, match="snapshot unavailable"):
        run_session_observation_pass(db, refresh=fail)

    rows = db.drain_session_observations()
    assert len(rows) == 1
    assert rows[0]["idempotency_key"] == "one"
    assert rows[0]["credential_revision"] == "revision-1"
    assert rows[0]["launch_nonce"] == "launch-1"
