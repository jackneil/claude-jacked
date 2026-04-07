# Usage Analytics — Live Token Consumption Monitor

**Date:** 2026-04-07
**Status:** Draft
**Scope:** JSONL parsing, anomaly detection, live monitoring, analytics dashboard

## Problem Statement

Claude Code users have no visibility into what's consuming their usage quota. Sessions can silently burn tokens through cache bugs, subagent explosions, context bloat, and resume cost spikes. Users only discover the problem when they hit rate limits. Existing tools (ccusage, cchubber) require manual invocation and produce static reports. Jacked should surface this data live, flag anomalies automatically, and show trends over time — all integrated into the existing dashboard.

## Goals

1. **Parse Claude Code's local JSONL conversation files** into a queryable format
2. **Detect anomalies** — cache health drops, cost outlier sessions, subagent explosion, context bloat, resume spikes, inactive burn
3. **Live monitoring** — watch active sessions and push updates via WebSocket
4. **Trend visibility** — daily/weekly/monthly token consumption and cost trends
5. **Cross-platform** — macOS, Linux, Windows with identical behavior

## Non-Goals

- Recommending cheaper models or lower effort levels (user chose Opus deliberately)
- Replacing ccusage or cchubber (complementary, not competing)
- Modifying Claude Code's behavior (read-only observer)
- Real-time modification of sessions or tokens

## Design

### 1. Data Pipeline

Three-layer architecture:

**Layer 1: JSONL Scanner** (background, async)
- Walks all Claude Code project directories for `.jsonl` files + `*/subagents/*.jsonl`
- Deduplicates by `message.id` (~48% of lines are duplicates from session resume)
- Computes estimated cost from token counts using LiteLLM pricing:
  - Opus input: $15/M, output: $75/M, cache read: $1.875/M, cache create: $18.75/M
  - Sonnet input: $3/M, output: $15/M, cache read: $0.30/M, cache create: $3.75/M
  - Haiku input: $0.80/M, output: $4/M, cache read: $0.08/M, cache create: $1/M
- Writes parsed messages to `usage_analytics.db`
- Tracks per-file byte offset + mtime for incremental updates (only new bytes parsed on subsequent runs)
- **Handles JSONL rewrites:** Claude Code rewrites JSONL files on session resume (replays conversation prefix with updated cache counts). If stored byte offset > current file size, reset offset to 0 and re-scan the file. Dedup by message.id prevents duplicate inserts.
- **Runs in thread pool:** All file I/O and JSON parsing runs via `asyncio.to_thread()` to avoid blocking the event loop. The scanner yields control between projects with `await asyncio.sleep(0)`.
- Starts automatically on server startup, runs in background asyncio task
- **Scan state garbage collection:** On each full scan, prune `scan_state` entries where the file no longer exists on disk. Prevents accumulated stale entries from slowing startup over months of use.

**Layer 2: Anomaly Detector** (runs after each scan batch)
- Compares session metrics against rolling 7-day averages
- Generates flags for anomalous sessions
- Writes flags to DB; auto-resolves when condition clears

**Layer 3: Live Monitor** (WebSocket-driven)
- Polls active session JSONL files via `pathlib.Path.stat()` for size changes
- Poll interval: 5 seconds normally, 1 second when analytics tab has active WebSocket subscribers
- Pushes new messages + flag updates via existing `WebSocketRegistry`

**Cross-platform data paths:**
- macOS/Linux: `~/.claude/projects/`
- Windows: `%USERPROFILE%\.claude\projects\`
- XDG fallback (Linux): `~/.config/claude/projects/` (checked if primary is empty)
- Resolved via `get_claude_data_dirs()` helper returning list of valid Path objects

**Initial scan progress via WebSocket:**
```python
await ws_registry.broadcast("analytics_scan_progress", {
    "status": "scanning",       # scanning | complete | error
    "projects_scanned": 12,
    "projects_total": 96,
    "sessions_found": 4200,
    "messages_parsed": 128000,
    "current_project": "claude-jacked",
})
```

Starts parsing immediately on server startup. Dashboard shows partial results as projects complete. No user action required.

**Incremental updates:** Each JSONL file's last-read byte position stored in `scan_state` table. On subsequent startups, mtime checked first — unchanged files skipped entirely. Only new bytes from modified files are parsed. This makes startup after the initial scan near-instant.

### 2. Database Schema

Separate `usage_analytics.db` file — isolated from jacked's core DB to avoid lock contention and bloat. Located alongside the main DB.

```sql
-- Per-message granular data (retention configurable, default: keep all)
CREATE TABLE messages (
    id TEXT PRIMARY KEY,              -- message.id from JSONL (dedup key)
    session_id TEXT NOT NULL,
    project_hash TEXT NOT NULL,       -- encoded project directory name
    timestamp TEXT NOT NULL,          -- ISO-8601
    model TEXT,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_create_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    is_subagent INTEGER DEFAULT 0     -- 1 if from subagents/ directory
);
CREATE INDEX idx_messages_ts ON messages(timestamp);
CREATE INDEX idx_messages_session ON messages(session_id);
CREATE INDEX idx_messages_project ON messages(project_hash);

-- Daily rollups (kept indefinitely for long-term trends)
CREATE TABLE daily_summaries (
    date TEXT NOT NULL,                -- YYYY-MM-DD
    project_hash TEXT NOT NULL,
    model TEXT NOT NULL,
    total_messages INTEGER DEFAULT 0,
    total_sessions INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    cache_read_tokens INTEGER DEFAULT 0,
    cache_create_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0,
    cache_hit_ratio REAL,
    PRIMARY KEY (date, project_hash, model)
);

-- Anomaly flags
CREATE TABLE flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    flag_type TEXT NOT NULL,           -- cache_drop, cost_outlier, subagent_explosion,
                                      -- context_bloat, resume_spike, inactive_burn
    severity TEXT NOT NULL,            -- warning, critical
    session_id TEXT,
    project_hash TEXT,
    message TEXT NOT NULL,             -- human-readable one-liner
    detail TEXT,                       -- JSON with specifics
    resolved_at TEXT                   -- NULL = active
);
CREATE INDEX idx_flags_active ON flags(resolved_at) WHERE resolved_at IS NULL;

-- File scan state (incremental parsing)
CREATE TABLE scan_state (
    file_path TEXT PRIMARY KEY,
    last_byte_offset INTEGER DEFAULT 0,
    last_mtime REAL,
    messages_count INTEGER DEFAULT 0
);

-- User settings
CREATE TABLE analytics_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Default: purge_days = NULL (keep all). User can set via UI.
```

**Purge behavior:** Configurable via UI setting "Auto-purge message data older than N days" (default: off — keep everything). When set, purge runs after each scan cycle. Before purging, un-summarized days are rolled up into `daily_summaries`. Daily summaries are never purged.

**Rollup watermark:** `analytics_settings` stores `last_rollup_date` (YYYY-MM-DD). Rollup only processes days after this date. After successful rollup, the watermark advances. This prevents double-counting if the process crashes mid-rollup. On first run, `last_rollup_date` is NULL and all days are rolled up.

### 3. Anomaly Detection

**V1: Three flag types** (most impactful, lowest false-positive risk). Additional types added in follow-up after user feedback.

| Flag Type | Detection | Warning Threshold | Critical Threshold |
|-----------|-----------|-------------------|-------------------|
| `cache_drop` | Session cache hit ratio vs 7-day session average. **Excludes first 5 messages** of each session (cache warmup). Only fires for sessions with 10+ messages. | < 70% (when avg is 90%+) | < 30% |
| `cost_outlier` | Session estimated cost vs 7-day per-session average. Only fires for sessions with 5+ messages and 5+ minutes duration (avoids flagging quick tests). | > 3× average | > 6× average |
| `subagent_explosion` | Count of subagent messages (is_subagent=1) per session | > 20 subagent messages | > 50 subagent messages |

**Deferred to v2** (need user feedback on false-positive rates first):
- `context_bloat` — input tokens growth detection
- `resume_spike` — first-message cache miss detection
- `inactive_burn` — output without user input (too many false positives with subagent workflows)

**Flag descriptions are actionable and specific:**
- "hank-rcm session 4a2f: 312K output in 22 min — 3.2× your average"
- "claude-jacked session 8b1c: cache hit rate dropped to 17% (your average is 94%) — possible cache bug"
- "hank-os session 3f9a: 42 subagent calls — each duplicates full context"
- "Session e2d1: context growing 180% across messages — consider /compact"
- "Session 7c3b: resume cost spike — 480K cache_create tokens, 0 cache_read"

**No model routing recommendations.** Flags focus exclusively on anomalies the user didn't choose.

**Auto-resolution:** Flags for completed sessions auto-resolve 1 hour after the last message in that session. Active session flags are re-evaluated each scan cycle — if the condition has cleared (e.g., cache recovered), `resolved_at` is set. Resolved flags drop off the active list but remain in DB for history.

**Dismiss/snooze:** Users can dismiss individual flags (sets `resolved_at`) or snooze a flag type for 24 hours (stored in `analytics_settings` as `snooze_{flag_type}_until`). Snoozed types are excluded from detection.

### 4. Frontend — Analytics Page

Replaces the existing empty analytics route with three sub-tabs. Uses Chart.js (already in `jacked/data/web/js/vendor/chart.umd.min.js`).

**Tab 1: Overview (diagnosis-first)**
- **Health banner:** Cache hit rate grade (A ≥ 90%, B ≥ 80%, C ≥ 70%, D ≥ 50%, F < 50%). Today's total tokens + estimated cost. Trend arrow vs yesterday (↑12% / ↓8% / →).
- **Active flags:** Ranked by severity then recency. Each flag: severity dot (yellow/red), project + session link, one-line description, timestamp. Empty state: "No anomalies detected — usage looks healthy."
- **Project breakdown:** Table ranked by token consumption. Columns: project name (decoded from hash), tokens, estimated cost, cache hit %, session count, mini progress bar for relative consumption. Clickable → drills to Sessions tab filtered by project.
- **7-day sparkline:** Small line chart at bottom showing daily token totals. Context, not the focus.
- **Live pulse:** Green animated dot when the live monitor is active and receiving data.

**Tab 2: Sessions**
- Session list ranked by estimated cost (most expensive first).
- Columns: status dot (red/yellow/green), project, session ID (truncated), duration, tokens, cost, cache hit %, model, flag count.
- Click to expand: message-level timeline showing per-message token count, cache ratio, subagent indicators.
- Filters: project dropdown, date range picker, "flagged only" toggle.

**Tab 3: Trends**
- Daily stacked bar chart (tokens per day, stacked by project). Uses `daily_summaries` table.
- Cache health line overlay: daily cache hit ratio as a line on secondary y-axis.
- Time range selector: 7d / 30d / 90d / all.
- Toggle: token view ↔ cost view.

**Settings panel** (gear icon on analytics page):
- Auto-purge: Off / 30 / 60 / 90 / custom days
- Scan info: last scan time, DB size, total messages, total sessions

**Initial scan UX:** Progress banner at top of analytics page: "Scanning your Claude Code history... Project 12/96 — 4,200 sessions found". Partial results appear below as projects complete. Banner dismisses when scan finishes.

### 5. Live Monitoring

**Polling mechanism:**

```python
async def _analytics_live_monitor(app):
    active_files: dict[str, int] = {}  # path → last_byte_offset

    while True:
        has_viewers = ws_registry.has_subscribers("analytics")
        interval = 1.0 if has_viewers else 5.0

        for jsonl_path in find_active_jsonl_files():
            size = jsonl_path.stat().st_size
            last_offset = active_files.get(str(jsonl_path), 0)

            if size > last_offset:
                new_messages = parse_from_offset(jsonl_path, last_offset)
                active_files[str(jsonl_path)] = size

                if new_messages:
                    analytics_db.insert_messages(new_messages)
                    new_flags = detect_anomalies(new_messages)
                    await ws_registry.broadcast("analytics_live_update", {
                        "messages": [...],
                        "session_totals": {...},
                        "today_totals": {...},
                    })
                    for flag in new_flags:
                        await ws_registry.broadcast("analytics_flag_raised", {...})

        await asyncio.sleep(interval)
```

`find_active_jsonl_files()` uses `Path.stat().st_mtime` to find files modified in the last 10 minutes. Cross-platform — `pathlib` works identically on macOS, Linux, Windows.

**WebSocket events:**

| Event | When | Payload |
|-------|------|---------|
| `analytics_scan_progress` | During initial/incremental scan | projects_scanned, projects_total, sessions_found, current_project |
| `analytics_scan_complete` | Scan finished | totals, duration |
| `analytics_live_update` | New messages in active sessions | new messages, updated session/today totals |
| `analytics_flag_raised` | Anomaly detected | flag type, severity, description, session/project |
| `analytics_flag_resolved` | Anomaly cleared | flag id |

**`has_subscribers("analytics")` check:** Add a `has_subscribers(topic)` method to `WebSocketRegistry` (returns True if any connected client subscribed to the given topic or `"*"`). The analytics page subscribes to the `"analytics"` topic on mount by sending `{"type": "subscribe", "topic": "analytics"}` over WS. The live monitor checks this to determine poll interval. Existing `connect()` already accepts topics — `has_subscribers` just exposes a query method.

### 6. Project Hash Decoding

Claude Code encodes project paths as directory names: `/Users/jack/Github/my-repo` → `-Users-jack-Github-my-repo`. The analytics UI needs readable names.

Decoding logic (same approach as cchubber):
1. Replace leading drive letter pattern: `C--` → `C:/`
2. Split on `-`, skip common prefixes (Users, Documents, Desktop, Github, etc.)
3. Take the last 2-3 meaningful segments as the display name
4. Cache decoded names in memory

Example: `-Users-jack-neil-Github-claude-jacked` → "claude-jacked"

### 7. Cost Estimation

Claude Code doesn't report costs for Max/Pro plans (`costUSD` is always null). **Reuse the existing pricing from `jacked/web/db_analytics.py`** which already defines `MODEL_PRICING` with per-model rates. Import and use it — do NOT duplicate the pricing table. The existing pricing:
- Opus: input $5/M, output $25/M, cache_read $0.50/M, cache_write $6.25/M
- Sonnet: input $3/M, output $15/M, cache_read $0.30/M, cache_write $3.75/M
- Haiku: input $0.80/M, output $4/M, cache_read $0.08/M, cache_write $1/M

Model names from JSONL (e.g., `claude-opus-4-6`) are matched via `db_analytics.MODEL_PRICING`. Unknown models fall back to Opus pricing (conservative). Costs labeled "estimated API equivalent" in the UI.

Note: `cache_write` in the existing code corresponds to `cache_creation_input_tokens` in the JSONL data.

## Files

| File | Role |
|------|------|
| `jacked/web/analytics_db.py` | New: AnalyticsDB class — schema, insert, query, purge, rollup |
| `jacked/web/analytics_scanner.py` | New: JSONL scanner — walks dirs, parses messages, dedupes, incremental |
| `jacked/web/analytics_anomalies.py` | New: Anomaly detector — flag generation, auto-resolution |
| `jacked/web/analytics_monitor.py` | New: Live monitor loop — polls active files, broadcasts via WS |
| `jacked/web/analytics_paths.py` | New: Cross-platform path resolution, project hash decoding |
| `jacked/api/routes/analytics.py` | New: API endpoints — overview, sessions, trends, settings, flags |
| `jacked/api/main.py` | Modified: start scanner + monitor background tasks on startup |
| `jacked/data/web/js/components/analytics.js` | Modified: Overview tab with flags, project breakdown |
| `jacked/data/web/js/components/analytics-charts.js` | Modified: Trends tab with Chart.js charts |
| `jacked/data/web/js/components/analytics-tables.js` | Modified: Sessions tab with expandable rows |
| `jacked/data/web/js/websocket.js` | Modified: analytics WS event handlers |
| `jacked/data/web/index.html` | Modified: ensure Chart.js loaded |
| `tests/unit/test_analytics_scanner.py` | New: scanner tests |
| `tests/unit/test_analytics_anomalies.py` | New: anomaly detection tests |
