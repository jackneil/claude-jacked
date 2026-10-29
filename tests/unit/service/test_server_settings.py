"""Every jacked uvicorn server shares SERVER_SETTINGS (jacked/api/serving.py),
and the dashboard socket really runs on the websockets Sans-I/O protocol.

The live test serves the real app on a loopback socket under a throwaway HOME
(the fixture from test_multi_socket_serving), so nothing touches ~/.claude or
the running service.
"""

import ast
import json
import threading
import time
from pathlib import Path

import pytest

from jacked.api.serving import SERVER_SETTINGS
from tests.unit.service.test_multi_socket_serving import (  # noqa: F401 - fixture
    _bind_ephemeral_loopback,
    _isolate_home,
)

uvicorn = pytest.importorskip("uvicorn")
ws_client = pytest.importorskip("websockets.sync.client")

JACKED = Path(__file__).resolve().parents[3] / "jacked"


def _server_calls():
    """Every uvicorn.Config(...) / uvicorn.run(...) call in the package."""
    for path in sorted(JACKED.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"Config", "run"}
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "uvicorn"
            ):
                yield path.relative_to(JACKED.parent), node


def test_every_server_spreads_the_shared_settings():
    calls = list(_server_calls())
    assert len(calls) >= 3  # tray service, webux, webux --reload
    for path, node in calls:
        spread = [
            kw.value.id for kw in node.keywords
            if kw.arg is None and isinstance(kw.value, ast.Name)
        ]
        assert "SERVER_SETTINGS" in spread, f"{path}:{node.lineno} skips SERVER_SETTINGS"
        overridden = {kw.arg for kw in node.keywords if kw.arg} & set(SERVER_SETTINGS)
        assert not overridden, f"{path}:{node.lineno} overrides {overridden}"


def test_the_settings_resolve_to_the_sans_io_protocol():
    from uvicorn.protocols.websockets.websockets_sansio_impl import (
        WebSocketsSansIOProtocol,
    )

    config = uvicorn.Config("jacked.api.main:app", **SERVER_SETTINGS)
    config.load()
    assert config.ws_protocol_class is WebSocketsSansIOProtocol


def test_the_settings_cannot_be_changed_at_runtime():
    with pytest.raises(TypeError):
        SERVER_SETTINGS["ws"] = "websockets"  # type: ignore[index]


def test_the_dashboard_socket_serves_over_sans_io(_isolate_home, monkeypatch):  # noqa: F811
    """A real server, a real websocket client: the app's broadcasts and the
    keepalive frame arrive, and a foreign Origin is refused at the handshake
    (the origin guard still runs in front of the socket)."""
    import jacked.api.main as main

    monkeypatch.setattr(main, "WS_KEEPALIVE_INTERVAL", 0.2)
    sock, port = _bind_ephemeral_loopback()
    server = uvicorn.Server(
        uvicorn.Config("jacked.api.main:app", log_level="warning", **SERVER_SETTINGS)
    )
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline and not getattr(server, "started", False):
            time.sleep(0.1)
        assert getattr(server, "started", False), "uvicorn did not start"

        url = f"ws://127.0.0.1:{port}/api/ws"
        import logging

        marker = f"sansio-probe-{port}"
        got_ping = got_marker = False
        with ws_client.connect(url, origin=f"http://127.0.0.1:{port}", open_timeout=5) as conn:
            # A log line emitted AFTER the socket is open must come back as an
            # app broadcast (the server log feed), independent of startup logs.
            logging.getLogger("jacked.api.main").warning(marker)
            deadline = time.monotonic() + 10
            while not (got_ping and got_marker) and time.monotonic() < deadline:
                frame = json.loads(conn.recv(timeout=5))
                got_ping |= frame["type"] == "ping"
                got_marker |= frame["type"] == "server_logs_changed" and marker in json.dumps(frame)
        assert got_ping and got_marker

        from websockets.exceptions import InvalidStatus

        with pytest.raises(InvalidStatus) as refused:
            ws_client.connect(url, origin="http://evil.example", open_timeout=5)
        assert refused.value.response.status_code == 403
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        if thread.is_alive():
            server.force_exit = True
            thread.join(timeout=5)
        sock.close()
