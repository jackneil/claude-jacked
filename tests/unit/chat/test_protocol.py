"""Unit tests for the NDJSON protocol parser/builder.

Validates parse/build roundtrip, message classification,
and builder functions against the reversed Companion protocol spec.
"""

import json

from jacked.chat.protocol import (
    build_control_response,
    build_ndjson_line,
    build_user_message,
    classify_message,
    extract_message_type,
    parse_ndjson_line,
)


# --- parse_ndjson_line ---


def test_parse_valid_json():
    result = parse_ndjson_line('{"type":"user","message":{"role":"user","content":"hello"}}')
    assert result["type"] == "user"
    assert result["message"]["content"] == "hello"


def test_parse_with_trailing_newline():
    result = parse_ndjson_line('{"type":"keep_alive"}\n')
    assert result == {"type": "keep_alive"}


def test_parse_empty_line_returns_none():
    assert parse_ndjson_line("") is None
    assert parse_ndjson_line("  \n") is None


def test_parse_malformed_json_returns_none():
    assert parse_ndjson_line("{not valid json}") is None
    assert parse_ndjson_line("just text") is None


def test_parse_nested_content_blocks():
    """Assistant messages have complex nested content arrays."""
    line = json.dumps({
        "type": "assistant",
        "message": {
            "id": "msg_01",
            "role": "assistant",
            "content": [
                {"type": "text", "text": "I'll list files..."},
                {"type": "tool_use", "id": "tool_01", "name": "Bash",
                 "input": {"command": "ls"}},
            ],
        },
        "session_id": "abc-123",
    })
    result = parse_ndjson_line(line)
    assert len(result["message"]["content"]) == 2
    assert result["message"]["content"][1]["name"] == "Bash"


# --- build_ndjson_line ---


def test_build_produces_compact_json_with_newline():
    result = build_ndjson_line({"type": "keep_alive"})
    assert result == '{"type":"keep_alive"}\n'


def test_build_roundtrip():
    original = {"type": "user", "message": {"role": "user", "content": "test"}}
    line = build_ndjson_line(original)
    parsed = parse_ndjson_line(line)
    assert parsed == original


# --- classify_message ---


def test_classify_persistable_types():
    assert classify_message({"type": "user"}) == "persist"
    assert classify_message({"type": "assistant"}) == "persist"
    assert classify_message({"type": "result"}) == "persist"


def test_classify_system_init_persistable():
    assert classify_message({"type": "system", "subtype": "init"}) == "persist"


def test_classify_system_non_init_ephemeral():
    assert classify_message({"type": "system", "subtype": "status"}) == "ephemeral"
    assert classify_message({"type": "system", "subtype": "hook_started"}) == "ephemeral"


def test_classify_ephemeral_types():
    assert classify_message({"type": "stream_event"}) == "ephemeral"
    assert classify_message({"type": "keep_alive"}) == "ephemeral"
    assert classify_message({"type": "tool_progress"}) == "ephemeral"


def test_classify_control_types():
    assert classify_message({"type": "control_request"}) == "control"
    assert classify_message({"type": "control_response"}) == "control"


def test_classify_unknown_type():
    assert classify_message({"type": "some_future_type"}) == "unknown"


def test_classify_missing_type_key():
    assert classify_message({"data": "no type field"}) == "unknown"


# --- build_user_message ---


def test_build_user_message_first_prompt():
    msg = build_user_message("Hello Claude", session_id="")
    assert msg["type"] == "user"
    assert msg["session_id"] == ""
    assert msg["message"]["role"] == "user"
    assert msg["message"]["content"] == "Hello Claude"
    assert msg["parent_tool_use_id"] is None


def test_build_user_message_subsequent():
    msg = build_user_message("Follow up", session_id="abc-123")
    assert msg["session_id"] == "abc-123"


# --- build_control_response ---


def test_build_allow_response():
    resp = build_control_response("req-1", behavior="allow", updated_input={"command": "ls"})
    assert resp["type"] == "control_response"
    inner = resp["response"]["response"]
    assert inner["behavior"] == "allow"
    assert inner["updatedInput"] == {"command": "ls"}


def test_build_deny_response():
    resp = build_control_response("req-1", behavior="deny", message="Not allowed")
    inner = resp["response"]["response"]
    assert inner["behavior"] == "deny"
    assert inner["message"] == "Not allowed"


def test_control_response_has_request_id():
    resp = build_control_response("my-req-123", behavior="allow", updated_input={})
    assert resp["response"]["request_id"] == "my-req-123"


# --- extract_message_type ---


def test_extract_type_and_subtype():
    assert extract_message_type({"type": "system", "subtype": "init"}) == ("system", "init")


def test_extract_type_only():
    assert extract_message_type({"type": "user"}) == ("user", None)


def test_extract_missing_type():
    assert extract_message_type({}) == ("unknown", None)
