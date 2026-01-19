"""Tests for config module."""

import os
import pytest

from jacked.config import (
    SmartForkConfig,
    get_repo_id,
    get_repo_name,
    content_hash,
)


class TestGetRepoId:
    """Tests for get_repo_id function."""

    def test_consistent_hash(self):
        """Same path should always produce same hash."""
        path = "/c/Github/my-project"
        hash1 = get_repo_id(path)
        hash2 = get_repo_id(path)
        assert hash1 == hash2

    def test_different_paths_different_hashes(self):
        """Different paths should produce different hashes."""
        hash1 = get_repo_id("/c/Github/project-a")
        hash2 = get_repo_id("/c/Github/project-b")
        assert hash1 != hash2

    def test_normalizes_slashes(self):
        """Forward and backslashes should produce same hash."""
        hash1 = get_repo_id("/c/Github/project")
        hash2 = get_repo_id("\\c\\Github\\project")
        assert hash1 == hash2

    def test_case_insensitive(self):
        """Paths should be case-insensitive."""
        hash1 = get_repo_id("/c/Github/Project")
        hash2 = get_repo_id("/c/github/project")
        assert hash1 == hash2

    def test_returns_8_chars(self):
        """Hash should be 8 characters."""
        result = get_repo_id("/c/Github/my-project")
        assert len(result) == 8


class TestGetRepoName:
    """Tests for get_repo_name function."""

    def test_extracts_last_component(self):
        """Should extract the repository name from path."""
        assert get_repo_name("/c/Github/my-project") == "my-project"

    def test_handles_windows_paths(self):
        """Should handle Windows-style paths."""
        assert get_repo_name("C:\\Github\\my-project") == "my-project"

    def test_strips_trailing_slash(self):
        """Should handle trailing slashes."""
        assert get_repo_name("/c/Github/my-project/") == "my-project"


class TestContentHash:
    """Tests for content_hash function."""

    def test_consistent_hash(self):
        """Same content should produce same hash."""
        content = "Hello, world!"
        hash1 = content_hash(content)
        hash2 = content_hash(content)
        assert hash1 == hash2

    def test_different_content_different_hash(self):
        """Different content should produce different hash."""
        hash1 = content_hash("Hello")
        hash2 = content_hash("World")
        assert hash1 != hash2

    def test_has_sha256_prefix(self):
        """Hash should have sha256: prefix."""
        result = content_hash("test")
        assert result.startswith("sha256:")


class TestSmartForkConfig:
    """Tests for SmartForkConfig class."""

    def test_from_env_with_env_vars(self, monkeypatch):
        """Should load config from environment variables."""
        monkeypatch.setenv("QDRANT_CLAUDE_SESSIONS_ENDPOINT", "https://test.qdrant.io")
        monkeypatch.setenv("QDRANT_CLAUDE_SESSIONS_API_KEY", "test-key")

        config = SmartForkConfig.from_env()

        assert config.qdrant_endpoint == "https://test.qdrant.io"
        assert config.qdrant_api_key == "test-key"
        assert config.collection_name == "claude_sessions"

    def test_from_env_missing_endpoint(self, monkeypatch):
        """Should raise error if endpoint missing."""
        # Mock load_dotenv to prevent it from loading .env file
        monkeypatch.setattr("jacked.config.load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.delenv("QDRANT_CLAUDE_SESSIONS_ENDPOINT", raising=False)
        monkeypatch.setenv("QDRANT_CLAUDE_SESSIONS_API_KEY", "test-key")

        with pytest.raises(ValueError, match="QDRANT_CLAUDE_SESSIONS_ENDPOINT"):
            SmartForkConfig.from_env()

    def test_from_env_missing_api_key(self, monkeypatch):
        """Should raise error if API key missing."""
        # Mock load_dotenv to prevent it from loading .env file
        monkeypatch.setattr("jacked.config.load_dotenv", lambda *args, **kwargs: None)
        monkeypatch.setenv("QDRANT_CLAUDE_SESSIONS_ENDPOINT", "https://test.qdrant.io")
        monkeypatch.delenv("QDRANT_CLAUDE_SESSIONS_API_KEY", raising=False)

        with pytest.raises(ValueError, match="QDRANT_CLAUDE_SESSIONS_API_KEY"):
            SmartForkConfig.from_env()

    def test_validate_valid_config(self, monkeypatch):
        """Valid config should pass validation."""
        monkeypatch.setenv("QDRANT_CLAUDE_SESSIONS_ENDPOINT", "https://test.qdrant.io")
        monkeypatch.setenv("QDRANT_CLAUDE_SESSIONS_API_KEY", "test-key")

        config = SmartForkConfig.from_env()
        assert config.validate() is True

    def test_validate_invalid_endpoint(self):
        """Invalid endpoint should fail validation."""
        config = SmartForkConfig(
            qdrant_endpoint="not-a-url",
            qdrant_api_key="test-key",
        )

        with pytest.raises(ValueError, match="Invalid Qdrant endpoint"):
            config.validate()
