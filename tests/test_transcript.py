"""Tests for transcript module."""

import json
import tempfile
from pathlib import Path

import pytest

from jacked.transcript import (
    parse_jsonl_file,
    chunk_text,
    chunk_intent_text,
    _strip_xml_tags,
    _is_command_only,
    _is_uuid_format,
    _decode_repo_path,
)


class TestStripXmlTags:
    """Tests for _strip_xml_tags function."""

    def test_removes_command_tags(self):
        """Should remove command-related XML tags."""
        text = "<command-name>test</command-name> hello"
        result = _strip_xml_tags(text)
        assert "command-name" not in result
        assert "hello" in result

    def test_keeps_stdout_content(self):
        """Should keep content from stdout tags."""
        text = "<local-command-stdout>output here</local-command-stdout>"
        result = _strip_xml_tags(text)
        assert "output here" in result
        assert "<local-command-stdout>" not in result

    def test_plain_text_unchanged(self):
        """Plain text should pass through unchanged."""
        text = "plain text without tags"
        result = _strip_xml_tags(text)
        assert result == text


class TestIsCommandOnly:
    """Tests for _is_command_only function."""

    def test_slash_command(self):
        """Slash commands should be command-only."""
        assert _is_command_only("/login") is True
        assert _is_command_only("/help") is True

    def test_command_with_args(self):
        """Commands with arguments are not command-only."""
        assert _is_command_only("/search something") is False

    def test_normal_text(self):
        """Normal text should not be command-only."""
        assert _is_command_only("help me with this code") is False

    def test_empty_string(self):
        """Empty string is command-only."""
        assert _is_command_only("") is True
        assert _is_command_only("   ") is True

    def test_short_text(self):
        """Very short text is considered command-only."""
        assert _is_command_only("ok") is True
        assert _is_command_only("yes") is True


class TestIsUuidFormat:
    """Tests for _is_uuid_format function."""

    def test_valid_uuid(self):
        """Valid UUIDs should match."""
        assert _is_uuid_format("533e6824-6fb0-4f12-a406-517d2677734e") is True
        assert _is_uuid_format("AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE") is True

    def test_agent_id(self):
        """Agent IDs are not UUIDs."""
        assert _is_uuid_format("agent-a08e819") is False

    def test_random_string(self):
        """Random strings are not UUIDs."""
        assert _is_uuid_format("hello-world") is False


class TestDecodeRepoPath:
    """Tests for _decode_repo_path function."""

    def test_windows_path(self):
        """Should decode Windows-style encoded paths."""
        encoded = "C--Github-my-project"
        result = _decode_repo_path(encoded)
        assert result == "C:/Github/my/project"  # Note: hyphens become slashes

    def test_drive_letter(self):
        """Should handle drive letter correctly."""
        encoded = "F--act-envision"
        result = _decode_repo_path(encoded)
        assert result.startswith("F:/")


class TestChunkText:
    """Tests for chunk_text function."""

    def test_short_text_single_chunk(self):
        """Short text should be a single chunk."""
        text = "Hello world"
        chunks = chunk_text(text, chunk_size=100)
        assert len(chunks) == 1
        assert chunks[0] == text

    def test_long_text_multiple_chunks(self):
        """Long text should be split into chunks."""
        text = "Hello world " * 100  # 1200 chars
        chunks = chunk_text(text, chunk_size=100, overlap=20)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 100

    def test_empty_text(self):
        """Empty text should return empty list."""
        chunks = chunk_text("")
        assert chunks == []

    def test_respects_word_boundaries(self):
        """Chunks should try to break at word boundaries."""
        text = "word " * 30  # 150 chars
        chunks = chunk_text(text, chunk_size=50, overlap=10)
        # Should not break in middle of "word"
        for chunk in chunks:
            assert not chunk.endswith("wor")


class TestChunkIntentText:
    """Tests for chunk_intent_text function."""

    def test_short_intent(self):
        """Short intent should be single chunk."""
        text = "help me fix this bug"
        chunks = chunk_intent_text(text, max_tokens=100)
        assert len(chunks) == 1

    def test_long_intent(self):
        """Long intent should be chunked."""
        text = "help me " * 500  # Very long
        chunks = chunk_intent_text(text, max_tokens=100)
        assert len(chunks) > 1


class TestParseJsonlFile:
    """Tests for parse_jsonl_file function."""

    def test_parse_simple_session(self, tmp_path):
        """Should parse a simple session file."""
        session_file = tmp_path / "test-session.jsonl"

        lines = [
            {
                "type": "user",
                "message": {"role": "user", "content": "Hello Claude"},
                "timestamp": "2025-01-18T10:00:00Z",
                "uuid": "msg-1",
                "isMeta": False,
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hello! How can I help?"}],
                },
                "timestamp": "2025-01-18T10:00:01Z",
                "uuid": "msg-2",
            },
        ]

        with open(session_file, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        transcript = parse_jsonl_file(session_file)

        assert transcript.session_id == "test-session"
        assert len(transcript.messages) == 2
        assert len(transcript.user_messages) == 1
        assert "Hello Claude" in transcript.intent_text
        assert "USER:" in transcript.full_text
        assert "ASSISTANT:" in transcript.full_text

    def test_skips_meta_messages(self, tmp_path):
        """Should skip meta messages from intent."""
        session_file = tmp_path / "test-session.jsonl"

        lines = [
            {
                "type": "user",
                "message": {"role": "user", "content": "System message"},
                "timestamp": "2025-01-18T10:00:00Z",
                "uuid": "msg-1",
                "isMeta": True,
            },
            {
                "type": "user",
                "message": {"role": "user", "content": "Real user message"},
                "timestamp": "2025-01-18T10:00:01Z",
                "uuid": "msg-2",
                "isMeta": False,
            },
        ]

        with open(session_file, "w") as f:
            for line in lines:
                f.write(json.dumps(line) + "\n")

        transcript = parse_jsonl_file(session_file)

        # Only non-meta message should be in intent
        assert len(transcript.user_messages) == 1
        assert "Real user message" in transcript.intent_text
        assert "System message" not in transcript.intent_text

    def test_file_not_found(self, tmp_path):
        """Should raise error for missing file."""
        with pytest.raises(FileNotFoundError):
            parse_jsonl_file(tmp_path / "nonexistent.jsonl")
