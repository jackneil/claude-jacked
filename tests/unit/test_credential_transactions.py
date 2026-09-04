from __future__ import annotations

from dataclasses import replace

from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.models import (
    CapabilityMode,
    CredentialCapability,
    CredentialIdentity,
    ExecutableIdentity,
    InteractionMode,
    StoreDeclaration,
    StoreRole,
    StoreStatus,
    StoreWriteResult,
    SwitchContext,
    SwitchOutcome,
)
from jacked.credentials.repository import InMemoryCredentialSwitchRepository
from jacked.credentials.recovery import CredentialRecovery
from jacked.credentials.resolver import MemoryResolverSnapshotSink
from jacked.credentials.store import MemoryCredentialStore
from jacked.credentials.transaction import (
    CredentialTransactionEngine,
    StaticInstallKeyProvider,
    SwitchRequest,
    TransactionDependencies,
)
from jacked.credentials.writer_fence import StaticWriterInspector, WriterFence


def _payload(account_id: int, token: str) -> CredentialPayload:
    return CredentialPayload.from_mapping(
        {
            "_jackedAccountId": account_id,
            "claudeAiOauth": {
                "accessToken": token,
                "refreshToken": f"refresh-{token}",
            },
        }
    )


def _capability(mode: CapabilityMode) -> CredentialCapability:
    return CredentialCapability(
        executable=ExecutableIdentity("/test/claude", "a" * 64, "1.0", "global"),
        mode=mode,
        authority=StoreDeclaration("authority", "auth", StoreRole.AUTHORITY),
        consumers=("claude",),
        capability_epoch=4,
        writer_protocol_epoch=2,
        provenance="test",
        registry_version=1,
    )


def _engine(mode: CapabilityMode, before: CredentialPayload):
    repository = InMemoryCredentialSwitchRepository()
    store = MemoryCredentialStore("auth", before)
    fence = WriterFence(StaticWriterInspector(()))
    deps = TransactionDependencies(
        capability=_capability(mode),
        repository=repository,
        authority=store,
        mirrors={},
        writer_fence=fence,
        install_key=StaticInstallKeyProvider(b"k" * 32),
        machine_install_id="machine-1",
        snapshot_sink=MemoryResolverSnapshotSink(),
    )
    return CredentialTransactionEngine(deps), repository, store


def _request(payload: CredentialPayload) -> SwitchRequest:
    return SwitchRequest(
        operation_id="op-1",
        account_id=2,
        email="two@example.com",
        organization_id="org-2",
        payload=payload,
        context=SwitchContext.MANUAL,
        interaction=InteractionMode.FOREGROUND,
    )


def test_uncooperative_store_is_observed_but_never_committed() -> None:
    engine, repository, _ = _engine(
        CapabilityMode.GLOBAL_UNCOOPERATIVE, _payload(1, "before")
    )

    result = engine.activate(_request(_payload(2, "target")))

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    assert result.committed_authority.account_id is None
    assert result.storage.account_id == 2
    assert repository.finalized == []
    assert repository.outcomes[-1].outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    assert repository.event_log.index("create_pending") < repository.event_log.index(
        "record_outcome"
    )


def test_activation_preserves_authority_top_level_fields_in_required_stores() -> None:
    before = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "before"},
            "claudeSessionMetadata": {"theme": "violet", "opaque": "preserve"},
        }
    )
    repository = InMemoryCredentialSwitchRepository()
    authority = MemoryCredentialStore("auth", before)
    mirror = MemoryCredentialStore("mirror", before)
    capability = replace(
        _capability(CapabilityMode.GLOBAL_UNCOOPERATIVE),
        required_mirrors=(
            StoreDeclaration("mirror", "mirror", StoreRole.REQUIRED_MIRROR),
        ),
    )
    engine = CredentialTransactionEngine(
        TransactionDependencies(
            capability=capability,
            repository=repository,
            authority=authority,
            mirrors={"mirror": mirror},
            writer_fence=WriterFence(StaticWriterInspector(())),
            install_key=StaticInstallKeyProvider(None),
            machine_install_id="unfenced-local",
            snapshot_sink=MemoryResolverSnapshotSink(),
        )
    )

    result = engine.activate(_request(_payload(2, "target")))

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    for store in (authority, mirror):
        mapping = store.read().payload.to_mapping()
        assert mapping["_jackedAccountId"] == 2
        assert mapping["claudeAiOauth"]["accessToken"] == "target"
        assert mapping["claudeSessionMetadata"] == {
            "theme": "violet",
            "opaque": "preserve",
        }


def test_cooperative_divergent_required_store_fails_before_any_mutation() -> None:
    authority_before = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "same-token"},
            "authorityOnly": "do-not-guess",
        }
    )
    mirror_before = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "same-token"},
            "mirrorOnly": "conflict",
        }
    )
    repository = InMemoryCredentialSwitchRepository()
    authority = MemoryCredentialStore("auth", authority_before)
    mirror = MemoryCredentialStore("mirror", mirror_before)
    capability = replace(
        _capability(CapabilityMode.GLOBAL_COOPERATIVE),
        required_mirrors=(
            StoreDeclaration("mirror", "mirror", StoreRole.REQUIRED_MIRROR),
        ),
    )
    engine = CredentialTransactionEngine(
        TransactionDependencies(
            capability=capability,
            repository=repository,
            authority=authority,
            mirrors={"mirror": mirror},
            writer_fence=WriterFence(StaticWriterInspector(())),
            install_key=StaticInstallKeyProvider(None),
            machine_install_id="unfenced-local",
            snapshot_sink=MemoryResolverSnapshotSink(),
        )
    )

    result = engine.activate(_request(_payload(2, "target")))

    assert result.outcome is SwitchOutcome.DIVERGED
    assert authority.write_count == 0
    assert mirror.write_count == 0
    assert authority.read().payload == authority_before
    assert mirror.read().payload == mirror_before


def test_explicit_uncooperative_switch_repairs_preexisting_split_brain() -> None:
    authority_before = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "raider-token"},
            "authorityOnly": {"preserve": True},
        }
    )
    mirror_before = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": 2,
            "claudeAiOauth": {"accessToken": "udifi-old-token"},
            "mirrorOnly": "must-not-be-imported",
        }
    )
    repository = InMemoryCredentialSwitchRepository()
    authority = MemoryCredentialStore("auth", authority_before)
    mirror = MemoryCredentialStore("mirror", mirror_before)
    capability = replace(
        _capability(CapabilityMode.GLOBAL_UNCOOPERATIVE),
        required_mirrors=(
            StoreDeclaration("mirror", "mirror", StoreRole.REQUIRED_MIRROR),
        ),
    )
    engine = CredentialTransactionEngine(
        TransactionDependencies(
            capability=capability,
            repository=repository,
            authority=authority,
            mirrors={"mirror": mirror},
            writer_fence=WriterFence(StaticWriterInspector(())),
            install_key=StaticInstallKeyProvider(None),
            machine_install_id="unfenced-local",
            snapshot_sink=MemoryResolverSnapshotSink(),
        )
    )

    result = engine.activate(_request(_payload(2, "udifi-new-token")))

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    assert result.committed_authority.account_id is None
    assert result.existing_session_activation.value == "restart_required"
    assert repository.finalized == []
    assert repository.created
    for store in (authority, mirror):
        mapping = store.read().payload.to_mapping()
        assert mapping["_jackedAccountId"] == 2
        assert mapping["claudeAiOauth"]["accessToken"] == "udifi-new-token"
        assert mapping["authorityOnly"] == {"preserve": True}
        assert "mirrorOnly" not in mapping


def test_missing_authority_never_erases_readable_mirror_during_repair() -> None:
    mirror_before = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": 2,
            "claudeAiOauth": {"accessToken": "udifi-old-token"},
            "mirrorOnly": "cannot-be-preserved-without-authority",
        }
    )
    repository = InMemoryCredentialSwitchRepository()
    authority = MemoryCredentialStore("auth", None)
    mirror = MemoryCredentialStore("mirror", mirror_before)
    capability = replace(
        _capability(CapabilityMode.GLOBAL_UNCOOPERATIVE),
        required_mirrors=(
            StoreDeclaration("mirror", "mirror", StoreRole.REQUIRED_MIRROR),
        ),
    )
    engine = CredentialTransactionEngine(
        TransactionDependencies(
            capability=capability,
            repository=repository,
            authority=authority,
            mirrors={"mirror": mirror},
            writer_fence=WriterFence(StaticWriterInspector(())),
            install_key=StaticInstallKeyProvider(None),
            machine_install_id="unfenced-local",
            snapshot_sink=MemoryResolverSnapshotSink(),
        )
    )

    result = engine.activate(_request(_payload(2, "udifi-new-token")))

    assert result.outcome is SwitchOutcome.DIVERGED
    assert authority.write_count == 0
    assert mirror.write_count == 0
    assert authority.read().status is StoreStatus.MISSING
    assert mirror.read().payload == mirror_before


def test_uncooperative_crash_after_write_leaves_recoverable_evidence() -> None:
    class CrashAfterWriteStore(MemoryCredentialStore):
        def write(self, payload, interaction):
            super().write(payload, interaction)
            raise RuntimeError("simulated process crash after native write")

    repository = InMemoryCredentialSwitchRepository()
    store = CrashAfterWriteStore("auth", _payload(1, "before"))
    store.event_log = repository.event_log
    deps = TransactionDependencies(
        capability=_capability(CapabilityMode.GLOBAL_UNCOOPERATIVE),
        repository=repository,
        authority=store,
        mirrors={},
        writer_fence=WriterFence(StaticWriterInspector(())),
        install_key=StaticInstallKeyProvider(None),
        machine_install_id="unfenced-local",
        snapshot_sink=MemoryResolverSnapshotSink(),
    )
    engine = CredentialTransactionEngine(deps)

    try:
        engine.activate(_request(_payload(2, "target")))
    except RuntimeError as exc:
        assert "simulated process crash" in str(exc)
    else:
        raise AssertionError("crash injection did not fire")

    assert repository.get_pending("op-1") is not None
    assert repository.event_log.index("create_pending") < repository.event_log.index(
        "store_write"
    )
    recovery = CredentialRecovery(
        repository,
        store,
        StaticInstallKeyProvider(None),
        "unfenced-local",
    )
    recovered = recovery.recover("op-1")
    assert recovered.outcome is SwitchOutcome.INDETERMINATE
    assert repository.get_pending("op-1") is not None


def test_cooperative_switch_journals_before_mutation_and_finalizes() -> None:
    engine, repository, store = _engine(
        CapabilityMode.GLOBAL_COOPERATIVE, _payload(1, "before")
    )
    store.event_log = repository.event_log

    result = engine.activate(_request(_payload(2, "target")))

    assert result.outcome is SwitchOutcome.COMMITTED
    assert result.committed_authority.account_id == 2
    assert repository.event_log.index("create_pending") < repository.event_log.index(
        "store_write"
    )
    assert repository.event_log.index("store_read") < repository.event_log.index(
        "finalize"
    )
    assert "refresh-target" not in repr(repository.created[0])


def test_authority_write_failure_preserves_previous_value() -> None:
    engine, repository, store = _engine(
        CapabilityMode.GLOBAL_COOPERATIVE, _payload(1, "before")
    )
    store.fail_writes = True

    result = engine.activate(_request(_payload(2, "target")))

    assert result.outcome is SwitchOutcome.FAILED_PRESERVED
    assert repository.finalized == []
    assert store.read().payload.identity.account_id == 1


def test_unknown_capability_never_writes() -> None:
    engine, repository, store = _engine(
        CapabilityMode.UNSUPPORTED, _payload(1, "before")
    )

    result = engine.activate(_request(_payload(2, "target")))

    assert result.outcome is SwitchOutcome.UNSUPPORTED
    assert store.write_count == 0
    assert repository.pending == {}


def test_interactive_required_is_distinct() -> None:
    engine, repository, store = _engine(
        CapabilityMode.GLOBAL_COOPERATIVE, _payload(1, "before")
    )
    store.requires_interaction = True

    result = engine.activate(
        replace(_request(_payload(2, "target")), interaction=InteractionMode.BACKGROUND)
    )

    assert result.outcome is SwitchOutcome.INTERACTIVE_REQUIRED
    assert repository.finalized == []


def test_payload_repr_is_secret_free() -> None:
    payload = _payload(2, "access-canary")

    assert "access-canary" not in repr(payload)
    assert "refresh-access-canary" not in repr(payload)
    assert payload.identity == CredentialIdentity(account_id=2)


def test_uninspected_build_may_not_create_a_missing_authority():
    repository = InMemoryCredentialSwitchRepository()
    store = MemoryCredentialStore("auth", None)
    deps = TransactionDependencies(
        capability=_capability(CapabilityMode.GLOBAL_UNCOOPERATIVE),
        repository=repository,
        authority=store,
        mirrors={},
        writer_fence=WriterFence(StaticWriterInspector(())),
        install_key=StaticInstallKeyProvider(None),
        machine_install_id="unfenced-local",
        snapshot_sink=MemoryResolverSnapshotSink(),
        allow_missing_authority=False,
    )
    engine = CredentialTransactionEngine(deps)

    result = engine.activate(_request(_payload(2, "token")))

    assert result.outcome is SwitchOutcome.UNUSABLE
    assert "authority is missing" in result.message
    assert store.read().status is StoreStatus.MISSING


def test_inspected_build_keeps_authority_creation_enabled(tmp_path):
    from jacked.credentials.runtime import SHIPPED_REGISTRY, _engine_for
    from tests.unit.test_credential_runtime import _identity

    inspected = _engine_for(
        object(),
        SHIPPED_REGISTRY.resolve(_identity(platform_system="linux")),
        tmp_path,
    )
    newer = _engine_for(
        object(),
        SHIPPED_REGISTRY.resolve(_identity(platform_system="linux", build_version="2.1.261")),
        tmp_path,
    )
    assert inspected._deps.allow_missing_authority is True
    assert newer._deps.allow_missing_authority is False


def test_concurrent_write_on_authority_reports_the_refreshed_contents_as_preserved():
    refreshed = _payload(7, "refreshed-by-claude")

    class RefreshedUnderneath(MemoryCredentialStore):
        def write(self, payload, interaction):
            self._payload = refreshed  # what the other writer left behind
            return StoreWriteResult(StoreStatus.CONCURRENT_WRITE, "changed since read")

    before = _payload(1, "old")
    repository = InMemoryCredentialSwitchRepository()
    store = RefreshedUnderneath("auth", before)
    deps = TransactionDependencies(
        capability=_capability(CapabilityMode.GLOBAL_UNCOOPERATIVE),
        repository=repository,
        authority=store,
        mirrors={},
        writer_fence=WriterFence(StaticWriterInspector(())),
        install_key=StaticInstallKeyProvider(None),
        machine_install_id="unfenced-local",
        snapshot_sink=MemoryResolverSnapshotSink(),
    )

    result = CredentialTransactionEngine(deps).activate(_request(_payload(2, "new")))

    assert result.outcome is SwitchOutcome.FAILED_PRESERVED
    assert result.observed_identity.account_id == 7
    assert "changed since read" in result.message


def test_required_mirror_concurrent_write_is_retried_once_after_reread():
    class RefreshedMirror(MemoryCredentialStore):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.refusals_left = 1
            self.reads = 0

        def read(self):
            self.reads += 1
            return super().read()

        def write(self, payload, interaction):
            if self.refusals_left:
                self.refusals_left -= 1
                return StoreWriteResult(StoreStatus.CONCURRENT_WRITE, "changed since read")
            return super().write(payload, interaction)

    before = _payload(1, "old")
    authority = MemoryCredentialStore("auth", before)
    mirror = RefreshedMirror("mirror", before)
    capability = CredentialCapability(
        **{
            **_capability(CapabilityMode.GLOBAL_UNCOOPERATIVE).__dict__,
            "required_mirrors": (StoreDeclaration("mirror", "mirror", StoreRole.REQUIRED_MIRROR),),
        }
    )
    deps = TransactionDependencies(
        capability=capability,
        repository=InMemoryCredentialSwitchRepository(),
        authority=authority,
        mirrors={"mirror": mirror},
        writer_fence=WriterFence(StaticWriterInspector(())),
        install_key=StaticInstallKeyProvider(None),
        machine_install_id="unfenced-local",
        snapshot_sink=MemoryResolverSnapshotSink(),
    )

    result = CredentialTransactionEngine(deps).activate(_request(_payload(2, "new")))

    assert result.outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED
    assert mirror.read().payload.digest == _payload(2, "new").digest
    assert mirror.reads >= 2  # re-armed before the retry


def test_schema_drift_is_logged_by_key_name(caplog):
    before = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": 1,
            "claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "newField": 1},
        }
    )
    engine, _repository, _store = _engine(CapabilityMode.GLOBAL_UNCOOPERATIVE, before)

    with caplog.at_level("WARNING"):
        engine.activate(_request(_payload(2, "token")))

    assert "newField" in caplog.text
    assert "refresh-" not in caplog.text  # key names only, never values
