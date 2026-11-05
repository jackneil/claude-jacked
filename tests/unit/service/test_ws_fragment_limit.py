"""The dashboard socket protocol limits the fragments of one message.

uvicorn's Sans-I/O protocol keeps every continuation frame in a list, and an
empty frame adds nothing to the ws_max_size total, so a client could grow the
list without a limit. These tests serve a small echo app with the real
SERVER_SETTINGS on a loopback socket and send real fragmented messages.
"""

from __future__ import annotations

import socket
import threading
import time

import pytest

from jacked.api.serving import SERVER_SETTINGS
from jacked.api.ws_protocol import MAX_FRAGMENTS

uvicorn = pytest.importorskip("uvicorn")
ws_client = pytest.importorskip("websockets.sync.client")
from websockets.exceptions import ConnectionClosed  # noqa: E402


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
def echo_url():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(_echo_app, log_level="warning", **SERVER_SETTINGS))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline and not getattr(server, "started", False):
        time.sleep(0.05)
    assert getattr(server, "started", False), "uvicorn did not start"
    try:
        yield f"ws://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        sock.close()


# send(iterable) sends one frame per chunk, then an empty final frame that
# ends the message, so N chunks travel as N + 1 frames.
AT_LIMIT_CHUNKS = MAX_FRAGMENTS - 1


def _close_code(conn) -> int | None:
    try:
        conn.recv(timeout=10)
    except ConnectionClosed as exc:
        return exc.rcvd.code if exc.rcvd else None
    return None


def test_a_message_at_the_limit_arrives_whole(echo_url):
    with ws_client.connect(echo_url, open_timeout=5) as conn:
        conn.send(["a"] * AT_LIMIT_CHUNKS)
        assert conn.recv(timeout=10) == "a" * AT_LIMIT_CHUNKS
        # The connection stays usable after a long fragmented message.
        conn.send("next")
        assert conn.recv(timeout=10) == "next"


def test_one_fragment_past_the_limit_closes_with_1009(echo_url):
    with ws_client.connect(echo_url, open_timeout=5) as conn:
        try:
            conn.send(["a"] * (AT_LIMIT_CHUNKS + 1))
        except ConnectionClosed:
            pass  # the server can close while the client still sends
        assert _close_code(conn) == 1009


def test_a_stream_of_empty_fragments_is_refused(echo_url):
    """The attack shape: empty continuation frames add zero bytes, so only
    the fragment count stops them."""
    with ws_client.connect(echo_url, open_timeout=5) as conn:
        try:
            conn.send([""] * (MAX_FRAGMENTS * 3))
        except ConnectionClosed:
            pass
        assert _close_code(conn) == 1009
