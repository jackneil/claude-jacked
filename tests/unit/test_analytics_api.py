"""Unit tests for the analytics dashboard API endpoints.

The gatekeeper analytics endpoints were removed in 0.70.0; this covers the
surviving agents/hooks/lessons endpoints using FastAPI TestClient.
"""

import tempfile
import os

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


class TestExistingEndpoints:
    def test_gatekeeper_endpoints_are_gone(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        for path in ("gatekeeper", "gatekeeper-dashboard", "gatekeeper-heatmap",
                     "gatekeeper-sessions", "gatekeeper-rules"):
            resp = client.get(f"/api/analytics/{path}?days=7")
            assert resp.status_code == 404, path

    def test_agents_still_works(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/agents?days=7")
        assert resp.status_code == 200

    def test_hooks_still_works(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/hooks?days=7")
        assert resp.status_code == 200

    def test_lessons_still_works(self):
        db = _make_db()
        app = _make_app(db)
        client = TestClient(app)
        resp = client.get("/api/analytics/lessons?days=7")
        assert resp.status_code == 200
