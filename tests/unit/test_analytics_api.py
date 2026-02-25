"""Unit tests for the analytics dashboard API endpoints.

Tests the new gatekeeper-dashboard, gatekeeper-heatmap, gatekeeper-sessions,
and gatekeeper-rules endpoints using FastAPI TestClient.
"""

import tempfile
import os
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked.api.routes.analytics import router
from jacked.web.database import Database

# Use a temp file for DB so multiple threads share the same data
# (in-memory DBs are per-connection, which breaks cross-thread TestClient)
_tmp_counter = 0


def _make_db():
    """Create a file-backed temp Database for cross-thread testing."""
    global _tmp_counter
    _tmp_counter += 1
    path = os.path.join(tempfile.gettempdir(), f"jacked_test_analytics_{os.getpid()}_{_tmp_counter}.db")
    # Remove stale file if it exists
    if os.path.exists(path):
        os.unlink(path)
    return Database(path)


def _make_app(db=None):
    """Create a minimal FastAPI app with analytics routes."""
    app = FastAPI()
    app.state.db = db
    app.include_router(router, prefix="/api/analytics")
    return app


def _ts_ago(days=0, hours=0):
    dt = datetime.now(timezone.utc) - timedelta(days=days, hours=hours)
    return dt.isoformat()


def _insert(db, decision="ALLOW", method="LOCAL", command="ls",
            session_id="s1", repo_path="/repo", timestamp=None):
    db.record_gatekeeper_decision(
        decision,
        timestamp=timestamp or _ts_ago(0),
        command=command,
        method=method,
        reason="ok",
        elapsed_ms=1.0,
        session_id=session_id,
        repo_path=repo_path,
    )


# ------------------------------------------------------------------
# /gatekeeper-dashboard
# ------------------------------------------------------------------

class TestGatekeeperDashboard:
    def test_empty_db(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-dashboard?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpi"]["total_decisions"] == 0
        assert data["time_series"]["buckets"] == []
        assert data["method_breakdown"] == {}

    def test_with_data(self):
        db = _make_db()
        _insert(db, decision="ALLOW", method="LOCAL")
        _insert(db, decision="ASK_USER", method="API")
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-dashboard?days=1")
        assert resp.status_code == 200
        data = resp.json()
        assert data["kpi"]["total_decisions"] == 2
        assert data["kpi"]["approval_rate"] == 50.0
        assert "LOCAL" in data["method_breakdown"]
        assert len(data["time_series"]["buckets"]) >= 1

    def test_no_db_returns_503(self):
        app = _make_app(db=None)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-dashboard")
        assert resp.status_code == 503

    def test_days_param_validation(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-dashboard?days=0")
        assert resp.status_code == 422  # ge=1 validation


# ------------------------------------------------------------------
# /gatekeeper-heatmap
# ------------------------------------------------------------------

class TestGatekeeperHeatmap:
    def test_empty_db(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-heatmap?days=7")
        assert resp.status_code == 200
        assert resp.json()["timestamps"] == []

    def test_with_data(self):
        db = _make_db()
        _insert(db)
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-heatmap?days=1")
        assert resp.status_code == 200
        ts = resp.json()["timestamps"]
        assert len(ts) == 1
        assert "timestamp" in ts[0]


# ------------------------------------------------------------------
# /gatekeeper-sessions
# ------------------------------------------------------------------

class TestGatekeeperSessions:
    def test_empty_db(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-sessions?days=7")
        assert resp.status_code == 200
        assert resp.json()["sessions"] == []

    def test_with_risky_session(self):
        db = _make_db()
        _insert(db, decision="ASK_USER", method="API", session_id="risky")
        _insert(db, decision="DENY_PATTERN", method="DENY_PATTERN", session_id="risky")
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-sessions?days=1")
        assert resp.status_code == 200
        sessions = resp.json()["sessions"]
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "risky"
        assert sessions[0]["risk_score"] == 4  # 3 + 1

    def test_limit_param(self):
        db = _make_db()
        for i in range(5):
            _insert(db, decision="ASK_USER", method="API", session_id=f"s{i}")
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-sessions?days=1&limit=3")
        assert resp.status_code == 200
        assert len(resp.json()["sessions"]) == 3


# ------------------------------------------------------------------
# /gatekeeper-rules
# ------------------------------------------------------------------

class TestGatekeeperRules:
    def test_empty_db(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-rules?days=7")
        assert resp.status_code == 200
        data = resp.json()
        assert data["suggested"] == []
        assert data["hot"] == []

    def test_with_data(self):
        db = _make_db()
        for _ in range(4):
            _insert(db, decision="ASK_USER", method="API", command="npm test")
        _insert(db, decision="ALLOW", method="LOCAL", command="ls")
        _insert(db, decision="ALLOW", method="LOCAL", command="pwd")
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper-rules?days=1")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["suggested"]) >= 1
        assert data["suggested"][0]["command"] == "npm test"
        assert len(data["hot"]) >= 1


# ------------------------------------------------------------------
# Existing endpoints still work
# ------------------------------------------------------------------

class TestExistingEndpoints:
    def test_gatekeeper_stats_still_works(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/gatekeeper?days=7")
        assert resp.status_code == 200
        assert resp.json()["total_decisions"] == 0

    def test_lessons_still_works(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/lessons?days=7")
        assert resp.status_code == 200
