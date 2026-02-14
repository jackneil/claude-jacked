# Chat Sessions: Run Claude from Inside Jacked Web Dashboard — Design Document

## Status: Design Complete, Ready for Implementation

**Last Updated**: 2025-02-11
**Target Version**: v0.7.0

---

## 1. Executive Summary

Add a full chat UI to the jacked web dashboard that spawns and controls Claude Code sessions directly from the browser. Uses Claude CLI's hidden `--sdk-url` WebSocket flag (reverse-engineered by the [Companion project](https://github.com/The-Vibe-Company/companion)) to bridge browser ↔ FastAPI ↔ Claude CLI.

**Why this exists:**
- Jacked extends Claude Code via hooks/agents/commands but can't **drive** Claude sessions
- No way to run Claude with a specific jacked-managed account/subscription from a UI
- No way to see Claude's tool calls, approve permissions, or interact with AskUserQuestion from a dashboard
- The Companion project proved this is feasible — we're bringing it into jacked's existing infrastructure

**Scope:**
- **Phase 1 (MVP)**: End-to-end message flow — spawn CLI, relay messages, display in browser, approve permissions
- **Phase 2 (Robustness)**: Session resume, browser reconnection, message pagination, queue overflow handling
- **Phase 3 (Polish)**: Streaming tokens, mid-session model/mode changes, interrupt, auto-titles, cost tracking

**Important caveat**: `--sdk-url` is undocumented and could break with CLI updates. Anthropic's official `@anthropic-ai/claude-agent-sdk` may supersede it. The protocol layer sits behind an abstract interface so it can be swapped later.

---

## 2. Architecture Overview

```
Browser (vanilla JS)          FastAPI (Python)              Claude CLI
   localhost:8321      <--WS-->  /ws/browser/{id}?token=xxx
                                 SessionBridge        <--WS-->  /ws/cli/{id}
                                 (relay + persist)              (spawned with --sdk-url)
```

Three-tier WebSocket bridge:
1. **Browser** connects to FastAPI over standard WebSocket (JSON messages)
2. **FastAPI** relays messages bidirectionally, persists to SQLite, handles permissions
3. **Claude CLI** connects as WebSocket client to FastAPI (NDJSON messages) after being spawned with `--sdk-url ws://127.0.0.1:8321/ws/cli/{session_id}`

---

## 3. Files to Create

### Backend — `jacked/chat/` package (new)

| File | ~Lines | Purpose |
|------|--------|---------|
| `__init__.py` | 5 | Package exports |
| `models.py` | ~100 | Pydantic v2: `ChatSession`, `ChatMessage`, `SessionState` enum |
| `protocol.py` | ~150 | NDJSON parse/build per reversed protocol. Pure functions, no I/O. Validates `system/init` version field. |
| `process.py` | ~180 | `CLIProcess` class — spawn/stop/kill Claude CLI on Windows. 3-stage shutdown. |
| `store.py` | ~200 | `SessionStore` — own SQLite DB (`~/.claude/jacked_chat.db`), CRUD for sessions + messages |
| `bridge.py` | ~250 | `SessionBridge` + `BridgeManager` — WebSocket relay between CLI and browser |

### Backend — API route

| File | ~Lines | Purpose |
|------|--------|---------|
| `jacked/api/routes/chat.py` | ~280 | REST endpoints (session CRUD with account selection) + WebSocket endpoints (`/ws/cli/{id}`, `/ws/browser/{id}`) |

### Frontend — `jacked/data/web/`

| File | ~Lines | Purpose |
|------|--------|---------|
| `js/chat-lib.js` | ~200 | `ChatWSClient` class, incremental DOM message renderer, tool call renderer |
| `js/components/chat.js` | ~280 | Chat page: session sidebar + message feed + composer + permission banner |
| `css/chat.css` | ~100 | Chat bubbles, streaming animation, tool cards, permission banner |

### Tests

| File | ~Lines | Purpose |
|------|--------|---------|
| `tests/unit/chat/test_protocol.py` | ~100 | NDJSON parse/build, message classification (doctest + pytest) |
| `tests/unit/chat/test_models.py` | ~60 | Pydantic validation, state transitions |
| `tests/unit/chat/test_store.py` | ~120 | CRUD against `:memory:` SQLite |
| `tests/unit/chat/test_process.py` | ~80 | Subprocess mock, command building |
| `tests/debugging/chat/mock_cli.py` | ~100 | Fake CLI WS client for dev (connects to `--sdk-url`, sends canned responses) |

---

## 4. Files to Modify

| File | Changes |
|------|---------|
| `jacked/api/main.py` | Import + register chat routes, init `SessionStore`/`BridgeManager` in lifespan, orphan cleanup on startup, shutdown cleanup |
| `jacked/data/web/index.html` | Add "Chat" nav link, add `<script>` tags for chat-lib.js + chat.js, add `<link>` for chat.css |
| `jacked/data/web/js/app.js` | Add `'chat'` to `ROUTES` array, add `case 'chat':` to `renderRoute()` switch |

---

## 5. Key Design Decisions

### 5.1 Account Selection

The session creation modal includes an account picker dropdown. Jacked already stores OAuth/API tokens in `~/.claude/jacked.db` (accounts table with `access_token` field holding either `sk-ant-api-...` API keys or short-lived OAuth tokens).

**How it works server-side:**
1. User picks an account (or "Default") in the new session modal
2. `POST /api/chat/sessions` includes `account_id` (optional, null = system default)
3. If `account_id` is set, server reads `access_token` from `db.get_account(account_id)`
4. Token is injected as `ANTHROPIC_API_KEY={access_token}` in the subprocess `env` dict
5. If no account selected, CLI uses its own credentials from `~/.claude/.credentials.json`

**Frontend**: Account dropdown shows email + subscription type badge. Only active, non-expired accounts shown. Tokens NEVER sent to the browser (already excluded from `AccountResponse` in `auth.py`).

**Existing infra we reuse:**
- `Database.get_account()` / `Database.get_default_account()` / `Database.list_accounts()` in `jacked/web/database.py`
- `AccountResponse` model in `jacked/api/routes/auth.py` (strips sensitive fields)

### 5.2 Security

- **Session IDs**: Generated with `secrets.token_urlsafe(32)` — cryptographically random, not guessable from process listing.
- **Browser WS auth**: Session creation returns a one-time `connection_token`. Browser must pass it as `?token=xxx` on WebSocket upgrade. Validated and consumed on first connect.
- **Origin validation**: `/ws/browser/` endpoint validates `Origin` header against `http://127.0.0.1:8321` and `http://localhost:8321`. Rejects cross-origin connections.
- **CLI env filtering**: `Popen` gets an explicit `env` dict with only necessary vars (PATH, HOME, CLAUDECODE, and optionally ANTHROPIC_API_KEY from selected account). No secrets leak through error messages.

### 5.3 Separate Database

Chat sessions use their own `~/.claude/jacked_chat.db` (not shared `jacked.db`). Reasons:
- Avoids write contention with analytics/gatekeeper during fast message streaming
- Independent WAL journal = no interference with dashboard operations
- Can be nuked without affecting account/settings data
- `store.py` creates its own `sqlite3` connection with the same WAL + thread-safe pattern

### 5.4 Protocol Version Safety

On `system/init` message from CLI:
1. Extract protocol version info from the init payload
2. If version is unrecognized, log a warning but attempt to continue (be permissive)
3. If `--sdk-url` flag doesn't exist at all (CLI too old), detect via 30s connection timeout, mark session as `FAILED`, show clear error

At dashboard startup:
- Probe for `claude` binary existence (`shutil.which("claude")`)
- If not found, Chat tab shows "Claude CLI not found" instead of crashing

### 5.5 WebSocket Handling in FastAPI

Two WebSocket endpoints, both in `chat.py`:

- **`/ws/cli/{session_id}`** — Claude CLI connects here after spawn. FastAPI accepts, registers with bridge, enters NDJSON receive loop.
- **`/ws/browser/{session_id}`** — Browser connects here with `?token=xxx`. Validates token, replays last 200 messages on connect, relays live messages.

These are mounted at the **app root** (not under `/api/chat`) because the CLI needs a clean URL for `--sdk-url`.

### 5.6 Process Spawning on Windows

```python
# CLIProcess.start() uses subprocess.Popen with:
cmd = ["claude", "--sdk-url", f"ws://127.0.0.1:8321/ws/cli/{session_id}"]
# Optional: --model, --permission-mode, --resume
subprocess.Popen(cmd, creationflags=CREATE_NEW_PROCESS_GROUP, cwd=project_path, env=filtered_env)
```

Three-stage shutdown:
1. `CTRL_BREAK_EVENT`, wait 5s
2. `process.terminate()`, wait 3s
3. `taskkill /F /T /PID {pid}` to kill entire process tree (children included)

### 5.7 Interactive Features (Permission Banner)

ALL interactive features come as `control_request` with `subtype: "can_use_tool"`. The `tool_name` field determines the UI. The permission banner must render differently per tool:

**AskUserQuestion** (`tool_name: "AskUserQuestion"`):
- Input: `{ questions: [{ header, question, options: [{label, description}], multiSelect }] }`
- Render: Each question with header, radio buttons (or checkboxes if multiSelect), plus "Other..." free-text option
- Response: `behavior: "allow"` with `updatedInput: { questions: [...], answers: { "0": "selected option" } }`
- Auto-submit on single-question single-select forms when an option is clicked

**ExitPlanMode** (`tool_name: "ExitPlanMode"`):
- Input: `{ plan: "## markdown plan...", allowedPrompts: [{ tool: "Bash", prompt: "run tests" }] }`
- Render: Plan as rendered markdown + list of requested permissions
- Response: Standard Allow/Deny

**Bash** (`tool_name: "Bash"`):
- Input: `{ command: "git status", description: "Show working tree status" }`
- Render: Command in monospace with `$` prefix, description below
- Response: Standard Allow/Deny

**Edit** (`tool_name: "Edit"`):
- Input: `{ file_path, old_string, new_string }`
- Render: File path + diff view (red removed block, green added block)
- Response: Standard Allow/Deny

**Write** (`tool_name: "Write"`):
- Input: `{ file_path, content }`
- Render: File path + content preview (truncated at 500 chars)
- Response: Standard Allow/Deny

**Read/Glob/Grep** and others:
- Generic key-value display of tool input
- Response: Standard Allow/Deny

**Permission suggestions**: The CLI may include `permission_suggestions` with the request (e.g., "Allow always for this tool", "Allow for this session"). These render as additional buttons alongside Allow/Deny.

**Non-interactive tool calls** (extracted from `assistant` message content blocks, NOT permission requests):
- `TodoWrite` / `TaskCreate` / `TaskUpdate`: Extracted client-side from `assistant` message `content` blocks. Update a task panel display. No user input needed.
- `tool_use` / `tool_result`: Rendered as collapsible tool cards in the message feed.

### 5.8 Message Persistence Strategy

**DO persist**: `user`, `assistant`, `result`, `control_request`, `control_response`, `system/init` (as session metadata)

**DO NOT persist**: `stream_event`, `keep_alive`, `tool_progress` — these are ephemeral display data. The final `assistant` message contains the complete response.

### 5.9 Session Lifecycle

```
POST /sessions → [CREATED] → spawn CLI → [STARTING] → CLI connects (30s timeout) → [RUNNING]
  ↓ result received: [COMPLETED]
  ↓ CLI crashes: [CRASHED] → POST /resume → [STARTING] (with --resume flag)
  ↓ user stops: [PAUSED] → POST /resume → [STARTING]
  ↓ 30s timeout, no CLI connect: [FAILED] (show error)
  ↓ POST /archive → [ARCHIVED]
```

On server startup: scan for sessions in RUNNING/STARTING state, check PIDs, mark dead ones as CRASHED.

### 5.10 Frontend Pattern

Chat page uses a **hybrid approach** — different from the rest of the dashboard:
- `renderChat()` returns the static shell (sidebar, composer, header) as HTML string
- Message feed uses **incremental DOM** (`createElement`/`appendChild`) not `innerHTML` — streaming messages require appending without destroying scroll position or animation state
- `bindChatEvents()` wires up WebSocket connection, message handlers, send button
- Session list sidebar still uses the `innerHTML` pattern (it's static enough)

### 5.11 Multi-tab Handling

Only one browser WebSocket per session. Second connection attempt gets rejected with a "Session already open in another tab" error. The first tab owns the session.

---

## 6. Frontend Design Spec

### 6.1 Design Direction

**Industrial terminal meets modern IDE.** The existing dashboard is utilitarian slate-dark. The chat page is the only *live* page — streaming state, pulsing indicators, expanding tool cards. It should feel like a command center for Claude, not a chat app.

Stays cohesive with the slate/blue palette. No new fonts, no CDN additions. Just purposeful CSS that makes the chat area feel alive.

### 6.2 Layout Structure

```
+--[existing sidebar w-56]--+--[content area ml-56 p-0 (no padding for chat)]--+
|                            |  +--[session sidebar w-60]--+--[chat area]------+|
|  Accounts                  |  | + New Session            | [header bar]      ||
|  Installations             |  |                          | model | status    ||
|  Settings                  |  | > my-project             |-------------------|
|  Logs                      |  |   * Fix login bug  *     | [message feed]    ||
|  Analytics                 |  |   * Add tests            |                   ||
|  > Chat                    |  |                          | user msg          ||
|                            |  | > other-repo             | assistant msg     ||
|                            |  |   * Refactor auth        | [tool card]       ||
|                            |  |                          | [permission bar]  ||
|                            |  |                          |-------------------|
|                            |  |                          | [composer]        ||
|                            |  +--------------------------+-------------------+|
+----------------------------+--------------------------------------------------+
```

**Key**: When on the `#chat` route, the content area uses `p-0` instead of `p-6` so the chat fills edge-to-edge. This is a special case handled in `renderRoute()`.

### 6.3 Session Sidebar (`#chat-sidebar`, w-60)

- **Header**: "Sessions" label + "+ New" button (blue-500 bg, small)
- **Session items** grouped by project directory:
  - **Group header**: Folder icon + project name (truncated), collapsible
  - **Session row**: Status dot + title + time ago. Active session has `bg-slate-700` highlight + `border-l-2 border-blue-500`
  - Status dots reuse existing `.status-dot` classes: `.valid` (green=running), `.checking` (blue=starting), `.invalid` (orange=crashed), `.disabled` (gray=paused/completed)
- **Empty state**: "No sessions yet" with a big "+ Create Session" button
- **Bottom**: Collapsible, scrollable if many sessions

### 6.4 Chat Header Bar (`#chat-header`)

Fixed at top of chat area. `bg-slate-800 border-b border-slate-700 px-4 py-2.5`

```
[*] Session Title                          [sonnet] [default] [Running *]  [Stop]
    C:\Github\my-project
```

- **Left**: Status dot + editable session title (click to rename). Project path below in `text-xs text-slate-500`
- **Right**: Model badge (reuse `.badge-primary`), permission mode badge (`.badge-muted`), status text with animated dot, Stop button (red, only visible when running)

### 6.5 Message Feed (`#chat-messages`)

`flex-1 overflow-y-auto` container. Messages appended via `createElement`/`appendChild`.

**User messages** (`.chat-msg.chat-msg--user`):
- Right-aligned block, max-width 80%
- `bg-blue-900/40 border border-blue-800/50 rounded-lg px-4 py-3`
- White text, `text-sm`
- Small "You" label + timestamp above in `text-xs text-slate-500`

**Assistant text messages** (`.chat-msg.chat-msg--assistant`):
- Left-aligned block, max-width 90%
- `bg-slate-800 border-l-2 border-blue-500 rounded-r-lg px-4 py-3`
- Slate-200 text, `text-sm`, basic markdown rendering (bold, code, links, lists)
- Small "Claude" label + timestamp + model badge above

**Tool use cards** (`.chat-tool-card`):
- Left-aligned, indented slightly from assistant messages
- Collapsed by default: `bg-slate-850 border border-slate-700 rounded-lg`
- Header row: tool name badge (color-coded by tool type) + collapsible chevron
  - `Bash` = amber badge
  - `Read`/`Write`/`Edit` = green badge
  - `Glob`/`Grep` = cyan badge
  - `Task` = purple badge
  - Default = slate badge
- Expanded: monospace content area with `bg-slate-900 rounded p-3 text-xs font-mono`
- Tool result nested inside: green left-border for success, red for error. Collapsible.

**Permission banner** (`.chat-permission-banner`):
- Full-width banner pinned above the composer (not in message feed)
- `bg-amber-900/30 border border-amber-700/50 rounded-lg p-4`
- Slides in with `animation: slideUp 200ms ease-out`
- **Renders differently per tool_name** (see Section 5.7)
- Permission suggestion buttons (if provided by CLI): "Allow always", "Allow for session" rendered as secondary buttons alongside Allow/Deny

**System/status messages** (`.chat-msg.chat-msg--system`):
- Centered, full-width
- `text-xs text-slate-500` with a horizontal rule style
- e.g., "Session started", "CLI connected", "Session resumed"

**Streaming indicator**:
- Appended to last assistant message container while streaming
- Blinking block cursor with `animation: blink 1s step-end infinite`
- Removed when streaming completes

**Empty chat state**:
- Centered in message feed
- Large muted text: "Send a message to start"
- Or if no session selected: "Select a session or create a new one"

### 6.6 Composer (`#chat-composer`)

`border-t border-slate-700 p-3 bg-slate-800`

```
+------------------------------------------------------+--------+
| Send a message...                                     | Send > |
|                                                       |        |
+------------------------------------------------------+--------+
```

- `<textarea>` with auto-resize (min 1 row, max 8 rows)
- `bg-slate-900 border border-slate-700 rounded-lg px-4 py-3 text-sm text-slate-200`
- Focus: `border-blue-500 ring-1 ring-blue-500/50`
- Send button: `bg-blue-600 hover:bg-blue-500 rounded-lg px-4` with arrow icon
- **Enter** sends (unless Shift held for newline)
- Disabled state when not connected: grayed out, "Connecting..." placeholder

### 6.7 New Session Modal (SweetAlert2)

Triggered by "+ New" button. Uses existing dark SweetAlert2 theme.

```
+------------------------------------------+
|  New Chat Session                        |
|                                          |
|  Account                                 |
|  [user@example.com (Pro) v]              |
|                                          |
|  Project Directory                       |
|  [C:\Github\my-project         ]         |
|                                          |
|  Model                                   |
|  [sonnet v]                              |
|                                          |
|  Permission Mode                         |
|  [default v]                             |
|                                          |
|  Initial Prompt (optional)               |
|  [Fix the login bug...          ]        |
|                                          |
|           [Cancel]  [Create Session]     |
+------------------------------------------+
```

- **Account**: Dropdown populated from `GET /api/auth/accounts` (already exists). Shows email + subscription badge. "System Default" option uses whatever auth Claude CLI already has. Selecting an account injects `ANTHROPIC_API_KEY` server-side.
- Model options: sonnet, opus, haiku
- Permission modes: default, plan, acceptEdits, bypassPermissions
- Project directory: text input (paste path) — no native file picker in browser
- Initial prompt: optional textarea, if provided it's sent immediately on connect

### 6.8 CSS Animations (`chat.css`)

```css
/* Streaming cursor blink */
@keyframes blink { 50% { opacity: 0; } }
.streaming-cursor { animation: blink 1s step-end infinite; color: #3b82f6; }

/* Permission banner slide-up */
@keyframes slideUp {
    from { transform: translateY(100%); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
}
.chat-permission-banner { animation: slideUp 200ms ease-out; }

/* Message entry fade-in */
@keyframes msgIn {
    from { opacity: 0; transform: translateY(8px); }
    to { opacity: 1; transform: translateY(0); }
}
.chat-msg { animation: msgIn 150ms ease-out; }

/* Tool card expand/collapse */
.chat-tool-body {
    overflow: hidden;
    transition: max-height 200ms ease, opacity 150ms ease;
}
.chat-tool-body.collapsed { max-height: 0; opacity: 0; }

/* Thinking/processing pulse on header status */
@keyframes statusPulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.4; }
}
.status-running { animation: statusPulse 2s ease-in-out infinite; color: #22c55e; }
```

### 6.9 chat-lib.js API Surface

```javascript
class ChatWSClient {
    constructor(sessionId, token)
    connect()                              // opens ws://localhost:8321/ws/browser/{id}?token=xxx
    disconnect()
    sendPrompt(text)                       // {type:"prompt", content:text}
    respondPermission(requestId, allow, updatedInput)  // {type:"permission_response", ...}
    interrupt()                            // {type:"interrupt"}

    // Event callbacks (set by chat.js)
    onMessage      = null   // (parsedMsg) => void
    onStatusChange = null   // ("connecting"|"connected"|"disconnected") => void
    onPermission   = null   // (permissionData) => void
    onError        = null   // (errorMsg) => void
}

// DOM rendering — messages (all return HTMLElement, not strings)
function createUserMessage(text, timestamp)
function createAssistantMessage(content, model, timestamp)
function createToolCard(toolName, input, toolUseId)
function appendToolResult(toolCard, content, isError)
function createSystemMessage(text)
function createStreamingCursor()
function renderMarkdownBasic(text)         // bold, code, blocks, links, lists -> HTML string
function getToolBadgeClass(toolName)       // returns tailwind classes for tool-type color

// DOM rendering — permission banner (returns HTMLElement)
function createPermissionBanner(permissionData, onRespond)
// Dispatches to specialized renderers based on tool_name:
function _renderAskUserQuestion(input, onRespond)
function _renderExitPlanMode(input, onRespond)
function _renderBashPermission(input, onRespond)
function _renderEditPermission(input, onRespond)
function _renderWritePermission(input, onRespond)
function _renderGenericPermission(input, onRespond)
```

### 6.10 chat.js Structure

```javascript
// Module state
let chatActiveSessionId = null;
let chatWsClient = null;
let chatSessions = [];
let chatStreamingEl = null;         // reference to current streaming cursor element
let chatAccounts = [];              // loaded from /api/auth/accounts for account picker

function renderChat() { ... }       // returns static shell HTML
function bindChatEvents() { ... }   // wires up sidebar clicks, new session, send, etc.

// Internal helpers
function _loadSessions()             // GET /api/chat/sessions -> update sidebar
function _loadAccounts()             // GET /api/auth/accounts -> populate account picker
function _selectSession(sessionId)   // connect WS, load history, update header
function _onWsMessage(msg)           // route msg to appropriate DOM creator
function _appendMessage(el)          // appendChild to feed, auto-scroll if at bottom
function _showPermissionBanner(data) // render smart banner via createPermissionBanner()
function _hidePermissionBanner()     // remove banner
function _updateHeader(session)      // update model/status/title in header
function _handleNewSession()         // SweetAlert modal with account picker -> POST /api/chat/sessions
function _autoScroll()               // scroll to bottom if user hasn't scrolled up
function _extractTasks(content)      // pull TodoWrite/TaskCreate/TaskUpdate from assistant content blocks
```

### 6.11 Auto-scroll Behavior

Track whether user has scrolled up (reading history). If `scrollTop + clientHeight >= scrollHeight - 50`, auto-scroll on new messages. If user has scrolled up, show a "New messages" pill at the bottom that scrolls to bottom on click.

---

## 7. Phased Implementation

### Phase 1 — MVP (get messages flowing end-to-end)

1. `chat/models.py` — Pydantic models + state enum
2. `chat/protocol.py` — NDJSON parse/build (with tests)
3. `chat/store.py` — SQLite tables + CRUD (with tests, own DB file)
4. `chat/process.py` — CLI subprocess spawn/kill (with tests)
5. `chat/bridge.py` — WebSocket relay + 30s CLI connect timeout
6. `api/routes/chat.py` — REST + WS endpoints with token auth + origin check
7. Modify `api/main.py` — register routes + lifecycle + orphan cleanup on startup
8. `data/web/js/chat-lib.js` — browser WS client with incremental DOM
9. `data/web/js/components/chat.js` — chat UI
10. `data/web/css/chat.css` — styles
11. Modify `index.html` + `app.js` — add Chat route
12. `tests/debugging/chat/mock_cli.py` — fake CLI for dev/testing

### Phase 2 — Robustness

- Session resume (`--resume` with CLI session ID captured from `system/init`)
- Browser WebSocket reconnection with capped history replay (last 200 messages)
- Replay pending permission requests on browser reconnect
- Message pagination for long sessions
- Max queue size per browser connection (drop `stream_event` on overflow)

### Phase 3 — Polish

- Streaming token display (handle `stream_event` messages for live display only, not persisted)
- Model picker / permission mode selector mid-session
- Interrupt button (send `control_request` with `subtype:"interrupt"`)
- Auto-generated session titles from first prompt
- Session grouping by project in sidebar
- Cost/token tracking in header bar
- Keyboard shortcuts (Ctrl+Enter to send)

---

## 8. Verification Plan

### Automated Tests

```
C:/Users/jack/.conda/envs/jacked/python.exe -m pytest tests/unit/chat/ -v
```

- `test_protocol.py`: NDJSON parse/build, message classification, edge cases (empty lines, malformed JSON)
- `test_models.py`: Pydantic validation, state enum values
- `test_store.py`: CRUD against `:memory:` SQLite, message ordering, session state transitions
- `test_process.py`: command building, env filtering (mocked subprocess)

### Manual Integration Tests

1. Start dashboard: `jacked webux`
2. Navigate to `localhost:8321/#chat`
3. Create session with a project path
4. Verify Claude CLI process spawns (check Task Manager)
5. Verify CLI WebSocket connects (server logs)
6. Send a prompt, verify response streams back
7. Trigger a tool call, verify permission banner appears
8. Allow/deny permission, verify flow continues
9. Stop session, verify 3-stage process termination
10. Resume session, verify `--resume` flag passes CLI session ID

### Mock CLI Development Loop

Use `tests/debugging/chat/mock_cli.py` to develop without burning Claude tokens:
```
C:/Users/jack/.conda/envs/jacked/python.exe tests/debugging/chat/mock_cli.py ws://127.0.0.1:8321/ws/cli/{session_id}
```

---

## 9. Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| `--sdk-url` removed in CLI update | Protocol module behind abstract interface; monitor Companion project + Claude Code changelog |
| `--sdk-url` not in user's CLI version | 30s connection timeout -> FAILED state with clear "CLI too old or missing --sdk-url" error |
| Protocol differs from Companion findings | Validate `system/init` version field; be permissive on unknown message types (log + skip) |
| Windows process orphaning | 3-stage shutdown: CTRL_BREAK -> terminate -> taskkill /T |
| Race: browser connects before CLI | Buffer messages in bridge; show "Connecting..." status; 30s timeout |
| SQLite write contention during streaming | Separate `jacked_chat.db`; don't persist `stream_event` messages |
| Large message history | Cap replay to 200 messages; paginate via REST endpoint |
| Cross-origin WebSocket hijack | Origin header validation on `/ws/browser/` endpoint |

---

## 10. Reference: Companion Project

The [Companion project](https://github.com/The-Vibe-Company/companion) by The Vibe Company reverse-engineered the `--sdk-url` protocol. Key files for reference:

- `src/lib/ws-bridge.ts` — WebSocket bridge between browser and CLI
- `src/lib/cli-launcher.ts` — Process spawning with `--sdk-url`
- `src/lib/session-manager.ts` — Session CRUD and lifecycle
- `PROTOCOL.md` — NDJSON message format documentation
- `src/components/chat/` — React chat UI (we're doing vanilla JS but same concepts)

Their implementation uses Bun + Hono (TypeScript). Ours is FastAPI + vanilla JS, but the protocol layer is identical since we're talking to the same Claude CLI.
