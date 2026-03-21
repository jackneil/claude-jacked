"""Integration tests for CLI WebSocket handshake and NDJSON relay.

Uses Starlette TestClient for synchronous WebSocket testing —
no real HTTP server, no real CLI, no tokens burned.
"""

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from starlette.testclient import TestClient

from jacked.chat.protocol import (
    build_control_response,
    build_ndjson_line,
    build_user_message,
    classify_message,
    extract_message_type,
    parse_ndjson_line,
)


def _create_test_app() -> FastAPI:
    """Minimal FastAPI app with just the CLI WebSocket endpoint."""
    app = FastAPI()
    app.state.received_messages = []
    app.state.cli_session_id = None

    @app.websocket("/ws/cli/{session_id}")
    async def ws_cli(websocket: WebSocket, session_id: str):
        await websocket.accept()
        try:
            while True:
                data = await websocket.receive_text()
                msg = parse_ndjson_line(data)
                if msg is None:
                    continue

                app.state.received_messages.append(msg)
                msg_type, subtype = extract_message_type(msg)

                if msg_type == "system" and subtype == "init":
                    app.state.cli_session_id = msg.get("session_id")

                if msg_type == "control_request":
                    req = msg.get("request", {})
                    if req.get("subtype") == "can_use_tool":
                        response = build_control_response(
                            request_id=msg["request_id"],
                            behavior="allow",
                            updated_input=req.get("input", {}),
                        )
                        await websocket.send_text(build_ndjson_line(response))

        except WebSocketDisconnect:
            pass

    return app


def test_cli_connects_and_sends_init():
    """CLI connects to /ws/cli/{id} and sends system/init."""
    app = _create_test_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/cli/test-session-1") as ws:
        init_msg = {
            "type": "system",
            "subtype": "init",
            "cwd": "/mock/project",
            "session_id": "cli-session-abc",
            "tools": ["Bash", "Read", "Write"],
            "model": "claude-sonnet-4-5-20250929",
            "permissionMode": "default",
        }
        ws.send_text(build_ndjson_line(init_msg))

    assert len(app.state.received_messages) == 1
    assert app.state.cli_session_id == "cli-session-abc"


def test_bidirectional_message_flow():
    """Full cycle: init -> user prompt -> assistant response."""
    app = _create_test_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/cli/test-session-2") as ws:
        # CLI sends init
        ws.send_text(build_ndjson_line({
            "type": "system", "subtype": "init",
            "cwd": "/mock", "session_id": "sess-002",
            "tools": ["Bash"], "model": "sonnet", "permissionMode": "default",
        }))

        # Server sends user prompt to CLI
        user_msg = build_user_message("List files", session_id="sess-002")
        ws.send_text(build_ndjson_line(user_msg))

        # CLI sends assistant response
        ws.send_text(build_ndjson_line({
            "type": "assistant",
            "message": {
                "id": "msg_01", "role": "assistant",
                "content": [{"type": "text", "text": "Here are the files..."}],
            },
            "session_id": "sess-002",
        }))

    types = [m["type"] for m in app.state.received_messages]
    assert types == ["system", "user", "assistant"]
    assert app.state.received_messages[2]["message"]["content"][0]["text"] == "Here are the files..."


def test_permission_request_response_cycle():
    """CLI sends control_request, server auto-allows, verify response."""
    app = _create_test_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/cli/test-session-3") as ws:
        ws.send_text(build_ndjson_line({
            "type": "system", "subtype": "init",
            "cwd": "/mock", "session_id": "sess-003",
            "tools": ["Bash"], "model": "sonnet", "permissionMode": "default",
        }))

        # CLI sends permission request
        ws.send_text(build_ndjson_line({
            "type": "control_request",
            "request_id": "req-perm-001",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "git status"},
                "tool_use_id": "tool_01",
            },
        }))

        # Read the auto-allow response from server
        response_raw = ws.receive_text()
        response = parse_ndjson_line(response_raw)

        assert response["type"] == "control_response"
        assert response["response"]["request_id"] == "req-perm-001"
        assert response["response"]["response"]["behavior"] == "allow"
        assert response["response"]["response"]["updatedInput"] == {"command": "git status"}


def test_keepalive_passthrough():
    """keep_alive messages are received and classified as ephemeral."""
    app = _create_test_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/cli/test-session-4") as ws:
        ws.send_text(build_ndjson_line({"type": "keep_alive"}))

    assert len(app.state.received_messages) == 1
    assert classify_message(app.state.received_messages[0]) == "ephemeral"


def test_rapid_sequential_messages():
    """Multiple NDJSON lines sent rapidly are all parsed correctly."""
    app = _create_test_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/cli/test-session-5") as ws:
        for i in range(5):
            ws.send_text(build_ndjson_line({"type": "keep_alive", "seq": i}))

    assert len(app.state.received_messages) == 5
    sequences = [m["seq"] for m in app.state.received_messages]
    assert sequences == [0, 1, 2, 3, 4]


def test_result_message_ends_session():
    """Result message is classified as persistable (session completion)."""
    app = _create_test_app()
    client = TestClient(app)

    with client.websocket_connect("/ws/cli/test-session-6") as ws:
        ws.send_text(build_ndjson_line({
            "type": "system", "subtype": "init",
            "cwd": "/mock", "session_id": "sess-006",
            "tools": ["Bash"], "model": "sonnet", "permissionMode": "default",
        }))
        ws.send_text(build_ndjson_line({
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": "Done",
            "duration_ms": 1200,
            "num_turns": 1,
            "session_id": "sess-006",
        }))

    result_msg = app.state.received_messages[-1]
    assert result_msg["type"] == "result"
    assert result_msg["subtype"] == "success"
    assert classify_message(result_msg) == "persist"
