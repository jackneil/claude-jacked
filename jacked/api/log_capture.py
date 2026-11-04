"""In-memory server log capture with batched WebSocket broadcast.

Captures Python logging output into a ring buffer (``deque``) and
periodically flushes accumulated entries to connected WebSocket clients.

The handler starts in **buffer-only mode** — it collects log entries
but does not broadcast until ``set_loop``/``set_registry`` are called
from the async lifespan.

The buffer reaches every dashboard client, remote ones too, so an entry
names each exception by its type only. The host's log file keeps the full
text.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from jacked.public_errors import describe

if TYPE_CHECKING:
    from jacked.api.websocket import WebSocketRegistry

MAX_BUFFER_SIZE = 2000
MAX_MSG_LENGTH = 4096
MAX_FLUSH_ENTRIES = 500
FLUSH_INTERVAL_SEC = 0.15

# Loggers whose events skip broadcast scheduling (prevents infinite loops
# where broadcast() → logger.debug() → emit() → broadcast() …)
_BROADCAST_SKIP_LOGGERS = frozenset({"jacked.api.websocket"})


class _CaptureHandler(logging.Handler):
    """Logging handler that delegates to a :class:`ServerLogBuffer`."""

    def __init__(self, buf: ServerLogBuffer) -> None:
        super().__init__()
        self._buf = buf

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._buf._handle_record(record)
        except Exception:
            self.handleError(record)


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


class _Described:
    """An exception in a log record, as the dashboard shows it.

    ``%s``, ``%r`` and ``format()`` all give :func:`describe`, so the record
    template formats as written and no exception text gets into the entry.
    """

    __slots__ = ("_text",)

    def __init__(self, exc: BaseException) -> None:
        self._text = describe(exc)

    def __str__(self) -> str:
        return self._text

    __repr__ = __str__

    def __format__(self, spec: str) -> str:
        return format(self._text, spec)


def _client_value(value: object) -> object:
    """Return ``value`` with each exception in it replaced by its description.

    A list, tuple or set is examined one level deep.

    >>> _client_value(ValueError("secret"))
    ValueError
    >>> _client_value([KeyError("k"), 3])
    [KeyError, 3]
    >>> _client_value("text")
    'text'
    """
    if isinstance(value, BaseException):
        return _Described(value)
    if isinstance(value, (list, tuple, set, frozenset)) and any(
        isinstance(item, BaseException) for item in value
    ):
        items = [_client_value(item) for item in value]
        # Base types only: a namedtuple or a subclass can need other arguments.
        if isinstance(value, list):
            return items
        return tuple(items) if isinstance(value, tuple) else set(items)
    return value


def _client_message(record: logging.LogRecord) -> str:
    """Format the message of ``record`` for a dashboard client.

    Each exception in ``record.args`` (or an exception that is the message
    itself) gives :func:`describe`, not its text. Other values format as
    usual. The host's log file formats the same record with the full text.

    >>> rec = logging.LogRecord("t", logging.WARNING, "", 0, "failed: %s", (OSError("/home/u/x"),), None)
    >>> _client_message(rec)
    'failed: OSError'
    >>> rec.getMessage()
    'failed: /home/u/x'

    A template that does not format gives the template only:

    >>> rec = logging.LogRecord("t", logging.WARNING, "", 0, "n=%d", (ValueError("v"),), None)
    >>> _client_message(rec)
    'n=%d [message did not format]'
    """
    if isinstance(record.msg, BaseException):
        return describe(record.msg)
    template = str(record.msg)
    args = record.args
    if not args:
        return template
    if isinstance(args, Mapping):
        safe: object = {key: _client_value(val) for key, val in args.items()}
    else:
        safe = tuple(_client_value(arg) for arg in args)
    try:
        return template % safe
    except Exception:
        # Never fall back to getMessage(): that would format the raw text.
        return f"{template} [message did not format]"


def _format_entry(record: logging.LogRecord) -> dict:
    """Convert a LogRecord into a JSON-serialisable dict.

    The entry goes to every dashboard client, remote ones too. The message
    names each exception it formats by its type only (see
    :func:`_client_message`). A record with ``exc_info`` gives its message
    and the exception type only: the traceback contains the exception text,
    which can name a path or a response body. The host's log file keeps the
    full text and the traceback (#289i).

    >>> import sys
    >>> try:
    ...     raise OSError("detail")
    ... except OSError:
    ...     rec = logging.LogRecord("t", logging.ERROR, "", 0, "failed", (), sys.exc_info())
    >>> _format_entry(rec)["msg"]
    'failed [OSError]'
    """
    msg = _client_message(record)
    if record.exc_info and record.exc_info[1] is not None:
        msg = f"{msg} [{describe(record.exc_info[1])}]"
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
