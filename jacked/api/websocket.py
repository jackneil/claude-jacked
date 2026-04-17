"""Topic-based WebSocket client registry for the jacked event bus.

Provides a single registry that tracks connected WebSocket clients and
their topic subscriptions.  Any server component can broadcast typed
events — only clients subscribed to a matching topic (or the wildcard
``*``) receive the message.

>>> registry = WebSocketRegistry()
>>> registry.client_count
0
"""

import asyncio
import json
import logging
import time
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# Per-client send timeout. A zombie Chrome tab with a stuck WebSocket used to
# block the broadcast loop indefinitely — and since bulk usage refresh broadcasts
# progress events while holding _bulk_refresh_lock, one slow client could wedge
# all future refreshes behind a 409 "already in progress" response.
_SEND_TIMEOUT_SECONDS = 5.0


class WebSocketRegistry:
    """Topic-based WebSocket client registry.

    >>> r = WebSocketRegistry()
    >>> r.client_count
    0
    """

    def __init__(self):
        self._clients: dict[WebSocket, set[str]] = {}

    async def connect(self, ws: WebSocket, topics: Optional[list[str]] = None):
        """Register a WebSocket client with optional topic subscriptions.

        >>> import asyncio
        >>> r = WebSocketRegistry()
        >>> # connect() is async, tested via unit tests
        """
        self._clients[ws] = set(topics or ["*"])

    def disconnect(self, ws: WebSocket):
        """Remove a WebSocket client from the registry.

        >>> r = WebSocketRegistry()
        >>> r.disconnect(object())  # no-op for unknown client
        """
        self._clients.pop(ws, None)

    async def broadcast(
        self, topic: str, payload: Optional[dict] = None, source: Optional[str] = None
    ):
        """Send an event to all clients subscribed to *topic* or ``*``.

        Sends happen in parallel with a per-client timeout so one stuck
        socket (e.g. backgrounded Chrome tab with full send buffer) cannot
        block other clients or the caller. Dead / timed-out clients are
        pruned from the registry.
        """
        message = json.dumps(
            {
                "type": topic,
                "payload": payload or {},
                "source": source or "server",
                "timestamp": int(time.time()),
            }
        )

        targets: list[WebSocket] = [
            ws for ws, subs in list(self._clients.items())
            if "*" in subs or topic in subs
        ]
        if not targets:
            return

        async def _send(ws: WebSocket) -> Optional[WebSocket]:
            try:
                await asyncio.wait_for(ws.send_text(message), timeout=_SEND_TIMEOUT_SECONDS)
                return None
            except Exception:
                return ws

        results = await asyncio.gather(
            *(_send(ws) for ws in targets), return_exceptions=False
        )
        for dead_ws in (r for r in results if r is not None):
            self._clients.pop(dead_ws, None)
            logger.debug("Pruned dead / slow WebSocket client")

    @property
    def client_count(self) -> int:
        """Number of currently connected clients.

        >>> WebSocketRegistry().client_count
        0
        """
        return len(self._clients)

    def has_subscribers(self, topic: str) -> bool:
        """Check if any connected client is subscribed to the given topic.

        Returns True if at least one client subscribes to *topic* or ``*``.

        >>> WebSocketRegistry().has_subscribers("analytics")
        False
        """
        for subs in self._clients.values():
            if "*" in subs or topic in subs:
                return True
        return False
