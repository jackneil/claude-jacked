"""Classification of interrupted credential transactions."""

from __future__ import annotations

import hmac

from .models import (
    CredentialIdentity,
    FinalizeSwitchRecord,
    IdentityAxis,
    OutcomeSwitchRecord,
    ProviderVerificationState,
    SessionActivationState,
    StoreStatus,
    SwitchOutcome,
    SwitchResult,
)
from .repository import CredentialSwitchRepository
from .store import CredentialStore
from .transaction import InstallKeyProvider, transcript_hmac


class CredentialRecovery:
    """Recover pending operations by observation; never rewrite credentials."""

    def __init__(
        self,
        repository: CredentialSwitchRepository,
        authority: CredentialStore,
        install_key: InstallKeyProvider,
        machine_install_id: str,
    ) -> None:
        self._repository = repository
        self._authority = authority
        self._install_key = install_key
        self._machine_install_id = machine_install_id

    def recover(self, operation_id: str) -> SwitchResult:
        pending = self._repository.get_pending(operation_id)
        if pending is None:
            return self._result(operation_id, None, SwitchOutcome.UNUSABLE)
        observed = self._authority.read()
        identity = (
            observed.payload.identity if observed.payload else CredentialIdentity()
        )
        key = self._install_key.get_key()
        if (
            observed.status is not StoreStatus.OK
            or observed.payload is None
            or not key
            or pending.machine_install_id != self._machine_install_id
        ):
            return self._indeterminate(pending, identity)
        digest = observed.payload.digest
        if self._matches(pending, "target", digest, pending.target_hmac, key):
            self._repository.finalize(
                FinalizeSwitchRecord(
                    pending.operation_id,
                    pending.account_id,
                    SwitchOutcome.COMMITTED,
                    identity,
                    f"switch:{pending.operation_id}",
                )
            )
            return self._result(
                pending.operation_id,
                pending.account_id,
                SwitchOutcome.COMMITTED,
                identity,
                is_committed=True,
            )
        if self._matches(pending, "before", digest, pending.before_hmac, key):
            outcome = SwitchOutcome.FAILED_PRESERVED
            self._repository.record_outcome(
                OutcomeSwitchRecord(
                    pending.operation_id,
                    pending.account_id,
                    outcome,
                    identity,
                    "previous authority remains unchanged",
                    pending.context,
                    pending.capability_mode,
                )
            )
            return self._result(
                pending.operation_id, pending.account_id, outcome, identity
            )
        return self._indeterminate(pending, identity)

    @staticmethod
    def _matches(pending, role: str, digest: str, expected: str, key: bytes) -> bool:
        actual = transcript_hmac(pending, state_role=role, digest=digest, key=key)
        return hmac.compare_digest(actual, expected)

    def _indeterminate(self, pending, identity: CredentialIdentity) -> SwitchResult:
        outcome = SwitchOutcome.INDETERMINATE
        self._repository.record_outcome(
            OutcomeSwitchRecord(
                pending.operation_id,
                pending.account_id,
                outcome,
                identity,
                "authority does not match a recoverable transaction state",
                pending.context,
                pending.capability_mode,
            )
        )
        return self._result(
            pending.operation_id, pending.account_id, outcome, identity
        )

    @staticmethod
    def _result(
        operation_id: str,
        account_id: int | None,
        outcome: SwitchOutcome,
        identity: CredentialIdentity = CredentialIdentity(),
        *,
        is_committed: bool = False,
    ) -> SwitchResult:
        return SwitchResult(
            operation_id=operation_id,
            outcome=outcome,
            desired_default=IdentityAxis(account_id, "desired"),
            storage=IdentityAxis(identity.account_id, outcome.value),
            committed_authority=IdentityAxis(
                account_id if is_committed else None,
                "committed" if is_committed else "unchanged",
            ),
            existing_session_activation=(
                SessionActivationState.PENDING_NEXT_ACTIVITY
                if is_committed
                else SessionActivationState.UNCHANGED
            ),
            provider_verification=ProviderVerificationState.UNVERIFIED,
            observed_identity=identity,
        )
