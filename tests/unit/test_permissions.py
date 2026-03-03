"""Tests for jacked.api.routes.permissions module."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from jacked.api.routes.permissions import (
    _DANGEROUS_PATTERNS,
    _FILE_TOOL_PREFIX_RE,
    _FILE_TOOLS,
    _read_project_settings,
    _validate_repo_path,
    _write_project_settings,
)


class TestValidateRepoPath:
    def test_rejects_none(self):
        assert _validate_repo_path(None) is None

    def test_rejects_empty(self):
        assert _validate_repo_path("") is None

    def test_rejects_root(self):
        assert _validate_repo_path("/") is None

    def test_rejects_home(self):
        assert _validate_repo_path(str(Path.home())) is None

    def test_rejects_non_dir(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        assert _validate_repo_path(str(f)) is None

    def test_rejects_non_git_dir(self, tmp_path):
        assert _validate_repo_path(str(tmp_path)) is None

    def test_accepts_git_dir_no_db(self, tmp_path):
        (tmp_path / ".git").mkdir()
        assert _validate_repo_path(str(tmp_path)) == tmp_path

    def test_rejects_unknown_project_with_db(self, tmp_path):
        """DB check rejects git dirs not known to jacked."""
        (tmp_path / ".git").mkdir()
        mock_db = MagicMock()
        mock_db.get_project_activity_summary.return_value = []
        assert _validate_repo_path(str(tmp_path), db=mock_db) is None

    def test_accepts_known_project_with_db(self, tmp_path):
        """DB check passes for projects with jacked activity."""
        (tmp_path / ".git").mkdir()
        mock_db = MagicMock()
        mock_db.get_project_activity_summary.return_value = [
            {"repo_path": str(tmp_path)}
        ]
        assert _validate_repo_path(str(tmp_path), db=mock_db) == tmp_path

    def test_fails_closed_on_db_error(self, tmp_path):
        """DB errors reject (fail closed)."""
        (tmp_path / ".git").mkdir()
        mock_db = MagicMock()
        mock_db.get_project_activity_summary.side_effect = Exception("DB down")
        assert _validate_repo_path(str(tmp_path), db=mock_db) is None


class TestProjectSettings:
    def test_read_missing_file(self, tmp_path):
        result = _read_project_settings(str(tmp_path))
        assert result == {}

    def test_write_and_read_back(self, tmp_path):
        _write_project_settings(
            str(tmp_path), {"permissions": {"allow": ["Bash(curl:*)"]}}
        )
        result = _read_project_settings(str(tmp_path))
        assert result["permissions"]["allow"] == ["Bash(curl:*)"]

    def test_corrupt_json_returns_empty(self, tmp_path):
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        (claude_dir / "settings.local.json").write_text("not json")
        assert _read_project_settings(str(tmp_path)) == {}

    def test_write_creates_claude_dir(self, tmp_path):
        _write_project_settings(str(tmp_path), {"test": True})
        assert (tmp_path / ".claude" / "settings.local.json").exists()

    def test_atomic_write(self, tmp_path):
        """No .json.tmp left behind after write."""
        _write_project_settings(str(tmp_path), {"test": True})
        assert not (tmp_path / ".claude" / "settings.local.json.tmp").exists()


class TestDangerousPatterns:
    """Tests for the _DANGEROUS_PATTERNS blocklist and file-tool validation."""

    def test_bash_root_blocked(self):
        assert "Bash(*:*)" in _DANGEROUS_PATTERNS

    def test_bash_rm_blocked(self):
        assert "Bash(rm:*)" in _DANGEROUS_PATTERNS

    @pytest.mark.parametrize("tool", ["Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"])
    def test_file_tool_root_patterns_blocked(self, tool):
        """Root file-tool patterns like Read(/:*) are in the blocklist."""
        assert f"{tool}(/:*)" in _DANGEROUS_PATTERNS

    def test_file_tools_set_complete(self):
        """_FILE_TOOLS includes all 6 file tools."""
        assert _FILE_TOOLS == {"Read", "Edit", "Write", "Grep", "Glob", "NotebookEdit"}


class TestFileToolPrefixValidation:
    """Tests for the dynamic shallow-path validation regex."""

    def test_regex_matches_prefix_pattern(self):
        m = _FILE_TOOL_PREFIX_RE.match("Read(/Users/jack/project:*)")
        assert m is not None
        assert m.group(1) == "Read"
        assert m.group(2) == "/Users/jack/project"

    def test_regex_matches_single_segment(self):
        m = _FILE_TOOL_PREFIX_RE.match("Edit(/Users:*)")
        assert m is not None
        assert m.group(2) == "/Users"

    def test_regex_no_match_exact(self):
        """Exact path patterns (no :*) don't match the prefix regex."""
        m = _FILE_TOOL_PREFIX_RE.match("Read(/Users/jack/project/file.py)")
        assert m is None

    def test_regex_no_match_bash(self):
        m = _FILE_TOOL_PREFIX_RE.match("Bash(git push:*)")
        # Matches the regex shape but tool won't be in _FILE_TOOLS
        if m:
            assert m.group(1) not in _FILE_TOOLS

    @pytest.mark.parametrize(
        "pattern,should_reject",
        [
            ("Read(/:*)", True),           # root — 0 segments
            ("Read(/Users:*)", True),      # 1 segment
            ("Edit(/Users/jack:*)", True), # 2 segments
            ("Read(/Users/jack/project:*)", False),  # 3 segments — OK
            ("Write(/a/b/c/d:*)", False),  # 4 segments — OK
        ],
    )
    def test_segment_count_validation(self, pattern, should_reject):
        """Prefix patterns with < 3 path segments should be rejected."""
        m = _FILE_TOOL_PREFIX_RE.match(pattern)
        assert m is not None
        tool = m.group(1)
        path_prefix = m.group(2)
        segments = [s for s in path_prefix.split("/") if s]
        too_broad = tool in _FILE_TOOLS and len(segments) < 3
        assert too_broad == should_reject
