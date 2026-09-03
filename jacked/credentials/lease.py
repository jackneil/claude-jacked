"""Serialized credential switch lease abstraction."""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import ContextManager, Iterator, Protocol


class SwitchLease(Protocol):
    def acquire(self) -> ContextManager[bool]: ...


class ProcessSwitchLease:
    """Nonblocking in-process lease; service adapters may supply an OS lease."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    @contextmanager
    def acquire(self) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        try:
            yield acquired
        finally:
            if acquired:
                self._lock.release()
