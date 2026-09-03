"""Fail-closed minimum writer-protocol fence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .models import WriterFenceResult, WriterWitness


@dataclass(frozen=True)
class WriterInspection:
    witnesses: tuple[WriterWitness, ...]
    is_complete: bool


class WriterInspector(Protocol):
    def inspect(self) -> WriterInspection: ...


class StaticWriterInspector:
    """Deterministic writer inspection for explicit integrations and tests."""

    def __init__(
        self, witnesses: tuple[WriterWitness, ...], *, is_complete: bool = True
    ) -> None:
        self._inspection = WriterInspection(witnesses, is_complete)

    def inspect(self) -> WriterInspection:
        return self._inspection


class WriterFence:
    """Permit mutation only when every possible active writer is certified."""

    def __init__(self, inspector: WriterInspector) -> None:
        self._inspector = inspector

    def inspect(
        self, *, required_protocol_epoch: int, capability_epoch: int
    ) -> WriterFenceResult:
        inspection = self._inspector.inspect()
        if not inspection.is_complete:
            return WriterFenceResult(False, "cannot exclude an unfenced writer")
        active = tuple(item for item in inspection.witnesses if item.is_active)
        for witness in active:
            if witness.protocol_epoch < required_protocol_epoch:
                return WriterFenceResult(
                    False,
                    f"legacy writer {witness.writer_id} has protocol "
                    f"{witness.protocol_epoch}",
                    active,
                )
            if witness.capability_epoch != capability_epoch:
                return WriterFenceResult(
                    False,
                    f"writer {witness.writer_id} has a different capability epoch",
                    active,
                )
        return WriterFenceResult(True, "all possible writers are fenced", active)
