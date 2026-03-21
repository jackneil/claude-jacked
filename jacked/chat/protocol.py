"""NDJSON protocol for Claude CLI's --sdk-url WebSocket interface.

Pure functions, no I/O, no state. Handles parsing, building, and
classifying messages per the protocol reverse-engineered by the
Companion project (github.com/The-Vibe-Company/companion).

>>> parse_ndjson_line('{"type":"keep_alive"}\\n')
{'type': 'keep_alive'}
>>> classify_message({"type": "keep_alive"})
'ephemeral'
"""

import json
import logging

logger = logging.getLogger(__name__)

# Message types that should be persisted to the chat store
_PERSIST_TYPES = {"user", "assistant", "result"}

# Ephemeral types — display-only, never persisted
_EPHEMERAL_TYPES = {
    "stream_event", "keep_alive", "tool_progress",
    "tool_use_summary", "auth_status", "streamlined_text",
    "streamlined_tool_use_summary",
}

# Control flow types — permission requests/responses
_CONTROL_TYPES = {
    "control_request", "control_response", "control_cancel_request",
}


def parse_ndjson_line(line: str) -> dict | None:
    """Parse a single NDJSON line into a dict.

    Returns None for empty lines and malformed JSON (permissive).

    >>> parse_ndjson_line('{"type":"user"}')
    {'type': 'user'}
    >>> parse_ndjson_line('') is None
    True
    >>> parse_ndjson_line('not json') is None
    True
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        logger.debug("Malformed NDJSON: %s", stripped[:100])
        return None


def build_ndjson_line(msg: dict) -> str:
    """Build a compact NDJSON line from a dict.

    Output is compact JSON (no extra whitespace) terminated by newline.

    >>> build_ndjson_line({"type": "keep_alive"})
    '{"type":"keep_alive"}\\n'
    """
    return json.dumps(msg, separators=(",", ":")) + "\n"


def classify_message(msg: dict) -> str:
    """Classify a message for persistence/relay decisions.

    Returns: "persist", "ephemeral", "control", or "unknown".

    >>> classify_message({"type": "assistant"})
    'persist'
    >>> classify_message({"type": "stream_event"})
    'ephemeral'
    >>> classify_message({"type": "control_request"})
    'control'
    """
    msg_type = msg.get("type", "")

    if msg_type in _PERSIST_TYPES:
        return "persist"

    if msg_type == "system":
        # Only system/init is persisted (contains session metadata)
        return "persist" if msg.get("subtype") == "init" else "ephemeral"

    if msg_type in _EPHEMERAL_TYPES:
        return "ephemeral"

    if msg_type in _CONTROL_TYPES:
        return "control"

    return "unknown"


def extract_message_type(msg: dict) -> tuple[str, str | None]:
    """Extract (type, subtype) from a message.

    >>> extract_message_type({"type": "system", "subtype": "init"})
    ('system', 'init')
    >>> extract_message_type({"type": "user"})
    ('user', None)
    >>> extract_message_type({})
    ('unknown', None)
    """
    return msg.get("type", "unknown"), msg.get("subtype")


def build_user_message(
    content: str,
    session_id: str = "",
    parent_tool_use_id: str | None = None,
) -> dict:
    """Build a user prompt message in the CLI's expected format.

    First prompt uses empty session_id. Subsequent prompts use
    the session_id from the CLI's system/init response.

    >>> msg = build_user_message("hello", session_id="abc")
    >>> msg["type"], msg["session_id"]
    ('user', 'abc')
    """
    return {
        "type": "user",
        "message": {"role": "user", "content": content},
        "session_id": session_id,
        "parent_tool_use_id": parent_tool_use_id,
    }


def build_control_response(
    request_id: str,
    behavior: str = "allow",
    updated_input: dict | None = None,
    message: str | None = None,
) -> dict:
    """Build a control_response for a can_use_tool permission request.

    When behavior="allow", updatedInput is required by the protocol
    (even if unchanged from the original request input).

    >>> resp = build_control_response("req-1", "allow", {"command": "ls"})
    >>> resp["response"]["response"]["behavior"]
    'allow'
    """
    inner: dict = {"behavior": behavior}
    if behavior == "allow":
        inner["updatedInput"] = updated_input or {}
    if message is not None:
        inner["message"] = message

    return {
        "type": "control_response",
        "response": {
            "subtype": "success",
            "request_id": request_id,
            "response": inner,
        },
    }
