"""Capability-aware credential storage and transaction primitives."""

from .canonical import CredentialPayload
from .models import (
    CapabilityMode,
    CredentialIdentity,
    SwitchOutcome,
    SwitchResult,
)
from .transaction import CredentialTransactionEngine, SwitchRequest

__all__ = [
    "CapabilityMode",
    "CredentialIdentity",
    "CredentialPayload",
    "CredentialTransactionEngine",
    "SwitchOutcome",
    "SwitchRequest",
    "SwitchResult",
]
