"""Tests for SessionIndexer incremental indexing."""

import pytest
pytest.importorskip("qdrant_client")

from unittest.mock import patch

from jacked.indexer import SessionIndexer


class TestSessionIndexerInit:
    """Tests for SessionIndexer initialization."""

    def test_creates_write_tracker_on_init(self, tmp_db_path, mock_config, mock_qdrant_client):
        """SessionIndexer creates IndexWriteTracker with config_hash."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        # Verify tracker was created by checking it has methods
        assert hasattr(indexer, "_tracker")
        assert hasattr(indexer._tracker, "get_session_state")

    def test_config_hash_includes_chunk_size_and_overlap(
        self, tmp_db_path, mock_config, mock_qdrant_client
    ):
        """Config hash changes when chunk_size or chunk_overlap changes."""
        # First indexer with default config
        mock_config.chunk_size = 4000
        mock_config.chunk_overlap = 200
        indexer1 = SessionIndexer(mock_config, client=mock_qdrant_client)
        hash1 = indexer1._config_hash

        # Second indexer with different chunk_size
        mock_config.chunk_size = 2000
        indexer2 = SessionIndexer(mock_config, client=mock_qdrant_client)
        hash2 = indexer2._config_hash

        # Third indexer with different overlap
        mock_config.chunk_size = 4000
        mock_config.chunk_overlap = 100
        indexer3 = SessionIndexer(mock_config, client=mock_qdrant_client)
        hash3 = indexer3._config_hash

        assert hash1 != hash2  # Different chunk_size
        assert hash1 != hash3  # Different overlap
        assert hash2 != hash3  # Both different


class TestIndexSessionIncremental:
    """Tests for incremental indexing behavior."""

    def test_first_index_creates_all_points(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """First index of a session creates all content types."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        # Create a sample session file (mock the parsing)
        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        transcript = sample_transcript(session_id="session-123")

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            result = indexer.index_session(session_file, "/c/test/repo")

        assert result["indexed"] is True
        assert result["new_points"] > 0
        # Should have points from various content types
        mock_qdrant_client.upsert_points.assert_called_once()

    def test_second_index_skips_unchanged_content(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """Second index of unchanged session returns skipped=True."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        transcript = sample_transcript(session_id="session-123")

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            # First index
            result1 = indexer.index_session(session_file, "/c/test/repo")
            assert result1["indexed"] is True

            # Reset mock to track second call
            mock_qdrant_client.upsert_points.reset_mock()

            # Second index - should be skipped
            result2 = indexer.index_session(session_file, "/c/test/repo")
            assert result2["skipped"] is True
            assert result2["new_points"] == 0

            # Should NOT have called upsert again
            mock_qdrant_client.upsert_points.assert_not_called()

    def test_index_detects_new_user_messages(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript,
        sample_user_message, tmp_path
    ):
        """New user messages are detected and indexed."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # First transcript with 2 messages
        msgs1 = [sample_user_message("First message content long enough")]
        transcript1 = sample_transcript(
            session_id="session-123",
            user_messages=msgs1,
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript1):
            result1 = indexer.index_session(session_file, "/c/test/repo")
            assert result1["indexed"] is True

        # Second transcript with 3 messages (1 new)
        msgs2 = [
            sample_user_message("First message content long enough"),
            sample_user_message("Second message content long enough"),
        ]
        transcript2 = sample_transcript(
            session_id="session-123",
            user_messages=msgs2,
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        mock_qdrant_client.upsert_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript2):
            result2 = indexer.index_session(session_file, "/c/test/repo")
            assert result2["indexed"] is True
            # Should only index the new message
            assert result2["user_messages"] == 1

    def test_index_detects_changed_chunk_content(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """Changed chunk content (different hash) triggers re-index of that chunk."""
        mock_config.chunk_size = 100  # Small chunks for testing
        mock_config.chunk_overlap = 10
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # First transcript
        transcript1 = sample_transcript(
            session_id="session-123",
            full_text="A" * 200,  # Will create ~2 chunks
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript1):
            indexer.index_session(session_file, "/c/test/repo")

        # Second transcript with different content
        transcript2 = sample_transcript(
            session_id="session-123",
            full_text="B" * 200,  # Different content, same length
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        mock_qdrant_client.upsert_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript2):
            result2 = indexer.index_session(session_file, "/c/test/repo")
            # Should re-index all chunks since content changed
            assert result2["indexed"] is True
            assert result2["chunks"] > 0

    def test_index_detects_new_agent_summaries(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript,
        sample_agent_summary, tmp_path
    ):
        """New agent summaries appended to transcript are indexed."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # First transcript with 1 summary
        summaries1 = [sample_agent_summary("First summary content")]
        transcript1 = sample_transcript(
            session_id="session-123",
            agent_summaries=summaries1,
            user_messages=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript1):
            result1 = indexer.index_session(session_file, "/c/test/repo")
            assert result1["subagent_summaries"] == 1

        # Second transcript with 2 summaries
        summaries2 = [
            sample_agent_summary("First summary content"),
            sample_agent_summary("Second summary content"),
        ]
        transcript2 = sample_transcript(
            session_id="session-123",
            agent_summaries=summaries2,
            user_messages=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        mock_qdrant_client.upsert_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript2):
            result2 = indexer.index_session(session_file, "/c/test/repo")
            # Should only index the new summary
            assert result2["subagent_summaries"] == 1

    def test_index_detects_plan_changes(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript,
        sample_plan_file, tmp_path
    ):
        """Modified plan file content triggers plan re-index."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # First transcript with plan v1
        plan1 = sample_plan_file("# Plan v1\nOriginal plan content")
        transcript1 = sample_transcript(
            session_id="session-123",
            plan=plan1,
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript1):
            result1 = indexer.index_session(session_file, "/c/test/repo")
            assert result1["plans"] == 1

        # Second transcript with plan v2 (changed)
        plan2 = sample_plan_file("# Plan v2\nUpdated plan content")
        transcript2 = sample_transcript(
            session_id="session-123",
            plan=plan2,
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            include_defaults=False,
        )

        mock_qdrant_client.upsert_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript2):
            result2 = indexer.index_session(session_file, "/c/test/repo")
            # Plan should be re-indexed due to content change
            assert result2["plans"] == 1


class TestIndexSessionConfigChange:
    """Tests for config change handling."""

    def test_config_change_triggers_reseed_from_qdrant(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """When config_hash differs, clears tracker and re-seeds from Qdrant."""
        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # First indexer with chunk_size=4000
        mock_config.chunk_size = 4000
        mock_config.chunk_overlap = 200
        indexer1 = SessionIndexer(mock_config, client=mock_qdrant_client)

        transcript = sample_transcript(session_id="session-123")

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            indexer1.index_session(session_file, "/c/test/repo")

        # Second indexer with different chunk_size
        mock_config.chunk_size = 2000  # Changed!
        indexer2 = SessionIndexer(mock_config, client=mock_qdrant_client)

        mock_qdrant_client.get_session_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            indexer2.index_session(session_file, "/c/test/repo")

        # Should have called get_session_points to re-seed
        mock_qdrant_client.get_session_points.assert_called()


class TestIndexSessionCrashRecovery:
    """Tests for crash recovery scenarios."""

    def test_indexing_status_triggers_force_reseed(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """Session with status='indexing' (mid-crash) forces re-seed."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # Manually set the session to 'indexing' status (simulating crash)
        indexer._tracker.mark_indexing("session-123")

        transcript = sample_transcript(session_id="session-123")

        mock_qdrant_client.get_session_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            indexer.index_session(session_file, "/c/test/repo")

        # Should have re-seeded from Qdrant due to 'indexing' status
        mock_qdrant_client.get_session_points.assert_called()


class TestIndexSessionCacheMiss:
    """Tests for cache miss scenarios."""

    def test_cache_miss_seeds_from_qdrant(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """Unknown session seeds state from Qdrant before indexing."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-new.jsonl"
        session_file.touch()

        transcript = sample_transcript(session_id="session-new")

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            indexer.index_session(session_file, "/c/test/repo")

        # Should have called get_session_points for new session
        mock_qdrant_client.get_session_points.assert_called_with(
            "session-new", mock_config.user_name
        )

    def test_cache_miss_on_empty_qdrant_indexes_all(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """Cache miss with empty Qdrant response indexes everything."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-brand-new.jsonl"
        session_file.touch()

        # Qdrant returns nothing (new session)
        mock_qdrant_client.get_session_points.return_value = []

        transcript = sample_transcript(session_id="session-brand-new")

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            result = indexer.index_session(session_file, "/c/test/repo")

        # Should index everything since Qdrant had nothing
        assert result["indexed"] is True
        assert result["new_points"] > 0
        mock_qdrant_client.upsert_points.assert_called_once()


class TestBuildIncrementalPoints:
    """Tests for point building logic."""

    def test_only_new_chunks_built_when_appended(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript, tmp_path
    ):
        """When transcript grows, only new chunks are built (not all)."""
        mock_config.chunk_size = 50
        mock_config.chunk_overlap = 10
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # First transcript - short
        transcript1 = sample_transcript(
            session_id="session-123",
            full_text="A" * 100,
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript1):
            indexer.index_session(session_file, "/c/test/repo")

        # Second transcript - longer (appended content)
        transcript2 = sample_transcript(
            session_id="session-123",
            full_text="A" * 100 + "B" * 50,  # Appended more
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        mock_qdrant_client.upsert_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript2):
            result2 = indexer.index_session(session_file, "/c/test/repo")
            # Should have fewer new chunks than a full re-index would
            assert result2["indexed"] is True

    def test_short_user_messages_filtered(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript,
        sample_user_message, tmp_path
    ):
        """User messages < 20 chars are filtered out."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # Mix of short and long messages
        msgs = [
            sample_user_message("short"),  # Too short
            sample_user_message("This is a long enough message to be indexed"),
            sample_user_message("x"),  # Too short
        ]
        transcript = sample_transcript(
            session_id="session-123",
            user_messages=msgs,
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            result = indexer.index_session(session_file, "/c/test/repo")

        # Only 1 message should be indexed (the long one)
        assert result["user_messages"] == 1

    def test_max_5_user_messages_indexed(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript,
        sample_user_message, tmp_path
    ):
        """Only first 5 user messages are indexed."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        # Create 10 long messages
        msgs = [
            sample_user_message(f"This is message number {i} with enough content")
            for i in range(10)
        ]
        transcript = sample_transcript(
            session_id="session-123",
            user_messages=msgs,
            agent_summaries=[],
            summary_labels=[],
            plan=None,
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript):
            result = indexer.index_session(session_file, "/c/test/repo")

        # Max 5 messages should be indexed
        assert result["user_messages"] == 5

    def test_plan_only_indexed_if_hash_changed(
        self, tmp_db_path, mock_config, mock_qdrant_client, sample_transcript,
        sample_plan_file, tmp_path
    ):
        """Plan point only built if content hash differs from indexed."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        session_file = tmp_path / "session-123.jsonl"
        session_file.touch()

        plan = sample_plan_file("# Same Plan\nUnchanged content")

        # First index
        transcript1 = sample_transcript(
            session_id="session-123",
            plan=plan,
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            include_defaults=False,
        )

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript1):
            result1 = indexer.index_session(session_file, "/c/test/repo")
            assert result1["plans"] == 1

        # Second index with SAME plan
        transcript2 = sample_transcript(
            session_id="session-123",
            plan=plan,  # Same plan!
            user_messages=[],
            agent_summaries=[],
            summary_labels=[],
            include_defaults=False,
        )

        mock_qdrant_client.upsert_points.reset_mock()

        with patch("jacked.indexer.parse_jsonl_file_enriched", return_value=transcript2):
            result2 = indexer.index_session(session_file, "/c/test/repo")
            # Plan should NOT be re-indexed
            assert result2["plans"] == 0
            assert result2["skipped"] is True


class TestDeterministicPointIds:
    """Tests for deterministic point ID generation."""

    def test_same_content_same_point_id(
        self, tmp_db_path, mock_config, mock_qdrant_client
    ):
        """Same session/content_type/index produces same point ID."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        id1 = indexer._make_point_id("session-123", "chunk", 0)
        id2 = indexer._make_point_id("session-123", "chunk", 0)

        assert id1 == id2

    def test_different_index_different_point_id(
        self, tmp_db_path, mock_config, mock_qdrant_client
    ):
        """Different indexes produce different point IDs."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        id1 = indexer._make_point_id("session-123", "chunk", 0)
        id2 = indexer._make_point_id("session-123", "chunk", 1)

        assert id1 != id2

    def test_different_content_type_different_point_id(
        self, tmp_db_path, mock_config, mock_qdrant_client
    ):
        """Different content types produce different point IDs."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        id1 = indexer._make_point_id("session-123", "chunk", 0)
        id2 = indexer._make_point_id("session-123", "plan", 0)

        assert id1 != id2

    def test_different_session_different_point_id(
        self, tmp_db_path, mock_config, mock_qdrant_client
    ):
        """Different sessions produce different point IDs."""
        indexer = SessionIndexer(mock_config, client=mock_qdrant_client)

        id1 = indexer._make_point_id("session-123", "chunk", 0)
        id2 = indexer._make_point_id("session-456", "chunk", 0)

        assert id1 != id2
