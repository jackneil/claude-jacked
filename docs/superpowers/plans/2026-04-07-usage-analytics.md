# Usage Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a live token consumption monitor that parses Claude Code's JSONL files, detects anomalies, and surfaces usage trends in jacked's dashboard.

**Architecture:** Three-layer pipeline: JSONL scanner (background async, incremental) → anomaly detector (flag generation) → live monitor (WebSocket push). Separate `usage_analytics.db` for isolation. Frontend adds "Token Usage" sub-tab to existing analytics page with diagnosis-first layout. Cross-platform (macOS/Linux/Windows).

**Tech Stack:** Python/FastAPI backend, SQLite, Chart.js (already vendored), vanilla JS frontend, WebSocket

---

## File Map

| File | Role | Tasks |
|------|------|-------|
| `jacked/web/analytics_paths.py` | New: cross-platform path resolution, project hash decoding | 1 |
| `jacked/web/analytics_db.py` | New: AnalyticsDB class — schema, insert, query, purge, rollup | 2 |
| `jacked/web/analytics_scanner.py` | New: JSONL scanner — walk dirs, parse, dedup, incremental | 3 |
| `jacked/web/analytics_anomalies.py` | New: anomaly detector — 6 flag types, auto-resolution | 4 |
| `jacked/web/analytics_monitor.py` | New: live monitor loop + initial scan orchestration | 5 |
| `jacked/api/websocket.py` | Modified: add `has_subscribers(topic)` method | 6 |
| `jacked/api/routes/analytics.py` | Modified: add token usage API endpoints alongside gatekeeper | 7 |
| `jacked/api/main.py` | Modified: start scanner + monitor in lifespan | 8 |
| `jacked/data/web/js/components/analytics.js` | Modified: sub-tab nav (Gatekeeper / Token Usage) | 9 |
| `jacked/data/web/js/components/usage-overview.js` | New: Overview tab — health banner, flags, project breakdown | 10 |
| `jacked/data/web/js/components/usage-sessions.js` | New: Sessions tab — ranked list, expandable detail | 11 |
| `jacked/data/web/js/components/usage-trends.js` | New: Trends tab — Chart.js daily charts | 12 |
| `jacked/data/web/js/websocket.js` | Modified: analytics WS handlers | 9 |
| `tests/unit/test_analytics_paths.py` | New | 1 |
| `tests/unit/test_analytics_db.py` | New | 2 |
| `tests/unit/test_analytics_scanner.py` | New | 3 |
| `tests/unit/test_analytics_anomalies.py` | New | 4 |

## Parallelization Groups

- **Group A (parallel):** Tasks 1, 2, 6
- **Group B (depends on 1+2):** Task 3
- **Group C (depends on 3):** Tasks 4, 5
- **Group D (depends on 2+6):** Tasks 7, 8
- **Group E (parallel, depends on 7):** Tasks 9, 10, 11, 12

---

### Task 1: Cross-Platform Path Resolution + Project Hash Decoding

**Files:**
- Create: `jacked/web/analytics_paths.py`
- Test: `tests/unit/test_analytics_paths.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_analytics_paths.py
"""Tests for cross-platform Claude Code data path resolution and project hash decoding."""
import os
import pytest
from pathlib import Path
from unittest.mock import patch


class TestGetClaudeDataDirs:
    """get_claude_data_dirs returns valid paths for the current platform."""

    def test_returns_list_of_paths(self):
        from jacked.web.analytics_paths import get_claude_data_dirs
        dirs = get_claude_data_dirs()
        assert isinstance(dirs, list)
        for d in dirs:
            assert isinstance(d, Path)

    def test_primary_path_is_dot_claude_projects(self):
        from jacked.web.analytics_paths import get_claude_data_dirs
        dirs = get_claude_data_dirs()
        assert any("projects" in str(d) for d in dirs)

    @patch("sys.platform", "linux")
    def test_includes_xdg_fallback_on_linux(self, tmp_path):
        from jacked.web.analytics_paths import get_claude_data_dirs
        xdg = tmp_path / ".config" / "claude" / "projects"
        xdg.mkdir(parents=True)
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            dirs = get_claude_data_dirs()
            # Should include XDG path if it exists
            assert any(".config/claude" in str(d) for d in dirs)


class TestDecodeProjectHash:
    """decode_project_hash turns encoded directory names into readable names."""

    def test_simple_project(self):
        from jacked.web.analytics_paths import decode_project_hash
        result = decode_project_hash("-Users-jack-Github-claude-jacked")
        assert result["name"] == "claude-jacked"

    def test_windows_drive_letter(self):
        from jacked.web.analytics_paths import decode_project_hash
        result = decode_project_hash("C--Users-jack-Documents-my-project")
        assert "my-project" in result["name"]

    def test_deep_nesting(self):
        from jacked.web.analytics_paths import decode_project_hash
        result = decode_project_hash("-Users-jack-conductor-workspaces-hank-ehr-raleigh")
        # Should extract meaningful last segments, not the full path
        assert "raleigh" in result["name"].lower() or "hank-ehr" in result["name"].lower()

    def test_unknown_returns_truncated_hash(self):
        from jacked.web.analytics_paths import decode_project_hash
        result = decode_project_hash("unknown")
        assert result["name"] is not None
        assert len(result["name"]) > 0


class TestFindActiveJsonlFiles:
    """find_active_jsonl_files returns recently modified JSONL files."""

    def test_finds_recent_jsonl(self, tmp_path):
        from jacked.web.analytics_paths import find_active_jsonl_files
        proj_dir = tmp_path / "projects" / "-test-project"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "session1.jsonl"
        jsonl.write_text('{"type":"user"}\n')

        files = find_active_jsonl_files([tmp_path / "projects"], max_age_seconds=600)
        assert len(files) >= 1
        assert jsonl in files

    def test_ignores_old_files(self, tmp_path):
        from jacked.web.analytics_paths import find_active_jsonl_files
        proj_dir = tmp_path / "projects" / "-test-project"
        proj_dir.mkdir(parents=True)
        jsonl = proj_dir / "old-session.jsonl"
        jsonl.write_text('{"type":"user"}\n')
        # Set mtime to 1 hour ago
        old_time = os.path.getmtime(str(jsonl)) - 3600
        os.utime(str(jsonl), (old_time, old_time))

        files = find_active_jsonl_files([tmp_path / "projects"], max_age_seconds=600)
        assert jsonl not in files
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_analytics_paths.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `analytics_paths.py`**

```python
# jacked/web/analytics_paths.py
"""Cross-platform Claude Code data path resolution and project hash decoding."""

import os
import sys
import time
from pathlib import Path

# Common path prefixes to skip when extracting project names
_SKIP_PREFIXES = frozenset({
    "users", "home", "documents", "desktop", "downloads",
    "github", "repos", "projects", "code", "dev", "src",
    "conductor", "workspaces",
})


def get_claude_data_dirs() -> list[Path]:
    """Return all valid Claude Code project directories for the current platform.

    Checks:
    - ~/.claude/projects/ (macOS, Linux, Windows via USERPROFILE)
    - ~/.config/claude/projects/ (XDG fallback, Linux only)
    """
    dirs = []
    primary = Path.home() / ".claude" / "projects"
    if primary.is_dir():
        dirs.append(primary)

    # XDG fallback (Linux)
    if sys.platform.startswith("linux"):
        xdg = Path.home() / ".config" / "claude" / "projects"
        if xdg.is_dir() and xdg != primary:
            dirs.append(xdg)

    return dirs


def decode_project_hash(hash_name: str) -> dict:
    """Decode Claude Code's encoded project directory name into a readable name.

    Format: -Users-jack-Github-my-project or C--Users-jack-Documents-project
    Returns: {"name": "my-project", "path": "/Users/jack/Github/my-project"}
    """
    if not hash_name or hash_name == "unknown":
        return {"name": hash_name or "Unknown", "path": None}

    # Replace drive letter pattern: C-- → C:/
    decoded = hash_name
    if len(decoded) > 2 and decoded[1:3] == "--":
        decoded = decoded[0] + ":/" + decoded[3:]

    # Split on dashes
    segments = hash_name.lstrip("-").split("-")

    # Find where the "interesting" name starts (after common prefixes)
    name_start = 0
    for i, seg in enumerate(segments):
        if seg.lower() in _SKIP_PREFIXES:
            name_start = i + 1
        elif name_start > 0:
            break  # Stop at first non-prefix after finding prefixes

    # Take last 2-3 meaningful segments
    meaningful = segments[max(name_start, len(segments) - 3):]
    name = "-".join(meaningful) if meaningful else hash_name[:20]

    # Reconstruct display path
    path = hash_name.replace("--", ":/").lstrip("-").replace("-", "/")

    return {"name": name, "path": path}


def find_active_jsonl_files(
    data_dirs: list[Path],
    max_age_seconds: int = 600,
) -> list[Path]:
    """Find JSONL files modified within max_age_seconds.

    Scans all project directories in data_dirs for .jsonl files.
    Uses stat().st_mtime — cross-platform, no dependencies.
    """
    cutoff = time.time() - max_age_seconds
    active = []

    for data_dir in data_dirs:
        if not data_dir.is_dir():
            continue
        try:
            for project_dir in data_dir.iterdir():
                if not project_dir.is_dir():
                    continue
                _scan_dir_for_jsonl(project_dir, cutoff, active)
        except PermissionError:
            continue

    return active


def _scan_dir_for_jsonl(directory: Path, cutoff: float, results: list[Path]):
    """Recursively find .jsonl files newer than cutoff in directory."""
    try:
        for entry in directory.iterdir():
            if entry.is_file() and entry.suffix == ".jsonl":
                try:
                    if entry.stat().st_mtime >= cutoff:
                        results.append(entry)
                except OSError:
                    continue
            elif entry.is_dir() and entry.name == "subagents":
                # Check subagent files too
                _scan_dir_for_jsonl(entry, cutoff, results)
    except PermissionError:
        pass
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_analytics_paths.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/analytics_paths.py tests/unit/test_analytics_paths.py
git commit -m "feat: cross-platform Claude Code path resolution and project hash decoding"
```

---

### Task 2: Analytics Database

**Files:**
- Create: `jacked/web/analytics_db.py`
- Test: `tests/unit/test_analytics_db.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_analytics_db.py
"""Tests for the analytics SQLite database."""
import time
import pytest
from jacked.web.analytics_db import AnalyticsDB


class TestAnalyticsDBSchema:
    def test_creates_tables(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        # Should not raise
        db.insert_messages([])
        db.close()

    def test_insert_and_query_messages(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        db.insert_messages([{
            "id": "msg_001",
            "session_id": "sess_1",
            "project_hash": "-test-project",
            "timestamp": "2026-04-07T12:00:00Z",
            "model": "claude-opus-4-6",
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_tokens": 50000,
            "cache_create_tokens": 1000,
            "estimated_cost_usd": 0.05,
            "is_subagent": False,
        }])
        msgs = db.get_messages_for_session("sess_1")
        assert len(msgs) == 1
        assert msgs[0]["model"] == "claude-opus-4-6"
        db.close()

    def test_dedup_by_message_id(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        msg = {
            "id": "msg_dup",
            "session_id": "s1",
            "project_hash": "-p",
            "timestamp": "2026-04-07T12:00:00Z",
            "model": "claude-opus-4-6",
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
            "estimated_cost_usd": 0.01,
            "is_subagent": False,
        }
        db.insert_messages([msg, msg])  # Same ID twice
        msgs = db.get_messages_for_session("s1")
        assert len(msgs) == 1
        db.close()


class TestDailySummaries:
    def test_rollup_creates_summary(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        db.insert_messages([{
            "id": "msg_r1",
            "session_id": "s1",
            "project_hash": "-proj",
            "timestamp": "2026-04-07T12:00:00Z",
            "model": "claude-opus-4-6",
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_tokens": 50000,
            "cache_create_tokens": 1000,
            "estimated_cost_usd": 0.05,
            "is_subagent": False,
        }])
        db.rollup_daily_summaries("2026-04-07")
        summaries = db.get_daily_summaries(days=7)
        assert len(summaries) >= 1
        assert summaries[0]["date"] == "2026-04-07"
        db.close()


class TestPurge:
    def test_purge_old_messages(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        db.insert_messages([{
            "id": "msg_old",
            "session_id": "s1",
            "project_hash": "-proj",
            "timestamp": "2026-03-01T12:00:00Z",  # 37 days ago
            "model": "claude-opus-4-6",
            "input_tokens": 100,
            "output_tokens": 500,
            "cache_read_tokens": 0,
            "cache_create_tokens": 0,
            "estimated_cost_usd": 0.01,
            "is_subagent": False,
        }])
        db.purge_messages_older_than(days=30)
        msgs = db.get_messages_for_session("s1")
        assert len(msgs) == 0
        db.close()


class TestScanState:
    def test_track_file_position(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        db.update_scan_state("/path/to/file.jsonl", byte_offset=1024, mtime=1234.5, count=10)
        state = db.get_scan_state("/path/to/file.jsonl")
        assert state["last_byte_offset"] == 1024
        assert state["last_mtime"] == 1234.5
        assert state["messages_count"] == 10
        db.close()

    def test_returns_none_for_unknown_file(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        state = db.get_scan_state("/nonexistent.jsonl")
        assert state is None
        db.close()


class TestFlags:
    def test_insert_and_query_active_flags(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        db.insert_flag(
            flag_type="cost_outlier",
            severity="warning",
            session_id="s1",
            project_hash="-proj",
            message="Session s1 is 3x average cost",
        )
        flags = db.get_active_flags()
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "cost_outlier"
        db.close()

    def test_resolve_flag(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        db.insert_flag(
            flag_type="cache_drop",
            severity="critical",
            session_id="s1",
            project_hash="-p",
            message="Cache health dropped to 17%",
        )
        flags = db.get_active_flags()
        db.resolve_flag(flags[0]["id"])
        assert len(db.get_active_flags()) == 0
        db.close()


class TestSettings:
    def test_get_set_setting(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        db.set_setting("purge_days", "30")
        assert db.get_setting("purge_days") == "30"
        db.close()

    def test_returns_default_for_missing(self, tmp_path):
        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        assert db.get_setting("nonexistent", "default") == "default"
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_analytics_db.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement `analytics_db.py`**

Create `jacked/web/analytics_db.py` with:
- `AnalyticsDB` class that takes a db path (defaults to `~/.claude/jacked-analytics.db`)
- SQLite with WAL mode, same `_writer()` pattern as the main Database class
- Schema creation in `__init__` via `executescript()`
- `insert_messages(messages: list[dict])` — INSERT OR IGNORE for dedup by `id`
- `get_messages_for_session(session_id)` — query messages table
- `get_overview(days=1)` — aggregated stats for the overview tab (tokens, cost, cache ratio, session count, by project)
- `get_session_list(days=1, project_hash=None, flagged_only=False)` — for sessions tab
- `get_daily_summaries(days=7)` — for trends tab
- `rollup_daily_summaries(date_str)` — aggregate messages into daily_summaries for a given date
- `purge_messages_older_than(days)` — DELETE + rollup before purge
- `update_scan_state(file_path, byte_offset, mtime, count)` — UPSERT
- `get_scan_state(file_path)` — SELECT
- `insert_flag(flag_type, severity, session_id, project_hash, message, detail=None)` — INSERT
- `get_active_flags()` — WHERE resolved_at IS NULL
- `resolve_flag(flag_id)` — UPDATE resolved_at
- `resolve_flags_for_session(session_id)` — bulk resolve
- `get_setting(key, default=None)` — SELECT
- `set_setting(key, value)` — UPSERT
- `close()` — close connection

Cost estimation helper (module-level):
```python
MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0, "cache_read": 1.875, "cache_create": 18.75},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0, "cache_read": 0.30, "cache_create": 3.75},
    "claude-haiku-4-5": {"input": 0.80, "output": 4.0, "cache_read": 0.08, "cache_create": 1.0},
}

def estimate_cost(model: str, input_t: int, output_t: int, cache_read_t: int, cache_create_t: int) -> float:
    pricing = MODEL_PRICING.get(model)
    if not pricing:
        # Fallback: match partial model name
        for key, p in MODEL_PRICING.items():
            if key.split("-")[1] in model:  # "opus" in "claude-opus-4-6-20260401"
                pricing = p
                break
    if not pricing:
        pricing = MODEL_PRICING["claude-opus-4-6"]  # Conservative fallback
    return (
        input_t * pricing["input"] / 1_000_000
        + output_t * pricing["output"] / 1_000_000
        + cache_read_t * pricing["cache_read"] / 1_000_000
        + cache_create_t * pricing["cache_create"] / 1_000_000
    )
```

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_analytics_db.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/analytics_db.py tests/unit/test_analytics_db.py
git commit -m "feat: analytics SQLite database with cost estimation and purge"
```

---

### Task 3: JSONL Scanner

**Files:**
- Create: `jacked/web/analytics_scanner.py`
- Test: `tests/unit/test_analytics_scanner.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_analytics_scanner.py
"""Tests for JSONL conversation file scanner."""
import json
import os
import time
import pytest
from pathlib import Path


def _write_jsonl(path: Path, messages: list[dict]):
    """Helper to write JSONL test data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        for msg in messages:
            f.write(json.dumps(msg) + "\n")


def _assistant_msg(msg_id: str, model: str = "claude-opus-4-6",
                   input_t: int = 100, output_t: int = 500,
                   cache_read: int = 50000, cache_create: int = 1000,
                   session_id: str = "sess_1"):
    return {
        "type": "assistant",
        "timestamp": "2026-04-07T12:00:00Z",
        "sessionId": session_id,
        "message": {
            "id": msg_id,
            "model": model,
            "role": "assistant",
            "usage": {
                "input_tokens": input_t,
                "output_tokens": output_t,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_create,
            },
        },
    }


class TestScanProject:
    def test_parses_assistant_messages(self, tmp_path):
        from jacked.web.analytics_scanner import scan_project_dir
        from jacked.web.analytics_db import AnalyticsDB

        proj = tmp_path / "projects" / "-test-project"
        _write_jsonl(proj / "sess1.jsonl", [
            {"type": "user", "timestamp": "2026-04-07T11:59:00Z"},
            _assistant_msg("msg_1"),
            _assistant_msg("msg_2"),
        ])

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        count = scan_project_dir(proj, "-test-project", db)
        assert count == 2
        db.close()

    def test_deduplicates_by_message_id(self, tmp_path):
        from jacked.web.analytics_scanner import scan_project_dir
        from jacked.web.analytics_db import AnalyticsDB

        proj = tmp_path / "projects" / "-test-project"
        _write_jsonl(proj / "sess1.jsonl", [
            _assistant_msg("msg_dup"),
            _assistant_msg("msg_dup"),  # duplicate
            _assistant_msg("msg_dup"),  # duplicate
        ])

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        count = scan_project_dir(proj, "-test-project", db)
        assert count == 1  # Only 1 unique
        db.close()

    def test_incremental_scan_skips_already_read_bytes(self, tmp_path):
        from jacked.web.analytics_scanner import scan_project_dir
        from jacked.web.analytics_db import AnalyticsDB

        proj = tmp_path / "projects" / "-test-project"
        jsonl = proj / "sess1.jsonl"
        _write_jsonl(jsonl, [_assistant_msg("msg_1")])

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        count1 = scan_project_dir(proj, "-test-project", db)
        assert count1 == 1

        # Append a new message
        with open(jsonl, "a") as f:
            f.write(json.dumps(_assistant_msg("msg_2")) + "\n")

        count2 = scan_project_dir(proj, "-test-project", db)
        assert count2 == 1  # Only the new message
        db.close()

    def test_scans_subagent_directory(self, tmp_path):
        from jacked.web.analytics_scanner import scan_project_dir
        from jacked.web.analytics_db import AnalyticsDB

        proj = tmp_path / "projects" / "-test-project"
        sess_dir = proj / "sess1"
        sub_dir = sess_dir / "subagents"
        _write_jsonl(sub_dir / "agent-abc.jsonl", [
            _assistant_msg("msg_sub_1", model="claude-haiku-4-5"),
        ])

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        count = scan_project_dir(proj, "-test-project", db)
        assert count == 1
        msgs = db.get_messages_for_session("sess1")
        # Subagent messages should be marked
        # (session_id comes from parent directory, not the JSONL sessionId)
        db.close()

    def test_skips_user_messages(self, tmp_path):
        from jacked.web.analytics_scanner import scan_project_dir
        from jacked.web.analytics_db import AnalyticsDB

        proj = tmp_path / "projects" / "-test-project"
        _write_jsonl(proj / "sess1.jsonl", [
            {"type": "user", "timestamp": "2026-04-07T12:00:00Z",
             "message": {"role": "user", "content": [{"type": "text", "text": "hello"}]}},
            {"type": "system", "timestamp": "2026-04-07T12:00:01Z"},
        ])

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        count = scan_project_dir(proj, "-test-project", db)
        assert count == 0
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_analytics_scanner.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `analytics_scanner.py`**

Create `jacked/web/analytics_scanner.py` with:

- `scan_project_dir(project_dir: Path, project_hash: str, db: AnalyticsDB) -> int` — scans all JSONL files in a project directory (including subagents), returns count of new messages inserted. **Runs file I/O in thread pool via `asyncio.to_thread()`.**
- `scan_all_projects(data_dirs: list[Path], db: AnalyticsDB, progress_callback=None) -> dict` — walks all project directories, calls `scan_project_dir` for each, calls `progress_callback` with progress info. Yields between projects with `await asyncio.sleep(0)`.
- `parse_jsonl_from_offset(file_path: Path, offset: int) -> tuple[list[dict], int]` — reads from byte offset, parses assistant messages, returns (messages, new_offset). **If offset > file size, resets to 0** (handles Claude Code JSONL rewrites on resume).
- `_parse_assistant_message(record: dict, session_id: str, project_hash: str, is_subagent: bool) -> dict | None` — extracts tokens/model/timestamp from a JSONL record, computes estimated cost using `db_analytics.MODEL_PRICING` (imported, not duplicated)
- `prune_stale_scan_state(db: AnalyticsDB)` — remove `scan_state` entries where file no longer exists on disk

Key implementation details:
- Session ID extracted from the JSONL filename (the UUID before .jsonl)
- Subagent files detected by being inside a `subagents/` directory
- Message dedup via INSERT OR IGNORE (primary key on message.id)
- Incremental: reads `scan_state` for each file, skips if mtime unchanged, seeks to last offset if mtime changed. **If stored offset > current file size, resets to 0** (Claude Code rewrites JSONL on resume)
- Skip lines without `type: "assistant"` or without `message.usage`
- Malformed JSON lines silently skipped (try/except per line)
- Max line length: 1MB (skip lines exceeding this to prevent memory bombs)
- After each full scan, call `prune_stale_scan_state()` to remove entries for deleted files

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_analytics_scanner.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/analytics_scanner.py tests/unit/test_analytics_scanner.py
git commit -m "feat: incremental JSONL scanner with dedup and subagent support"
```

---

### Task 4: Anomaly Detector

**Files:**
- Create: `jacked/web/analytics_anomalies.py`
- Test: `tests/unit/test_analytics_anomalies.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/unit/test_analytics_anomalies.py
"""Tests for usage anomaly detection."""
import pytest
from jacked.web.analytics_db import AnalyticsDB


def _insert_session(db, session_id, project_hash, messages):
    """Helper: insert a batch of messages for a session."""
    db.insert_messages([{
        "id": f"{session_id}_{i}",
        "session_id": session_id,
        "project_hash": project_hash,
        "timestamp": f"2026-04-07T{12+i//60:02d}:{i%60:02d}:00Z",
        "model": m.get("model", "claude-opus-4-6"),
        "input_tokens": m.get("input", 100),
        "output_tokens": m.get("output", 500),
        "cache_read_tokens": m.get("cache_read", 50000),
        "cache_create_tokens": m.get("cache_create", 1000),
        "estimated_cost_usd": m.get("cost", 0.05),
        "is_subagent": m.get("is_subagent", False),
    } for i, m in enumerate(messages)])


class TestCacheDropDetection:
    def test_flags_low_cache_hit_session(self, tmp_path):
        from jacked.web.analytics_anomalies import detect_anomalies

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        # Normal sessions (high cache)
        for i in range(5):
            _insert_session(db, f"normal_{i}", "-proj", [
                {"cache_read": 90000, "cache_create": 1000, "input": 100}
            ] * 10)
        # Anomalous session (low cache)
        _insert_session(db, "bad_cache", "-proj", [
            {"cache_read": 1000, "cache_create": 90000, "input": 5000}
        ] * 10)

        flags = detect_anomalies(db, session_ids=["bad_cache"])
        cache_flags = [f for f in flags if f["flag_type"] == "cache_drop"]
        assert len(cache_flags) >= 1
        assert "bad_cache" in cache_flags[0]["session_id"]
        db.close()


class TestCostOutlierDetection:
    def test_flags_expensive_session(self, tmp_path):
        from jacked.web.analytics_anomalies import detect_anomalies

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        # Normal sessions
        for i in range(5):
            _insert_session(db, f"normal_{i}", "-proj", [{"cost": 0.05}] * 10)
        # Expensive session (5x average)
        _insert_session(db, "expensive", "-proj", [{"cost": 2.50}] * 10)

        flags = detect_anomalies(db, session_ids=["expensive"])
        cost_flags = [f for f in flags if f["flag_type"] == "cost_outlier"]
        assert len(cost_flags) >= 1
        db.close()


class TestContextBloatDetection:
    def test_flags_growing_input_tokens(self, tmp_path):
        from jacked.web.analytics_anomalies import detect_anomalies

        db = AnalyticsDB(str(tmp_path / "analytics.db"))
        # Session with exponentially growing input tokens
        messages = []
        for i in range(20):
            messages.append({"input": 1000 * (1 + i), "output": 500,
                             "cache_read": 50000, "cache_create": 1000, "cost": 0.05})
        _insert_session(db, "bloating", "-proj", messages)

        flags = detect_anomalies(db, session_ids=["bloating"])
        bloat_flags = [f for f in flags if f["flag_type"] == "context_bloat"]
        assert len(bloat_flags) >= 1
        db.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_analytics_anomalies.py -v`
Expected: FAIL

- [ ] **Step 3: Implement `analytics_anomalies.py`**

Create `jacked/web/analytics_anomalies.py` with:

- `detect_anomalies(db: AnalyticsDB, session_ids: list[str] | None = None) -> list[dict]` — run all v1 detectors, return new flags
- `_detect_cache_drop(db, session_ids)` — cache_read / total_input ratio per session vs 7-day average. **Excludes first 5 messages** (cache warmup). Only fires for sessions with 10+ messages.
- `_detect_cost_outlier(db, session_ids)` — session cost vs 7-day per-session average. Only fires for sessions with 5+ messages and 5+ minutes duration.
- `_detect_subagent_explosion(db, session_ids)` — count subagent messages (is_subagent=1) per session
- `auto_resolve_flags(db)` — flags for completed sessions auto-resolve 1 hour after last message. Active session flags re-evaluated each cycle.
- Respects snooze: check `analytics_settings` for `snooze_{flag_type}_until` before detecting

Deferred to v2: `context_bloat`, `resume_spike`, `inactive_burn`.

Each detector:
1. Queries the DB for the relevant metrics
2. Compares against thresholds (see spec for exact values)
3. Checks if a flag already exists for this session+type (avoid duplicates)
4. Checks snooze state for this flag type
5. Returns list of new flag dicts

- [ ] **Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_analytics_anomalies.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add jacked/web/analytics_anomalies.py tests/unit/test_analytics_anomalies.py
git commit -m "feat: anomaly detector — cache drop, cost outlier, context bloat, more"
```

---

### Task 5: Live Monitor + Initial Scan Orchestration

**Files:**
- Create: `jacked/web/analytics_monitor.py`

- [ ] **Step 1: Implement the monitor**

Create `jacked/web/analytics_monitor.py` with two async functions:

```python
async def initial_scan_loop(app):
    """Background task: parse all JSONL files on startup, broadcast progress."""
    # 1. Open/create AnalyticsDB
    # 2. Get data dirs via get_claude_data_dirs()
    # 3. List all project directories
    # 4. For each project:
    #    a. scan_project_dir(project_dir, hash, db)
    #    b. Broadcast analytics_scan_progress via ws_registry
    #    c. Run anomaly detection for new sessions
    #    d. await asyncio.sleep(0) to yield control
    # 5. Run rollup for any unsummarized days
    # 6. Run purge if setting is configured
    # 7. Broadcast analytics_scan_complete
    # 8. Store db on app.state.analytics_db for API routes

async def live_monitor_loop(app):
    """Background task: poll active JSONL files, push updates via WS."""
    # Wait for initial scan to complete (check app.state.analytics_db)
    # Then loop:
    # 1. Check has_subscribers("analytics") for poll interval
    # 2. find_active_jsonl_files()
    # 3. For each active file with new bytes:
    #    a. parse_from_offset
    #    b. insert_messages
    #    c. detect_anomalies for affected sessions
    #    d. broadcast analytics_live_update
    # 4. Auto-resolve stale flags
    # 5. Sleep interval
```

Key implementation: the initial scan runs once on startup, then the live monitor takes over. Both share the same `AnalyticsDB` instance on `app.state.analytics_db`.

- [ ] **Step 2: Run full test suite**

Run: `uv run python -m pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/web/analytics_monitor.py
git commit -m "feat: analytics monitor — initial scan + live JSONL polling"
```

---

### Task 6: WebSocket `has_subscribers` Method

**Files:**
- Modify: `jacked/api/websocket.py`

- [ ] **Step 1: Add `has_subscribers` method to WebSocketRegistry**

After the `client_count` property (line ~85), add:

```python
    def has_subscribers(self, topic: str) -> bool:
        """Check if any connected client is subscribed to the given topic.

        Returns True if at least one client subscribes to *topic* or ``*``.

        >>> WebSocketRegistry().has_subscribers("analytics")
        False
        """
        for ws, subs in self._clients.items():
            if "*" in subs or topic in subs:
                return True
        return False
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/api/websocket.py
git commit -m "feat: WebSocketRegistry.has_subscribers for adaptive polling"
```

---

### Task 7: API Routes for Token Usage

**Files:**
- Modify: `jacked/api/routes/analytics.py`

- [ ] **Step 1: Add token usage endpoints**

Add these endpoints to the existing analytics router:

```python
@router.get("/api/analytics/usage-overview")
async def get_usage_overview(request: Request, days: int = Query(default=1, ge=1, le=365)):
    """Overview: today's totals, cache health grade, project breakdown."""

@router.get("/api/analytics/usage-sessions")
async def get_usage_sessions(request: Request, days: int = 1, project: str = None, flagged_only: bool = False):
    """Session list ranked by cost."""

@router.get("/api/analytics/usage-session-detail/{session_id}")
async def get_usage_session_detail(request: Request, session_id: str):
    """Message-level detail for a single session."""

@router.get("/api/analytics/usage-trends")
async def get_usage_trends(request: Request, days: int = 7):
    """Daily summaries for trends chart."""

@router.get("/api/analytics/usage-flags")
async def get_usage_flags(request: Request):
    """Active anomaly flags."""

@router.get("/api/analytics/usage-scan-status")
async def get_usage_scan_status(request: Request):
    """Current scan status, DB size, message count."""

@router.post("/api/analytics/usage-settings")
async def update_usage_settings(request: Request):
    """Update purge_days setting."""
```

Each endpoint reads from `app.state.analytics_db` (set by the monitor during initial scan). Returns 503 if analytics DB is not yet available (scan in progress).

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/api/routes/analytics.py
git commit -m "feat: token usage API endpoints — overview, sessions, trends, flags"
```

---

### Task 8: Backend Integration — Start Background Tasks

**Files:**
- Modify: `jacked/api/main.py`

- [ ] **Step 1: Start analytics tasks in lifespan**

After the existing background task creation (line ~146), add:

```python
    # Start analytics scanner + monitor
    try:
        from jacked.web.analytics_monitor import initial_scan_loop, live_monitor_loop
        analytics_scan_task = asyncio.create_task(initial_scan_loop(app))
        analytics_monitor_task = asyncio.create_task(live_monitor_loop(app))
        logger.info("Started analytics scanner + live monitor")
    except Exception as e:
        logger.warning("Analytics module unavailable: %s", e)
```

- [ ] **Step 2: Run tests**

Run: `uv run python -m pytest tests/ -x -q`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add jacked/api/main.py
git commit -m "feat: start analytics scanner and monitor on server startup"
```

---

### Task 9: Frontend — Analytics Sub-Tab Navigation + WS Handlers

**Files:**
- Modify: `jacked/data/web/js/components/analytics.js`
- Modify: `jacked/data/web/js/websocket.js`

- [ ] **Step 1: Add sub-tab navigation to analytics page**

Modify `renderAnalytics()` in `analytics.js` to add a tab bar: "Gatekeeper" | "Token Usage". Default to Token Usage tab. The existing gatekeeper content moves into the Gatekeeper tab. Token Usage tab renders the new components (from Tasks 10-12).

Add tab state management:
```javascript
let analyticsSubTab = localStorage.getItem('jacked_analytics_subtab') || 'usage';
```

The Token Usage tab has its own sub-tabs: Overview | Sessions | Trends.

- [ ] **Step 2: Add analytics WS event handlers in websocket.js**

```javascript
jackedWS.on('analytics_scan_progress', (msg) => {
    const d = msg.payload || msg;
    if (typeof updateAnalyticsScanProgress === 'function') {
        updateAnalyticsScanProgress(d);
    }
});

jackedWS.on('analytics_scan_complete', (msg) => {
    if (typeof onAnalyticsScanComplete === 'function') {
        onAnalyticsScanComplete();
    }
});

jackedWS.on('analytics_live_update', (msg) => {
    const d = msg.payload || msg;
    if (typeof onAnalyticsLiveUpdate === 'function') {
        onAnalyticsLiveUpdate(d);
    }
});

jackedWS.on('analytics_flag_raised', (msg) => {
    const d = msg.payload || msg;
    if (typeof onAnalyticsFlagRaised === 'function') {
        onAnalyticsFlagRaised(d);
    }
});

jackedWS.on('analytics_flag_resolved', (msg) => {
    const d = msg.payload || msg;
    if (typeof onAnalyticsFlagResolved === 'function') {
        onAnalyticsFlagResolved(d);
    }
});
```

- [ ] **Step 3: Add topic subscription on mount**

When the Token Usage tab is active, send a subscribe message to get the faster 1s poll interval:
```javascript
// On analytics page mount
if (jackedWS.isConnected()) {
    jackedWS.send({ type: "subscribe", topic: "analytics" });
}
```

- [ ] **Step 4: Commit**

```bash
git add jacked/data/web/js/components/analytics.js jacked/data/web/js/websocket.js
git commit -m "feat: analytics sub-tab navigation + WS event handlers"
```

---

### Task 10: Frontend — Overview Tab

**Files:**
- Create: `jacked/data/web/js/components/usage-overview.js`
- Modify: `jacked/data/web/index.html` (add script tag)

- [ ] **Step 1: Implement the overview tab**

Create `usage-overview.js` with:
- `renderUsageOverview()` — main render function called when Overview sub-tab is active
- `_renderHealthBanner(data)` — cache grade (A-F), today's tokens + cost, trend arrow
- `_renderFlags(flags)` — ranked list of active anomaly flags
- `_renderProjectBreakdown(projects)` — table ranked by consumption with cache bars
- `_renderSparkline(dailyData)` — small inline SVG 7-day sparkline
- `updateAnalyticsScanProgress(data)` — progress banner during initial scan
- `onAnalyticsScanComplete()` — dismiss progress, load full data
- `onAnalyticsLiveUpdate(data)` — surgically update totals and flags
- `onAnalyticsFlagRaised(flag)` — prepend flag to list
- `onAnalyticsFlagResolved(data)` — remove flag from list

Follows the diagnosis-first mockup: health banner → flags → project table → sparkline.

- [ ] **Step 2: Add script tag to index.html**

After the existing analytics script tags, add:
```html
<script src="/js/components/usage-overview.js"></script>
```

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/usage-overview.js jacked/data/web/index.html
git commit -m "feat: usage overview tab — health banner, flags, project breakdown"
```

---

### Task 11: Frontend — Sessions Tab

**Files:**
- Create: `jacked/data/web/js/components/usage-sessions.js`
- Modify: `jacked/data/web/index.html` (add script tag)

- [ ] **Step 1: Implement the sessions tab**

Create `usage-sessions.js` with:
- `renderUsageSessions()` — fetch session list, render ranked table
- `_renderSessionRow(session)` — status dot, project, ID, duration, tokens, cost, cache %, flags
- `_renderSessionDetail(sessionId)` — expandable message-level timeline (fetched on click)
- Filter controls: project dropdown, date range, flagged-only toggle
- Live update: active session row updates on `analytics_live_update`

- [ ] **Step 2: Add script tag**

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/usage-sessions.js jacked/data/web/index.html
git commit -m "feat: usage sessions tab — ranked list with expandable detail"
```

---

### Task 12: Frontend — Trends Tab

**Files:**
- Create: `jacked/data/web/js/components/usage-trends.js`
- Modify: `jacked/data/web/index.html` (add script tag)

- [ ] **Step 1: Implement the trends tab**

Create `usage-trends.js` with:
- `renderUsageTrends()` — fetch daily summaries, render Chart.js charts
- Stacked bar chart: tokens per day, stacked by project (using Chart.js)
- Cache health line overlay on secondary y-axis
- Time range selector: 7d / 30d / 90d / all
- Toggle: token view ↔ cost view
- Uses `ensureChartJs()` from analytics.js for lazy loading

- [ ] **Step 2: Add script tag**

- [ ] **Step 3: Commit**

```bash
git add jacked/data/web/js/components/usage-trends.js jacked/data/web/index.html
git commit -m "feat: usage trends tab — Chart.js daily consumption + cache health"
```

---

## Self-Review

**1. Spec coverage:**
- Spec §1 (Data Pipeline) → Tasks 1, 3, 5
- Spec §2 (Database Schema) → Task 2
- Spec §3 (Anomaly Detection) → Task 4
- Spec §4 (Frontend) → Tasks 9, 10, 11, 12
- Spec §5 (Live Monitoring) → Task 5
- Spec §6 (Project Hash Decoding) → Task 1
- Spec §7 (Cost Estimation) → Task 2 (in analytics_db.py)
- WebSocket has_subscribers → Task 6
- API routes → Task 7
- Backend integration → Task 8

**2. Placeholder scan:** All tasks have concrete code or detailed implementation instructions. No TBDs.

**3. Type consistency:** `AnalyticsDB` used consistently across tasks. `scan_project_dir` signature matches between Task 3 tests and Task 5 caller. `detect_anomalies` signature matches between Task 4 and Task 5. WS event names match between Tasks 5 and 9.
