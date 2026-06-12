"""Unit tests for the WebSocketRegistry event bus.

Tests cover connect/disconnect, topic-based broadcasting, wildcard
subscriptions, dead client cleanup, and empty broadcast safety.
"""

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

from jacked.api.websocket import WebSocketRegistry


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.new_event_loop().run_until_complete(coro)


def _mock_ws():
    """Create a mock WebSocket with async send_text."""
    ws = MagicMock()
    ws.send_text = AsyncMock()
    return ws


def test_connect_disconnect():
    """Connect and disconnect clients, verify count.

    >>> r = WebSocketRegistry()
    >>> r.client_count
    0
    """
    r = WebSocketRegistry()
    ws1 = _mock_ws()
    ws2 = _mock_ws()

    _run(r.connect(ws1))
    assert r.client_count == 1

    _run(r.connect(ws2, topics=["credentials_changed"]))
    assert r.client_count == 2

    r.disconnect(ws1)
    assert r.client_count == 1

    r.disconnect(ws2)
    assert r.client_count == 0


def test_disconnect_unknown_client():
    """Disconnecting an unknown client is a no-op.

    >>> r = WebSocketRegistry()
    >>> r.disconnect(object())
    """
    r = WebSocketRegistry()
    r.disconnect(_mock_ws())
    assert r.client_count == 0


def test_broadcast_to_subscribed_topic():
    """Only clients subscribed to the topic receive the message.

    >>> # Verified via unit test
    """
    r = WebSocketRegistry()
    ws_cred = _mock_ws()
    ws_usage = _mock_ws()

    _run(r.connect(ws_cred, topics=["credentials_changed"]))
    _run(r.connect(ws_usage, topics=["usage_updated"]))

    _run(r.broadcast("credentials_changed", payload={"file": "test"}))

    ws_cred.send_text.assert_called_once()
    ws_usage.send_text.assert_not_called()

    # Verify message envelope
    msg = json.loads(ws_cred.send_text.call_args[0][0])
    assert msg["type"] == "credentials_changed"
    assert msg["payload"] == {"file": "test"}
    assert msg["source"] == "server"
    assert "timestamp" in msg


def test_broadcast_wildcard():
    """Wildcard subscribers receive all events.

    >>> # Verified via unit test
    """
    r = WebSocketRegistry()
    ws_all = _mock_ws()
    ws_specific = _mock_ws()

    _run(r.connect(ws_all))  # default = ["*"]
    _run(r.connect(ws_specific, topics=["usage_updated"]))

    _run(r.broadcast("credentials_changed"))

    ws_all.send_text.assert_called_once()
    ws_specific.send_text.assert_not_called()


def test_broadcast_custom_source():
    """Custom source is included in the message envelope.

    >>> # Verified via unit test
    """
    r = WebSocketRegistry()
    ws = _mock_ws()
    _run(r.connect(ws))

    _run(r.broadcast("test_event", source="file_watcher"))

    msg = json.loads(ws.send_text.call_args[0][0])
    assert msg["source"] == "file_watcher"


def test_dead_client_cleanup():
    """Failed sends remove the dead client from the registry.

    >>> # Verified via unit test
    """
    r = WebSocketRegistry()
    ws_alive = _mock_ws()
    ws_dead = _mock_ws()
    ws_dead.send_text.side_effect = ConnectionError("gone")

    _run(r.connect(ws_alive))
    _run(r.connect(ws_dead))
    assert r.client_count == 2

    _run(r.broadcast("test"))

    assert r.client_count == 1
    ws_alive.send_text.assert_called_once()


def test_empty_broadcast():
    """Broadcasting with no clients doesn't crash.

    >>> # Verified via unit test
    """
    r = WebSocketRegistry()
    _run(r.broadcast("credentials_changed"))
    assert r.client_count == 0


def test_multi_topic_subscription():
    """Client subscribed to multiple topics receives matching events.

    >>> # Verified via unit test
    """
    r = WebSocketRegistry()
    ws = _mock_ws()
    _run(r.connect(ws, topics=["credentials_changed", "usage_updated"]))

    _run(r.broadcast("credentials_changed"))
    assert ws.send_text.call_count == 1

    _run(r.broadcast("usage_updated"))
    assert ws.send_text.call_count == 2

    _run(r.broadcast("some_other_event"))
    assert ws.send_text.call_count == 2  # Not subscribed to this one


def test_slow_client_does_not_block_others():
    """A hanging send_text must not stall sends to other clients.

    Regression: bulk usage refresh broadcasts progress events while holding
    _bulk_refresh_lock. A backgrounded Chrome tab with a wedged WS send used
    to block the broadcast loop indefinitely, wedging all future refreshes
    behind a 409.
    """
    import time
    from unittest.mock import patch

    r = WebSocketRegistry()
    slow_ws = _mock_ws()
    fast_ws = _mock_ws()

    async def _hang_forever(_msg):
        await asyncio.sleep(300)

    slow_ws.send_text = AsyncMock(side_effect=_hang_forever)

    _run(r.connect(slow_ws))
    _run(r.connect(fast_ws))

    with patch("jacked.api.websocket._SEND_TIMEOUT_SECONDS", 0.1):
        t0 = time.monotonic()
        _run(r.broadcast("usage_refresh_progress"))
        elapsed = time.monotonic() - t0

    assert elapsed < 2.0, f"broadcast took {elapsed:.2f}s — slow client wedged it"
    fast_ws.send_text.assert_called_once()
    assert slow_ws not in r._clients
    assert fast_ws in r._clients


def test_broadcast_prunes_timed_out_client():
    """Per-client timeout: stuck client is pruned, not retained."""
    from unittest.mock import patch

    async def _hang(_m):
        await asyncio.sleep(300)

    r = WebSocketRegistry()
    stuck_ws = _mock_ws()
    stuck_ws.send_text = AsyncMock(side_effect=_hang)

    _run(r.connect(stuck_ws))
    assert r.client_count == 1

    with patch("jacked.api.websocket._SEND_TIMEOUT_SECONDS", 0.05):
        _run(r.broadcast("x"))

    assert r.client_count == 0
