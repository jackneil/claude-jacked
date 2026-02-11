"""Shared fixtures for jacked tests."""

import pytest
from pathlib import Path
from unittest.mock import Mock
from datetime import datetime

from jacked.config import SmartForkConfig
from jacked.transcript import (
    EnrichedTranscript,
    TranscriptMessage,
    PlanFile,
    AgentSummary,
    SummaryLabel,
)


@pytest.fixture
def tmp_db_path(tmp_path, monkeypatch):
    """Temporary path for SQLite database, patched into IndexWriteTracker."""
    db_path = tmp_path / "test_tracker.db"
    monkeypatch.setattr("jacked.index_write_tracker.DB_PATH", db_path)
    return db_path


@pytest.fixture
def mock_config():
    """Mock SmartForkConfig with test values."""
    config = Mock(spec=SmartForkConfig)
    config.chunk_size = 4000
    config.chunk_overlap = 200
    config.user_name = "test_user"
    config.machine_name = "test_machine"
    config.collection_name = "test_collection"
    config.qdrant_endpoint = "http://localhost:6333"
    config.qdrant_api_key = "test_key"
    config.claude_projects_dir = Path.home() / ".claude" / "projects"
    return config


@pytest.fixture
def mock_qdrant_client():
    """Mock QdrantSessionClient for unit tests."""
    client = Mock()
    client.ensure_collection.return_value = True
    client.upsert_points.return_value = True
    client.get_session_points.return_value = []
    return client


@pytest.fixture
def sample_user_message():
    """Factory for TranscriptMessage user objects."""
    def _create(content="Test user message content here that is long enough"):
        return TranscriptMessage(
            role="user",
            content=content,
            timestamp=datetime.now(),
            uuid="msg-123",
            is_meta=False,
        )
    return _create


@pytest.fixture
def sample_plan_file():
    """Factory for PlanFile objects."""
    def _create(content="# Test Plan\n\nThis is a test plan."):
        return PlanFile(
            slug="test-slug",
            path=Path("/fake/path/test-slug.md"),
            content=content,
        )
    return _create


@pytest.fixture
def sample_agent_summary():
    """Factory for AgentSummary objects."""
    def _create(summary_text="## Summary\n\nThis is an agent summary."):
        return AgentSummary(
            agent_id="agent-abc123",
            agent_type="Explore",
            summary_text=summary_text,
            timestamp=datetime.now(),
        )
    return _create


@pytest.fixture
def sample_summary_label():
    """Factory for SummaryLabel objects."""
    def _create(label="Implementing authentication flow"):
        return SummaryLabel(
            label=label,
            leaf_uuid="label-uuid-123",
            timestamp=datetime.now(),
        )
    return _create


@pytest.fixture
def sample_transcript(sample_user_message, sample_plan_file, sample_agent_summary, sample_summary_label):
    """Factory for creating EnrichedTranscript objects."""
    def _create(
        session_id="test-session-123",
        full_text="Sample transcript content that is long enough for testing purposes and more.",
        user_messages=None,
        agent_summaries=None,
        summary_labels=None,
        plan=None,
        include_defaults=True,
    ):
        if include_defaults:
            user_messages = user_messages or [sample_user_message() for _ in range(3)]
            agent_summaries = agent_summaries or [sample_agent_summary()]
            summary_labels = summary_labels or [sample_summary_label()]
            plan = plan if plan is not None else sample_plan_file()
        else:
            user_messages = user_messages or []
            agent_summaries = agent_summaries or []
            summary_labels = summary_labels or []

        return EnrichedTranscript(
            session_id=session_id,
            messages=[],
            user_messages=user_messages,
            full_text=full_text,
            intent_text="test intent",
            timestamp=datetime.now(),
            summary_labels=summary_labels,
            agent_summaries=agent_summaries,
            plan=plan,
            slug="test-slug",
        )
    return _create


@pytest.fixture
def sample_qdrant_points():
    """Factory for mock Qdrant point responses."""
    def _create(session_id, content_types):
        """
        Create mock Qdrant points.

        Args:
            session_id: Session UUID
            content_types: Dict mapping content_type -> count
                e.g., {"chunk": 3, "plan": 1} creates 3 chunk points and 1 plan point
        """
        points = []
        for ct, count in content_types.items():
            for i in range(count):
                point = Mock()
                point.id = f"{session_id}:{ct}:{i}"
                point.payload = {
                    "session_id": session_id,
                    "content_type": ct,
                    "chunk_index": i,
                    "content_hash": f"sha256:hash_{ct}_{i}",
                    "user_name": "test_user",
                }
                points.append(point)
        return points
    return _create
