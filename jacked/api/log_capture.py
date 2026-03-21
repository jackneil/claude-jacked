"""In-memory server log capture with batched WebSocket broadcast.

Captures Python logging output into a ring buffer (``deque``) and
periodically flushes accumulated entries to connected WebSocket clients.

The handler starts in **buffer-only mode** — it collects log entries
but does not broadcast until ``set_loop``/``set_registry`` are called
from the async lifespan.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jacked.api.websocket import WebSocketRegistry

MAX_BUFFER_SIZE = 2000
MAX_MSG_LENGTH = 4096
MAX_FLUSH_ENTRIES = 500
FLUSH_INTERVAL_SEC = 0.15

# Loggers whose events skip broadcast scheduling (prevents infinite loops
# where broadcast() → logger.debug() → emit() → broadcast() …)
_BROADCAST_SKIP_LOGGERS = frozenset({"jacked.api.websocket"})

_formatter = logging.Formatter("%(message)s")


class _CaptureHandler(logging.Handler):
    """Logging handler that delegates to a :class:`ServerLogBuffer`."""

    def __init__(self, buf: ServerLogBuffer) -> None:
        super().__init__()
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        self._buf._handle_record(record)


class ServerLogBuffer:
    """Thread-safe ring buffer for server log entries.

    >>> buf = ServerLogBuffer(maxlen=5)
    >>> buf.get_recent()
    []
    >>> buf.buffer_size
    5
    """

    def __init__(self, maxlen: int = MAX_BUFFER_SIZE) -> None:
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        self._lock = threading.Lock()
        self._pending: list[dict] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._registry: WebSocketRegistry | None = None
        self._broadcasting = False
        self._flush_scheduled = False
        self.handler = _CaptureHandler(self)

    # -- Wiring (called from lifespan) ------------------------------------

    def set_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """Store the running event loop for ``call_soon_threadsafe``."""
        self._loop = loop

    def set_registry(self, registry: WebSocketRegistry) -> None:
        """Enable WebSocket broadcast via *registry*."""
        self._registry = registry

    def detach(self) -> None:
        """Clear loop/registry refs and reset state on shutdown."""
        self._loop = None
        self._registry = None
        self._flush_scheduled = False
        self._broadcasting = False
        with self._lock:
            self._pending.clear()

    # -- Record handling ---------------------------------------------------

    def _handle_record(self, record: logging.LogRecord) -> None:
        """Format, buffer, and optionally schedule a broadcast flush."""
        entry = _format_entry(record)

        with self._lock:
            self._buffer.append(entry)
            self._pending.append(entry)
            # Cap pending to buffer size to prevent unbounded growth pre-wiring
            if len(self._pending) > self._buffer.maxlen:
                self._pending = self._pending[-self._buffer.maxlen:]

        # Skip broadcast for websocket-internal logs (prevents recursion)
        if record.name in _BROADCAST_SKIP_LOGGERS:
            return
        if self._broadcasting:
            return

        self._maybe_schedule_flush()

    def _maybe_schedule_flush(self) -> None:
        if not self._loop or not self._registry:
            return
        if self._flush_scheduled:
            return
        self._flush_scheduled = True
        try:
            self._loop.call_soon_threadsafe(self._start_flush_timer)
        except RuntimeError:
            # Event loop already closed
            self._flush_scheduled = False

    def _start_flush_timer(self) -> None:
        """Schedule a batched flush after the accumulation window."""
        self._loop.call_later(FLUSH_INTERVAL_SEC, self._trigger_flush)

    def _trigger_flush(self) -> None:
        """Fire the batched flush (runs on event loop thread)."""
        self._flush_scheduled = False
        asyncio.ensure_future(self._flush_async())

    async def _flush_async(self) -> None:
        """Broadcast accumulated entries to WebSocket clients."""
        with self._lock:
            entries = list(self._pending)
            self._pending.clear()

        if not entries or not self._registry:
            return

        # Cap per-flush to prevent oversized WebSocket frames on bursts
        if len(entries) > MAX_FLUSH_ENTRIES:
            entries = entries[-MAX_FLUSH_ENTRIES:]

        self._broadcasting = True
        try:
            await self._registry.broadcast(
                "server_logs_changed", {"entries": entries}
            )
        except Exception:
            logging.getLogger(__name__).debug(
                "Server log broadcast failed", exc_info=True
            )
        finally:
            self._broadcasting = False

        # Reschedule if entries accumulated during the broadcast await
        with self._lock:
            has_pending = bool(self._pending)
        if has_pending:
            self._maybe_schedule_flush()

    # -- Public API --------------------------------------------------------

    def get_recent(self, limit: int = 200) -> list[dict]:
        """Return the most recent *limit* log entries.

        >>> buf = ServerLogBuffer(maxlen=5)
        >>> buf.get_recent(10)
        []
        """
        with self._lock:
            snapshot = list(self._buffer)
        return snapshot[-limit:] if limit < len(snapshot) else snapshot

    @property
    def buffer_size(self) -> int:
        """Maximum number of entries the buffer can hold."""
        return self._buffer.maxlen


# -- Helpers ---------------------------------------------------------------


def _format_entry(record: logging.LogRecord) -> dict:
    """Convert a LogRecord into a JSON-serialisable dict."""
    msg = _formatter.format(record)
    if len(msg) > MAX_MSG_LENGTH:
        msg = msg[:MAX_MSG_LENGTH] + "…"
    return {
        "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        "level": record.levelname,
        "msg": msg,
        "logger": record.name,
    }


# -- Module singleton ------------------------------------------------------

server_log_buffer = ServerLogBuffer()
