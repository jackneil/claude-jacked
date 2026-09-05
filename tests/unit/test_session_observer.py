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


def test_the_authority_heal_runs_before_the_observation(tmp_path):
    """A foreign write is repaired first, so the published snapshot
    describes the healed authority and names the repair as evidence."""
    from jacked.api.authority_guard import HealResult

    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")
    db.set_setting("desired_account_id", 11)
    order: list[str] = []

    class _OrderedResolver(_Resolver):
        def resolve(self) -> ResolverObservation:
            order.append("resolve")
            return super().resolve()

    def heal(database):
        assert database is db
        order.append("heal")
        return HealResult("reasserted", foreign_account_id=3, desired_account_id=11)

    resolver = _OrderedResolver(
        ResolverObservation(
            ResolverState.RESOLVED,
            CredentialIdentity(11, organization_id="org-11"),
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
        heal=heal,
    )

    assert order == ["heal", "resolve"]
    assert "authority:foreign-write:reasserted" in update.evidence


def test_a_healed_authority_without_a_repair_adds_no_evidence(tmp_path):
    from jacked.api.authority_guard import HealResult

    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")
    resolver = _Resolver(
        ResolverObservation(
            ResolverState.RESOLVED,
            CredentialIdentity(11, organization_id="org-11"),
            ("authority:test:ok",),
        )
    )

    update = refresh_resolver_snapshot(
        db,
        resolver=resolver,
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path),
        scope="global",
        heal=lambda _database: HealResult("none"),
    )

    assert not any(item.startswith("authority:foreign-write") for item in update.evidence)


def test_a_failing_heal_never_blocks_the_snapshot(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")

    def heal(_database):
        raise RuntimeError("guard exploded")

    update = refresh_resolver_snapshot(
        db,
        resolver=_Resolver(
            ResolverObservation(
                ResolverState.RESOLVED,
                CredentialIdentity(11, organization_id="org-11"),
                ("authority:test:ok",),
            )
        ),
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path),
        scope="global",
        heal=heal,
    )

    assert update.state is ResolverState.RESOLVED


def test_an_injected_resolver_never_touches_the_runtime_authority(tmp_path, monkeypatch):
    """Only the production path (no injected resolver) runs the default
    guard, which reads the real credential authority."""
    from jacked.api import session_observer

    calls = []
    monkeypatch.setattr(
        session_observer, "_default_heal", lambda database: calls.append(database)
    )
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")

    refresh_resolver_snapshot(
        db,
        resolver=_Resolver(
            ResolverObservation(ResolverState.RESOLVED, CredentialIdentity(11), ())
        ),
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path),
        scope="global",
    )

    assert calls == []
