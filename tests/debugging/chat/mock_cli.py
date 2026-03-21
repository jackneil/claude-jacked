#!/usr/bin/env python3
"""Mock Claude CLI for chat session development.

Connects to a WebSocket URL (simulating --sdk-url) and speaks the
NDJSON protocol with canned responses. No tokens burned.

Usage:
    uv run python tests/debugging/chat/mock_cli.py ws://127.0.0.1:8321/ws/cli/test-session-1
"""

import asyncio
import json
import sys
import uuid
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent.parent))

import websockets  # noqa: E402

from jacked.chat.protocol import build_ndjson_line, parse_ndjson_line  # noqa: E402

SESSION_ID = "mock-session-" + uuid.uuid4().hex[:8]
MSG_COUNTER = 0


def _init_message() -> str:
    return build_ndjson_line({
        "type": "system",
        "subtype": "init",
        "cwd": "/mock/project",
        "session_id": SESSION_ID,
        "tools": ["Bash", "Read", "Write", "Edit", "Glob", "Grep"],
        "model": "claude-sonnet-4-5-20250929",
        "permissionMode": "default",
        "uuid": f"init-{uuid.uuid4().hex[:6]}",
        "claude_code_version": "2.1.0-mock",
    })


def _assistant_response(user_text: str) -> str:
    return build_ndjson_line({
        "type": "assistant",
        "message": {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "role": "assistant",
            "content": [
                {"type": "text", "text": f"Mock response to: {user_text[:80]}"},
            ],
        },
        "session_id": SESSION_ID,
        "uuid": f"resp-{uuid.uuid4().hex[:6]}",
    })


def _permission_request(tool_name: str, command: str) -> str:
    return build_ndjson_line({
        "type": "control_request",
        "request_id": f"req-{uuid.uuid4().hex[:8]}",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "input": {"command": command},
            "tool_use_id": f"tool-{uuid.uuid4().hex[:6]}",
            "decision_reason": "classifier",
        },
    })


def _result_message() -> str:
    return build_ndjson_line({
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": "",
        "duration_ms": 500,
        "num_turns": 1,
        "session_id": SESSION_ID,
        "uuid": f"result-{uuid.uuid4().hex[:6]}",
    })


async def run(url: str):
    print(f"[mock-cli] Connecting to {url}")
    async with websockets.connect(url) as ws:
        # Send system/init
        await ws.send(_init_message())
        print(f"[mock-cli] Connected, sent init (session={SESSION_ID})")

        # Receive loop
        global MSG_COUNTER
        async for raw in ws:
            msg = parse_ndjson_line(raw)
            if msg is None:
                continue

            msg_type = msg.get("type")

            if msg_type == "user":
                MSG_COUNTER += 1
                user_text = msg.get("message", {}).get("content", "")
                print(f"[mock-cli] Received user message #{MSG_COUNTER}: {user_text[:60]}")

                # Every 3rd message, request permission first
                if MSG_COUNTER % 3 == 0:
                    perm = _permission_request("Bash", f"echo '{user_text[:20]}'")
                    await ws.send(perm)
                    print("[mock-cli] Sent permission request, waiting...")

                await asyncio.sleep(0.3)  # simulate thinking
                await ws.send(_assistant_response(user_text))
                await ws.send(_result_message())
                print(f"[mock-cli] Sent response + result")

            elif msg_type == "keep_alive":
                await ws.send(build_ndjson_line({"type": "keep_alive"}))

            elif msg_type == "control_response":
                behavior = msg.get("response", {}).get("response", {}).get("behavior")
                print(f"[mock-cli] Got permission response: {behavior}")

            else:
                print(f"[mock-cli] Unknown message type: {msg_type}")


def main():
    if len(sys.argv) < 2:
        print("Usage: mock_cli.py <ws-url>")
        print("Example: uv run python tests/debugging/chat/mock_cli.py ws://127.0.0.1:8321/ws/cli/test-1")
        sys.exit(1)
    asyncio.run(run(sys.argv[1]))


if __name__ == "__main__":
    main()
