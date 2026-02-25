# Analytics Dashboard & Security Profiles Design

**Date:** 2026-02-25
**Status:** Approved
**Scope:** Two features — gatekeeper analytics dashboard (full observability) and security profile import/export

---

## Feature 1: Analytics Dashboard

### Overview

Full observability dashboard for the gatekeeper, served at `#analytics` in the web UI. Visualizes decision patterns, surfaces security posture insights, and provides drill-down to raw logs.

### Architecture

**New files:**
- `jacked/web/db_analytics.py` — SQL aggregation queries (~300 lines)
- `jacked/api/routes/analytics.py` — Updated endpoints (existing file)
- `jacked/data/web/js/components/analytics-dashboard.js` — Layout, KPI cards, date range, coordination (~200 lines)
- `jacked/data/web/js/components/analytics-charts.js` — Chart.js rendering: line, doughnut, heatmap (~200 lines)
- `jacked/data/web/js/components/analytics-tables.js` — Session risk, suggested rules, hot rules (~200 lines)
- Chart.js + chartjs-chart-matrix bundled as static assets under `jacked/data/web/js/vendor/`

**Charting:** Chart.js (~60KB) + chartjs-chart-matrix plugin for heatmap. Loaded lazily via dynamic `<script>` on first `#analytics` navigation. Bundled locally for offline support (no CDN dependency).

### Dashboard Layout

**Date range selector** (top-right): Dropdown — 24h, 7d, 30d, 90d, 1y. Default 7d. All sections respond to this selector.

**KPI cards (5 across, always visible):**

| Card | Source | Display |
|------|--------|---------|
| Total Decisions | COUNT(*) in range | Number + trend arrow vs previous period |
| Approval Rate | ALLOW / total * 100 | Percentage, color-coded (green >90%, yellow 70-90%, red <70%) |
| Denials | COUNT where decision=DENY | Number, red accent |
| Rule Coverage | % decisions with method != API | Percentage (higher = better tuned) |
| API Evaluations | COUNT where method=API | Number (honest metric, no fabricated costs) |

**Charts section (collapsible, default open):**
- **Left: Time-series stacked line** — Decisions over time, stacked by decision type (ALLOW, DENY, ASK_USER). Auto-adaptive granularity.
- **Right: Doughnut** — Method breakdown (SAFE_PREFIX, LOCAL, API, DENY_PATTERN, PERMS, PATH_SAFETY, etc.). Click segment to drill down.

**Activity heatmap (collapsible, default collapsed):**
- 7x24 grid (day-of-week x hour-of-day), color intensity = decision volume
- Single SQL query returns UTC timestamps; bucketing happens in JS via `new Date(utc).getDay()` / `.getHours()` for correct local timezone display

**Session risk table (collapsible, default collapsed):**
- Top 20 sessions ranked by risk score
- Scoring: DENY_PATTERN = 3pts, DENY via API = 2pts, ASK_USER = 1pt
- Columns: session start, repo, total decisions, denials, risk score
- "Load more" button for pagination
- Click row → drill to logs

**Rule intelligence (collapsible, default collapsed):**
- **Suggested rules** — Commands hitting API 3+ times with same decision *within the selected date range*. Server-side `_DANGEROUS_PATTERNS` validation before displaying. One-click "Add Rule" (with warning for broad wildcards).
- **Hot rules** — Top 10 most-triggered permission rules with hit counts
- **Dead rules** — Deferred to phase 2. Requires `matched_rule` column in `gatekeeper_decisions` (schema migration).

### Auto-Adaptive Granularity

Backend determines granularity based on `days` parameter:
- `days < 2` → hourly buckets
- `2 <= days <= 30` → daily buckets
- `days > 30` → weekly buckets

Returns a consistent `{buckets: [{timestamp, allow, deny, ask_user}, ...]}` shape regardless of granularity.

### Drill-Down

Click any chart segment, heatmap cell, or table row → navigate to `#logs-gatekeeper?decision=X&method=Y&date_from=Z&from=analytics`.

The `logs-gatekeeper.js` component reads initial filter state from hash params on load. When `from=analytics` is present, a "Back to Dashboard" link appears at the top.

### Timezone Handling

- All timestamps stored as UTC in SQLite (existing behavior)
- All API responses return UTC timestamps
- Browser converts to local time via `new Date(utcString)` for display
- Heatmap bucketing happens in JS for correct local timezone

### UX Details

- **Collapsible sections** with collapse/expand state persisted in `localStorage`
- **Empty state:** When `total_decisions == 0`, show a single "No gatekeeper data yet" message instead of empty charts
- **Skeleton loaders** per section while data loads
- **Responsive:** KPI cards wrap on narrow screens, charts stack vertically

### Backend: New SQL Queries (db_analytics.py)

All queries use parameterized SQL (no f-strings). Each method gets 3+ doctests (empty DB, single row, boundary).

| Method | Purpose | Key SQL |
|--------|---------|---------|
| `get_kpi_totals(days)` | 5 KPI values | Single query with CASE expressions |
| `get_time_series(days)` | Stacked line data | GROUP BY date bucket, adaptive granularity |
| `get_method_breakdown(days)` | Doughnut data | GROUP BY method |
| `get_heatmap_raw(days)` | Raw UTC timestamps for JS bucketing | SELECT timestamp WHERE timestamp >= ? |
| `get_session_risk(days, limit=20)` | Weighted risk scores | GROUP BY session_id, ORDER BY risk DESC, LIMIT ? |
| `get_suggested_rules(days, min_hits=3)` | API patterns above threshold | GROUP BY command pattern, HAVING COUNT >= ? |
| `get_hot_rules(days, limit=10)` | Most-triggered methods | GROUP BY method, ORDER BY count DESC |

### Testing

- `tests/unit/test_db_analytics.py` — Pytest tests for each DB method
- Each `db_analytics.py` method gets 3+ inline doctests (empty, single, boundary)
- Drill-down hash param flow validated via manual browser testing or Playwright MCP during implementation

---

## Feature 2: Security Profiles (Rule Import/Export)

### Overview

Named security profiles that bundle permission rules + gatekeeper configuration into shareable, versionable JSON files. Import with review, auto-backup, and one-click undo.

### Architecture

**New files:**
- `jacked/profiles.py` — Core logic: validation, import, export, backup, restore (~350 lines)
- `jacked/api/routes/profiles.py` — REST endpoints (~200 lines)
- Profile storage: `~/.claude/jacked/profiles/`
- Backups: `~/.claude/jacked/profiles/.backups/`

### Profile Schema

```json
{
  "name": "strict-security",
  "description": "Locked-down profile for open-source work",
  "author": "jack",
  "jacked_version": "0.8.0",
  "created_at": "2026-02-25T14:30:00+00:00",
  "rules": {
    "allow": ["Read(*)"],
    "deny": ["Bash(rm:*)"],
    "ask": ["Bash(curl:*)"]
  },
  "gatekeeper_config": {
    "model": "sonnet",
    "eval_method": "api",
    "command_categories": {"git": "allow", "network": "evaluate"},
    "path_safety": {
      "enabled": true,
      "allowed_paths": ["/Users/jack/projects"],
      "disabled_patterns": [],
      "watched_paths": []
    },
    "tool_toggles": {
      "Bash": true, "Read": true, "Edit": true,
      "Write": true, "Grep": true, "Glob": true,
      "NotebookEdit": true
    }
  }
}
```

**Never included in profiles:** `api_key` (stripped on export, rejected on import).

### Export Flow

1. Read current permissions from `~/.claude/settings.json`
2. Read gatekeeper config by **allowlisted keys only**: `gatekeeper.model`, `gatekeeper.eval_method`, `gatekeeper.command_categories`, `gatekeeper.path_safety`, `gatekeeper.tools`
3. User provides name + description
4. Assemble profile JSON with `jacked_version` from `jacked.__version__`
5. Save to `~/.claude/jacked/profiles/<name>.json`
   - Filename: lowercase, spaces → hyphens, strip leading/trailing hyphens

### Import Flow

1. Upload or paste JSON
2. **Validate:**
   - Pydantic model with `extra = "forbid"` — reject unknown keys
   - Name: alphanumeric + dashes + spaces, max 64 chars
   - Description: max 500 chars
   - `jacked_version`: semver comparison via `packaging.version.Version` — reject if higher than running version
   - Enum validation: model ∈ {haiku, sonnet, opus}, eval_method ∈ known set, category modes ∈ {allow, evaluate, ask, deny}
   - Config key allowlist: only `model`, `eval_method`, `command_categories`, `path_safety`, `tool_toggles` accepted
3. **Diff preview:**
   - Show leaf-level changes: "model: haiku → sonnet", "network: evaluate → allow"
   - **Red warnings** for dangerous config:
     - `path_safety.enabled = false`
     - `path_safety.allowed_paths` containing `/`, `/*`, `/etc`
     - Broad wildcards in rules: `Bash(*)`, `Bash(command:*)`
     - Command categories set to `allow` for dangerous categories
   - **Red warnings** for permission patterns matching `_DANGEROUS_PATTERNS`
4. **Auto-backup** current config to `.backups/<ISO-timestamp>.json`
5. **Apply** on user confirmation
6. **Show "Undo" button** — restores from the timestamped backup. Persists until next navigation or dismiss.

### Profile Management

**UI (settings or dedicated section):**
- List saved profiles with name, description, date, version
- Delete profile
- Import / export buttons

**CLI:**
- `jacked profiles list`
- `jacked profiles export <name>`
- `jacked profiles import <path>`
- `jacked profiles delete <name>`

### Security

- Export builds config by **allowlist** (read only named keys), never iterates all `gatekeeper.*` settings
- Import validates with Pydantic `extra = "forbid"` — no key injection
- `api_key` never exported, rejected if present in import
- Profile filenames sanitized: reject `/`, `\`, `\0`, `..`
- All profile metadata rendered with `escapeHtml()` in UI

### Testing

- `tests/unit/test_profiles.py`:
  - Reject unknown keys (Pydantic extra=forbid)
  - Reject path traversal in name
  - Reject description over 500 chars
  - Reject profile with higher jacked_version
  - Reject invalid enum values
  - Flag dangerous config patterns
  - Export/import round-trip equivalence
  - Backup created before import
  - Restore from backup produces original config
  - Delete removes file

---

## Implementation Notes (from design review)

These are refinements to address during implementation, not design-level concerns:

1. Profile version comparison must use `packaging.version.Version`, not string comparison
2. Collapsed section state persisted in `localStorage`
3. Chart.js bundled locally under `jacked/data/web/js/vendor/` for offline support
4. Diff preview shows leaf-level changes for nested config objects
5. Suggested rules filtered by selected date range (recency), not all-time
6. Default collapsed state: heatmap, session risk, rule intelligence. Default open: KPI cards, charts.
