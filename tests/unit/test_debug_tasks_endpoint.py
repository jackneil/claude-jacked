"""Tests for GET /api/admin/debug/tasks — asyncio task dump endpoint.

Added in 0.43.1 to make a silently-dead background task diagnosable
without attaching py-spy or a debugger. See the 2026-05-10 incident
where active_account_poll_loop went silent for 5 days with no signal.
"""

from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked.api.routes.system import router


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return app


def test_returns_at_least_the_request_handler_task():
    """While serving the request, asyncio.all_tasks() includes the
    handler task itself. Shape must be: count >= 1, tasks list non-empty,
    each task has the documented keys."""
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/api/admin/debug/tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] >= 1
    assert isinstance(body["tasks"], list)
    assert len(body["tasks"]) == body["count"]
    keys = {"name", "done", "cancelled", "coro_name", "location"}
    for t in body["tasks"]:
        assert keys.issubset(t.keys()), f"missing keys in {t}"


def test_heartbeat_snapshot_shape_when_absent():
    """If active_poll_last_tick_at hasn't been set on app.state, the
    heartbeat block reports None values rather than crashing."""
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/api/admin/debug/tasks")
    body = resp.json()
    assert "heartbeat" in body
    assert body["heartbeat"]["active_poll_last_tick_at_monotonic"] is None
    assert body["heartbeat"]["active_poll_heartbeat_age_seconds"] is None


def test_heartbeat_snapshot_reports_age_when_present():
    """If app.state.active_poll_last_tick_at is set, the response includes
    a positive heartbeat age in seconds."""
    import time as _time
    app = _make_app()
    app.state.active_poll_last_tick_at = _time.monotonic() - 5
    client = TestClient(app)
    resp = client.get("/api/admin/debug/tasks")
    body = resp.json()
    age = body["heartbeat"]["active_poll_heartbeat_age_seconds"]
    assert age is not None
    assert 4.5 <= age <= 7.0, f"expected ~5s heartbeat age, got {age}"


def test_location_includes_filename_and_line_when_task_alive(monkeypatch):
    """A live task's location field should include a 'file:line in func'
    string so a wedge point can be pinpointed by reading the response."""
    app = _make_app()
    client = TestClient(app)
    resp = client.get("/api/admin/debug/tasks")
    body = resp.json()
    # Find at least one task with a location populated — the request
    # handler is itself a task and its frame should be visible.
    located = [t for t in body["tasks"] if t["location"]]
    assert located, "expected at least one task with a non-null location"
    for t in located:
        assert " in " in t["location"], f"malformed location: {t['location']}"
        assert ":" in t["location"], f"missing line number: {t['location']}"
