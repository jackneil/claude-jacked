"""Persistence interface for crash-safe credential switch transactions."""

from __future__ import annotations

from typing import Protocol

from .models import FinalizeSwitchRecord, OutcomeSwitchRecord, PendingSwitchRecord


class CredentialSwitchRepository(Protocol):
    """Database adapter contract. Finalize must be one SQLite transaction."""

    def create_pending(self, record: PendingSwitchRecord) -> None: ...

    def finalize(self, record: FinalizeSwitchRecord) -> None: ...

    def record_outcome(self, record: OutcomeSwitchRecord) -> None: ...

    def get_pending(self, operation_id: str) -> PendingSwitchRecord | None: ...

    def list_pending(self) -> tuple[PendingSwitchRecord, ...]: ...


class InMemoryCredentialSwitchRepository:
    """Reference implementation useful for adapter conformance tests."""

    def __init__(self) -> None:
        self.pending: dict[str, PendingSwitchRecord] = {}
        self.created: list[PendingSwitchRecord] = []
        self.finalized: list[FinalizeSwitchRecord] = []
        self.outcomes: list[OutcomeSwitchRecord] = []
        self.event_log: list[str] = []

    def create_pending(self, record: PendingSwitchRecord) -> None:
        if record.operation_id in self.pending:
            raise ValueError("credential switch operation already exists")
        self.pending[record.operation_id] = record
        self.created.append(record)
        self.event_log.append("create_pending")

    def finalize(self, record: FinalizeSwitchRecord) -> None:
        if record.operation_id not in self.pending:
            raise ValueError("credential switch operation is not pending")
        self.finalized.append(record)
        self.pending.pop(record.operation_id)
        self.event_log.append("finalize")

    def record_outcome(self, record: OutcomeSwitchRecord) -> None:
        self.outcomes.append(record)
        if record.outcome.value != "indeterminate":
            self.pending.pop(record.operation_id, None)
        self.event_log.append("record_outcome")

    def get_pending(self, operation_id: str) -> PendingSwitchRecord | None:
        return self.pending.get(operation_id)

    def list_pending(self) -> tuple[PendingSwitchRecord, ...]:
        return tuple(self.pending.values())
