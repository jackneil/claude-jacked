"""Topic-based WebSocket client registry for the jacked event bus.

Provides a single registry that tracks connected WebSocket clients and
their topic subscriptions.  Any server component can broadcast typed
events — only clients subscribed to a matching topic (or the wildcard
``*``) receive the message.

>>> registry = WebSocketRegistry()
>>> registry.client_count
0
"""

import json
import logging
import time
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger(__name__)


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

        Dead clients (failed sends) are automatically pruned.
        """
        message = json.dumps(
            {
                "type": topic,
                "payload": payload or {},
                "source": source or "server",
                "timestamp": int(time.time()),
            }
        )
        dead: list[WebSocket] = []
        for ws in list(self._clients):
            subs = self._clients[ws]
            if "*" in subs or topic in subs:
                try:
                    await ws.send_text(message)
                except Exception:
                    dead.append(ws)
        for ws in dead:
            self._clients.pop(ws, None)
            logger.debug("Pruned dead WebSocket client")

    @property
    def client_count(self) -> int:
        """Number of currently connected clients.

        >>> WebSocketRegistry().client_count
        0
        """
        return len(self._clients)
