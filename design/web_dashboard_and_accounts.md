# Web Dashboard + Account Management — Design Document

## Status: Design Complete, Ready for Implementation

**Last Updated**: 2025-02-07
**Target Version**: v0.4.0

---

## 1. Executive Summary

`jacked webux` spins up a local web dashboard for managing Claude Code accounts, monitoring usage, tracking jacked installations, and viewing analytics on hook/command/agent activity.

**Why this exists:**
- No way to see usage across multiple Claude subscriptions without logging into claude.ai per-account
- No way to manage credential priorities or auto-swap when a subscription hits its limit
- No visibility into what jacked has installed where, or how hooks/commands/agents are performing
- All of this data already exists (or should) — it just needs a UI and a proper database

**Scope:**
- **v1 (Now)**: Account management (OAuth, tokens, usage), jacked installation overview, settings, analytics tables + dashboard
- **Future State (Design Only)**: Rate limit detection, auto-credential swap, usage history graphs

---

## 2. Architecture Overview

```
                         ┌──────────────────────────┐
                         │  Browser (localhost:8321) │
                         │  Static HTML + Vanilla JS │
                         │  Tailwind CSS via CDN     │
                         └──────────┬───────────────┘
                                    │ fetch()
                         ┌──────────▼───────────────┐
                         │  FastAPI + Uvicorn        │
                         │  jacked/api/main.py       │
                         │  [web] optional extra     │
                         └──────┬──────────┬────────┘
                                │          │
                   ┌────────────▼──┐  ┌────▼─────────────────┐
                   │  SQLite DB    │  │  Anthropic APIs       │
                   │  ~/.claude/   │  │  OAuth + Usage +      │
                   │  jacked.db    │  │  Profile              │
                   │  (9 tables)   │  └───────────────────────┘
                   └───────────────┘
```

### Stack

| Layer | Choice | Why |
|-------|--------|-----|
| Frontend | Static HTML + Vanilla JS | Zero build step, no npm/node, instant startup |
| CSS | Tailwind via CDN | No install, dark theme out of the box |
| Backend | FastAPI + Uvicorn | Async, fast, Pydantic v2 models, great DX |
| Database | SQLite (WAL mode) | Already on every machine, queryable, expandable |
| HTTP Client | httpx | Async support, clean API, type hints |
| OAuth Callback | aiohttp | Async callback server (ralphx pattern) |
| Package | `[web]` optional extra | Base install stays at click+dotenv+rich only |

### Key Principle

The database is NOT just for accounts. If we're spinning up SQLite, we capture everything jacked does — hook decisions, command usage, agent spawns, lesson tracking. The web dashboard surfaces all of it. One DB, one dashboard, full observability.

---

## 3. Database Schema

SQLite at `~/.claude/jacked.db` with WAL mode enabled. Nine tables across three concerns.

### 3a. Account Management Tables

```sql
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    display_name TEXT,
    access_token TEXT NOT NULL,
    refresh_token TEXT,                     -- NULL for API key accounts (no refresh)
    expires_at INTEGER NOT NULL,            -- Unix timestamp (seconds)
    scopes TEXT,                            -- JSON array of OAuth scopes
    subscription_type TEXT,                 -- "free", "pro", "max", "enterprise", "team"
    rate_limit_tier TEXT,                   -- e.g. "default_claude_max_20x"
    has_extra_usage BOOLEAN DEFAULT FALSE,  -- from profile API
    priority INTEGER DEFAULT 0,            -- 0 = primary/default, lower = higher priority
    is_active BOOLEAN DEFAULT TRUE,        -- user can manually disable
    is_deleted BOOLEAN DEFAULT FALSE,      -- soft delete
    last_used_at TIMESTAMP,
    -- Usage cache (from Anthropic Usage API)
    cached_usage_5h REAL,                  -- 0-100 percentage
    cached_usage_7d REAL,                  -- 0-100 percentage
    cached_5h_resets_at TEXT,              -- ISO timestamp string
    cached_7d_resets_at TEXT,              -- ISO timestamp string
    usage_cached_at INTEGER,               -- Unix timestamp of last usage fetch
    -- Error tracking
    last_error TEXT,
    last_error_at TIMESTAMP,
    consecutive_failures INTEGER DEFAULT 0, -- >= 3 excludes from fallback
    -- Validation
    last_validated_at INTEGER,              -- Unix timestamp
    validation_status TEXT DEFAULT 'unknown', -- 'unknown'|'valid'|'invalid'|'checking'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

**Important: `is_default` is NOT a column.** It's computed as `priority == 0` in every API response. ralphx learned the hard way that storing both `is_default` and `priority` causes drift. Don't repeat that mistake.

**Important: `is_expired` is NOT a column.** It's computed as `now >= expires_at` at response time. Storing it would be stale within seconds.

```sql
CREATE TABLE IF NOT EXISTS installations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_path TEXT NOT NULL UNIQUE,         -- absolute path to repo
    repo_name TEXT NOT NULL,                -- human-readable name
    jacked_version TEXT,                    -- version installed
    hooks_installed TEXT,                   -- JSON array: ["security_gatekeeper", "pre_pr_lint"]
    rules_installed BOOLEAN DEFAULT FALSE,
    agents_installed TEXT,                  -- JSON array of agent filenames
    commands_installed TEXT,                -- JSON array of command filenames
    guardrails_installed BOOLEAN DEFAULT FALSE,
    last_scanned_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,                    -- JSON-encoded value
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

### 3b. Analytics Tables

```sql
CREATE TABLE IF NOT EXISTS gatekeeper_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,                -- ISO format
    command TEXT,                           -- first 1000 chars, redacted
    decision TEXT NOT NULL,                 -- "ALLOW", "ASK_USER", "DENY"
    method TEXT,                            -- "PERMS", "LOCAL", "API"
    reason TEXT,                            -- why this decision
    elapsed_ms REAL,                        -- hook execution time
    session_id TEXT,                        -- Claude Code session
    repo_path TEXT                          -- which repo
);

CREATE TABLE IF NOT EXISTS command_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    command_name TEXT NOT NULL,             -- "dc", "pr", "learn", "redo", etc.
    timestamp TEXT NOT NULL,
    session_id TEXT,
    success BOOLEAN,
    duration_ms REAL,
    repo_path TEXT
);

CREATE TABLE IF NOT EXISTS agent_invocations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_name TEXT NOT NULL,               -- "git-pr-workflow-manager", etc.
    timestamp TEXT NOT NULL,
    session_id TEXT,
    spawned_by TEXT,                        -- "user", "dc", "pr", etc.
    success BOOLEAN,
    duration_ms REAL,
    tasks_completed INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    repo_path TEXT
);

CREATE TABLE IF NOT EXISTS hook_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hook_type TEXT NOT NULL,                -- "PreToolUse", "PostToolUse", "Stop"
    hook_name TEXT,                         -- "security_gatekeeper", "pre_pr_lint"
    timestamp TEXT NOT NULL,
    session_id TEXT,
    success BOOLEAN,
    duration_ms REAL,
    error_msg TEXT,
    repo_path TEXT
);

CREATE TABLE IF NOT EXISTS lessons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,                  -- the lesson text
    project_id TEXT,                        -- null for global
    failure_count INTEGER DEFAULT 1,        -- 1x, 2x, 3x (triggers graduation at 3)
    status TEXT DEFAULT 'learning',         -- 'learning', 'graduated', 'archived'
    graduation_date TEXT,                   -- when moved to CLAUDE.md
    source_session_id TEXT,
    tags TEXT,                              -- JSON array: ["paths", "git", "security"]
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS version_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    current_version TEXT NOT NULL,
    latest_version TEXT NOT NULL,
    outdated BOOLEAN,
    cache_hit BOOLEAN
);
```

---

## 4. Anthropic API Reference

These are the 5 external API interactions jacked makes. Every URL, header, body field, and response field documented. This was hard to reverse-engineer — do not lose this.

### 4a. OAuth Authorization URL

**Purpose:** Open browser for user to authorize jacked to access their Claude account.

```
GET https://claude.ai/oauth/authorize
```

**Query Parameters:**

| Parameter | Value | Notes |
|-----------|-------|-------|
| `response_type` | `code` | Standard OAuth2 |
| `client_id` | `9d1c250a-e61b-44d9-88ed-5944d1962f5e` | Anthropic's public OAuth client ID |
| `redirect_uri` | `http://localhost:{port}/callback` | Port from 45100-45199 |
| `scope` | `org:create_api_key user:profile user:inference user:sessions:claude_code` | Space-separated |
| `state` | `{random_token}` | `secrets.token_urlsafe(32)` for CSRF |
| `code_challenge` | `{sha256_base64url}` | SHA256 of verifier, base64url no padding |
| `code_challenge_method` | `S256` | Always S256 |
| `code` | `true` | **NON-STANDARD. REQUIRED. Miss this and auth fails silently.** |

**PKCE Generation:**
```python
verifier = secrets.token_urlsafe(32)  # 43 random URL-safe chars
challenge = base64.urlsafe_b64encode(
    hashlib.sha256(verifier.encode()).digest()
).rstrip(b"=").decode()
```

### 4b. Token Exchange

**Purpose:** Exchange authorization code for access + refresh tokens.

```
POST https://platform.claude.com/v1/oauth/token
```

**Headers:**
| Header | Value |
|--------|-------|
| `Content-Type` | `application/json` |
| `anthropic-beta` | `oauth-2025-04-20` |

**Body (JSON, NOT form-encoded — non-standard for OAuth2):**
```json
{
    "grant_type": "authorization_code",
    "code": "{authorization_code}",
    "state": "{state_token}",
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
    "code_verifier": "{pkce_verifier}",
    "redirect_uri": "http://localhost:{port}/callback"
}
```

**Response:**
```json
{
    "access_token": "sk-ant-...",
    "refresh_token": "rt-...",
    "expires_in": 28800,
    "scope": "org:create_api_key user:profile user:inference user:sessions:claude_code",
    "account": {
        "email_address": "user@example.com",
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x"
    }
}
```

**Field Extraction:**
- `email` = `response.account.email_address`
- `subscription_type` = `response.account.subscriptionType`
- `rate_limit_tier` = `response.account.rateLimitTier`
- `scopes` = `response.scope.split(" ")` → stored as JSON array
- `expires_at` = `int(time.time()) + response.expires_in`

### 4c. API Key Creation (Optional)

**Purpose:** Convert short-lived access token to long-lived API key (1 year). Only possible if `org:create_api_key` scope was granted.

```
POST https://api.anthropic.com/api/oauth/claude_cli/create_api_key
```

**Headers:**
| Header | Value |
|--------|-------|
| `Content-Type` | `application/json` |
| `Authorization` | `Bearer {access_token}` |

**Note:** NO `anthropic-beta` header on this endpoint.

**Gate:** Only call if `"org:create_api_key" in scopes`.

**Response:**
```json
{
    "api_key": "sk-ant-api-..."
}
```

**CRITICAL side effects:**
```python
tokens["access_token"] = api_key_data["api_key"]   # Replaces the token!
tokens["expires_in"] = 31536000                      # 1 year
tokens["refresh_token"] = None                       # API keys can't be refreshed
```

This fundamentally changes the account's token lifecycle. After API key creation:
- The `access_token` field holds an API key, not an OAuth token
- `refresh_token` is `None` — refresh is impossible
- The key is valid for 1 year
- To re-auth, user must go through the full OAuth flow again

### 4d. Token Refresh

**Purpose:** Get a new access token using the refresh token before the current one expires.

```
POST https://platform.claude.com/v1/oauth/token
```

**Headers:**
| Header | Value |
|--------|-------|
| `Content-Type` | `application/json` |
| `anthropic-beta` | `oauth-2025-04-20` |

**Body:**
```json
{
    "grant_type": "refresh_token",
    "refresh_token": "{refresh_token}",
    "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
}
```

**Response:** Same shape as token exchange (access_token, refresh_token, expires_in).

**Critical behaviors:**
- **Token rotation:** Response MAY include a NEW `refresh_token`. If it does, you MUST save it. The old refresh token is consumed and will not work again.
  ```python
  new_refresh = response.get("refresh_token", account["refresh_token"])
  ```
- **Skip if no refresh_token:** API key accounts have `refresh_token = None`. Don't attempt refresh.
- **Timing:** Refresh proactively when `now > expires_at - 300` (5 minutes before expiry).

**Error handling:**
| Error | Action |
|-------|--------|
| `{"error": "invalid_grant"}` | Mark account `is_active = False`, set `validation_status = 'invalid'`, prompt user to re-auth |
| HTTP 401/403 | Token revoked. Mark `validation_status = 'invalid'` |
| HTTP 429 | Rate limited. Back off, retry later |
| HTTP 5xx | Transient. Retry with exponential backoff |
| DB update failure after consuming token | **Critical.** Log "Token may be lost!" — the old refresh token is consumed but the new one wasn't saved |

### 4e. Profile API

**Purpose:** Get account metadata (subscription type, rate limit tier, display name). Called after login AND after every token refresh to keep metadata current.

```
GET https://api.anthropic.com/api/oauth/profile
```

**Headers:**
| Header | Value |
|--------|-------|
| `Authorization` | `Bearer {access_token}` |
| `anthropic-beta` | `oauth-2025-04-20` |

**Note:** No `Content-Type` header needed (GET request).

**Response:**
```json
{
    "organization": {
        "organization_type": "claude_max",
        "rate_limit_tier": "default_claude_max_20x",
        "has_extra_usage_enabled": false
    },
    "account": {
        "display_name": "Jack",
        "full_name": "Jack Neil"
    }
}
```

**`organization_type` mapping:**
| API Value | Stored Value |
|-----------|--------------|
| `claude_max` | `max` |
| `claude_pro` | `pro` |
| `claude_enterprise` | `enterprise` |
| `claude_team` | `team` |

**When to call:**
1. After successful OAuth login (token exchange)
2. After every successful token refresh
3. On manual account validation

### 4f. Usage API

**Purpose:** Get 5-hour and 7-day usage utilization percentages.

```
GET https://api.anthropic.com/api/oauth/usage
```

**Headers:**
| Header | Value |
|--------|-------|
| `Authorization` | `Bearer {access_token}` |
| `anthropic-beta` | `oauth-2025-04-20` |

**Note:** No `Content-Type` header.

**Response:**
```json
{
    "five_hour": {
        "utilization": 42.5,
        "resets_at": "2025-02-07T15:30:00Z"
    },
    "seven_day": {
        "utilization": 18.2,
        "resets_at": "2025-02-10T00:00:00Z"
    }
}
```

**Field mapping to DB:**
- `cached_usage_5h` = `response.five_hour.utilization`
- `cached_usage_7d` = `response.seven_day.utilization`
- `cached_5h_resets_at` = `response.five_hour.resets_at`
- `cached_7d_resets_at` = `response.seven_day.resets_at`
- `usage_cached_at` = `int(time.time())`

### Header Matrix

| Endpoint | `anthropic-beta` | `Content-Type` | `Authorization` |
|----------|:-:|:-:|:-:|
| Token Exchange | YES | `application/json` | NO |
| Token Refresh | YES | `application/json` | NO |
| API Key Create | **NO** | `application/json` | `Bearer {token}` |
| Profile | YES | **NO** | `Bearer {token}` |
| Usage | YES | **NO** | `Bearer {token}` |

---

## 5. OAuth PKCE Flow

### Constants

```python
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTH_URL = "https://claude.ai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
API_KEY_URL = "https://api.anthropic.com/api/oauth/claude_cli/create_api_key"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
SCOPES = "org:create_api_key user:profile user:inference user:sessions:claude_code"
OAUTH_BETA_HEADER = "oauth-2025-04-20"
CALLBACK_PORT_RANGE = range(45100, 45200)
```

### Flow Sequence

```
1. Generate PKCE verifier + challenge
2. Generate state token (CSRF)
3. Start callback server on port 45100-45199 (try until available)
4. Build auth URL with all query params (including code=true!)
5. Open browser to auth URL
6. User authorizes → Anthropic redirects to localhost callback
7. Callback receives authorization code
8. POST token exchange → get access_token + refresh_token
9. IF org:create_api_key in scopes → POST create API key (replaces tokens)
10. GET profile → subscription type, rate limit tier, display name
11. GET usage → 5h/7d utilization
12. Store everything in accounts table
13. Return flow_id + result to polling frontend
```

### Callback Server

- Uses `aiohttp` to spin up a temporary HTTP server
- Tries ports 45100 through 45199 sequentially until one is available
- Single endpoint: `GET /callback?code={code}&state={state}`
- Also handles error callback: `GET /callback?error={error}&error_description={desc}`
- Shuts down after receiving the callback (one-shot)

---

## 6. Account Lifecycle & States

### State Diagram

```
                    ┌───────────┐
                    │  OAuth    │
                    │  Add Flow │
                    └─────┬─────┘
                          │ success
                    ┌─────▼─────┐
              ┌─────│   valid   │─────┐
              │     └─────┬─────┘     │
              │           │           │
         user disable   token      validation
              │         expires      fails
              │           │           │
        ┌─────▼─────┐ ┌──▼──┐  ┌────▼────┐
        │ disabled  │ │expired│  │ invalid │
        └─────┬─────┘ └──┬──┘  └────┬────┘
              │           │          │
         user enable   refresh    re-auth
              │         succeeds   succeeds
              │           │          │
              └───────────┴──────────┘
                          │
                    ┌─────▼─────┐
                    │   valid   │
                    └───────────┘

         user deletes any state → soft deleted (is_deleted=true, unrecoverable)
```

### States

| State | How Determined | Visual |
|-------|---------------|--------|
| `valid` | `validation_status='valid'` AND `now < expires_at` AND `is_active=true` | Green dot |
| `checking` | `validation_status='checking'` | Blue pulsing dot + "CHECKING..." badge |
| `invalid` | `validation_status='invalid'` | Orange "TOKEN INVALID" badge |
| `expired` | `now >= expires_at` (computed, not stored) | Yellow/amber indicator |
| `disabled` | `is_active=false` | Grey, "DISABLED" badge |
| `deleted` | `is_deleted=true` | Not displayed |

### Priority System

- `priority` is the SOLE authority for ordering. No `is_default` column.
- `priority = 0` = primary/default account. Computed as `is_default: true` in API response.
- Lower number = higher priority in fallback chain.
- Drag-to-reorder in UI recalculates priority values for all accounts.

### Constraints

- Cannot delete the primary account (priority=0) while other active accounts exist.
- Cannot have two accounts with the same email.
- `consecutive_failures` increments on refresh/API failure, resets to 0 on success.
- `consecutive_failures >= 3` excludes account from fallback selection.

---

## 7. OAuth Add Account — Complete UX Journey

```
  User                    Frontend                   Backend                    Anthropic
   │                        │                          │                          │
   │ click "Add Account"    │                          │                          │
   │───────────────────────>│                          │                          │
   │                        │ POST /api/auth/          │                          │
   │                        │   accounts/add           │                          │
   │                        │─────────────────────────>│                          │
   │                        │                          │ start callback server    │
   │                        │                          │ build auth URL           │
   │                        │ {flow_id}                │                          │
   │                        │<─────────────────────────│                          │
   │                        │                          │ open browser ───────────>│
   │                        │                          │                          │
   │                        │ poll GET /api/auth/      │                          │
   │                        │   flow/{flow_id}         │                          │
   │                        │   every 1 second         │                          │
   │                        │─────────────────────────>│ status: "pending"        │
   │                        │<─────────────────────────│                          │
   │                        │         ...              │                          │
   │ authorize in browser   │                          │                  callback│
   │────────────────────────│──────────────────────────│<─────────────────────────│
   │                        │                          │ token exchange           │
   │                        │                          │─────────────────────────>│
   │                        │                          │<─────────────────────────│
   │                        │                          │ create API key (maybe)   │
   │                        │                          │─────────────────────────>│
   │                        │                          │<─────────────────────────│
   │                        │                          │ fetch profile            │
   │                        │                          │─────────────────────────>│
   │                        │                          │<─────────────────────────│
   │                        │                          │ fetch usage              │
   │                        │                          │─────────────────────────>│
   │                        │                          │<─────────────────────────│
   │                        │                          │ store in DB              │
   │                        │ poll → "completed"       │                          │
   │                        │<─────────────────────────│                          │
   │                        │                          │                          │
   │                        │ reload account list      │                          │
   │                        │ auto-validate            │                          │
   │                        │ show new account         │                          │
   │<───────────────────────│                          │                          │
```

**Flow states:**
| State | Meaning |
|-------|---------|
| `pending` | Waiting for user to authorize in browser |
| `completed` | Auth successful, account stored |
| `error` | Auth failed (user denied, network error, etc.) |
| `not_found` | Flow expired (>2 min timeout) |

**Edge cases:**
- If the email of the new account matches an existing deleted account: undelete and update tokens
- If the email matches an existing active account: update tokens in place, show "Account updated" instead of "Account added"
- Email mismatch warning: if the flow was started expecting one email but a different email was authorized, show yellow warning

---

## 8. API Surface

### Account Management Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `POST` | `/api/auth/accounts/add` | Start OAuth flow, return `{flow_id}` |
| `GET` | `/api/auth/flow/{flow_id}` | Poll OAuth flow status |
| `GET` | `/api/auth/accounts` | List all accounts (active by default, `?include_inactive=true` for all) |
| `PATCH` | `/api/auth/accounts/{id}` | Update `display_name`, `is_active` |
| `DELETE` | `/api/auth/accounts/{id}` | Soft-delete account |
| `POST` | `/api/auth/accounts/reorder` | Reorder priorities. Body: `{order: [id1, id2, id3]}` |
| `POST` | `/api/auth/accounts/{id}/refresh` | Force token refresh |
| `POST` | `/api/auth/accounts/{id}/refresh-usage` | Refresh usage cache for one account |
| `POST` | `/api/auth/accounts/refresh-all-usage` | Refresh usage for all active accounts |
| `POST` | `/api/auth/accounts/{id}/validate` | Validate token works (calls profile API) |

### System Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/health` | Health check. Returns `{status: "ok", db: true}` |
| `GET` | `/api/version` | Current + latest PyPI version (reuses `version_check.py`) |
| `GET` | `/api/installations` | List repos where jacked is installed |
| `GET` | `/api/settings` | All settings as key/value pairs |
| `PUT` | `/api/settings/{key}` | Update a setting |

### Analytics Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/analytics/gatekeeper` | Security decision stats (approval rate, method breakdown) |
| `GET` | `/api/analytics/commands` | Command usage stats (frequency, success rate) |
| `GET` | `/api/analytics/agents` | Agent invocation stats (spawn frequency, duration) |
| `GET` | `/api/analytics/hooks` | Hook health stats (success rate, avg duration) |
| `GET` | `/api/analytics/lessons` | Lesson tracking (active, graduated, failure counts) |

### Error Response Format

```json
{
    "error": {
        "message": "Account not found",
        "code": "NOT_FOUND",
        "detail": "No account with id=42"
    }
}
```

---

## 9. Frontend Structure & UI Patterns

### File Structure

```
jacked/data/web/
  index.html                  -- SPA shell, Tailwind CDN, dark theme
  css/
    style.css                 -- Custom styles (status dots, usage bars, animations)
  js/
    app.js                    -- Router, global state, API client wrapper
    components/
      accounts.js             -- Account list, add/edit/delete/reorder
      usage.js                -- Usage bar components with elapsed-time markers
      installations.js        -- Repo list with installed hooks/rules/agents
      settings.js             -- Settings key/value editor
      analytics.js            -- Gatekeeper stats, command/agent usage charts
      header.js               -- Navigation bar, version indicator, refresh button
  assets/
    favicon.ico
```

### Account Card Layout

```
┌─────────────────────────────────────────────────────────┐
│ ⠇ ● user@example.com                     [Primary]     │
│     Max subscription (20x tier)                          │
│                                                          │
│   5h limit  ████████░░░░░░░░░░  42%    resets 2:30 PM  │
│   7d limit  ███░░░░░░░░░░░░░░░  18%    resets Feb 10   │
│                     ↑                                    │
│              elapsed marker                              │
│                                                          │
│                            [Re-auth]  [Disable]  [🗑]  │
└─────────────────────────────────────────────────────────┘
```

### Usage Bar Colors

| Utilization | Color |
|-------------|-------|
| 0-70% | Green |
| 71-89% | Yellow/amber |
| 90-100% | Red |

Each usage bar includes a white vertical line indicating how far through the time window we are (e.g., 3 hours into a 5-hour window = 60% position). This gives temporal context — "42% used but we're 80% through the window" vs "42% used and we're only 20% in."

### Error Display (Three Levels)

1. **Panel-level banner** (top of accounts section) — critical errors affecting all accounts
2. **Per-account orange badge** — `TOKEN INVALID` for accounts with `validation_status='invalid'`
3. **Per-account red text** — `last_error` displayed below the account metadata

### Empty State

When zero accounts exist, show a centered layout:
- Large icon (key or user)
- "No accounts connected"
- "Connect your Claude account to get started"
- Large "Connect Account" button

### Polling & Auto-Validation

- Frontend polls `GET /api/auth/accounts` every 30 seconds to refresh the list
- On page load, auto-validates all accounts not validated in the last 5 minutes
- During OAuth flow, polls flow status every 1 second with 2-minute timeout
- Delete confirmation: inline "Remove? Yes / Cancel" with 5-second auto-cancel timer

---

## 10. Token Refresh (v1 — Simplified)

No background refresh loop. The frontend drives refresh on-demand.

### When Refresh Happens

1. Frontend loads account list → sees `expires_in_seconds < 300` → calls `POST /api/auth/accounts/{id}/refresh`
2. User clicks "Refresh" button on an account
3. Before any outbound API call (usage, profile, validate), check if token needs refresh first

### Refresh Logic

```python
def should_refresh(account: dict) -> bool:
    if not account.get("refresh_token"):
        return False  # API key account — can't refresh
    return time.time() > account["expires_at"] - 300  # 5 min buffer

def refresh_token(account_id: int, db: Database) -> bool:
    account = db.get_account(account_id)
    if not should_refresh(account):
        return True  # Still valid

    response = httpx.post(TOKEN_URL, json={
        "grant_type": "refresh_token",
        "refresh_token": account["refresh_token"],
        "client_id": CLIENT_ID,
    }, headers={
        "Content-Type": "application/json",
        "anthropic-beta": OAUTH_BETA_HEADER,
    })

    if response.status_code == 200:
        tokens = response.json()
        db.update_account(account_id,
            access_token=tokens["access_token"],
            refresh_token=tokens.get("refresh_token", account["refresh_token"]),  # Token rotation!
            expires_at=int(time.time()) + tokens.get("expires_in", 28800),
            consecutive_failures=0,
        )
        # Also refresh profile metadata
        fetch_and_update_profile(account_id, tokens["access_token"], db)
        return True

    if response.status_code == 400:
        error = response.json()
        if error.get("error") == "invalid_grant":
            db.update_account(account_id,
                is_active=False,
                validation_status="invalid",
                last_error="Refresh token expired or revoked",
                last_error_at=datetime.utcnow().isoformat(),
            )
    return False
```

---

## 11. Windows vs Unix

| Concern | v1 Solution | Future State |
|---------|-------------|--------------|
| `fcntl.flock()` file locking | Not needed — single-process server | Platform abstraction: `msvcrt.locking()` on Windows, `fcntl.flock()` on Unix |
| `os.chmod(path, 0o600)` | Skip on Windows (`os.name == 'nt'`) — Windows uses ACLs not POSIX perms | Same |
| `signal.SIGHUP` | Don't register — doesn't exist on Windows. Use `SIGTERM` + `SIGINT` only | Same |
| `webbrowser.open()` | Always print URL to console as fallback (WSL, SSH, headless) | Same |
| Path separators | Use `pathlib.Path` everywhere — handles both | Same |
| SQLite file permissions | DB is in `~/.claude/` which is user-home. No encryption. | Consider Windows DPAPI encryption |

---

## 12. CLI Integration

### New Command: `jacked webux`

```python
@main.command(name="webux")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8321, type=int, help="Port to bind to")
@click.option("--no-browser", is_flag=True, help="Don't auto-open browser")
def webux(host: str, port: int, no_browser: bool):
    """Start the jacked web dashboard."""
    try:
        import uvicorn
    except ImportError:
        console.print("[red]Error:[/red] webux requires the web extra.")
        console.print('Install it with:')
        console.print('  [bold]pip install "claude-jacked[web]"[/bold]')
        sys.exit(1)

    url = f"http://{host}:{port}"
    console.print(f"[bold]Starting jacked dashboard at {url}[/bold]")

    if not no_browser:
        import webbrowser
        webbrowser.open(url)

    uvicorn.run("jacked.api.main:app", host=host, port=port)
```

### pyproject.toml Changes

```toml
[project.optional-dependencies]
web = [
    "fastapi>=0.100",
    "uvicorn[standard]>=0.20",
    "httpx>=0.24",
    "aiohttp>=3.9.0",
]
all = [
    "qdrant-client>=1.7.0",
    "anthropic>=0.40.0",
    "fastapi>=0.100",
    "uvicorn[standard]>=0.20",
    "httpx>=0.24",
    "aiohttp>=3.9.0",
]
```

### Install Integration

After `jacked install`, if `[web]` extra is detected:
```
Installed: agents, commands, rules, hooks
Tip: Run 'jacked webux' to open the dashboard
```

---

## 13. Future State — Rate Limit Detection + Auto-Credential Swap

**This section is design only. Not implemented in v1.**

### Rate Limit Detection

**Pattern matching (from ralphx):**
```python
RATE_LIMIT_PATTERNS = ["429", "rate limit", "overloaded", "rate_limit_error", "too many requests"]
```

**4-layer detection:**
1. JSONL API error messages in session output
2. Session file not found — check stderr
3. Final result JSON — check stdout
4. CLI exit code + stderr

**Proactive detection via usage polling:**
- Dashboard auto-refreshes usage every 60 seconds when any account's 5h utilization > 80%
- Alert banner when utilization > 90%

### Auto-Credential Swap

**Context manager pattern:**
```
1. Acquire credential lock (file lock)
2. Backup ~/.claude/.credentials.json
3. Write target account's credentials
4. Yield to caller
5. Capture any token refresh by Claude CLI (compare before/after)
6. Restore original credentials from backup
7. Clean up backup
8. Release lock
```

**Credentials JSON format (what Claude Code reads):**
```json
{
    "claudeAiOauth": {
        "accessToken": "sk-ant-...",
        "refreshToken": "rt-...",
        "expiresAt": 1707350400000,
        "scopes": ["user:inference", "user:profile", "user:sessions:claude_code"],
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x"
    }
}
```

**Note:** `expiresAt` is in MILLISECONDS (multiply by 1000). Everything else in jacked uses seconds.

**Fallback selection algorithm:**
```sql
SELECT * FROM accounts
WHERE is_active = 1
  AND is_deleted = 0
  AND consecutive_failures < 3
  AND id NOT IN ({failed_account_ids})
ORDER BY
    priority ASC,                    -- 1st: explicit priority ordering
    COALESCE(cached_usage_5h, 0) ASC,  -- 2nd: lowest 5h usage
    COALESCE(cached_usage_7d, 0) ASC,  -- 3rd: lowest 7d usage
    consecutive_failures ASC,         -- 4th: fewest failures
    created_at ASC                   -- 5th: oldest account (tiebreaker)
LIMIT 1
```

**Safety net constants:**
- Max 2 fallback attempts per rate limit event
- 2-second cooldown between swap attempts
- `atexit.register(emergency_restore)` for crash recovery
- Signal handlers for `SIGTERM`, `SIGINT` (no `SIGHUP` on Windows)
- Orphaned backup check on startup: if `.credentials.backup.json` exists, restore it

**File paths:**
| File | Purpose |
|------|---------|
| `~/.claude/.credentials.json` | Active credentials (read by Claude Code) |
| `~/.claude/.credentials.backup.json` | Backup during swap |
| `~/.claude/.credentials.lock` | File lock for cross-process safety |

---

## 14. Testing Strategy

### Test Files

| File | Scope | Est. Tests |
|------|-------|------------|
| `tests/unit/test_web_database.py` | Schema creation, CRUD for all 9 tables, WAL mode, migration | ~25 |
| `tests/unit/test_web_oauth.py` | PKCE generation, token exchange mocking, API key path, refresh token rotation | ~20 |
| `tests/unit/test_web_auth.py` | Token refresh, invalid_grant, usage cache, profile fetch, fallback ordering | ~20 |
| `tests/unit/test_web_api.py` | FastAPI TestClient for all endpoints, error cases | ~30 |
| `tests/unit/test_web_analytics.py` | Analytics endpoints, gatekeeper stats, command/agent aggregation | ~15 |
| `tests/unit/test_web_frontend.py` | Static file serving, SPA catch-all, CORS | ~10 |

**Total estimate: ~120 unit tests**

### Testing Patterns

- SQLite tests use `:memory:` database or `tmp_path` fixture
- HTTP calls mocked with `unittest.mock.patch` on httpx
- FastAPI tests use `TestClient` (no real server needed)
- Skip tests requiring network with `@pytest.mark.skipif` when credentials missing
- Every public function gets at least a doctest
- Pydantic v2 models for all request/response schemas (per CLAUDE.md rules)

---

## Files to Create/Modify

### New Files

| File | Purpose |
|------|---------|
| `jacked/web/__init__.py` | Web module init |
| `jacked/web/database.py` | SQLite schema + CRUD for all 9 tables |
| `jacked/web/oauth.py` | PKCE OAuth flow (adapted from ralphx) |
| `jacked/web/auth.py` | Token refresh, usage fetch, profile fetch |
| `jacked/api/__init__.py` | API module init |
| `jacked/api/main.py` | FastAPI app, static file serving, CORS, SPA catch-all |
| `jacked/api/routes/__init__.py` | Routes init |
| `jacked/api/routes/auth.py` | Account management endpoints |
| `jacked/api/routes/system.py` | Version, installations, settings endpoints |
| `jacked/api/routes/analytics.py` | Analytics/stats endpoints |
| `jacked/data/web/index.html` | SPA shell with Tailwind CDN |
| `jacked/data/web/css/style.css` | Custom dark theme styles |
| `jacked/data/web/js/app.js` | Frontend router, state, API client |
| `jacked/data/web/js/components/accounts.js` | Account management UI |
| `jacked/data/web/js/components/usage.js` | Usage bar components |
| `jacked/data/web/js/components/installations.js` | Installation list UI |
| `jacked/data/web/js/components/settings.js` | Settings editor UI |
| `jacked/data/web/js/components/analytics.js` | Analytics dashboard UI |
| `jacked/data/web/js/components/header.js` | Navigation + version |
| `tests/unit/test_web_database.py` | Database tests |
| `tests/unit/test_web_oauth.py` | OAuth tests |
| `tests/unit/test_web_auth.py` | Auth/refresh tests |
| `tests/unit/test_web_api.py` | API endpoint tests |
| `tests/unit/test_web_analytics.py` | Analytics tests |

### Modified Files

| File | Change |
|------|--------|
| `jacked/cli.py` | Add `webux` command |
| `pyproject.toml` | Add `[web]` optional deps, update `[all]` |
| `jacked/data/hooks/security_gatekeeper.py` | Add SQLite logging of decisions |
| `jacked/__init__.py` | Version bump |

---

## Design Decisions

### Why static HTML+JS over React?
Zero build step. `pip install "claude-jacked[web]"` then `jacked webux` and it works. No npm, no node, no webpack, no vite, no 200MB node_modules. The dashboard is a settings panel, not a SPA that needs a framework.

### Why SQLite for everything?
It's already on every machine. One file, one connection, queryable with SQL. Trying to store gatekeeper decisions in a log file then parse them later is stupid. Trying to track command usage in JSON is stupid. SQLite is free observability.

### Why the same CLIENT_ID as ralphx?
`9d1c250a-e61b-44d9-88ed-5944d1962f5e` is Anthropic's public OAuth client ID for CLI tools. It's not a secret. Both ralphx and jacked are CLI tools accessing the same Anthropic OAuth infrastructure.

### Why no `is_default` column?
ralphx had both `is_default` (boolean) and `priority` (integer). They drifted. Migration v12 fixed it by making `priority` the sole authority. `is_default` is now computed as `priority == 0`. Learn from their pain.

### Why 9 tables instead of 3?
If you're spinning up a database, use it. The marginal cost of additional tables is zero. The marginal value of having queryable gatekeeper decisions, command usage stats, and lesson tracking is enormous. Don't half-ass a database.

### Why no background refresh tasks in v1?
Simplicity. The frontend polls every 30 seconds and triggers refresh on-demand. No background threads, no asyncio event loops, no task scheduling. When we add auto-credential-swap in future state, THEN we add background tasks.

### Why port 8321?
High port (no root required), unlikely to collide with common dev tools. Memorable enough. `--port` flag for customization.

### Why `aiohttp` for the OAuth callback server?
The callback server needs to be async (it's waiting for a browser redirect while the main server continues running). ralphx uses `aiohttp` for this and it works. Could potentially use a temporary FastAPI route instead, but that adds complexity to the main app's lifecycle.
