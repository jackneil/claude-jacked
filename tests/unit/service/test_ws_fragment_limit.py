"""The dashboard socket protocol limits the fragments of one message.

uvicorn's Sans-I/O protocol keeps every continuation frame in a list, and an
empty frame adds nothing to the ws_max_size total, so a client could grow the
list without a limit. These tests serve a small echo app with the real
SERVER_SETTINGS on a loopback socket and send real fragmented messages.

A client that is still writing when the server closes can get a TCP reset
before it reads the 1009 close frame, so the refusal tests check the server's
own WARNING record. That record is also what an operator sees in the log.
"""

from __future__ import annotations

import base64
import logging
import os
import socket
import threading
import time

import pytest

from jacked.api.serving import SERVER_SETTINGS
from jacked.api.ws_protocol import MAX_FRAGMENTS

uvicorn = pytest.importorskip("uvicorn")
ws_client = pytest.importorskip("websockets.sync.client")
from websockets.exceptions import ConnectionClosed  # noqa: E402

# send(iterable) sends one frame per chunk, then an empty final frame that
# ends the message, so N chunks travel as N + 1 frames.
AT_LIMIT_CHUNKS = MAX_FRAGMENTS - 1
REFUSAL = "websocket closed: a message passed"


async def _echo_app(scope, receive, send):
    if scope["type"] != "websocket":
        return
    await receive()  # websocket.connect
    await send({"type": "websocket.accept"})
    while True:
        event = await receive()
        if event["type"] == "websocket.disconnect":
            return
        await send({"type": "websocket.send", "text": event.get("text") or ""})


@pytest.fixture
def echo_port():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_echo_app, log_level="warning", **SERVER_SETTINGS))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not getattr(server, "started", False):
            time.sleep(0.05)
        assert getattr(server, "started", False), "uvicorn did not start"
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        sock.close()


def _closed_by_server(conn) -> int | None:
    """Wait for the server to end the connection. Return the close code, or
    None when a TCP reset arrived before the close frame."""
    try:
        conn.recv(timeout=10)
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd else None
    pytest.fail("the server sent a message instead of closing the connection")


def _wait_for_refusal(caplog, timeout=10.0):
    deadline = time.monotonic() + timeout
    while True:
        records = [r for r in caplog.records if REFUSAL in r.getMessage()]
        if records or time.monotonic() >= deadline:
            return records
        time.sleep(0.05)


def test_a_message_at_the_limit_arrives_whole(echo_port, caplog):
    caplog.set_level(logging.WARNING, logger="jacked.api.ws_protocol")
    with ws_client.connect(f"ws://127.0.0.1:{echo_port}/", open_timeout=5) as conn:
        conn.send(["a"] * AT_LIMIT_CHUNKS)
        assert conn.recv(timeout=10) == "a" * AT_LIMIT_CHUNKS
        # The connection stays usable after a long fragmented message.
        conn.send("next")
        assert conn.recv(timeout=10) == "next"
    assert not [r for r in caplog.records if REFUSAL in r.getMessage()]


def test_one_fragment_past_the_limit_closes_with_1009(echo_port, caplog):
    caplog.set_level(logging.WARNING, logger="jacked.api.ws_protocol")
    with ws_client.connect(f"ws://127.0.0.1:{echo_port}/", open_timeout=5) as conn:
        try:
            conn.send(["a"] * (AT_LIMIT_CHUNKS + 1))
        except ConnectionClosed:
            pass  # the server can close while the client still sends
        assert _closed_by_server(conn) in (1009, None)
    (record,) = _wait_for_refusal(caplog)
    assert record.levelno == logging.WARNING
    assert "1009" in record.getMessage() and "handshake complete" in record.getMessage()


def test_a_stream_of_empty_fragments_is_refused(echo_port, caplog):
    """The attack shape: empty continuation frames add zero bytes, so only
    the fragment count stops them."""
    caplog.set_level(logging.WARNING, logger="jacked.api.ws_protocol")
    with ws_client.connect(f"ws://127.0.0.1:{echo_port}/", open_timeout=5) as conn:
        try:
            conn.send([""] * (MAX_FRAGMENTS * 3))
        except ConnectionClosed:
            pass
        assert _closed_by_server(conn) in (1009, None)
    assert _wait_for_refusal(caplog)


def _masked_frame(opcode: int, fin: bool) -> bytes:
    return bytes([(0x80 if fin else 0) | opcode, 0x80]) + os.urandom(4)


def test_fragments_before_the_handshake_completes_close_cleanly(caplog):
    """uvicorn's HTTP layer passes only the upgrade request to the socket
    protocol, so frames that arrive in a later read can reach it before the
    app accepts (state CONNECTING). websockets sends no close frame in that
    state, so the limit must close the transport itself. This drives the
    protocol directly to make that order certain."""
    import asyncio
    from unittest.mock import MagicMock

    from jacked.api.ws_protocol import BoundedFragmentsProtocol

    caplog.set_level(logging.WARNING, logger="jacked.api.ws_protocol")
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        "GET / HTTP/1.1\r\nHost: 127.0.0.1:8321\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
    ).encode()
    frames = _masked_frame(0x1, False) + _masked_frame(0x0, False) * MAX_FRAGMENTS

    async def scenario():
        config = uvicorn.Config(_echo_app, **SERVER_SETTINGS)
        config.load()
        protocol = BoundedFragmentsProtocol(
            config=config, server_state=MagicMock(connections=set(), tasks=set()),
            app_state={},
        )
        transport = MagicMock()
        transport.get_extra_info.return_value = None
        protocol.connection_made(transport)
        protocol.data_received(request)
        # No await between the two reads: the app task has not run, so the
        # handshake is not complete when the frames arrive.
        protocol.data_received(frames)
        assert not protocol.handshake_complete
        assert protocol.frames == []
        transport.close.assert_called()
        for task in list(protocol.tasks):
            task.cancel()

    asyncio.run(scenario())
    records = _wait_for_refusal(caplog, timeout=0)
    assert len(records) == 1
    assert "handshake not complete" in records[0].getMessage()


def test_the_service_limits_a_message_to_64_kib():
    assert SERVER_SETTINGS["ws_max_size"] == 64 * 1024


def test_uvicorn_still_has_the_members_the_limit_uses():
    """The subclass overrides and calls uvicorn members that are not a public
    API. A uvicorn release that renames one turns the limit off; this test
    makes that a CI failure instead of a log line."""
    from jacked.api import ws_protocol

    assert ws_protocol._MISSING == []
