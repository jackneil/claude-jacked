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


# --- identity watcher integration (AC5, revision 3) --------------------------


class _RecordingSink(MemoryResolverSnapshotSink):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    def publish(self, update) -> None:
        self.order.append("publish")
        super().publish(update)


class _Watcher:
    def __init__(self, order: list[str], *, sink=None, fail=False, sleep=0.0):
        self.order = order
        self.sink = sink
        self.fail = fail
        self.sleep = sleep
        self.held: tuple[str, ...] = ()
        self.runs: list[tuple] = []
        self.published_before_run: list[bool] = []

    def drain_evidence(self) -> tuple[str, ...]:
        drained, self.held = self.held, ()
        return drained

    def run(self, update, db, home) -> None:
        import time

        self.order.append("watcher")
        if self.sink is not None:
            self.published_before_run.append(update in self.sink.updates)
        if self.sleep:
            time.sleep(self.sleep)
        self.runs.append((update, db, home))
        if self.fail:
            raise RuntimeError("watcher exploded")


def _resolved(account_id: int = 11, org: str = "org-11") -> _Resolver:
    return _Resolver(
        ResolverObservation(
            ResolverState.RESOLVED,
            CredentialIdentity(account_id, organization_id=org),
            ("authority:test:ok",),
        )
    )


def test_the_watcher_runs_after_the_publish_with_the_update(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")
    order: list[str] = []
    sink = _RecordingSink(order)
    watcher = _Watcher(order, sink=sink)

    update = refresh_resolver_snapshot(
        db,
        resolver=_resolved(),
        sink=sink,
        config_root=Path(tmp_path) / ".claude",
        scope="global",
        heal=lambda _database: None,
        watcher=watcher,
    )

    assert order == ["publish", "watcher"]
    assert watcher.runs == [(update, db, Path(tmp_path))]
    assert watcher.published_before_run == [True]


def test_a_slow_watcher_never_delays_the_publish(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")
    order: list[str] = []
    sink = _RecordingSink(order)
    watcher = _Watcher(order, sink=sink, sleep=0.2)

    refresh_resolver_snapshot(
        db,
        resolver=_resolved(),
        sink=sink,
        config_root=Path(tmp_path),
        scope="global",
        heal=lambda _database: None,
        watcher=watcher,
    )

    # The snapshot was already published when the slow watcher started.
    assert watcher.published_before_run == [True]
    assert order == ["publish", "watcher"]


def test_a_failing_watcher_still_publishes_and_never_raises(tmp_path, caplog):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")
    order: list[str] = []
    sink = _RecordingSink(order)

    update = refresh_resolver_snapshot(
        db,
        resolver=_resolved(),
        sink=sink,
        config_root=Path(tmp_path),
        scope="global",
        heal=lambda _database: None,
        watcher=_Watcher(order, fail=True),
    )

    assert sink.updates == [update]
    assert update.state is ResolverState.RESOLVED
    assert "identity watcher failed" in caplog.text


def test_watcher_evidence_is_prepended_to_the_next_snapshot(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")
    order: list[str] = []
    watcher = _Watcher(order)

    first = refresh_resolver_snapshot(
        db,
        resolver=_resolved(),
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path),
        scope="global",
        heal=lambda _database: None,
        watcher=watcher,
    )
    watcher.held = ("claude-config:identity:republished",)
    second = refresh_resolver_snapshot(
        db,
        resolver=_resolved(),
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path),
        scope="global",
        heal=lambda _database: None,
        watcher=watcher,
    )

    assert "claude-config:identity:republished" not in first.evidence
    assert second.evidence[0] == "claude-config:identity:republished"
    assert "authority:test:ok" in second.evidence
    assert watcher.held == ()


def test_a_failing_evidence_drain_never_blocks_the_snapshot(tmp_path):
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")

    class _BrokenDrain(_Watcher):
        def drain_evidence(self):
            raise RuntimeError("drain exploded")

    sink = MemoryResolverSnapshotSink()
    update = refresh_resolver_snapshot(
        db,
        resolver=_resolved(),
        sink=sink,
        config_root=Path(tmp_path),
        scope="global",
        heal=lambda _database: None,
        watcher=_BrokenDrain([]),
    )

    assert sink.updates == [update]


def test_an_injected_resolver_never_runs_the_default_watcher(tmp_path, monkeypatch):
    """The default watcher reads the real authority and the real config."""
    from jacked.api import session_observer

    order: list[str] = []
    monkeypatch.setattr(session_observer, "IDENTITY_WATCHER", _Watcher(order))
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")

    refresh_resolver_snapshot(
        db,
        resolver=_resolved(),
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path),
        scope="global",
        heal=lambda _database: None,
    )

    assert order == []


def test_the_production_path_runs_the_module_watcher(tmp_path, monkeypatch):
    from jacked.api import session_observer

    order: list[str] = []
    watcher = _Watcher(order)
    monkeypatch.setattr(session_observer, "IDENTITY_WATCHER", watcher)
    monkeypatch.setattr(session_observer, "_build_resolver", lambda root, scope: _resolved())
    db = Database(str(tmp_path / "jacked.db"))
    _account(db, 11, "kim@example.com", "org-11")

    refresh_resolver_snapshot(
        db,
        sink=MemoryResolverSnapshotSink(),
        config_root=Path(tmp_path) / ".claude",
        scope="global",
        heal=lambda _database: None,
    )

    assert order == ["watcher"]
    assert watcher.runs[0][2] == Path(tmp_path)
