"""Typed credential capability, store, and transaction contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .canonical import CredentialPayload


class CapabilityMode(str, Enum):
    SCOPED_COOPERATIVE = "scoped_cooperative"
    GLOBAL_COOPERATIVE = "global_cooperative"
    GLOBAL_UNCOOPERATIVE = "global_uncooperative"
    UNSUPPORTED = "unsupported"


class StoreRole(str, Enum):
    AUTHORITY = "authority"
    REQUIRED_MIRROR = "required_mirror"
    OPTIONAL_METADATA = "optional_metadata"


class InteractionMode(str, Enum):
    BACKGROUND = "background"
    FOREGROUND = "foreground"


class StoreStatus(str, Enum):
    OK = "ok"
    MISSING = "missing"
    UNUSABLE = "unusable"
    INTERACTIVE_REQUIRED = "interactive_required"
    DENIED = "denied"
    CONCURRENT_WRITE = "concurrent_write"
    ERROR = "error"


class SwitchContext(str, Enum):
    MANUAL = "manual"
    OAUTH = "oauth"
    LAUNCH = "launch"
    AUTO_SWAP = "auto_swap"


class SwitchOutcome(str, Enum):
    COMMITTED = "committed"
    COMMITTED_DEGRADED = "committed_degraded"
    OBSERVED_TARGET_UNFENCED = "observed_target_unfenced"
    INTERACTIVE_REQUIRED = "interactive_required"
    INTERACTIVE_OPERATION_IN_PROGRESS = "interactive_operation_in_progress"
    BUSY = "busy"
    CONCURRENT_WRITE = "concurrent_write"
    DIVERGED = "diverged"
    RESTART_REQUIRED = "restart_required"
    UNUSABLE = "unusable"
    UNSUPPORTED = "unsupported"
    FAILED_PRESERVED = "failed_preserved"
    INDETERMINATE = "indeterminate"


class SessionActivationState(str, Enum):
    UNCHANGED = "unchanged"
    PENDING_NEXT_ACTIVITY = "pending_next_activity"
    SCOPED_TARGET = "scoped_target"
    RESTART_REQUIRED = "restart_required"
    UNKNOWN = "unknown"


class ProviderVerificationState(str, Enum):
    UNVERIFIED = "unverified"
    VERIFIED = "verified"


@dataclass(frozen=True)
class CredentialIdentity:
    account_id: int | None = None
    email: str | None = None
    organization_id: str | None = None


@dataclass(frozen=True)
class ExecutableIdentity:
    resolved_path: str
    sha256: str
    build_version: str
    config_mode: str
    platform_system: str = ""
    platform_machine: str = ""


@dataclass(frozen=True)
class StoreDeclaration:
    name: str
    locator: str
    role: StoreRole
    consumers: tuple[str, ...] = ()


@dataclass(frozen=True)
class CredentialCapability:
    executable: ExecutableIdentity
    mode: CapabilityMode
    authority: StoreDeclaration
    consumers: tuple[str, ...]
    capability_epoch: int
    writer_protocol_epoch: int
    provenance: str
    registry_version: int
    required_mirrors: tuple[StoreDeclaration, ...] = ()
    optional_metadata: tuple[StoreDeclaration, ...] = ()
    canonicalizer_version: int = 1

    def __post_init__(self) -> None:
        if self.authority.role is not StoreRole.AUTHORITY:
            raise ValueError("capability authority must have the authority role")
        if any(
            item.role is not StoreRole.REQUIRED_MIRROR for item in self.required_mirrors
        ):
            raise ValueError("required mirror has the wrong store role")
        if any(
            item.role is not StoreRole.OPTIONAL_METADATA
            for item in self.optional_metadata
        ):
            raise ValueError("optional metadata has the wrong store role")
        locators = [
            self.authority.locator,
            *(item.locator for item in self.required_mirrors),
            *(item.locator for item in self.optional_metadata),
        ]
        if len(locators) != len(set(locators)):
            raise ValueError("credential store locators must be unique")
        if self.mode is not CapabilityMode.UNSUPPORTED:
            if not self.consumers:
                raise ValueError("supported capability must declare its consumers")
            if self.capability_epoch <= 0 or self.writer_protocol_epoch <= 0:
                raise ValueError("supported capability epochs must be positive")


@dataclass(frozen=True)
class CapabilityResolution:
    capability: CredentialCapability
    can_mutate: bool
    reason: str


@dataclass(frozen=True)
class StoreReadResult:
    status: StoreStatus
    payload: CredentialPayload | None = field(default=None, repr=False)
    reason: str = ""


@dataclass(frozen=True)
class StoreWriteResult:
    status: StoreStatus
    reason: str = ""


@dataclass(frozen=True)
class IdentityAxis:
    account_id: int | None = None
    state: str = "unknown"


@dataclass(frozen=True)
class SwitchResult:
    operation_id: str
    outcome: SwitchOutcome
    desired_default: IdentityAxis
    storage: IdentityAxis
    committed_authority: IdentityAxis
    existing_session_activation: SessionActivationState
    provider_verification: ProviderVerificationState
    observed_identity: CredentialIdentity = CredentialIdentity()
    message: str = ""


@dataclass(frozen=True)
class WriterWitness:
    writer_id: str
    protocol_epoch: int
    capability_epoch: int
    is_active: bool
    evidence: str


@dataclass(frozen=True)
class WriterFenceResult:
    is_allowed: bool
    reason: str
    witnesses: tuple[WriterWitness, ...] = ()


@dataclass(frozen=True)
class PendingSwitchRecord:
    operation_id: str
    account_id: int
    organization_id: str | None
    context: SwitchContext
    capability_mode: CapabilityMode
    machine_install_id: str
    backend_locator: str
    capability_epoch: int
    canonicalizer_version: int
    before_hmac: str
    target_hmac: str


@dataclass(frozen=True)
class FinalizeSwitchRecord:
    operation_id: str
    account_id: int
    outcome: SwitchOutcome
    observed_identity: CredentialIdentity
    credential_revision: str


@dataclass(frozen=True)
class OutcomeSwitchRecord:
    operation_id: str
    account_id: int
    outcome: SwitchOutcome
    observed_identity: CredentialIdentity
    message: str
    context: SwitchContext = SwitchContext.MANUAL
    capability_mode: CapabilityMode = CapabilityMode.UNSUPPORTED
