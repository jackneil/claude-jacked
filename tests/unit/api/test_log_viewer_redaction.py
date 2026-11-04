"""The dashboard log viewer never carries raw exception text.

Every server log record goes to the in-memory log buffer, and the buffer goes
to every dashboard client: the ``server_logs_changed`` WebSocket event and
``GET /api/logs/server``. With remote access on, those clients can be on a
different machine, and neither surface has a login. The text of an exception
can name a home-directory path, a host, or a fragment of an HTTP response.

The rule under test: the viewer entry replaces each exception that a record
formats into its message with ``describe(exc)`` (the type and the errno
name). The host's log file formats the same record with the full text, so
local diagnosis keeps the detail.
"""

from __future__ import annotations

import asyncio
import errno
import io
import json
import logging

import httpx
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked.api.log_capture import ServerLogBuffer, _format_entry
from jacked.api.websocket import WebSocketRegistry

TOKEN = "sk-ant-oat01-SECRETTOKENFRAGMENT"
PATH = "/Users/someone/.claude/.credentials.json"


def _record(msg, args=(), level=logging.WARNING):
    return logging.LogRecord("jacked.test", level, __file__, 1, msg, args, None)


def _leaky() -> RuntimeError:
    return RuntimeError(f"401 body: {{'error': 'invalid_grant', 'token': '{TOKEN}'}}")


# ---------------------------------------------------------------------------
# The viewer entry
# ---------------------------------------------------------------------------


def test_exception_arg_is_described_not_formatted():
    entry = _format_entry(_record("Refresh failed for account %d: %s", (7, _leaky())))
    assert entry["msg"] == "Refresh failed for account 7: RuntimeError"
    assert TOKEN not in entry["msg"]


def test_os_error_arg_keeps_the_errno_name_only():
    exc = PermissionError(errno.EACCES, "Permission denied", PATH)
    entry = _format_entry(_record("Could not read %s", (exc,)))
    assert entry["msg"] == "Could not read PermissionError(EACCES)"


def test_repr_conversion_is_described_too():
    entry = _format_entry(_record("failed: %r", (_leaky(),)))
    assert entry["msg"] == "failed: RuntimeError"


def test_mapping_args_are_described():
    entry = _format_entry(_record("failed: %(err)s (n=%(n)d)", ({"err": _leaky(), "n": 3},)))
    assert entry["msg"] == "failed: RuntimeError (n=3)"


def test_exceptions_inside_a_list_arg_are_described():
    entry = _format_entry(_record("errors: %s", ([_leaky(), ValueError(PATH)],)))
    assert TOKEN not in entry["msg"]
    assert PATH not in entry["msg"]
    assert "RuntimeError" in entry["msg"] and "ValueError" in entry["msg"]


def test_an_exception_as_the_message_is_described():
    entry = _format_entry(_record(_leaky()))
    assert entry["msg"] == "RuntimeError"


def test_a_message_that_does_not_format_shows_only_the_template():
    entry = _format_entry(_record("bad %d template", (_leaky(),)))
    assert TOKEN not in entry["msg"]
    assert entry["msg"].startswith("bad %d template")


def test_records_without_exceptions_are_unchanged():
    assert _format_entry(_record("val=%d name=%s", (42, "x")))["msg"] == "val=42 name=x"


# ---------------------------------------------------------------------------
# The two surfaces, and the host log file
# ---------------------------------------------------------------------------


class _FakeWebSocket:
    """A dashboard client on another machine, as the registry sees it."""

    def __init__(self):
        self.frames: list[str] = []

    async def send_text(self, text: str) -> None:
        self.frames.append(text)


def _logger_with(buf: ServerLogBuffer, stream: io.StringIO) -> logging.Logger:
    log = logging.getLogger("jacked.test.log_viewer_redaction")
    log.handlers.clear()
    log.propagate = False
    log.setLevel(logging.DEBUG)
    log.addHandler(buf.handler)
    file_like = logging.StreamHandler(stream)
    file_like.setFormatter(
        logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
    )
    log.addHandler(file_like)
    return log


def test_websocket_client_gets_the_redacted_entry_and_the_file_keeps_detail():
    stream = io.StringIO()
    buf = ServerLogBuffer(maxlen=50)
    registry = WebSocketRegistry()
    remote = _FakeWebSocket()

    async def scenario():
        await registry.connect(remote, ["*"])
        buf.set_loop(asyncio.get_running_loop())
        buf.set_registry(registry)
        log = _logger_with(buf, stream)
        try:
            raise _leaky()
        except RuntimeError as exc:
            log.warning("Token refresh failed for account %s: %s", 7, exc)
        await asyncio.sleep(0.4)  # one flush window (FLUSH_INTERVAL_SEC = 0.15)
        buf.detach()

    asyncio.run(scenario())

    assert remote.frames, "the WebSocket client got no server_logs_changed event"
    event = json.loads(remote.frames[-1])
    assert event["type"] == "server_logs_changed"
    messages = [e["msg"] for e in event["payload"]["entries"]]
    assert messages == ["Token refresh failed for account 7: RuntimeError"]
    assert TOKEN not in remote.frames[-1]

    # The host log file keeps the full text for local diagnosis.
    assert TOKEN in stream.getvalue()


def test_remote_http_client_gets_the_redacted_entry():
    import jacked.api.routes.logs as logs_mod

    buf = ServerLogBuffer(maxlen=50)
    buf._handle_record(_record("Profile fetch failed: %s", (_leaky(),)))
    app = FastAPI()
    app.include_router(logs_mod.router, prefix="/api")
    original = logs_mod.server_log_buffer
    logs_mod.server_log_buffer = buf
    try:
        client = TestClient(app, client=("100.101.102.103", 51000))
        body = client.get("/api/logs/server").text
    finally:
        logs_mod.server_log_buffer = original
    assert TOKEN not in body
    assert "Profile fetch failed: RuntimeError" in body


def test_a_namedtuple_arg_does_not_break_the_entry():
    from collections import namedtuple

    Pair = namedtuple("Pair", "a b")
    entry = _format_entry(_record("pair %s", (Pair(_leaky(), 1),)))
    assert TOKEN not in entry["msg"]


# ---------------------------------------------------------------------------
# HTTP and OAuth failures: values-free in every log, the file included
# ---------------------------------------------------------------------------

BODY = json.dumps({"error": "invalid_grant", "error_description": f"code {TOKEN} expired"})


def test_http_failure_keeps_the_status_and_the_error_code_only():
    from jacked.public_errors import http_failure

    assert http_failure(400, BODY) == "HTTP 400 (error=invalid_grant)"
    assert http_failure(400, json.dumps({"error": f"bad {TOKEN}"})) == "HTTP 400"
    assert http_failure(500, f"<html>{TOKEN}</html>") == "HTTP 500"
    assert http_failure(500, "") == "HTTP 500"


def _flow(tmp_path):
    from jacked.web.database import Database
    from jacked.web.oauth import OAuthFlow

    flow = OAuthFlow(Database(str(tmp_path / "oauth.db")), purpose="primary")
    flow._state = "test-state"
    return flow


def _client_answering(status: int, text: str):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=text)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def test_token_exchange_failure_logs_no_body(tmp_path, caplog):
    flow = _flow(tmp_path)

    async def scenario():
        async with _client_answering(400, BODY) as client:
            await flow._exchange_code(client, "the-code")

    caplog.set_level(logging.DEBUG)
    try:
        asyncio.run(scenario())
    except httpx.HTTPStatusError:
        pass
    assert "Token exchange failed: HTTP 400 (error=invalid_grant)" in caplog.text
    assert TOKEN not in caplog.text


def test_api_key_creation_failure_logs_no_body(tmp_path, caplog):
    flow = _flow(tmp_path)

    async def scenario():
        async with _client_answering(403, BODY) as client:
            return await flow._create_api_key(client, "access", {})

    caplog.set_level(logging.DEBUG)
    asyncio.run(scenario())
    assert "API key creation failed: HTTP 403 (error=invalid_grant)" in caplog.text
    assert TOKEN not in caplog.text


def test_profile_and_usage_exceptions_log_the_type_only(tmp_path, caplog):
    flow = _flow(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"refused, token {TOKEN}")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            assert await flow._fetch_profile(client, "access") == {}
            assert await flow._fetch_usage(client, "access") == {}

    caplog.set_level(logging.DEBUG)
    asyncio.run(scenario())
    assert "Profile fetch failed: ConnectError" in caplog.text
    assert "Usage fetch failed: ConnectError" in caplog.text
    assert TOKEN not in caplog.text


def test_codex_app_server_error_carries_the_code_only():
    from jacked.codex.usage import CodexUsageError, _read_result

    line = json.dumps({"id": 2, "error": {"code": -32603, "message": f"401 body {TOKEN}"}})

    class _Proc:
        def __init__(self):
            self.stdout = asyncio.StreamReader()
            self.stdout.feed_data((line + "\n").encode())
            self.stdout.feed_eof()

    async def scenario():
        return await _read_result(_Proc(), 2)

    with pytest.raises(CodexUsageError) as caught:
        asyncio.run(scenario())
    assert str(caught.value) == "codex app-server returned JSON-RPC error -32603"
    assert TOKEN not in str(caught.value)
