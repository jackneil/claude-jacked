"""Credential store protocols and deterministic test implementation."""

from __future__ import annotations

from typing import Protocol

from .canonical import CredentialPayload
from .models import InteractionMode, StoreReadResult, StoreStatus, StoreWriteResult


class CredentialStore(Protocol):
    @property
    def locator(self) -> str: ...

    def read(self) -> StoreReadResult: ...

    def write(
        self, payload: CredentialPayload, interaction: InteractionMode
    ) -> StoreWriteResult: ...


class MemoryCredentialStore:
    """In-memory store used for state-machine and repository tests."""

    def __init__(self, locator: str, payload: CredentialPayload | None = None) -> None:
        self._locator = locator
        self._payload = payload
        self.fail_writes = False
        self.requires_interaction = False
        self.write_count = 0
        self.event_log: list[str] | None = None

    @property
    def locator(self) -> str:
        return self._locator

    def read(self) -> StoreReadResult:
        if self.event_log is not None:
            self.event_log.append("store_read")
        if self._payload is None:
            return StoreReadResult(StoreStatus.MISSING)
        return StoreReadResult(StoreStatus.OK, self._payload)

    def write(
        self, payload: CredentialPayload, interaction: InteractionMode
    ) -> StoreWriteResult:
        self.write_count += 1
        if self.event_log is not None:
            self.event_log.append("store_write")
        if self.requires_interaction and interaction is InteractionMode.BACKGROUND:
            return StoreWriteResult(
                StoreStatus.INTERACTIVE_REQUIRED, "interaction required"
            )
        if self.fail_writes:
            return StoreWriteResult(StoreStatus.ERROR, "injected write failure")
        self._payload = payload
        return StoreWriteResult(StoreStatus.OK)
