from __future__ import annotations

from jacked.credentials.canonical import CredentialPayload
from jacked.credentials.models import (
    CapabilityMode,
    CredentialCapability,
    ExecutableIdentity,
    PendingSwitchRecord,
    SessionActivationState,
    StoreDeclaration,
    StoreRole,
    SwitchContext,
    SwitchOutcome,
)
from jacked.credentials.recovery import CredentialRecovery
from jacked.credentials.repository import InMemoryCredentialSwitchRepository
from jacked.credentials.store import MemoryCredentialStore
from jacked.credentials.transaction import (
    StaticInstallKeyProvider,
    build_pending_record,
)


def _payload(account_id: int, token: str) -> CredentialPayload:
    return CredentialPayload.from_mapping(
        {
            "_jackedAccountId": account_id,
            "claudeAiOauth": {"accessToken": token},
        }
    )


def _capability() -> CredentialCapability:
    return CredentialCapability(
        executable=ExecutableIdentity("/test/claude", "a" * 64, "1.0", "global"),
        mode=CapabilityMode.GLOBAL_COOPERATIVE,
        authority=StoreDeclaration("authority", "auth", StoreRole.AUTHORITY),
        consumers=("claude",),
        capability_epoch=4,
        writer_protocol_epoch=2,
        provenance="test",
        registry_version=1,
    )


def _pending(
    before: CredentialPayload, target: CredentialPayload
) -> PendingSwitchRecord:
    return build_pending_record(
        operation_id="op-1",
        account_id=2,
        organization_id="org-2",
        context=SwitchContext.MANUAL,
        before=before,
        target=target,
        capability=_capability(),
        machine_install_id="machine-1",
        install_key=b"k" * 32,
    )


def test_recovery_classifies_target_without_rewriting_secret() -> None:
    before = _payload(1, "before")
    target = _payload(2, "target")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_pending(before, target))
    store = MemoryCredentialStore("auth", target)

    result = CredentialRecovery(
        repository, store, StaticInstallKeyProvider(b"k" * 32), "machine-1"
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.COMMITTED
    assert store.write_count == 0


def test_recovery_classifies_before_as_failed_preserved() -> None:
    before = _payload(1, "before")
    target = _payload(2, "target")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_pending(before, target))

    result = CredentialRecovery(
        repository,
        MemoryCredentialStore("auth", before),
        StaticInstallKeyProvider(b"k" * 32),
        "machine-1",
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.FAILED_PRESERVED


def test_recovery_third_value_is_indeterminate_even_if_identity_matches() -> None:
    before = _payload(1, "before")
    target = _payload(2, "target")
    rotated_target = _payload(2, "different-revision")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_pending(before, target))

    result = CredentialRecovery(
        repository,
        MemoryCredentialStore("auth", rotated_target),
        StaticInstallKeyProvider(b"k" * 32),
        "machine-1",
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.INDETERMINATE
    assert result.observed_identity.account_id == 2
    assert repository.finalized == []


def test_missing_recovery_key_is_indeterminate() -> None:
    before = _payload(1, "before")
    target = _payload(2, "target")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_pending(before, target))

    result = CredentialRecovery(
        repository,
        MemoryCredentialStore("auth", target),
        StaticInstallKeyProvider(None),
        "machine-1",
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.INDETERMINATE


class _Publisher:
    """Stand-in for the Claude config identity publisher."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.requests: list[object] = []
        self.fail = fail

    def __call__(self, request) -> None:
        self.requests.append(request)
        if self.fail is not None:
            raise self.fail


def _identity_pending(
    before: CredentialPayload, target: CredentialPayload
) -> PendingSwitchRecord:
    return build_pending_record(
        operation_id="op-1",
        account_id=2,
        organization_id="org-2",
        context=SwitchContext.MANUAL,
        before=before,
        target=target,
        capability=_capability(),
        machine_install_id="machine-1",
        install_key=b"k" * 32,
        email="two@example.com",
        display_name="Two Example",
        organization_name="Example Org",
    )


def test_recovered_commit_republishes_the_identity_claude_watches() -> None:
    before = _payload(1, "before")
    target = _payload(2, "target")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_identity_pending(before, target))
    publisher = _Publisher()

    result = CredentialRecovery(
        repository,
        MemoryCredentialStore("auth", target),
        StaticInstallKeyProvider(b"k" * 32),
        "machine-1",
        identity_publisher=publisher,
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.COMMITTED
    assert [r.email for r in publisher.requests] == ["two@example.com"]
    assert publisher.requests[0].display_name == "Two Example"
    assert publisher.requests[0].organization_name == "Example Org"
    assert publisher.requests[0].organization_id == "org-2"
    assert (
        result.existing_session_activation
        is SessionActivationState.PENDING_NEXT_ACTIVITY
    )


def test_recovered_commit_degrades_when_the_identity_cannot_be_published() -> None:
    before = _payload(1, "before")
    target = _payload(2, "target")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_identity_pending(before, target))

    result = CredentialRecovery(
        repository,
        MemoryCredentialStore("auth", target),
        StaticInstallKeyProvider(b"k" * 32),
        "machine-1",
        identity_publisher=_Publisher(fail=OSError("read-only config")),
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.COMMITTED_DEGRADED
    assert "claude config identity not updated" in result.message
    assert result.existing_session_activation is SessionActivationState.RESTART_REQUIRED
    assert repository.finalized[0].outcome is SwitchOutcome.COMMITTED_DEGRADED


def test_a_row_without_identity_fields_degrades_instead_of_guessing() -> None:
    """An older jacked wrote the pending row before the identity was carried."""
    before = _payload(1, "before")
    target = _payload(2, "target")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_pending(before, target))
    publisher = _Publisher()

    result = CredentialRecovery(
        repository,
        MemoryCredentialStore("auth", target),
        StaticInstallKeyProvider(b"k" * 32),
        "machine-1",
        identity_publisher=publisher,
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.COMMITTED_DEGRADED
    assert publisher.requests == []
    assert "identity not recorded" in result.message
    assert result.existing_session_activation is SessionActivationState.RESTART_REQUIRED


def test_a_preserved_recovery_never_publishes_an_identity() -> None:
    before = _payload(1, "before")
    target = _payload(2, "target")
    repository = InMemoryCredentialSwitchRepository()
    repository.create_pending(_identity_pending(before, target))
    publisher = _Publisher()

    result = CredentialRecovery(
        repository,
        MemoryCredentialStore("auth", before),
        StaticInstallKeyProvider(b"k" * 32),
        "machine-1",
        identity_publisher=publisher,
    ).recover("op-1")

    assert result.outcome is SwitchOutcome.FAILED_PRESERVED
    assert publisher.requests == []
    assert result.existing_session_activation is SessionActivationState.UNCHANGED
