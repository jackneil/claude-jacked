"""Classification of interrupted credential transactions."""

from __future__ import annotations

import hmac
import logging

from .models import (
    CredentialIdentity,
    FinalizeSwitchRecord,
    IdentityAxis,
    InteractionMode,
    OutcomeSwitchRecord,
    ProviderVerificationState,
    SessionActivationState,
    StoreStatus,
    SwitchOutcome,
    SwitchResult,
)
from .repository import CredentialSwitchRepository
from .store import CredentialStore
from .transaction import (
    IDENTITY_FAILURE_MESSAGE,
    IdentityPublisher,
    InstallKeyProvider,
    SwitchRequest,
    transcript_hmac,
)

logger = logging.getLogger(__name__)


class CredentialRecovery:
    """Recover pending operations by observation; never rewrite credentials."""

    def __init__(
        self,
        repository: CredentialSwitchRepository,
        authority: CredentialStore,
        install_key: InstallKeyProvider,
        machine_install_id: str,
        identity_publisher: IdentityPublisher | None = None,
    ) -> None:
        self._repository = repository
        self._authority = authority
        self._install_key = install_key
        self._machine_install_id = machine_install_id
        self._identity_publisher = identity_publisher

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
            # The target credentials are in the authority, but the interrupted
            # run may never have republished the identity Claude Code watches.
            # Publish it here, or say plainly that sessions must restart.
            published, message = self._publish_identity(pending, observed.payload)
            outcome = (
                SwitchOutcome.COMMITTED_DEGRADED if message else SwitchOutcome.COMMITTED
            )
            self._repository.finalize(
                FinalizeSwitchRecord(
                    pending.operation_id,
                    pending.account_id,
                    outcome,
                    identity,
                    f"switch:{pending.operation_id}",
                )
            )
            return self._result(
                pending.operation_id,
                pending.account_id,
                outcome,
                identity,
                is_committed=True,
                message=message,
                sessions_follow=published,
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

    def _publish_identity(self, pending, payload) -> tuple[bool, str]:
        """Mirror a recovered switch into Claude's config.

        Returns ``(published, message)``. ``published`` is True only when the
        identity landed. The authority already holds the target credentials, so
        a failure here changes nothing that is stored. It only means running
        Claude Code sessions keep the previous account until they restart.
        """
        publisher = self._identity_publisher
        if publisher is None:
            return False, ""
        if not pending.email:
            # An older jacked wrote this row without the identity fields. There
            # is nothing to publish and no way to know it already happened.
            return False, f"{IDENTITY_FAILURE_MESSAGE} (identity not recorded)"
        request = SwitchRequest(
            operation_id=pending.operation_id,
            account_id=pending.account_id,
            email=pending.email,
            organization_id=pending.organization_id,
            payload=payload,
            context=pending.context,
            interaction=InteractionMode.FOREGROUND,
            display_name=pending.display_name,
            organization_name=pending.organization_name,
        )
        try:
            publisher(request)
        except Exception as exc:  # noqa: BLE001 - config I/O must never undo a switch
            logger.warning(
                "Could not publish the recovered identity to Claude's config: %s", exc
            )
            return False, f"{IDENTITY_FAILURE_MESSAGE} ({type(exc).__name__})"
        return True, ""

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
        message: str = "",
        sessions_follow: bool = False,
    ) -> SwitchResult:
        session_state = SessionActivationState.UNCHANGED
        if is_committed:
            session_state = (
                SessionActivationState.PENDING_NEXT_ACTIVITY
                if sessions_follow
                else SessionActivationState.RESTART_REQUIRED
            )
        return SwitchResult(
            operation_id=operation_id,
            outcome=outcome,
            desired_default=IdentityAxis(account_id, "desired"),
            storage=IdentityAxis(identity.account_id, outcome.value),
            committed_authority=IdentityAxis(
                account_id if is_committed else None,
                "committed" if is_committed else "unchanged",
            ),
            existing_session_activation=session_state,
            provider_verification=ProviderVerificationState.UNVERIFIED,
            observed_identity=identity,
            message=message,
        )
