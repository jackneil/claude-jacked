# Analytics Dashboard & Security Profiles Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build a full observability gatekeeper dashboard with Chart.js visualizations and a security profile import/export system with review, auto-backup, and one-click undo.

**Architecture:** Two independent feature tracks. Track A (analytics) adds SQL aggregation queries in `db_analytics.py`, new API endpoints in `analytics.py`, and a 3-file JS dashboard. Track B (profiles) adds core logic in `profiles.py`, REST endpoints in `routes/profiles.py`, and UI in `profiles-panel.js`. Both use existing DB, routing, and Tailwind patterns.

**Tech Stack:** Python 3.11+, FastAPI, SQLite, Chart.js 4.x + chartjs-chart-matrix, vanilla JS, Tailwind CSS, Pydantic v2, `packaging` library.

---

## Track A: Analytics Dashboard

### Task 1: Add `packaging` dependency

**Files:**
- Modify: `pyproject.toml`

**Step 1: Add dependency**

In `pyproject.toml`, add `packaging>=23.0` to the `dependencies` list (around line 25-33, after `httpx`):

```toml
"packaging>=23.0",
```

**Step 2: Sync**

Run: `uv sync`

**Step 3: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "deps: add packaging for semver comparison"
```

---

### Task 2: Database analytics queries — KPI and time-series

**Files:**
- Create: `jacked/web/db_analytics.py`
- Test: `tests/unit/test_db_analytics.py`

**Step 1: Write failing tests**

Create `tests/unit/test_db_analytics.py`:

```python
"""Tests for db_analytics — SQL aggregation queries."""

import json
from datetime import datetime, timedelta, timezone

import pytest

from jacked.web.database import Database
from jacked.web.db_analytics import (
    get_kpi_totals,
    get_time_series,
)


def _insert_decision(db, decision="ALLOW", method="LOCAL", ts=None, session_id="s1", repo_path="/repo", command="git status", reason="safe", elapsed_ms=10.0):
    """Helper: insert a gatekeeper decision."""
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    with db._writer() as conn:
        conn.execute(
            "INSERT INTO gatekeeper_decisions (timestamp, command, decision, method, reason, elapsed_ms, session_id, repo_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, command, decision, method, reason, elapsed_ms, session_id, repo_path),
        )


class TestGetKpiTotals:
    def test_empty_db(self):
        db = Database(":memory:")
        result = get_kpi_totals(db, days=7)
        assert result["total_decisions"] == 0
        assert result["approval_rate"] == 0.0
        assert result["denials"] == 0
        assert result["api_evaluations"] == 0
        assert result["rule_coverage"] == 0.0

    def test_single_allow(self):
        db = Database(":memory:")
        _insert_decision(db, decision="ALLOW", method="LOCAL")
        result = get_kpi_totals(db, days=7)
        assert result["total_decisions"] == 1
        assert result["approval_rate"] == 100.0
        assert result["denials"] == 0
        assert result["api_evaluations"] == 0
        assert result["rule_coverage"] == 100.0

    def test_mixed_decisions(self):
        db = Database(":memory:")
        _insert_decision(db, decision="ALLOW", method="LOCAL")
        _insert_decision(db, decision="ALLOW", method="API")
        _insert_decision(db, decision="DENY", method="DENY_PATTERN")
        result = get_kpi_totals(db, days=7)
        assert result["total_decisions"] == 3
        assert result["approval_rate"] == pytest.approx(66.7, abs=0.1)
        assert result["denials"] == 1
        assert result["api_evaluations"] == 1
        assert result["rule_coverage"] == pytest.approx(66.7, abs=0.1)

    def test_respects_date_range(self):
        db = Database(":memory:")
        old_ts = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        _insert_decision(db, decision="ALLOW", method="LOCAL", ts=old_ts)
        _insert_decision(db, decision="ALLOW", method="LOCAL")
        result = get_kpi_totals(db, days=7)
        assert result["total_decisions"] == 1


class TestGetTimeSeries:
    def test_empty_db(self):
        db = Database(":memory:")
        result = get_time_series(db, days=7)
        assert result["granularity"] == "daily"
        assert result["buckets"] == []

    def test_single_entry_daily(self):
        db = Database(":memory:")
        _insert_decision(db)
        result = get_time_series(db, days=7)
        assert result["granularity"] == "daily"
        assert len(result["buckets"]) == 1
        assert result["buckets"][0]["allow"] == 1

    def test_hourly_for_short_range(self):
        db = Database(":memory:")
        _insert_decision(db)
        result = get_time_series(db, days=1)
        assert result["granularity"] == "hourly"

    def test_weekly_for_long_range(self):
        db = Database(":memory:")
        _insert_decision(db)
        result = get_time_series(db, days=60)
        assert result["granularity"] == "weekly"
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_db_analytics.py -v`
Expected: ImportError — `db_analytics` module does not exist

**Step 3: Write minimal implementation**

Create `jacked/web/db_analytics.py`:

```python
"""Gatekeeper analytics — SQL aggregation queries.

All queries use parameterized SQL. Each function takes a Database instance
and a ``days`` parameter for the date range filter.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jacked.web.database import Database


def _cutoff_iso(days: int) -> str:
    """UTC ISO timestamp for N days ago.

    >>> len(_cutoff_iso(7)) > 20
    True
    """
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _granularity(days: int) -> str:
    """Pick bucket granularity based on range.

    >>> _granularity(1)
    'hourly'
    >>> _granularity(7)
    'daily'
    >>> _granularity(90)
    'weekly'
    """
    if days < 2:
        return "hourly"
    if days <= 30:
        return "daily"
    return "weekly"


def _bucket_format(gran: str) -> str:
    """SQLite strftime format for a granularity level."""
    if gran == "hourly":
        return "%Y-%m-%dT%H"
    if gran == "daily":
        return "%Y-%m-%d"
    return "%Y-W%W"


def get_kpi_totals(db: Database, days: int = 7) -> dict:
    """Aggregate KPI values for the dashboard.

    Returns dict with: total_decisions, approval_rate, denials,
    api_evaluations, rule_coverage.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_kpi_totals(db, days=7)["total_decisions"]
    0
    """
    cutoff = _cutoff_iso(days)
    sql = """
        SELECT
            COUNT(*) as total,
            SUM(CASE WHEN decision = 'ALLOW' THEN 1 ELSE 0 END) as allows,
            SUM(CASE WHEN decision = 'DENY' THEN 1 ELSE 0 END) as denials,
            SUM(CASE WHEN method = 'API' OR method = 'CLI' THEN 1 ELSE 0 END) as api_evals
        FROM gatekeeper_decisions
        WHERE timestamp >= ?
    """
    with db._reader() as conn:
        row = conn.execute(sql, (cutoff,)).fetchone()

    total = row["total"] or 0
    allows = row["allows"] or 0
    denials = row["denials"] or 0
    api_evals = row["api_evals"] or 0

    approval_rate = round(allows / total * 100, 1) if total > 0 else 0.0
    local_decisions = total - api_evals
    rule_coverage = round(local_decisions / total * 100, 1) if total > 0 else 0.0

    return {
        "total_decisions": total,
        "approval_rate": approval_rate,
        "denials": denials,
        "api_evaluations": api_evals,
        "rule_coverage": rule_coverage,
    }


def get_time_series(db: Database, days: int = 7) -> dict:
    """Time-series decision counts bucketed by auto-adaptive granularity.

    Returns ``{"granularity": "daily", "buckets": [{"bucket": "2026-02-25", "allow": 5, ...}]}``.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_time_series(db, days=7)
    {'granularity': 'daily', 'buckets': []}
    """
    cutoff = _cutoff_iso(days)
    gran = _granularity(days)
    fmt = _bucket_format(gran)

    sql = f"""
        SELECT
            strftime(?, timestamp) as bucket,
            SUM(CASE WHEN decision = 'ALLOW' THEN 1 ELSE 0 END) as allow,
            SUM(CASE WHEN decision = 'DENY' THEN 1 ELSE 0 END) as deny,
            SUM(CASE WHEN decision = 'ASK_USER' THEN 1 ELSE 0 END) as ask_user,
            COUNT(*) as total
        FROM gatekeeper_decisions
        WHERE timestamp >= ?
        GROUP BY bucket
        ORDER BY bucket ASC
    """
    with db._reader() as conn:
        rows = conn.execute(sql, (fmt, cutoff)).fetchall()

    buckets = [
        {
            "bucket": row["bucket"],
            "allow": row["allow"],
            "deny": row["deny"],
            "ask_user": row["ask_user"],
            "total": row["total"],
        }
        for row in rows
    ]
    return {"granularity": gran, "buckets": buckets}
```

**Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_db_analytics.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add jacked/web/db_analytics.py tests/unit/test_db_analytics.py
git commit -m "feat(analytics): KPI totals and time-series SQL queries with tests"
```

---

### Task 3: Database analytics queries — heatmap, session risk, suggested rules, hot rules

**Files:**
- Modify: `jacked/web/db_analytics.py`
- Modify: `tests/unit/test_db_analytics.py`

**Step 1: Write failing tests**

Append to `tests/unit/test_db_analytics.py`:

```python
from jacked.web.db_analytics import (
    get_heatmap_raw,
    get_session_risk,
    get_suggested_rules,
    get_hot_rules,
    get_method_breakdown,
)


class TestGetMethodBreakdown:
    def test_empty_db(self):
        db = Database(":memory:")
        assert get_method_breakdown(db, days=7) == {}

    def test_counts_by_method(self):
        db = Database(":memory:")
        _insert_decision(db, method="LOCAL")
        _insert_decision(db, method="LOCAL")
        _insert_decision(db, method="API")
        result = get_method_breakdown(db, days=7)
        assert result["LOCAL"] == 2
        assert result["API"] == 1


class TestGetHeatmapRaw:
    def test_empty_db(self):
        db = Database(":memory:")
        assert get_heatmap_raw(db, days=7) == []

    def test_returns_timestamps(self):
        db = Database(":memory:")
        _insert_decision(db)
        result = get_heatmap_raw(db, days=7)
        assert len(result) == 1
        assert "timestamp" in result[0]


class TestGetSessionRisk:
    def test_empty_db(self):
        db = Database(":memory:")
        assert get_session_risk(db, days=7) == []

    def test_ranks_by_risk_score(self):
        db = Database(":memory:")
        # s1: 1 DENY_PATTERN (3 pts)
        _insert_decision(db, session_id="s1", decision="DENY", method="DENY_PATTERN")
        # s2: 2 DENY via API (4 pts)
        _insert_decision(db, session_id="s2", decision="DENY", method="API")
        _insert_decision(db, session_id="s2", decision="DENY", method="API")
        result = get_session_risk(db, days=7, limit=10)
        assert result[0]["session_id"] == "s2"
        assert result[0]["risk_score"] == 4
        assert result[1]["session_id"] == "s1"
        assert result[1]["risk_score"] == 3

    def test_respects_limit(self):
        db = Database(":memory:")
        for i in range(5):
            _insert_decision(db, session_id=f"s{i}", decision="DENY", method="API")
        result = get_session_risk(db, days=7, limit=2)
        assert len(result) == 2


class TestGetSuggestedRules:
    def test_empty_db(self):
        db = Database(":memory:")
        assert get_suggested_rules(db, days=7) == []

    def test_finds_repeated_api_patterns(self):
        db = Database(":memory:")
        for _ in range(3):
            _insert_decision(db, method="API", command="curl https://example.com", decision="ALLOW")
        result = get_suggested_rules(db, days=7, min_hits=3)
        assert len(result) == 1
        assert result[0]["command"] == "curl https://example.com"
        assert result[0]["count"] == 3
        assert result[0]["decision"] == "ALLOW"

    def test_ignores_below_threshold(self):
        db = Database(":memory:")
        for _ in range(2):
            _insert_decision(db, method="API", command="curl https://example.com", decision="ALLOW")
        assert get_suggested_rules(db, days=7, min_hits=3) == []


class TestGetHotRules:
    def test_empty_db(self):
        db = Database(":memory:")
        assert get_hot_rules(db, days=7) == []

    def test_ranks_by_count(self):
        db = Database(":memory:")
        for _ in range(5):
            _insert_decision(db, method="LOCAL")
        for _ in range(2):
            _insert_decision(db, method="API")
        result = get_hot_rules(db, days=7, limit=10)
        assert result[0]["method"] == "LOCAL"
        assert result[0]["count"] == 5
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_db_analytics.py -v`
Expected: ImportError for new functions

**Step 3: Implement**

Append to `jacked/web/db_analytics.py`:

```python
def get_method_breakdown(db: Database, days: int = 7) -> dict[str, int]:
    """Decision counts grouped by method.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_method_breakdown(db, days=7)
    {}
    """
    cutoff = _cutoff_iso(days)
    sql = """
        SELECT method, COUNT(*) as count
        FROM gatekeeper_decisions
        WHERE timestamp >= ?
        GROUP BY method
        ORDER BY count DESC
    """
    with db._reader() as conn:
        rows = conn.execute(sql, (cutoff,)).fetchall()
    return {row["method"]: row["count"] for row in rows}


def get_heatmap_raw(db: Database, days: int = 7) -> list[dict]:
    """Raw UTC timestamps for client-side heatmap bucketing.

    Returns list of ``{"timestamp": "..."}`` dicts. Bucketing into
    day-of-week × hour-of-day happens in the browser for correct
    local timezone.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_heatmap_raw(db, days=7)
    []
    """
    cutoff = _cutoff_iso(days)
    sql = "SELECT timestamp FROM gatekeeper_decisions WHERE timestamp >= ?"
    with db._reader() as conn:
        rows = conn.execute(sql, (cutoff,)).fetchall()
    return [{"timestamp": row["timestamp"]} for row in rows]


def get_session_risk(db: Database, days: int = 7, limit: int = 20) -> list[dict]:
    """Sessions ranked by weighted risk score.

    Scoring: DENY_PATTERN=3, DENY via API/CLI=2, ASK_USER=1.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_session_risk(db, days=7)
    []
    """
    cutoff = _cutoff_iso(days)
    sql = """
        SELECT
            session_id,
            repo_path,
            MIN(timestamp) as first_seen,
            COUNT(*) as total_decisions,
            SUM(CASE WHEN decision = 'DENY' THEN 1 ELSE 0 END) as denials,
            SUM(
                CASE
                    WHEN method = 'DENY_PATTERN' THEN 3
                    WHEN decision = 'DENY' AND method IN ('API', 'CLI') THEN 2
                    WHEN decision = 'ASK_USER' THEN 1
                    ELSE 0
                END
            ) as risk_score
        FROM gatekeeper_decisions
        WHERE timestamp >= ? AND session_id IS NOT NULL
        GROUP BY session_id
        HAVING risk_score > 0
        ORDER BY risk_score DESC
        LIMIT ?
    """
    with db._reader() as conn:
        rows = conn.execute(sql, (cutoff, limit)).fetchall()
    return [dict(row) for row in rows]


def get_suggested_rules(db: Database, days: int = 7, min_hits: int = 3) -> list[dict]:
    """Commands evaluated by API 3+ times with the same decision.

    These are candidates for local allow/deny rules.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_suggested_rules(db, days=7)
    []
    """
    cutoff = _cutoff_iso(days)
    sql = """
        SELECT command, decision, COUNT(*) as count
        FROM gatekeeper_decisions
        WHERE timestamp >= ? AND method IN ('API', 'CLI') AND command IS NOT NULL
        GROUP BY command, decision
        HAVING COUNT(*) >= ?
        ORDER BY count DESC
        LIMIT 20
    """
    with db._reader() as conn:
        rows = conn.execute(sql, (cutoff, min_hits)).fetchall()
    return [dict(row) for row in rows]


def get_hot_rules(db: Database, days: int = 7, limit: int = 10) -> list[dict]:
    """Most-triggered decision methods ranked by frequency.

    >>> from jacked.web.database import Database
    >>> db = Database(":memory:")
    >>> get_hot_rules(db, days=7)
    []
    """
    cutoff = _cutoff_iso(days)
    sql = """
        SELECT method, COUNT(*) as count
        FROM gatekeeper_decisions
        WHERE timestamp >= ?
        GROUP BY method
        ORDER BY count DESC
        LIMIT ?
    """
    with db._reader() as conn:
        rows = conn.execute(sql, (cutoff, limit)).fetchall()
    return [dict(row) for row in rows]
```

**Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_db_analytics.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add jacked/web/db_analytics.py tests/unit/test_db_analytics.py
git commit -m "feat(analytics): heatmap, session risk, suggested rules, hot rules queries"
```

---

### Task 4: Analytics API endpoints

**Files:**
- Modify: `jacked/api/routes/analytics.py` (replace gatekeeper_stats, add new endpoints)

**Step 1: Write failing tests**

Create `tests/unit/test_analytics_api.py`:

```python
"""Tests for analytics API endpoints."""

import pytest
from unittest.mock import MagicMock
from fastapi.testclient import TestClient

from jacked.web.database import Database
from jacked.api.routes.analytics import router
from fastapi import FastAPI


@pytest.fixture
def app_with_db():
    """Create test app with in-memory DB."""
    app = FastAPI()
    app.include_router(router, prefix="/api/analytics")
    db = Database(":memory:")
    app.state.db = db
    return app, db


@pytest.fixture
def client(app_with_db):
    app, db = app_with_db
    return TestClient(app), db


class TestGatekeeperDashboard:
    def test_returns_all_sections(self, client):
        c, db = client
        resp = c.get("/api/analytics/gatekeeper-dashboard?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "kpi" in data
        assert "time_series" in data
        assert "method_breakdown" in data

    def test_empty_db(self, client):
        c, db = client
        resp = c.get("/api/analytics/gatekeeper-dashboard?days=7")
        data = resp.json()
        assert data["kpi"]["total_decisions"] == 0
        assert data["time_series"]["buckets"] == []


class TestHeatmap:
    def test_returns_timestamps(self, client):
        c, db = client
        resp = c.get("/api/analytics/gatekeeper-heatmap?days=7")
        assert resp.status_code == 200
        assert "timestamps" in resp.json()


class TestSessionRisk:
    def test_returns_sessions(self, client):
        c, db = client
        resp = c.get("/api/analytics/gatekeeper-sessions?days=7")
        assert resp.status_code == 200
        assert "sessions" in resp.json()


class TestRuleIntelligence:
    def test_returns_sections(self, client):
        c, db = client
        resp = c.get("/api/analytics/gatekeeper-rules?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert "suggested" in data
        assert "hot" in data
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_analytics_api.py -v`
Expected: FAIL — endpoints don't exist yet

**Step 3: Add new endpoints to `jacked/api/routes/analytics.py`**

Add these new endpoints after the existing `gatekeeper_stats` function (keep existing endpoints working). Import `db_analytics` at top:

```python
from jacked.web import db_analytics
```

Add new endpoints:

```python
@router.get("/gatekeeper-dashboard")
async def gatekeeper_dashboard(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Combined KPI + time-series + method breakdown for the dashboard."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()
    return {
        "kpi": db_analytics.get_kpi_totals(db, days),
        "time_series": db_analytics.get_time_series(db, days),
        "method_breakdown": db_analytics.get_method_breakdown(db, days),
    }


@router.get("/gatekeeper-heatmap")
async def gatekeeper_heatmap(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Raw UTC timestamps for client-side heatmap bucketing."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()
    return {"timestamps": db_analytics.get_heatmap_raw(db, days)}


@router.get("/gatekeeper-sessions")
async def gatekeeper_sessions(request: Request, days: int = Query(default=7, ge=1, le=365), limit: int = Query(default=20, ge=1, le=100)):
    """Sessions ranked by risk score."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()
    return {"sessions": db_analytics.get_session_risk(db, days, limit)}


@router.get("/gatekeeper-rules")
async def gatekeeper_rules(request: Request, days: int = Query(default=7, ge=1, le=365)):
    """Suggested rules and hot rules for rule intelligence section."""
    db = _get_db(request)
    if db is None:
        return _db_unavailable()
    return {
        "suggested": db_analytics.get_suggested_rules(db, days),
        "hot": db_analytics.get_hot_rules(db, days),
    }
```

**Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_analytics_api.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add jacked/api/routes/analytics.py tests/unit/test_analytics_api.py
git commit -m "feat(analytics): dashboard, heatmap, sessions, rules API endpoints"
```

---

### Task 5: Bundle Chart.js as static assets

**Files:**
- Create: `jacked/data/web/js/vendor/chart.js` (Chart.js 4.x UMD bundle)
- Create: `jacked/data/web/js/vendor/chartjs-chart-matrix.js` (matrix plugin)

**Step 1: Download Chart.js and matrix plugin**

```bash
cd /Users/jack.neil/Github/claude-jacked/jacked/data/web/js
mkdir -p vendor
curl -L "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js" -o vendor/chart.umd.min.js
curl -L "https://cdn.jsdelivr.net/npm/chartjs-chart-matrix@2/dist/chartjs-chart-matrix.min.js" -o vendor/chartjs-chart-matrix.min.js
```

**Step 2: Verify files downloaded**

```bash
ls -la jacked/data/web/js/vendor/
# Expect chart.umd.min.js (~200KB) and chartjs-chart-matrix.min.js (~15KB)
```

**Step 3: Commit**

```bash
git add jacked/data/web/js/vendor/
git commit -m "vendor: bundle Chart.js 4.x + matrix plugin for offline analytics"
```

---

### Task 6: Analytics dashboard JS — layout and KPI cards

**Files:**
- Modify: `jacked/data/web/js/components/analytics.js` (replace existing content)
- Modify: `jacked/data/web/index.html` (add new script tags)

**Step 1: Rewrite `analytics.js` as the dashboard coordinator**

Replace the entire content of `jacked/data/web/js/components/analytics.js` with a new version that:
- Keeps the same `renderAnalytics()` / `bindAnalyticsEvents()` function names (app.js expects these)
- Adds date range dropdown (24h, 7d, 30d, 90d, 1y) replacing the old 3-button range
- Renders 5 KPI cards in a grid
- Provides collapsible section containers for charts/tables (rendered by companion files)
- Lazy-loads Chart.js from `/js/vendor/chart.umd.min.js` on first render
- Persists collapsed state in `localStorage`
- Delegates to `renderAnalyticsCharts()` and `renderAnalyticsTables()` from companion files

The KPI cards should show: Total Decisions (with trend arrow), Approval Rate % (color-coded), Denials (red), Rule Coverage %, API Evaluations.

When `total_decisions == 0`, show a single empty state: "No gatekeeper data yet — decisions will appear here once the security gatekeeper processes commands."

**Step 2: Add script tags to `index.html`**

Add before the closing `</body>`, after the `analytics.js` script tag (line 101):

```html
<script src="/js/components/analytics-charts.js"></script>
<script src="/js/components/analytics-tables.js"></script>
```

**Step 3: Manual verification**

Run: `jacked` to start the web dashboard, navigate to `#analytics`. Verify KPI cards render with real data or empty state.

**Step 4: Commit**

```bash
git add jacked/data/web/js/components/analytics.js jacked/data/web/index.html
git commit -m "feat(analytics): dashboard layout, KPI cards, date range selector"
```

---

### Task 7: Analytics charts JS — time-series, doughnut, heatmap

**Files:**
- Create: `jacked/data/web/js/components/analytics-charts.js`

**Step 1: Create the charts component**

This file provides `renderAnalyticsCharts(data)` called by the main `analytics.js`. It should:

- **Time-series stacked line chart:** Uses Chart.js `type: 'line'` with 3 datasets (ALLOW=green, DENY=red, ASK_USER=yellow). X-axis = bucket timestamps, stacked mode. Includes click handler: clicking a data point navigates to `#logs-gatekeeper?decision=X&date_from=Y&from=analytics`.

- **Doughnut chart:** Uses Chart.js `type: 'doughnut'` for method breakdown. Color-coded by method. Click handler: clicking a segment navigates to `#logs-gatekeeper?method=X&from=analytics`.

- **Heatmap:** Uses `chartjs-chart-matrix` with a 7×24 grid. Receives raw UTC timestamps from API, buckets them in JS using `new Date(ts).getDay()` and `new Date(ts).getHours()` for correct local timezone. Color scale: slate-800 (0) → blue-500 (mid) → red-500 (high).

All charts should have a loading skeleton state and handle empty data gracefully.

**Step 2: Manual verification**

Navigate to `#analytics` with data in the DB. Verify all 3 charts render. Click a doughnut segment → should navigate to filtered logs.

**Step 3: Commit**

```bash
git add jacked/data/web/js/components/analytics-charts.js
git commit -m "feat(analytics): time-series, doughnut, and heatmap Chart.js components"
```

---

### Task 8: Analytics tables JS — session risk, suggested rules, hot rules

**Files:**
- Create: `jacked/data/web/js/components/analytics-tables.js`

**Step 1: Create the tables component**

This file provides `renderAnalyticsTables(sessionData, rulesData)` called by the main `analytics.js`. It should:

- **Session risk table:** Columns: First Seen, Repo, Decisions, Denials, Risk Score. Color-code risk (red >=10, yellow >=5, green <5). Click row → `#logs-gatekeeper?session_id=X&from=analytics`. "Load more" button that fetches with `limit=20&offset=N`.

- **Suggested rules list:** Shows command pattern, decision, hit count. "Add Rule" button per row that calls `POST /api/claude-settings/permissions/rule` (reusing existing endpoint). Before adding, check if pattern is in `_DANGEROUS_PATTERNS` equivalent client-side — show SweetAlert2 warning for broad patterns containing `*:*`.

- **Hot rules table:** Method name + count, simple ranked table.

All sections are wrapped in collapsible containers (defaulting to collapsed). Use `escapeHtml()` for all user-provided data.

**Step 2: Manual verification**

Navigate to `#analytics`. Verify tables render. Click "Add Rule" on a suggested rule → verify it calls the API. Click a session row → verify navigation to filtered logs with "Back to Dashboard" link.

**Step 3: Commit**

```bash
git add jacked/data/web/js/components/analytics-tables.js
git commit -m "feat(analytics): session risk, suggested rules, hot rules table components"
```

---

### Task 9: Update logs-gatekeeper.js to support drill-down params

**Files:**
- Modify: `jacked/data/web/js/components/logs-gatekeeper.js`

**Step 1: Add hash param parsing on mount**

At the top of the component's init/bind function, parse hash params:

```javascript
// Parse drill-down params from analytics
const hashParams = new URLSearchParams(window.location.hash.split('?')[1] || '');
if (hashParams.get('decision')) { /* set filter */ }
if (hashParams.get('method')) { /* set filter */ }
if (hashParams.get('session_id')) { /* set filter */ }
if (hashParams.get('from') === 'analytics') {
    // Show "Back to Dashboard" link at top
}
```

This should set the existing filter variables (`logsMethodFilter`, etc.) from the hash params before the initial data load.

**Step 2: Add "Back to Dashboard" link**

When `from=analytics` param is present, prepend a link at the top of the logs view:

```html
<a href="#analytics" class="text-blue-400 hover:text-blue-300 text-sm mb-3 inline-block">
    ← Back to Dashboard
</a>
```

**Step 3: Manual verification**

Navigate to `#logs-gatekeeper?decision=DENY&from=analytics`. Verify filters are pre-applied and "Back to Dashboard" link appears.

**Step 4: Commit**

```bash
git add jacked/data/web/js/components/logs-gatekeeper.js
git commit -m "feat(analytics): drill-down hash param support in gatekeeper logs"
```

---

## Track B: Security Profiles

### Task 10: Core profile logic — validation, export, import

**Files:**
- Create: `jacked/profiles.py`
- Test: `tests/unit/test_profiles.py`

**Step 1: Write failing tests**

Create `tests/unit/test_profiles.py`:

```python
"""Tests for security profile import/export logic."""

import json
import os
from pathlib import Path

import pytest

from jacked.profiles import (
    ProfileSchema,
    validate_profile,
    export_profile,
    import_profile,
    list_profiles,
    delete_profile,
    restore_backup,
    get_latest_backup,
    PROFILE_DIR_NAME,
)
from jacked.web.database import Database


class TestProfileSchema:
    def test_valid_profile(self):
        data = {
            "name": "test-profile",
            "description": "A test profile",
            "author": "tester",
            "jacked_version": "0.8.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {
                "model": "haiku",
                "eval_method": "api",
            },
        }
        profile = ProfileSchema(**data)
        assert profile.name == "test-profile"

    def test_rejects_unknown_keys(self):
        data = {
            "name": "test",
            "description": "",
            "author": "",
            "jacked_version": "0.8.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {"model": "haiku"},
            "evil_key": "injection",
        }
        with pytest.raises(Exception):  # Pydantic ValidationError
            ProfileSchema(**data)

    def test_rejects_long_name(self):
        data = {
            "name": "a" * 100,
            "description": "",
            "author": "",
            "jacked_version": "0.8.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {},
        }
        with pytest.raises(Exception):
            ProfileSchema(**data)

    def test_rejects_name_with_path_traversal(self):
        data = {
            "name": "../evil",
            "description": "",
            "author": "",
            "jacked_version": "0.8.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {},
        }
        with pytest.raises(Exception):
            ProfileSchema(**data)

    def test_rejects_long_description(self):
        data = {
            "name": "test",
            "description": "x" * 600,
            "author": "",
            "jacked_version": "0.8.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {},
        }
        with pytest.raises(Exception):
            ProfileSchema(**data)

    def test_rejects_invalid_model(self):
        data = {
            "name": "test",
            "description": "",
            "author": "",
            "jacked_version": "0.8.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {"model": "gpt-4"},
        }
        with pytest.raises(Exception):
            ProfileSchema(**data)


class TestValidateProfile:
    def test_flags_dangerous_allow_pattern(self):
        data = {
            "name": "test",
            "description": "",
            "author": "",
            "jacked_version": "0.1.0",
            "rules": {"allow": ["Bash(*:*)"], "deny": [], "ask": []},
            "gatekeeper_config": {},
        }
        warnings = validate_profile(data)
        assert any("dangerous" in w.lower() or "broad" in w.lower() for w in warnings)

    def test_flags_path_safety_disabled(self):
        data = {
            "name": "test",
            "description": "",
            "author": "",
            "jacked_version": "0.1.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {
                "path_safety": {"enabled": False},
            },
        }
        warnings = validate_profile(data)
        assert any("path_safety" in w.lower() for w in warnings)

    def test_flags_root_allowed_path(self):
        data = {
            "name": "test",
            "description": "",
            "author": "",
            "jacked_version": "0.1.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {
                "path_safety": {"enabled": True, "allowed_paths": ["/"]},
            },
        }
        warnings = validate_profile(data)
        assert any("allowed_paths" in w.lower() or "/" in w for w in warnings)

    def test_rejects_higher_version(self):
        data = {
            "name": "test",
            "description": "",
            "author": "",
            "jacked_version": "99.0.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {},
        }
        with pytest.raises(ValueError, match="version"):
            validate_profile(data)


class TestExportImport:
    def test_export_creates_file(self, tmp_path):
        db = Database(":memory:")
        path = export_profile(
            name="my-profile",
            description="test export",
            author="tester",
            db=db,
            settings_json={},
            profiles_dir=tmp_path,
        )
        assert path.exists()
        data = json.loads(path.read_text())
        assert data["name"] == "my-profile"

    def test_export_strips_api_key(self, tmp_path):
        db = Database(":memory:")
        db.set_setting("gatekeeper.api_key", '"sk-secret-123"')
        path = export_profile(
            name="no-key",
            description="",
            author="",
            db=db,
            settings_json={},
            profiles_dir=tmp_path,
        )
        data = json.loads(path.read_text())
        assert "api_key" not in json.dumps(data["gatekeeper_config"])

    def test_import_creates_backup(self, tmp_path):
        db = Database(":memory:")
        backup_dir = tmp_path / ".backups"
        profile_data = {
            "name": "imported",
            "description": "",
            "author": "",
            "jacked_version": "0.1.0",
            "rules": {"allow": [], "deny": [], "ask": []},
            "gatekeeper_config": {"model": "haiku"},
        }
        import_profile(
            profile_data=profile_data,
            db=db,
            settings_json={},
            write_settings_fn=lambda s: None,
            profiles_dir=tmp_path,
            backup_dir=backup_dir,
        )
        assert any(backup_dir.iterdir())

    def test_roundtrip(self, tmp_path):
        db = Database(":memory:")
        db.set_setting("gatekeeper.model", '"sonnet"')
        settings = {"permissions": {"allow": ["Read(*)"], "deny": [], "ask": []}}

        path = export_profile(
            name="roundtrip",
            description="test",
            author="tester",
            db=db,
            settings_json=settings,
            profiles_dir=tmp_path,
        )
        exported = json.loads(path.read_text())
        assert exported["rules"]["allow"] == ["Read(*)"]
        assert exported["gatekeeper_config"]["model"] == "sonnet"


class TestListAndDelete:
    def test_list_empty(self, tmp_path):
        assert list_profiles(tmp_path) == []

    def test_list_and_delete(self, tmp_path):
        db = Database(":memory:")
        export_profile("p1", "", "", db, {}, tmp_path)
        export_profile("p2", "", "", db, {}, tmp_path)
        profiles = list_profiles(tmp_path)
        assert len(profiles) == 2
        delete_profile("p1", tmp_path)
        assert len(list_profiles(tmp_path)) == 1


class TestRestoreBackup:
    def test_restore_latest(self, tmp_path):
        backup_dir = tmp_path / ".backups"
        backup_dir.mkdir()
        # Write a fake backup
        backup_file = backup_dir / "2026-02-25T120000.json"
        backup_data = {
            "settings": {"permissions": {"allow": ["Read(*)"]}},
            "gatekeeper_settings": {"model": "haiku"},
        }
        backup_file.write_text(json.dumps(backup_data))

        latest = get_latest_backup(backup_dir)
        assert latest is not None
        assert "settings" in json.loads(latest.read_text())
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_profiles.py -v`
Expected: ImportError

**Step 3: Implement `jacked/profiles.py`**

```python
"""Security profile management — export, import, validate, backup, restore.

Profiles bundle permission rules + gatekeeper config into shareable JSON files.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from packaging.version import Version
from pydantic import BaseModel, Field, field_validator, model_config

import jacked

PROFILE_DIR_NAME = "profiles"
BACKUP_DIR_NAME = ".backups"

# Dangerous permission patterns — mirrors permissions.py._DANGEROUS_PATTERNS
_DANGEROUS_PATTERNS = {
    "Bash(*:*)", "Bash(rm:*)", "Bash(sudo:*)",
    "Bash(rm -rf:*)", "Bash(sh:*)", "Bash(bash:*)",
}

# Allowlisted gatekeeper config keys (never read api_key)
_CONFIG_KEYS = ("gatekeeper.model", "gatekeeper.eval_method",
                "gatekeeper.command_categories", "gatekeeper.path_safety",
                "gatekeeper.tools")

_VALID_MODELS = {"haiku", "sonnet", "opus"}
_VALID_EVAL_METHODS = {"api", "local", "hybrid"}
_VALID_CATEGORY_MODES = {"allow", "evaluate", "ask", "deny"}
_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9 -]{0,62}[a-zA-Z0-9]$|^[a-zA-Z0-9]$")


class GatekeeperConfig(BaseModel):
    model_config = {"extra": "forbid"}

    model: Optional[str] = None
    eval_method: Optional[str] = None
    command_categories: Optional[dict[str, str]] = None
    path_safety: Optional[dict[str, Any]] = None
    tool_toggles: Optional[dict[str, bool]] = None

    @field_validator("model")
    @classmethod
    def validate_model(cls, v):
        if v is not None and v not in _VALID_MODELS:
            raise ValueError(f"model must be one of {_VALID_MODELS}")
        return v

    @field_validator("eval_method")
    @classmethod
    def validate_eval_method(cls, v):
        if v is not None and v not in _VALID_EVAL_METHODS:
            raise ValueError(f"eval_method must be one of {_VALID_EVAL_METHODS}")
        return v

    @field_validator("command_categories")
    @classmethod
    def validate_categories(cls, v):
        if v is not None:
            for cat, mode in v.items():
                if mode not in _VALID_CATEGORY_MODES:
                    raise ValueError(f"Category '{cat}' has invalid mode '{mode}'")
        return v


class RulesSchema(BaseModel):
    model_config = {"extra": "forbid"}

    allow: list[str] = []
    deny: list[str] = []
    ask: list[str] = []


class ProfileSchema(BaseModel):
    model_config = {"extra": "forbid"}

    name: str = Field(..., min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    author: str = Field(default="", max_length=100)
    jacked_version: str = ""
    created_at: str = ""
    rules: RulesSchema = RulesSchema()
    gatekeeper_config: GatekeeperConfig = GatekeeperConfig()

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if ".." in v or "/" in v or "\\" in v or "\0" in v:
            raise ValueError("Name contains invalid characters")
        if not _NAME_RE.match(v):
            raise ValueError("Name must be alphanumeric with dashes/spaces")
        return v


def _name_to_filename(name: str) -> str:
    """Convert profile name to safe filename.

    >>> _name_to_filename("My Cool Profile")
    'my-cool-profile.json'
    >>> _name_to_filename("simple")
    'simple.json'
    """
    return re.sub(r"[^a-z0-9-]", "-", name.lower()).strip("-") + ".json"


def validate_profile(data: dict) -> list[str]:
    """Validate profile data and return warnings. Raises ValueError for blockers.

    >>> validate_profile({"name": "t", "jacked_version": "0.1.0", "rules": {"allow": [], "deny": [], "ask": []}, "gatekeeper_config": {}})
    []
    """
    warnings: list[str] = []

    # Version check
    profile_ver = data.get("jacked_version", "0.0.0")
    if profile_ver:
        try:
            if Version(profile_ver) > Version(jacked.__version__):
                raise ValueError(
                    f"Profile version {profile_ver} is newer than running jacked {jacked.__version__}"
                )
        except Exception as e:
            if "version" in str(e).lower():
                raise
            pass  # Unparseable version, skip

    # Check rules for dangerous patterns
    rules = data.get("rules", {})
    for list_name in ("allow", "deny", "ask"):
        for pattern in rules.get(list_name, []):
            if pattern in _DANGEROUS_PATTERNS:
                warnings.append(f"Dangerous {list_name} pattern: {pattern} — broad wildcard")

    # Check gatekeeper config
    gc = data.get("gatekeeper_config", {})
    ps = gc.get("path_safety", {})
    if isinstance(ps, dict):
        if ps.get("enabled") is False:
            warnings.append("path_safety is DISABLED — all file access allowed")
        for p in ps.get("allowed_paths", []):
            if p in ("/", "/*", "/etc"):
                warnings.append(f"path_safety.allowed_paths contains '{p}' — overly broad")

    # Check command categories
    cats = gc.get("command_categories", {})
    if isinstance(cats, dict):
        dangerous_cats = {"network", "system", "destructive"}
        for cat, mode in cats.items():
            if cat in dangerous_cats and mode == "allow":
                warnings.append(f"command_categories.{cat} set to 'allow' — risky")

    return warnings


def export_profile(
    name: str,
    description: str,
    author: str,
    db: Any,
    settings_json: dict,
    profiles_dir: Path,
) -> Path:
    """Export current config as a named profile.

    Reads gatekeeper config by allowlisted keys only. Never includes api_key.
    """
    profiles_dir.mkdir(parents=True, exist_ok=True)

    # Build gatekeeper config by allowlist
    gc: dict[str, Any] = {}
    for key in _CONFIG_KEYS:
        raw = db.get_setting(key)
        if raw is not None:
            short_key = key.replace("gatekeeper.", "")
            if short_key == "tools":
                short_key = "tool_toggles"
            try:
                gc[short_key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                gc[short_key] = raw

    profile = {
        "name": name,
        "description": description,
        "author": author,
        "jacked_version": jacked.__version__,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "rules": settings_json.get("permissions", {"allow": [], "deny": [], "ask": []}),
        "gatekeeper_config": gc,
    }

    filename = _name_to_filename(name)
    path = profiles_dir / filename
    path.write_text(json.dumps(profile, indent=2))
    return path


def _create_backup(db: Any, settings_json: dict, backup_dir: Path) -> Path:
    """Snapshot current config before an import."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H%M%S")
    backup = {
        "settings": settings_json,
        "gatekeeper_settings": {},
    }
    for key in _CONFIG_KEYS:
        raw = db.get_setting(key)
        if raw is not None:
            short_key = key.replace("gatekeeper.", "")
            try:
                backup["gatekeeper_settings"][short_key] = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                backup["gatekeeper_settings"][short_key] = raw

    path = backup_dir / f"{ts}.json"
    path.write_text(json.dumps(backup, indent=2))
    return path


def import_profile(
    profile_data: dict,
    db: Any,
    settings_json: dict,
    write_settings_fn: Callable[[dict], None],
    profiles_dir: Path,
    backup_dir: Path,
) -> Path:
    """Import a profile: validate, backup, apply.

    Returns the backup file path for undo.
    """
    # Validate schema
    ProfileSchema(**profile_data)

    # Create backup
    backup_path = _create_backup(db, settings_json, backup_dir)

    # Apply rules
    new_rules = profile_data.get("rules", {})
    settings_json["permissions"] = {
        "allow": new_rules.get("allow", []),
        "deny": new_rules.get("deny", []),
        "ask": new_rules.get("ask", []),
    }
    write_settings_fn(settings_json)

    # Apply gatekeeper config
    gc = profile_data.get("gatekeeper_config", {})
    key_map = {
        "model": "gatekeeper.model",
        "eval_method": "gatekeeper.eval_method",
        "command_categories": "gatekeeper.command_categories",
        "path_safety": "gatekeeper.path_safety",
        "tool_toggles": "gatekeeper.tools",
    }
    for field, db_key in key_map.items():
        if field in gc and gc[field] is not None:
            val = gc[field]
            db.set_setting(db_key, json.dumps(val) if not isinstance(val, str) else val)

    return backup_path


def list_profiles(profiles_dir: Path) -> list[dict]:
    """List all saved profiles."""
    if not profiles_dir.exists():
        return []
    profiles = []
    for f in sorted(profiles_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text())
            profiles.append({
                "name": data.get("name", f.stem),
                "description": data.get("description", ""),
                "author": data.get("author", ""),
                "jacked_version": data.get("jacked_version", ""),
                "created_at": data.get("created_at", ""),
                "filename": f.name,
            })
        except (json.JSONDecodeError, OSError):
            continue
    return profiles


def delete_profile(name: str, profiles_dir: Path) -> bool:
    """Delete a profile by name."""
    filename = _name_to_filename(name)
    path = profiles_dir / filename
    if path.exists():
        path.unlink()
        return True
    return False


def get_latest_backup(backup_dir: Path) -> Optional[Path]:
    """Get the most recent backup file."""
    if not backup_dir.exists():
        return None
    backups = sorted(backup_dir.glob("*.json"), reverse=True)
    return backups[0] if backups else None


def restore_backup(
    backup_path: Path,
    db: Any,
    write_settings_fn: Callable[[dict], None],
) -> None:
    """Restore config from a backup file."""
    data = json.loads(backup_path.read_text())

    # Restore settings.json
    write_settings_fn(data.get("settings", {}))

    # Restore gatekeeper settings
    gs = data.get("gatekeeper_settings", {})
    key_map = {
        "model": "gatekeeper.model",
        "eval_method": "gatekeeper.eval_method",
        "command_categories": "gatekeeper.command_categories",
        "path_safety": "gatekeeper.path_safety",
        "tools": "gatekeeper.tools",
    }
    for field, db_key in key_map.items():
        if field in gs:
            val = gs[field]
            db.set_setting(db_key, json.dumps(val) if not isinstance(val, str) else val)
```

**Step 4: Run tests**

Run: `uv run python -m pytest tests/unit/test_profiles.py -v`
Expected: All PASS

**Step 5: Commit**

```bash
git add jacked/profiles.py tests/unit/test_profiles.py
git commit -m "feat(profiles): core profile logic — schema, validate, export, import, backup, restore"
```

---

### Task 11: Profiles API endpoints

**Files:**
- Create: `jacked/api/routes/profiles.py`
- Modify: `jacked/api/main.py` (register router)

**Step 1: Write failing tests**

Create `tests/unit/test_profiles_api.py`:

```python
"""Tests for profiles API endpoints."""

import json
import pytest
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

from jacked.web.database import Database
from jacked.api.routes.profiles import router, _get_profiles_dir, _get_backup_dir


@pytest.fixture
def app_with_db(tmp_path):
    app = FastAPI()
    app.include_router(router, prefix="/api/profiles")
    db = Database(":memory:")
    app.state.db = db

    profiles_dir = tmp_path / "profiles"
    backup_dir = tmp_path / "profiles" / ".backups"

    with patch.object(
        __import__("jacked.api.routes.profiles", fromlist=["_get_profiles_dir"]),
        "_get_profiles_dir", return_value=profiles_dir
    ), patch.object(
        __import__("jacked.api.routes.profiles", fromlist=["_get_backup_dir"]),
        "_get_backup_dir", return_value=backup_dir
    ):
        yield TestClient(app), db, profiles_dir, backup_dir


class TestListProfiles:
    def test_empty(self, app_with_db):
        client, db, pdir, bdir = app_with_db
        resp = client.get("/api/profiles/")
        assert resp.status_code == 200
        assert resp.json()["profiles"] == []


class TestExportProfile:
    def test_export_creates_file(self, app_with_db):
        client, db, pdir, bdir = app_with_db
        resp = client.post("/api/profiles/export", json={
            "name": "test-export",
            "description": "testing",
            "author": "tester",
        })
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


class TestValidateProfile:
    def test_validate_returns_warnings(self, app_with_db):
        client, db, pdir, bdir = app_with_db
        resp = client.post("/api/profiles/validate", json={
            "name": "test",
            "description": "",
            "author": "",
            "jacked_version": "0.1.0",
            "rules": {"allow": ["Bash(*:*)"], "deny": [], "ask": []},
            "gatekeeper_config": {},
        })
        assert resp.status_code == 200
        assert len(resp.json()["warnings"]) > 0


class TestDeleteProfile:
    def test_delete_nonexistent(self, app_with_db):
        client, db, pdir, bdir = app_with_db
        resp = client.delete("/api/profiles/nonexistent")
        assert resp.status_code == 404
```

**Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_profiles_api.py -v`
Expected: ImportError

**Step 3: Implement `jacked/api/routes/profiles.py`**

```python
"""Profile management API routes — export, import, validate, list, delete, restore."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from jacked import profiles as prof

router = APIRouter()


def _get_profiles_dir() -> Path:
    return Path.home() / ".claude" / "jacked" / prof.PROFILE_DIR_NAME


def _get_backup_dir() -> Path:
    return _get_profiles_dir() / prof.BACKUP_DIR_NAME


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


def _read_settings_json() -> dict:
    """Read ~/.claude/settings.json."""
    path = Path.home() / ".claude" / "settings.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _write_settings_json(data: dict) -> None:
    """Write ~/.claude/settings.json atomically."""
    import tempfile
    path = Path.home() / ".claude" / "settings.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(path)


class ExportRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=64)
    description: str = ""
    author: str = ""


@router.get("/")
async def list_all(request: Request):
    return {"profiles": prof.list_profiles(_get_profiles_dir())}


@router.post("/export")
async def export_profile(body: ExportRequest, request: Request):
    db = _get_db(request)
    if db is None:
        return JSONResponse(status_code=503, content={"error": "DB unavailable"})
    settings = _read_settings_json()
    path = prof.export_profile(
        name=body.name,
        description=body.description,
        author=body.author,
        db=db,
        settings_json=settings,
        profiles_dir=_get_profiles_dir(),
    )
    return {"ok": True, "filename": path.name}


@router.post("/validate")
async def validate_profile(request: Request):
    body = await request.json()
    try:
        warnings = prof.validate_profile(body)
        # Also validate schema
        prof.ProfileSchema(**body)
        return {"valid": True, "warnings": warnings}
    except Exception as e:
        return {"valid": False, "error": str(e), "warnings": []}


@router.post("/import")
async def import_profile(request: Request):
    db = _get_db(request)
    if db is None:
        return JSONResponse(status_code=503, content={"error": "DB unavailable"})

    body = await request.json()

    # Validate first
    try:
        warnings = prof.validate_profile(body)
        prof.ProfileSchema(**body)
    except Exception as e:
        return JSONResponse(
            status_code=422,
            content={"error": str(e)},
        )

    settings = _read_settings_json()
    backup_path = prof.import_profile(
        profile_data=body,
        db=db,
        settings_json=settings,
        write_settings_fn=_write_settings_json,
        profiles_dir=_get_profiles_dir(),
        backup_dir=_get_backup_dir(),
    )
    return {"ok": True, "warnings": warnings, "backup": backup_path.name}


@router.post("/restore")
async def restore_latest(request: Request):
    db = _get_db(request)
    if db is None:
        return JSONResponse(status_code=503, content={"error": "DB unavailable"})

    latest = prof.get_latest_backup(_get_backup_dir())
    if latest is None:
        return JSONResponse(status_code=404, content={"error": "No backup found"})

    prof.restore_backup(latest, db, _write_settings_json)
    return {"ok": True, "restored_from": latest.name}


@router.delete("/{profile_name}")
async def delete(profile_name: str, request: Request):
    if prof.delete_profile(profile_name, _get_profiles_dir()):
        return {"ok": True}
    return JSONResponse(status_code=404, content={"error": "Profile not found"})
```

**Step 4: Register router in `main.py`**

In `jacked/api/main.py`, at line 250, add:

```python
from jacked.api.routes import system, analytics, features, permissions, profiles  # noqa: E402
```

And after the permissions include (line 255):

```python
app.include_router(profiles.router, prefix="/api/profiles", tags=["profiles"])
```

**Step 5: Run tests**

Run: `uv run python -m pytest tests/unit/test_profiles_api.py -v`
Expected: All PASS

**Step 6: Commit**

```bash
git add jacked/api/routes/profiles.py jacked/api/main.py tests/unit/test_profiles_api.py
git commit -m "feat(profiles): REST API for export, import, validate, list, delete, restore"
```

---

### Task 12: Profiles UI panel

**Files:**
- Create: `jacked/data/web/js/components/profiles-panel.js`
- Modify: `jacked/data/web/js/components/settings.js` (add profiles tab/section)
- Modify: `jacked/data/web/index.html` (add script tag)

**Step 1: Create profiles-panel.js**

This component provides `renderProfilesPanel()` and `bindProfilesEvents()`, integrated into the settings page. It should:

- **List profiles** with name, description, author, version, date. Delete button per row.
- **Export button** — opens SweetAlert2 modal with name/description/author fields. Calls `POST /api/profiles/export`.
- **Import button** — opens file picker (or paste area) for JSON. Calls `POST /api/profiles/validate` first, shows diff preview with warnings (red for dangerous items). Confirm → calls `POST /api/profiles/import`. On success, show green toast + "Undo" button.
- **Undo button** — visible after import, calls `POST /api/profiles/restore`. Disappears on page navigation.
- Use `escapeHtml()` for all profile metadata rendering.

**Step 2: Add script tag to `index.html`**

After `settings.js` script tag (line 96):

```html
<script src="/js/components/profiles-panel.js"></script>
```

**Step 3: Wire into settings page**

In `settings.js`, add a "Security Profiles" collapsible section that calls `renderProfilesPanel()`.

**Step 4: Manual verification**

Navigate to `#settings`, find Security Profiles section. Export a profile. Import it back. Verify diff preview shows. Verify Undo button works.

**Step 5: Commit**

```bash
git add jacked/data/web/js/components/profiles-panel.js jacked/data/web/js/components/settings.js jacked/data/web/index.html
git commit -m "feat(profiles): settings UI panel — list, export, import, undo"
```

---

### Task 13: CLI commands for profiles

**Files:**
- Modify: `jacked/cli.py`

**Step 1: Add profile CLI group**

Add a `profiles` click group with subcommands: `list`, `export`, `import`, `delete`. These call the same core functions in `jacked/profiles.py`.

```python
@cli.group()
def profiles():
    """Manage security profiles."""
    pass

@profiles.command("list")
def profiles_list():
    """List saved profiles."""
    from jacked.profiles import list_profiles, PROFILE_DIR_NAME
    profiles_dir = Path.home() / ".claude" / "jacked" / PROFILE_DIR_NAME
    items = list_profiles(profiles_dir)
    if not items:
        click.echo("No profiles found.")
        return
    for p in items:
        click.echo(f"  {p['name']}  ({p.get('jacked_version', '?')})  {p.get('description', '')}")

@profiles.command("export")
@click.argument("name")
@click.option("--description", "-d", default="", help="Profile description")
def profiles_export(name, description):
    """Export current config as a named profile."""
    # Implementation uses profiles.export_profile()

@profiles.command("import")
@click.argument("path", type=click.Path(exists=True))
def profiles_import(path):
    """Import a profile from a JSON file."""
    # Implementation uses profiles.validate_profile() + profiles.import_profile()

@profiles.command("delete")
@click.argument("name")
def profiles_delete(name):
    """Delete a saved profile."""
    # Implementation uses profiles.delete_profile()
```

**Step 2: Manual verification**

Run: `jacked profiles list` — should show empty or existing profiles.
Run: `jacked profiles export my-test -d "test profile"` — should create file.

**Step 3: Commit**

```bash
git add jacked/cli.py
git commit -m "feat(profiles): CLI commands — list, export, import, delete"
```

---

### Task 14: Run full test suite and fix any issues

**Step 1: Run all tests**

```bash
uv run python -m pytest -v
```

**Step 2: Run doctests**

```bash
uv run python -m pytest --doctest-modules jacked/web/db_analytics.py jacked/profiles.py -v
```

**Step 3: Fix any failures**

Address test failures, import errors, or missing fixtures.

**Step 4: Commit fixes if any**

```bash
git add -u
git commit -m "fix: test suite cleanup for analytics + profiles"
```

---

### Task 15: Commit in-flight changes and final integration

**Step 1: Commit the pre-existing uncommitted changes**

The git status shows 1300+ lines of uncommitted changes from the permissions/gatekeeper work. These should be committed first (or alongside) the new feature work.

**Step 2: Manual end-to-end verification**

1. Start jacked: `jacked`
2. Navigate to `#analytics` — verify all dashboard sections render
3. Click a doughnut segment → verify drill-down to logs
4. Click "Back to Dashboard" → verify return
5. Navigate to `#settings` → find Security Profiles
6. Export a profile → verify file created
7. Import it back → verify diff preview + warnings
8. Click Undo → verify restore
9. Run `jacked profiles list` → verify CLI

**Step 3: Final commit**

```bash
git add -A
git commit -m "feat: analytics dashboard + security profiles — complete integration"
```
