"""Unit tests for the server log API endpoint (GET /api/logs/server)."""

import logging

from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked.api.log_capture import ServerLogBuffer
from jacked.api.routes.logs import router


def _make_app(buf: ServerLogBuffer | None = None) -> FastAPI:
    """Create a minimal FastAPI app with the logs router."""
    app = FastAPI()
    app.include_router(router, prefix="/api")
    if buf is not None:
        # Patch the module-level singleton for this test
        import jacked.api.routes.logs as logs_mod
        logs_mod.server_log_buffer = buf
    return app


def _make_buf(n: int = 0, maxlen: int = 2000) -> ServerLogBuffer:
    """Create a ServerLogBuffer and optionally fill it with *n* entries."""
    buf = ServerLogBuffer(maxlen=maxlen)
    for i in range(n):
        level = logging.ERROR if i % 5 == 0 else logging.INFO
        buf._handle_record(
            logging.LogRecord("test", level, "", 0, f"msg{i}", (), None)
        )
    return buf


# ------------------------------------------------------------------
# Response shape
# ------------------------------------------------------------------


def test_empty_buffer():
    """Empty buffer returns empty entries and correct buffer_size.

    >>> buf = _make_buf(0)
    >>> buf.get_recent()
    []
    """
    buf = _make_buf(0)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server")
    assert resp.status_code == 200
    data = resp.json()
    assert data["entries"] == []
    assert data["buffer_size"] == 2000


def test_basic_response_shape():
    """Response has entries list and buffer_size.

    >>> buf = _make_buf(5)
    >>> entries = buf.get_recent()
    >>> len(entries)
    5
    """
    buf = _make_buf(5)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 5
    assert data["buffer_size"] == 2000
    # Each entry has required keys
    entry = data["entries"][0]
    assert "ts" in entry
    assert "level" in entry
    assert "msg" in entry
    assert "logger" in entry


# ------------------------------------------------------------------
# Limit parameter
# ------------------------------------------------------------------


def test_limit_param():
    """?limit=N caps the result set.

    >>> # Verified via API test
    """
    buf = _make_buf(20)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server?limit=5")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 5


def test_limit_larger_than_buffer():
    """Limit larger than buffer returns all entries.

    >>> # Verified via API test
    """
    buf = _make_buf(3)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server?limit=1000")
    assert resp.status_code == 200
    assert len(resp.json()["entries"]) == 3


def test_limit_capped_at_2000():
    """Limit > 2000 is rejected by validation.

    >>> # Verified via API test
    """
    buf = _make_buf(0)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server?limit=3000")
    assert resp.status_code == 422  # Validation error


# ------------------------------------------------------------------
# Level filter
# ------------------------------------------------------------------


def test_level_filter_error():
    """?level=ERROR returns only ERROR+ entries.

    >>> buf = _make_buf(10)
    >>> # Every 5th entry (0, 5) is ERROR
    """
    buf = _make_buf(10)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server?level=ERROR")
    assert resp.status_code == 200
    data = resp.json()
    # Entries at index 0 and 5 are ERROR (i % 5 == 0)
    assert len(data["entries"]) == 2
    for entry in data["entries"]:
        assert entry["level"] in ("ERROR", "CRITICAL")


def test_level_filter_info():
    """?level=INFO returns INFO+ entries (excludes DEBUG).

    >>> # Verified via API test
    """
    buf = ServerLogBuffer(maxlen=100)
    for lvl in (logging.DEBUG, logging.INFO, logging.WARNING, logging.ERROR):
        buf._handle_record(
            logging.LogRecord("t", lvl, "", 0, f"{logging.getLevelName(lvl)}", (), None)
        )

    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server?level=INFO")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) == 3  # INFO, WARNING, ERROR
    levels = {e["level"] for e in data["entries"]}
    assert "DEBUG" not in levels


def test_level_filter_case_insensitive():
    """Level filter is case insensitive.

    >>> # Verified via API test
    """
    buf = _make_buf(10)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server?level=error")
    assert resp.status_code == 200
    for entry in resp.json()["entries"]:
        assert entry["level"] in ("ERROR", "CRITICAL")


def test_level_filter_with_limit():
    """Level filter + limit work together.

    >>> # Verified via API test
    """
    buf = _make_buf(50)
    app = _make_app(buf)
    client = TestClient(app)
    resp = client.get("/api/logs/server?level=ERROR&limit=3")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["entries"]) <= 3
    for entry in data["entries"]:
        assert entry["level"] in ("ERROR", "CRITICAL")
