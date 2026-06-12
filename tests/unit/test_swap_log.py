"""Unit tests for swap_log status/residency tracking and the swap-log endpoint.

Covers: record_swap status defaults and returned row id, update_swap_status,
swaps_last_24h windowing + committed filtering, migration of legacy swap_log
tables, and the /swap-log endpoint payload shape.
"""

import os
import sqlite3
import tempfile

from fastapi import FastAPI
from starlette.testclient import TestClient

from jacked.api.routes.settings_swap import router
from jacked.web.database import Database

# Use a temp file for DB so multiple threads share the same data
# (in-memory DBs are per-connection, which breaks cross-thread TestClient)
_tmp_counter = 0


def _tmp_db_path():
    global _tmp_counter
    _tmp_counter += 1
    path = os.path.join(
        tempfile.gettempdir(), f"jacked_test_swaplog_{os.getpid()}_{_tmp_counter}.db"
    )
    if os.path.exists(path):
        os.unlink(path)
    return path


def _make_app(db=None):
    """Create a minimal FastAPI app with swap settings routes."""
    app = FastAPI()
    app.state.db = db
    app.include_router(router, prefix="/api/settings")
    return app


def _swap_by_id(db, swap_id):
    return next(s for s in db.list_swaps(limit=500) if s["id"] == swap_id)


def _age_swap(db, swap_id, hours):
    """Backdate a swap_log row by the given number of hours."""
    with db._writer() as conn:
        conn.execute(
            "UPDATE swap_log SET timestamp = "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) WHERE id = ?",
            (f"-{hours} hours", swap_id),
        )


# ------------------------------------------------------------------
# record_swap / update_swap_status
# ------------------------------------------------------------------

class TestRecordSwap:
    def test_returns_inserted_row_id(self):
        db = Database(":memory:")
        rid1 = db.record_swap(1, 2, "usage high", "auto")
        rid2 = db.record_swap(2, 1, "usage high", "auto")
        assert isinstance(rid1, int) and rid1 > 0
        assert rid2 == rid1 + 1

    def test_default_status_committed(self):
        db = Database(":memory:")
        rid = db.record_swap(1, 2, "usage high", "auto")
        row = _swap_by_id(db, rid)
        assert row["status"] == "committed"
        assert row["residency_seconds"] is None

    def test_pending_then_update_to_failed(self):
        db = Database(":memory:")
        rid = db.record_swap(
            1, 2, "usage high", "auto", status="pending", residency_seconds=120,
        )
        row = _swap_by_id(db, rid)
        assert row["status"] == "pending"
        assert row["residency_seconds"] == 120

        db.update_swap_status(rid, "failed")
        row = _swap_by_id(db, rid)
        assert row["status"] == "failed"
        assert row["residency_seconds"] == 120  # residency untouched by status update


# ------------------------------------------------------------------
# swaps_last_24h
# ------------------------------------------------------------------

class TestSwapsLast24h:
    def test_empty_db(self):
        db = Database(":memory:")
        assert db.swaps_last_24h() == 0

    def test_counts_only_committed_by_default(self):
        db = Database(":memory:")
        db.record_swap(1, 2, "r", "auto")  # committed
        db.record_swap(2, 1, "r", "auto", status="pending")
        db.record_swap(1, 2, "r", "auto", status="failed")
        assert db.swaps_last_24h() == 1
        assert db.swaps_last_24h(committed_only=False) == 3

    def test_excludes_swaps_older_than_24h(self):
        db = Database(":memory:")
        old_id = db.record_swap(1, 2, "r", "auto")
        recent_id = db.record_swap(2, 1, "r", "auto")
        _age_swap(db, old_id, hours=25)
        _age_swap(db, recent_id, hours=23)
        assert db.swaps_last_24h() == 1


# ------------------------------------------------------------------
# Migration of legacy swap_log tables
# ------------------------------------------------------------------

class TestSwapLogMigration:
    def test_legacy_table_gains_status_and_residency(self):
        path = _tmp_db_path()
        # Build a pre-migration DB: swap_log without status/residency_seconds
        conn = sqlite3.connect(path)
        conn.execute(
            """CREATE TABLE swap_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                from_account_id INTEGER,
                to_account_id INTEGER,
                reason TEXT,
                trigger TEXT,
                from_5h_usage REAL,
                from_7d_usage REAL,
                to_5h_usage REAL,
                to_7d_usage REAL
            )"""
        )
        conn.execute(
            "INSERT INTO swap_log (from_account_id, to_account_id, reason, trigger) "
            "VALUES (1, 2, 'old row', 'auto')"
        )
        conn.commit()
        conn.close()

        db = Database(path)
        swaps = db.list_swaps()
        assert len(swaps) == 1
        # Pre-existing rows backfill to 'committed' (they were real swaps)
        assert swaps[0]["status"] == "committed"
        assert swaps[0]["residency_seconds"] is None
        assert db.swaps_last_24h() == 1
        # New writes work against the migrated table
        rid = db.record_swap(2, 1, "new row", "auto", status="pending")
        assert _swap_by_id(db, rid)["status"] == "pending"


# ------------------------------------------------------------------
# /swap-log endpoint
# ------------------------------------------------------------------

class TestSwapLogEndpoint:
    def test_payload_includes_status_residency_and_24h_count(self):
        db = Database(_tmp_db_path())
        db.record_swap(1, 2, "usage high", "auto", residency_seconds=3600)
        db.record_swap(2, 1, "usage high", "auto", status="pending")
        app = _make_app(db)
        client = TestClient(app)

        resp = client.get("/api/settings/swap-log?limit=20")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["swaps"]) == 2
        for swap in data["swaps"]:
            assert "status" in swap
            assert "residency_seconds" in swap
        statuses = {s["status"] for s in data["swaps"]}
        assert statuses == {"committed", "pending"}
        assert data["swaps_last_24h"] == 1  # committed only

    def test_no_db_returns_empty_payload(self):
        app = _make_app(db=None)
        client = TestClient(app)
        resp = client.get("/api/settings/swap-log")
        assert resp.status_code == 200
        assert resp.json() == {"swaps": [], "swaps_last_24h": 0}
