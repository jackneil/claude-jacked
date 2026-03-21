# Chat Sessions PoC — Prove the WebSocket + NDJSON Approach Works

## Context

The `design/chat_sessions.md` spec describes a chat UI that spawns and controls Claude Code sessions via the hidden `--sdk-url` WebSocket flag (reverse-engineered by the Companion project). Before building the full ~1,500-line feature, we need to prove the core protocol works: NDJSON parsing, FastAPI WebSocket handshake, bidirectional message relay, and permission request/response cycle.

User said: "first write some tests to see if this approach will even work"

## Files to Create

| # | File | ~Lines | Purpose |
|---|------|--------|---------|
| 1 | `jacked/chat/__init__.py` | 1 | Package init |
| 2 | `jacked/chat/protocol.py` | ~80 | NDJSON parse/build, message classification (pure functions, no I/O) |
| 3 | `tests/unit/chat/__init__.py` | 1 | Test package init |
| 4 | `tests/unit/chat/test_protocol.py` | ~120 | Unit tests for protocol functions |
| 5 | `tests/unit/chat/test_integration.py` | ~100 | Integration tests with inline FastAPI app |
| 6 | `tests/debugging/chat/mock_cli.py` | ~100 | Standalone mock CLI for token-free dev |

## Implementation Order

```
Step 1: jacked/chat/protocol.py          — pure functions, zero deps
Step 2: tests/unit/chat/test_protocol.py  — validates protocol functions
Step 3: tests/unit/chat/test_integration.py — inline FastAPI + WS handshake
Step 4: tests/debugging/chat/mock_cli.py  — standalone mock CLI script
Step 5: Run all tests, verify 0 failures
```

## Step 1: `jacked/chat/protocol.py`

Pure functions, no I/O, no state. Six functions:

- `parse_ndjson_line(line: str) -> dict | None` — JSON parse with empty-line/malformed handling
- `build_ndjson_line(msg: dict) -> str` — compact JSON + `\n` terminator
- `classify_message(msg: dict) -> str` — returns `"persist"` | `"ephemeral"` | `"control"` | `"unknown"`
  - Persist: `user`, `assistant`, `result`, `system` (subtype `init` only)
  - Ephemeral: `stream_event`, `keep_alive`, `tool_progress`, etc.
  - Control: `control_request`, `control_response`
- `extract_message_type(msg: dict) -> tuple[str, str | None]` — `(type, subtype)`
- `build_user_message(content, session_id, parent_tool_use_id=None) -> dict` — build user prompt message
- `build_control_response(request_id, behavior, updated_input=None, message=None) -> dict` — build permission response

## Step 2: `test_protocol.py` (~16 tests)

- Parse: valid JSON, trailing newline, empty line, malformed JSON, nested content blocks
- Build: compact JSON format, roundtrip parse/build
- Classify: persistable types, system/init vs system/other, ephemeral types, control types, unknown types, missing type key
- Build user message: first prompt (empty session_id), subsequent prompt
- Build control response: allow with updatedInput, deny with message, request_id correlation
- Extract: type+subtype, type-only, missing type

## Step 3: `test_integration.py` (~6 tests)

Uses Starlette `TestClient` for synchronous WebSocket testing — no HTTP server, no tokens.

Creates a minimal inline FastAPI app with `/ws/cli/{session_id}` endpoint that:
- Accepts WS connection, enters NDJSON receive loop
- On `system/init`: captures session_id
- On `control_request`: auto-responds with `control_response` (allow)

Tests:
1. CLI connects and sends `system/init` → server receives and extracts session_id
2. Full message cycle: init → user prompt → assistant response (bidirectional)
3. Permission request/response: `control_request` → server auto-allows → verify `request_id` + `updatedInput`
4. Keepalive passthrough and classification
5. Rapid sequential NDJSON messages all parsed correctly
6. Result message classification (persist, session completion signal)

## Step 4: `mock_cli.py`

Standalone script (runnable with `uv run python tests/debugging/chat/mock_cli.py ws://...`):
- Connects to WebSocket URL like `--sdk-url` does
- Sends `system/init` on connect
- Receives `user` messages, responds with canned `assistant` + `result`
- Every 3rd message: sends `control_request` (Bash permission) before responding
- Responds to `keep_alive` with `keep_alive`
- Requires `websockets` package (add to dev deps)

## Dependencies

- Add `websockets>=12.0` to `[dependency-groups] dev` in pyproject.toml (needed by mock_cli.py)
- No new runtime dependencies — protocol.py uses only stdlib `json`

## What This Proves

- NDJSON parsing/building matches the Companion-reversed protocol spec
- FastAPI WebSocket endpoints accept CLI-style client connections
- Bidirectional message relay works (prompt in, response out)
- Permission `control_request`/`control_response` cycle works with `request_id` correlation
- Message classification (persist vs ephemeral) is correct

## What This Defers

- Browser-side WebSocket relay (needs full bridge.py)
- SQLite persistence (needs store.py)
- CLI subprocess spawning (needs process.py)
- Session lifecycle management (needs bridge.py)
- Frontend UI (needs all of the above)

## Verification

```bash
uv run python -m pytest tests/unit/chat/ -v
# Expected: ~22 tests pass, 0 failures

# Manual smoke test (optional, after dashboard has the endpoint):
# uv run python tests/debugging/chat/mock_cli.py ws://127.0.0.1:8321/ws/cli/test-1
```
