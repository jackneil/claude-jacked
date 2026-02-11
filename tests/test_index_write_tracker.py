"""Tests for IndexWriteTracker SQLite-based write tracking."""

import pytest
import sqlite3
import threading
import time
from unittest.mock import Mock

from jacked.index_write_tracker import IndexWriteTracker, MAX_SEED_POINTS


class TestIndexWriteTrackerInit:
    """Tests for IndexWriteTracker initialization."""

    def test_creates_db_file_on_init(self, tmp_db_path):
        """DB file created in tmp directory when IndexWriteTracker initialized."""
        tracker = IndexWriteTracker("test_config_hash")
        assert tmp_db_path.exists()

    def test_creates_tables_on_init(self, tmp_db_path):
        """indexed_points and session_meta tables created with correct schema."""
        tracker = IndexWriteTracker("test_config_hash")

        conn = sqlite3.connect(tmp_db_path)
        cursor = conn.cursor()

        # Check indexed_points table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='indexed_points'"
        )
        assert cursor.fetchone() is not None

        # Check session_meta table exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='session_meta'"
        )
        assert cursor.fetchone() is not None

        conn.close()

    def test_wal_mode_enabled(self, tmp_db_path):
        """SQLite WAL mode is enabled for concurrent access."""
        tracker = IndexWriteTracker("test_config_hash")

        conn = sqlite3.connect(tmp_db_path)
        cursor = conn.cursor()
        cursor.execute("PRAGMA journal_mode")
        mode = cursor.fetchone()[0]
        conn.close()

        assert mode.lower() == "wal"


class TestIndexWriteTrackerSessionMeta:
    """Tests for session metadata operations."""

    def test_get_session_meta_returns_none_for_unknown_session(self, tmp_db_path):
        """Returns None when session doesn't exist in tracker."""
        tracker = IndexWriteTracker("test_config_hash")
        result = tracker.get_session_meta("nonexistent-session-id")
        assert result is None

    def test_mark_indexing_creates_session_meta(self, tmp_db_path):
        """mark_indexing creates entry with status='indexing'."""
        tracker = IndexWriteTracker("test_config_hash")
        tracker.mark_indexing("session-123")

        meta = tracker.get_session_meta("session-123")
        assert meta is not None
        assert meta["status"] == "indexing"

    def test_mark_indexing_updates_existing_session(self, tmp_db_path):
        """mark_indexing updates status to 'indexing' for existing session."""
        tracker = IndexWriteTracker("test_config_hash")

        # First mark as complete
        tracker.mark_indexing("session-123")
        tracker.mark_complete("session-123")
        meta = tracker.get_session_meta("session-123")
        assert meta["status"] == "complete"

        # Now mark as indexing again
        tracker.mark_indexing("session-123")
        meta = tracker.get_session_meta("session-123")
        assert meta["status"] == "indexing"

    def test_mark_complete_sets_status_complete(self, tmp_db_path):
        """mark_complete updates status to 'complete'."""
        tracker = IndexWriteTracker("test_config_hash")
        tracker.mark_indexing("session-123")
        tracker.mark_complete("session-123")

        meta = tracker.get_session_meta("session-123")
        assert meta["status"] == "complete"

    def test_config_hash_stored_in_session_meta(self, tmp_db_path):
        """Config hash is stored and retrievable from session_meta."""
        config_hash = "sha256:abc123"
        tracker = IndexWriteTracker(config_hash)
        tracker.mark_indexing("session-123")

        meta = tracker.get_session_meta("session-123")
        assert meta["config_hash"] == config_hash


class TestIndexWriteTrackerIndexedPoints:
    """Tests for indexed point operations."""

    def test_record_indexed_stores_point_data(self, tmp_db_path):
        """record_indexed stores session_id, content_type, index, hash, point_id."""
        tracker = IndexWriteTracker("test_config_hash")
        tracker.record_indexed(
            session_id="session-123",
            content_type="chunk",
            index=0,
            content_hash="sha256:abc123",
            point_id="point-uuid-123",
        )

        state = tracker.get_session_state("session-123")
        assert ("chunk", 0) in state
        assert state[("chunk", 0)] == "sha256:abc123"

    def test_record_indexed_overwrites_on_same_key(self, tmp_db_path):
        """Same (session_id, content_type, index) overwrites previous entry."""
        tracker = IndexWriteTracker("test_config_hash")

        # First record
        tracker.record_indexed("session-123", "chunk", 0, "sha256:old_hash", "point-1")
        state = tracker.get_session_state("session-123")
        assert state[("chunk", 0)] == "sha256:old_hash"

        # Overwrite with new hash
        tracker.record_indexed("session-123", "chunk", 0, "sha256:new_hash", "point-2")
        state = tracker.get_session_state("session-123")
        assert state[("chunk", 0)] == "sha256:new_hash"

    def test_get_session_state_returns_empty_dict_for_new_session(self, tmp_db_path):
        """Returns {} for session with no indexed points."""
        tracker = IndexWriteTracker("test_config_hash")
        state = tracker.get_session_state("new-session-id")
        assert state == {}

    def test_get_session_state_returns_all_content_types(self, tmp_db_path):
        """Returns dict with all indexed (type, index) -> hash mappings."""
        tracker = IndexWriteTracker("test_config_hash")

        # Record multiple content types
        tracker.record_indexed("session-123", "plan", 0, "sha256:plan_hash", "point-1")
        tracker.record_indexed("session-123", "chunk", 0, "sha256:chunk0_hash", "point-2")
        tracker.record_indexed("session-123", "chunk", 1, "sha256:chunk1_hash", "point-3")
        tracker.record_indexed("session-123", "user_message", 0, "sha256:msg_hash", "point-4")

        state = tracker.get_session_state("session-123")

        assert ("plan", 0) in state
        assert ("chunk", 0) in state
        assert ("chunk", 1) in state
        assert ("user_message", 0) in state
        assert len(state) == 4

    def test_is_indexed_returns_true_for_matching_hash(self, tmp_db_path):
        """is_indexed returns True when content_hash matches."""
        tracker = IndexWriteTracker("test_config_hash")
        tracker.record_indexed("session-123", "chunk", 0, "sha256:abc123", "point-1")

        assert tracker.is_indexed("session-123", "chunk", 0, "sha256:abc123") is True

    def test_is_indexed_returns_false_for_different_hash(self, tmp_db_path):
        """is_indexed returns False when content_hash differs (content changed)."""
        tracker = IndexWriteTracker("test_config_hash")
        tracker.record_indexed("session-123", "chunk", 0, "sha256:old_hash", "point-1")

        assert tracker.is_indexed("session-123", "chunk", 0, "sha256:new_hash") is False

    def test_is_indexed_returns_false_for_missing_content(self, tmp_db_path):
        """is_indexed returns False when content doesn't exist."""
        tracker = IndexWriteTracker("test_config_hash")
        assert tracker.is_indexed("session-123", "chunk", 0, "sha256:any_hash") is False


class TestIndexWriteTrackerClearSession:
    """Tests for session clearing operations."""

    def test_clear_session_removes_all_indexed_points(self, tmp_db_path):
        """clear_session deletes all points for a session."""
        tracker = IndexWriteTracker("test_config_hash")

        # Add some points
        tracker.record_indexed("session-123", "chunk", 0, "sha256:hash0", "point-1")
        tracker.record_indexed("session-123", "chunk", 1, "sha256:hash1", "point-2")
        tracker.record_indexed("session-123", "plan", 0, "sha256:plan_hash", "point-3")

        # Clear session
        tracker.clear_session("session-123")

        state = tracker.get_session_state("session-123")
        assert state == {}

    def test_clear_session_removes_session_meta(self, tmp_db_path):
        """clear_session deletes session_meta entry."""
        tracker = IndexWriteTracker("test_config_hash")
        tracker.mark_indexing("session-123")

        # Verify meta exists
        assert tracker.get_session_meta("session-123") is not None

        # Clear session
        tracker.clear_session("session-123")

        # Verify meta deleted
        assert tracker.get_session_meta("session-123") is None

    def test_clear_session_does_not_affect_other_sessions(self, tmp_db_path):
        """Clearing session A doesn't affect session B's data."""
        tracker = IndexWriteTracker("test_config_hash")

        # Add points to two sessions
        tracker.record_indexed("session-A", "chunk", 0, "sha256:hash_a", "point-1")
        tracker.record_indexed("session-B", "chunk", 0, "sha256:hash_b", "point-2")
        tracker.mark_indexing("session-A")
        tracker.mark_indexing("session-B")

        # Clear only session A
        tracker.clear_session("session-A")

        # Verify session A is cleared
        assert tracker.get_session_state("session-A") == {}
        assert tracker.get_session_meta("session-A") is None

        # Verify session B is NOT affected
        state_b = tracker.get_session_state("session-B")
        assert ("chunk", 0) in state_b
        assert tracker.get_session_meta("session-B") is not None


class TestIndexWriteTrackerSeedFromQdrant:
    """Tests for seeding from Qdrant."""

    def test_seed_from_qdrant_populates_indexed_points(
        self, tmp_db_path, mock_qdrant_client, sample_qdrant_points
    ):
        """seed_from_qdrant creates indexed_points entries from Qdrant response."""
        tracker = IndexWriteTracker("test_config_hash")
        points = sample_qdrant_points("session-123", {"chunk": 3, "plan": 1})
        mock_qdrant_client.get_session_points.return_value = points

        tracker.seed_from_qdrant("session-123", mock_qdrant_client, "test_user")

        state = tracker.get_session_state("session-123")
        assert len(state) == 4
        assert ("chunk", 0) in state
        assert ("chunk", 1) in state
        assert ("chunk", 2) in state
        assert ("plan", 0) in state

    def test_seed_from_qdrant_filters_by_user_name(
        self, tmp_db_path, mock_qdrant_client
    ):
        """Verifies user_name filter is passed to Qdrant query."""
        tracker = IndexWriteTracker("test_config_hash")
        mock_qdrant_client.get_session_points.return_value = []

        tracker.seed_from_qdrant("session-123", mock_qdrant_client, "specific_user")

        mock_qdrant_client.get_session_points.assert_called_once_with(
            "session-123", "specific_user"
        )

    def test_seed_from_qdrant_sets_session_meta_complete(
        self, tmp_db_path, mock_qdrant_client
    ):
        """After seeding, session status is 'complete'."""
        tracker = IndexWriteTracker("test_config_hash")
        mock_qdrant_client.get_session_points.return_value = []

        tracker.seed_from_qdrant("session-123", mock_qdrant_client, "test_user")

        meta = tracker.get_session_meta("session-123")
        assert meta is not None
        assert meta["status"] == "complete"

    def test_seed_from_qdrant_raises_on_too_many_points(
        self, tmp_db_path, mock_qdrant_client
    ):
        """Raises ValueError when session exceeds MAX_SEED_POINTS."""
        tracker = IndexWriteTracker("test_config_hash")

        # Create too many points
        too_many_points = []
        for i in range(MAX_SEED_POINTS + 1):
            point = Mock()
            point.id = f"point-{i}"
            point.payload = {
                "session_id": "session-123",
                "content_type": "chunk",
                "chunk_index": i,
                "content_hash": f"sha256:hash_{i}",
            }
            too_many_points.append(point)

        mock_qdrant_client.get_session_points.return_value = too_many_points

        with pytest.raises(ValueError, match=f"exceeds limit {MAX_SEED_POINTS}"):
            tracker.seed_from_qdrant("session-123", mock_qdrant_client, "test_user")

    def test_seed_from_qdrant_handles_empty_response(
        self, tmp_db_path, mock_qdrant_client
    ):
        """Handles case where Qdrant returns no points (new session)."""
        tracker = IndexWriteTracker("test_config_hash")
        mock_qdrant_client.get_session_points.return_value = []

        # Should not raise
        tracker.seed_from_qdrant("session-123", mock_qdrant_client, "test_user")

        state = tracker.get_session_state("session-123")
        assert state == {}

        meta = tracker.get_session_meta("session-123")
        assert meta["status"] == "complete"


class TestIndexWriteTrackerConcurrency:
    """Tests for concurrent access safety."""

    def test_concurrent_writes_no_corruption(self, tmp_db_path):
        """Multiple threads writing to same session don't corrupt data."""
        tracker = IndexWriteTracker("test_config_hash")
        errors = []

        def write_points(thread_id, count):
            try:
                for i in range(count):
                    tracker.record_indexed(
                        session_id="session-123",
                        content_type=f"chunk_{thread_id}",
                        index=i,
                        content_hash=f"sha256:hash_{thread_id}_{i}",
                        point_id=f"point-{thread_id}-{i}",
                    )
                    time.sleep(0.001)  # Small delay to encourage interleaving
            except Exception as e:
                errors.append(str(e))

        # Run 5 threads, each writing 10 points
        threads = []
        for t in range(5):
            thread = threading.Thread(target=write_points, args=(t, 10))
            threads.append(thread)
            thread.start()

        for thread in threads:
            thread.join()

        # Verify no errors
        assert len(errors) == 0

        # Verify all points recorded
        state = tracker.get_session_state("session-123")
        assert len(state) == 50  # 5 threads * 10 points each

    def test_busy_timeout_prevents_lock_errors(self, tmp_db_path):
        """SQLite busy_timeout handles concurrent access gracefully."""
        # This test verifies the PRAGMA busy_timeout is working
        # by doing rapid concurrent access
        tracker = IndexWriteTracker("test_config_hash")
        success_count = [0]
        error_count = [0]
        lock = threading.Lock()

        def rapid_operations():
            for i in range(20):
                try:
                    tracker.mark_indexing(f"session-{threading.current_thread().name}")
                    tracker.record_indexed(
                        f"session-{threading.current_thread().name}",
                        "chunk", i, f"sha256:{i}", f"point-{i}"
                    )
                    tracker.mark_complete(f"session-{threading.current_thread().name}")
                    with lock:
                        success_count[0] += 1
                except sqlite3.OperationalError:
                    with lock:
                        error_count[0] += 1

        threads = [threading.Thread(target=rapid_operations) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # Most operations should succeed due to busy_timeout
        # We expect very few (ideally zero) lock errors
        assert success_count[0] > 50  # At least most succeeded
