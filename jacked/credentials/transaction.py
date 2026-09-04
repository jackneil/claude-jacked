"""Crash-safe, evidence-qualified credential switch state machine."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Mapping, Protocol

from .canonical import CredentialPayload
from .lease import ProcessSwitchLease, SwitchLease
from .models import (
    CapabilityMode,
    CredentialCapability,
    CredentialIdentity,
    FinalizeSwitchRecord,
    IdentityAxis,
    InteractionMode,
    OutcomeSwitchRecord,
    PendingSwitchRecord,
    ProviderVerificationState,
    SessionActivationState,
    StoreStatus,
    StoreReadResult,
    SwitchContext,
    SwitchOutcome,
    SwitchResult,
)
from .repository import CredentialSwitchRepository
from .resolver import ResolverSnapshotSink, ResolverState, SnapshotUpdate
from .store import CredentialStore
from .writer_fence import WriterFence

logger = logging.getLogger(__name__)


class InstallKeyProvider(Protocol):
    def get_key(self) -> bytes | None: ...


class StaticInstallKeyProvider:
    def __init__(self, key: bytes | None) -> None:
        self._key = key

    def get_key(self) -> bytes | None:
        return self._key


@dataclass(frozen=True)
class SwitchRequest:
    operation_id: str
    account_id: int
    email: str
    organization_id: str | None
    payload: CredentialPayload
    context: SwitchContext
    interaction: InteractionMode


@dataclass(frozen=True)
class TransactionDependencies:
    capability: CredentialCapability
    repository: CredentialSwitchRepository
    authority: CredentialStore
    mirrors: Mapping[str, CredentialStore]
    writer_fence: WriterFence
    install_key: InstallKeyProvider
    machine_install_id: str
    snapshot_sink: ResolverSnapshotSink
    switch_lease: SwitchLease = field(default_factory=ProcessSwitchLease)
    allow_missing_authority: bool = True


def _transcript(record: PendingSwitchRecord, *, state_role: str, digest: str) -> bytes:
    value = {
        "account_id": record.account_id,
        "backend_locator": record.backend_locator,
        "canonical_digest": digest,
        "canonicalizer_version": record.canonicalizer_version,
        "capability_epoch": record.capability_epoch,
        "machine_install_id": record.machine_install_id,
        "operation_id": record.operation_id,
        "organization_id": record.organization_id,
        "state_role": state_role,
    }
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def transcript_hmac(
    record: PendingSwitchRecord, *, state_role: str, digest: str, key: bytes
) -> str:
    return hmac.new(
        key, _transcript(record, state_role=state_role, digest=digest), hashlib.sha256
    ).hexdigest()


def build_pending_record(
    *,
    operation_id: str,
    account_id: int,
    organization_id: str | None,
    context: SwitchContext,
    before: CredentialPayload,
    target: CredentialPayload,
    capability: CredentialCapability,
    machine_install_id: str,
    install_key: bytes,
) -> PendingSwitchRecord:
    """Build a secret-free pending record with transcript-bound verifiers."""
    unsigned = PendingSwitchRecord(
        operation_id=operation_id,
        account_id=account_id,
        organization_id=organization_id,
        context=context,
        capability_mode=capability.mode,
        machine_install_id=machine_install_id,
        backend_locator=capability.authority.locator,
        capability_epoch=capability.capability_epoch,
        canonicalizer_version=capability.canonicalizer_version,
        before_hmac="",
        target_hmac="",
    )
    return PendingSwitchRecord(
        **{
            **unsigned.__dict__,
            "before_hmac": transcript_hmac(
                unsigned,
                state_role="before",
                digest=before.digest,
                key=install_key,
            ),
            "target_hmac": transcript_hmac(
                unsigned,
                state_role="target",
                digest=target.digest,
                key=install_key,
            ),
        }
    )


def build_unverified_pending_record(
    *,
    request: SwitchRequest,
    capability: CredentialCapability,
    machine_install_id: str,
) -> PendingSwitchRecord:
    """Build a secret-free crash marker when no recovery key is available.

    GLOBAL_UNCOOPERATIVE operations cannot honestly be auto-finalized after a
    crash, but they still require a durable row before native mutation. Empty
    verifiers force recovery to classify the operation as indeterminate.
    """
    return PendingSwitchRecord(
        operation_id=request.operation_id,
        account_id=request.account_id,
        organization_id=request.organization_id,
        context=request.context,
        capability_mode=capability.mode,
        machine_install_id=machine_install_id,
        backend_locator=capability.authority.locator,
        capability_epoch=capability.capability_epoch,
        canonicalizer_version=capability.canonicalizer_version,
        before_hmac="",
        target_hmac="",
    )


def _warn_on_schema_drift(before: StoreReadResult, request: SwitchRequest) -> None:
    """Name any claudeAiOauth keys Claude wrote that jacked's payload does not carry.

    jacked replaces the whole object; a key it does not know is dropped. That
    is logged loudly so a Claude Code schema change is diagnosable from the
    tray log instead of surfacing as a mysterious re-login.
    """
    if before.payload is None:
        return
    current = before.payload.to_mapping().get("claudeAiOauth")
    target = request.payload.to_mapping().get("claudeAiOauth")
    if not isinstance(current, dict) or not isinstance(target, dict):
        return
    dropped = sorted(set(current) - set(target))
    if dropped:
        logger.warning(
            "Credential schema drift: authority carries claudeAiOauth keys jacked "
            "does not write and will drop: %s",
            ", ".join(dropped),
        )


def _result(
    request: SwitchRequest,
    outcome: SwitchOutcome,
    *,
    observed: CredentialIdentity = CredentialIdentity(),
    is_committed: bool = False,
    message: str = "",
) -> SwitchResult:
    observed = _enrich_identity(request, observed)
    storage_state = "committed" if is_committed else outcome.value
    session_state = SessionActivationState.UNCHANGED
    if is_committed:
        session_state = SessionActivationState.PENDING_NEXT_ACTIVITY
    elif outcome is SwitchOutcome.OBSERVED_TARGET_UNFENCED:
        session_state = SessionActivationState.RESTART_REQUIRED
    return SwitchResult(
        operation_id=request.operation_id,
        outcome=outcome,
        desired_default=IdentityAxis(request.account_id, "desired"),
        storage=IdentityAxis(observed.account_id, storage_state),
        committed_authority=IdentityAxis(
            request.account_id if is_committed else None,
            "committed" if is_committed else "unchanged",
        ),
        existing_session_activation=session_state,
        provider_verification=ProviderVerificationState.UNVERIFIED,
        observed_identity=observed,
        message=message,
    )


def _enrich_identity(
    request: SwitchRequest, identity: CredentialIdentity
) -> CredentialIdentity:
    if identity.account_id != request.account_id:
        return identity
    return CredentialIdentity(
        account_id=identity.account_id,
        email=request.email,
        organization_id=identity.organization_id or request.organization_id,
    )


class CredentialTransactionEngine:
    """Run one switch under a capability and repository contract."""

    def __init__(self, dependencies: TransactionDependencies) -> None:
        self._deps = dependencies

    def activate(self, request: SwitchRequest) -> SwitchResult:
        capability = self._deps.capability
        if capability.mode is CapabilityMode.UNSUPPORTED:
            return _result(
                request, SwitchOutcome.UNSUPPORTED, message="build unsupported"
            )
        if request.payload.identity.account_id != request.account_id:
            return _result(
                request, SwitchOutcome.UNUSABLE, message="account stamp mismatch"
            )
        if capability.mode is CapabilityMode.GLOBAL_UNCOOPERATIVE:
            if request.interaction is not InteractionMode.FOREGROUND:
                return _result(
                    request,
                    SwitchOutcome.RESTART_REQUIRED,
                    message="uncooperative stores require a foreground operation",
                )
            return self._with_lease(request, self._activate_unfenced)
        return self._activate_cooperative(request)

    def _with_lease(self, request: SwitchRequest, operation) -> SwitchResult:
        with self._deps.switch_lease.acquire() as acquired:
            if not acquired:
                return _result(
                    request,
                    SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS,
                    message="another credential operation holds the switch lease",
                )
            return operation(request)

    def _activate_unfenced(self, request: SwitchRequest) -> SwitchResult:
        before, prepared, failure = self._prepare_preserving_target(
            request,
            allow_missing_authority=self._deps.allow_missing_authority,
            allow_divergent_mirrors=True,
        )
        if failure is not None:
            outcome, reason = failure
            return self._record(request, outcome, message=reason)
        assert prepared is not None
        request = prepared
        _warn_on_schema_drift(before, request)
        # Even though this mode has no recovery key and can never commit an
        # active pointer, persist the operation before the native write. A
        # crash after Keychain mutation must remain observable and recover as
        # indeterminate instead of disappearing without evidence.
        self._deps.repository.create_pending(
            build_unverified_pending_record(
                request=request,
                capability=self._deps.capability,
                machine_install_id=self._deps.machine_install_id,
            )
        )
        write = self._deps.authority.write(request.payload, request.interaction)
        if write.status is not StoreStatus.OK:
            if before.payload is not None:
                return self._classify_after_failure(
                    request, before.payload, write.status, write.reason
                )
            return self._write_failure(request, write.status, write.reason)
        mirror_outcome, mirror_message = self._publish_mirrors(request)
        observed = self._observe_target_consensus()
        if mirror_outcome is SwitchOutcome.INDETERMINATE or observed is None:
            return self._record(
                request, SwitchOutcome.INDETERMINATE, CredentialIdentity()
            )
        if observed.digest != request.payload.digest:
            return self._record(
                request, SwitchOutcome.INDETERMINATE, observed.identity
            )
        result = self._record(
            request,
            SwitchOutcome.OBSERVED_TARGET_UNFENCED,
            observed.identity,
            "target observed; concurrent writers cannot be excluded"
            + (f"; {mirror_message}" if mirror_message else ""),
        )
        self._publish_snapshot(
            request,
            observed.identity,
            ResolverState.RESOLVED,
            ("authority:observed-target-unfenced",),
        )
        return result

    def _prepare_preserving_target(
        self,
        request: SwitchRequest,
        *,
        allow_missing_authority: bool,
        allow_divergent_mirrors: bool,
    ) -> tuple[
        StoreReadResult,
        SwitchRequest | None,
        tuple[SwitchOutcome, str] | None,
    ]:
        """Merge managed fields into a read-validated authority baseline.

        The authority is the only preservation source. Required mirrors may be
        absent and will be created. Cooperative modes require readable mirrors
        to match the full authority payload. An explicit foreground repair on
        the global-uncooperative build may overwrite a preexisting divergent
        mirror, but still preserves only from the declared authority and never
        imports fields or secrets from a lower-precedence mirror.
        """
        before = self._deps.authority.read()
        required = {
            item.locator for item in self._deps.capability.required_mirrors
        }
        mirror_reads = []
        for locator in required:
            mirror = self._deps.mirrors.get(locator)
            if mirror is None:
                return (
                    before,
                    None,
                    (SwitchOutcome.UNUSABLE, f"required store adapter missing: {locator}"),
                )
            mirror_reads.append((locator, mirror.read()))

        if before.status is StoreStatus.MISSING:
            readable_mirrors = [
                locator
                for locator, observed in mirror_reads
                if observed.status is StoreStatus.OK and observed.payload is not None
            ]
            unusable_mirrors = [
                locator
                for locator, observed in mirror_reads
                if observed.status not in {StoreStatus.OK, StoreStatus.MISSING}
            ]
            if readable_mirrors:
                return (
                    before,
                    None,
                    (
                        SwitchOutcome.DIVERGED,
                        "authority is missing while a required mirror contains credentials",
                    ),
                )
            if unusable_mirrors:
                return (
                    before,
                    None,
                    (
                        SwitchOutcome.UNUSABLE,
                        f"required store is unreadable: {unusable_mirrors[0]}",
                    ),
                )
            if not allow_missing_authority:
                return (
                    before,
                    None,
                    (
                        SwitchOutcome.UNUSABLE,
                        "credential authority is missing; jacked will not create it "
                        "for a Claude build newer than the inspected one; run "
                        "`claude` and log in once",
                    ),
                )
            baseline = {}
        elif before.status is StoreStatus.OK and before.payload is not None:
            for locator, observed in mirror_reads:
                if observed.status is StoreStatus.MISSING:
                    continue
                if observed.status is not StoreStatus.OK or observed.payload is None:
                    return (
                        before,
                        None,
                        (SwitchOutcome.UNUSABLE, f"required store is unreadable: {locator}"),
                    )
                if observed.payload.digest != before.payload.digest:
                    if allow_divergent_mirrors:
                        continue
                    return (
                        before,
                        None,
                        (
                            SwitchOutcome.DIVERGED,
                            f"required store conflicts with authority: {locator}",
                        ),
                    )
            baseline = before.payload.to_mapping()
        else:
            return (
                before,
                None,
                (SwitchOutcome.UNUSABLE, before.reason or "credential authority is unreadable"),
            )

        desired = request.payload.to_mapping()
        baseline["_jackedAccountId"] = desired["_jackedAccountId"]
        baseline["claudeAiOauth"] = desired["claudeAiOauth"]
        target = CredentialPayload.from_mapping(
            baseline,
            version=self._deps.capability.canonicalizer_version,
        )
        return before, replace(request, payload=target), None

    def _observe_target_consensus(self) -> CredentialPayload | None:
        stores = [self._deps.authority]
        required = {
            item.locator for item in self._deps.capability.required_mirrors
        }
        for locator in required:
            store = self._deps.mirrors.get(locator)
            if store is None:
                return None
            stores.append(store)
        observations = [store.read() for store in stores]
        if any(
            item.status is not StoreStatus.OK or item.payload is None
            for item in observations
        ):
            return None
        payloads = [item.payload for item in observations if item.payload is not None]
        if len({item.digest for item in payloads}) != 1:
            return None
        return payloads[0]

    def _activate_cooperative(self, request: SwitchRequest) -> SwitchResult:
        capability = self._deps.capability
        fence = self._deps.writer_fence.inspect(
            required_protocol_epoch=capability.writer_protocol_epoch,
            capability_epoch=capability.capability_epoch,
        )
        if not fence.is_allowed:
            return _result(request, SwitchOutcome.UNSUPPORTED, message=fence.reason)
        return self._with_lease(request, self._activate_cooperative_locked)

    def _activate_cooperative_locked(self, request: SwitchRequest) -> SwitchResult:
        capability = self._deps.capability
        before, prepared, failure = self._prepare_preserving_target(
            request,
            allow_missing_authority=False,
            allow_divergent_mirrors=False,
        )
        if failure is not None:
            outcome, reason = failure
            return self._record(request, outcome, message=reason)
        assert before.payload is not None
        assert prepared is not None
        request = prepared
        key = self._deps.install_key.get_key()
        if not key:
            return _result(
                request, SwitchOutcome.INDETERMINATE, message="recovery key unavailable"
            )
        pending = build_pending_record(
            operation_id=request.operation_id,
            account_id=request.account_id,
            organization_id=request.organization_id,
            context=request.context,
            before=before.payload,
            target=request.payload,
            capability=capability,
            machine_install_id=self._deps.machine_install_id,
            install_key=key,
        )
        self._deps.repository.create_pending(pending)
        write = self._deps.authority.write(request.payload, request.interaction)
        if write.status is not StoreStatus.OK:
            return self._classify_after_failure(
                request, before.payload, write.status, write.reason
            )
        return self._verify_and_finalize(request, before.payload)

    def _classify_after_failure(
        self,
        request: SwitchRequest,
        before: CredentialPayload,
        status: StoreStatus,
        reason: str,
    ) -> SwitchResult:
        if status is StoreStatus.INTERACTIVE_REQUIRED:
            return self._record(
                request, SwitchOutcome.INTERACTIVE_REQUIRED, message=reason
            )
        if status is StoreStatus.CONCURRENT_WRITE:
            # The adapter refused before writing because the authority changed
            # since it was read (Claude Code refreshed or re-logged in). Nothing
            # of ours landed, so report what is there now as preserved.
            observed = self._deps.authority.read()
            if observed.payload is not None:
                return self._record(
                    request, SwitchOutcome.FAILED_PRESERVED, observed.payload.identity, reason
                )
            return self._record(request, SwitchOutcome.INDETERMINATE, message=reason)
        observed = self._deps.authority.read()
        if observed.payload is not None and observed.payload.digest == before.digest:
            return self._record(
                request,
                SwitchOutcome.FAILED_PRESERVED,
                observed.payload.identity,
                reason,
            )
        identity = (
            observed.payload.identity if observed.payload else CredentialIdentity()
        )
        return self._record(request, SwitchOutcome.INDETERMINATE, identity, reason)

    def _verify_and_finalize(
        self, request: SwitchRequest, before: CredentialPayload
    ) -> SwitchResult:
        observed = self._deps.authority.read()
        if observed.payload is None:
            return self._record(
                request, SwitchOutcome.INDETERMINATE, message=observed.reason
            )
        if observed.payload.digest == before.digest:
            return self._record(
                request, SwitchOutcome.FAILED_PRESERVED, observed.payload.identity
            )
        if observed.payload.digest != request.payload.digest:
            return self._record(
                request, SwitchOutcome.INDETERMINATE, observed.payload.identity
            )
        fence = self._deps.writer_fence.inspect(
            required_protocol_epoch=self._deps.capability.writer_protocol_epoch,
            capability_epoch=self._deps.capability.capability_epoch,
        )
        if not fence.is_allowed:
            return self._record(
                request,
                SwitchOutcome.INDETERMINATE,
                observed.payload.identity,
                fence.reason,
            )
        outcome, message = self._publish_mirrors(request)
        if outcome is SwitchOutcome.INDETERMINATE:
            return self._record(request, outcome, observed.payload.identity, message)
        final = FinalizeSwitchRecord(
            request.operation_id,
            request.account_id,
            outcome,
            observed.payload.identity,
            f"switch:{request.operation_id}",
        )
        self._deps.repository.finalize(final)
        self._publish_snapshot(
            request,
            observed.payload.identity,
            ResolverState.RESOLVED,
            (f"authority:{self._deps.capability.authority.name}:readback",),
        )
        return _result(
            request,
            outcome,
            observed=observed.payload.identity,
            is_committed=True,
            message=message,
        )

    def _publish_mirrors(self, request: SwitchRequest) -> tuple[SwitchOutcome, str]:
        required = {item.locator for item in self._deps.capability.required_mirrors}
        optional = {item.locator for item in self._deps.capability.optional_metadata}
        failures = []
        for locator in required | optional:
            mirror = self._deps.mirrors.get(locator)
            if mirror is None:
                failures.append((locator, locator in required, "adapter missing"))
                continue
            write = mirror.write(request.payload, request.interaction)
            if write.status is StoreStatus.CONCURRENT_WRITE:
                # The mirror changed since it was read; re-arm and try once more.
                mirror.read()
                write = mirror.write(request.payload, request.interaction)
            if write.status is StoreStatus.OK:
                continue
            failures.append((locator, locator in required, write.reason))
        if any(is_required for _, is_required, _ in failures):
            return SwitchOutcome.INDETERMINATE, "; ".join(
                f"{locator}: {reason}" for locator, _, reason in failures
            )
        if failures:
            return SwitchOutcome.COMMITTED_DEGRADED, "; ".join(
                f"{locator}: {reason}" for locator, _, reason in failures
            )
        return SwitchOutcome.COMMITTED, ""

    def _publish_snapshot(
        self,
        request: SwitchRequest,
        observed: CredentialIdentity,
        state: ResolverState,
        evidence: tuple[str, ...],
    ) -> None:
        try:
            observed = _enrich_identity(request, observed)
            self._deps.snapshot_sink.publish(
                SnapshotUpdate(
                    scope="global",
                    state=state,
                    evidence=evidence,
                    credential_revision=f"switch:{request.operation_id}",
                    desired=CredentialIdentity(
                        request.account_id,
                        email=request.email,
                        organization_id=request.organization_id,
                    ),
                    observed=observed,
                )
            )
        except OSError as exc:
            logger.warning("Could not publish credential resolver snapshot: %s", exc)

    def _write_failure(
        self, request: SwitchRequest, status: StoreStatus, reason: str
    ) -> SwitchResult:
        outcome = (
            SwitchOutcome.INTERACTIVE_REQUIRED
            if status is StoreStatus.INTERACTIVE_REQUIRED
            else SwitchOutcome.FAILED_PRESERVED
        )
        return self._record(request, outcome, message=reason)

    def _record(
        self,
        request: SwitchRequest,
        outcome: SwitchOutcome,
        observed: CredentialIdentity = CredentialIdentity(),
        message: str = "",
    ) -> SwitchResult:
        self._deps.repository.record_outcome(
            OutcomeSwitchRecord(
                request.operation_id,
                request.account_id,
                outcome,
                observed,
                message,
                request.context,
                self._deps.capability.mode,
            )
        )
        return _result(request, outcome, observed=observed, message=message)
