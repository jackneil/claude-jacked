"""Unit tests for jacked.web.analytics_paths — cross-platform path resolution
and Claude Code project hash decoding."""

import os
import time
from pathlib import Path
from unittest.mock import patch

from jacked.web.analytics_paths import (
    decode_project_hash,
    find_active_jsonl_files,
    get_claude_data_dirs,
)


# ------------------------------------------------------------------
# get_claude_data_dirs
# ------------------------------------------------------------------

class TestGetClaudeDataDirs:
    def test_returns_list_of_paths(self):
        dirs = get_claude_data_dirs()
        assert isinstance(dirs, list)
        for d in dirs:
            assert isinstance(d, Path)

    def test_includes_home_projects_dir(self):
        dirs = get_claude_data_dirs()
        home = Path.home()
        expected = home / ".claude" / "projects"
        assert expected in dirs

    @patch("sys.platform", "linux")
    def test_xdg_fallback_included_when_exists(self, tmp_path):
        xdg_dir = tmp_path / ".config" / "claude" / "projects"
        xdg_dir.mkdir(parents=True)
        with patch("pathlib.Path.home", return_value=tmp_path):
            dirs = get_claude_data_dirs()
        assert xdg_dir in dirs

    @patch("sys.platform", "darwin")
    def test_xdg_fallback_excluded_on_macos(self, tmp_path):
        xdg_dir = tmp_path / ".config" / "claude" / "projects"
        xdg_dir.mkdir(parents=True)
        with patch("pathlib.Path.home", return_value=tmp_path):
            dirs = get_claude_data_dirs()
        assert xdg_dir not in dirs

    @patch("sys.platform", "linux")
    def test_xdg_fallback_excluded_when_missing(self, tmp_path):
        with patch("pathlib.Path.home", return_value=tmp_path):
            dirs = get_claude_data_dirs()
        xdg_dir = tmp_path / ".config" / "claude" / "projects"
        assert xdg_dir not in dirs


# ------------------------------------------------------------------
# decode_project_hash — filesystem probing tests
# ------------------------------------------------------------------

def _build_fs_tree(tmp_path, path_parts):
    """Create a real directory tree under tmp_path for filesystem probing."""
    current = tmp_path
    for part in path_parts:
        current = current / part
    current.mkdir(parents=True, exist_ok=True)
    return current


class TestDecodeProjectHash:
    def test_simple_project_with_fs(self, tmp_path):
        """Filesystem probing resolves 'claude-jacked' as a single dir."""
        _build_fs_tree(tmp_path, ["Users", "jack", "Github", "claude-jacked"])
        # The hash represents: /<tmp>/Users/jack/Github/claude-jacked
        # We need to make the probe root be tmp_path.
        # Since the hash starts with -, the fs_root is "/" — we'll patch
        # _probe_filesystem to use our tmp tree.
        with patch(
            "jacked.web.analytics_paths._probe_filesystem"
        ) as mock_probe:
            mock_probe.return_value = [
                "Users", "jack", "Github", "claude-jacked"
            ]
            result = decode_project_hash("-Users-jack-Github-claude-jacked")
        assert result["name"] == "claude-jacked"
        assert result["path"] == "Users/jack/Github/claude-jacked"

    def test_real_filesystem_probing(self):
        """On a real system, /Users should exist and be resolved correctly."""
        result = decode_project_hash("-Users-jack-Github-claude-jacked")
        # /Users exists on macOS, so 'Users' resolves as one segment.
        # The rest falls through as individual segments or merged.
        assert result["path"].startswith("Users/")
        # Name should contain the tail segments
        assert "jacked" in result["name"]

    def test_deep_nesting(self, tmp_path):
        _build_fs_tree(
            tmp_path,
            ["Users", "jack", "neil", "conductor", "workspaces",
             "hank", "ehr", "kolkata"],
        )
        with patch(
            "jacked.web.analytics_paths._probe_filesystem"
        ) as mock_probe:
            mock_probe.return_value = [
                "Users", "jack", "neil", "conductor", "workspaces",
                "hank", "ehr", "kolkata",
            ]
            result = decode_project_hash(
                "-Users-jack-neil-conductor-workspaces-hank-ehr-kolkata"
            )
        assert result["path"] == (
            "Users/jack/neil/conductor/workspaces/hank/ehr/kolkata"
        )
        assert "kolkata" in result["name"]

    def test_windows_drive_letter(self):
        """Windows drive prefix C-- is decoded to C:/."""
        with patch(
            "jacked.web.analytics_paths._probe_filesystem"
        ) as mock_probe:
            mock_probe.return_value = ["Users", "bob", "repos", "myapp"]
            result = decode_project_hash("C---Users-bob-repos-myapp")
        assert result["path"] == "C:/Users/bob/repos/myapp"
        assert result["name"] == "myapp"

    def test_unknown_hash(self):
        result = decode_project_hash("unknown")
        assert result["name"] == "unknown"
        assert result["path"] == "unknown"

    def test_empty_string(self):
        result = decode_project_hash("")
        assert result["name"] == ""
        assert result["path"] == ""

    def test_short_hash(self):
        with patch(
            "jacked.web.analytics_paths._probe_filesystem"
        ) as mock_probe:
            mock_probe.return_value = ["home", "x"]
            result = decode_project_hash("-home-x")
        assert result["path"] == "home/x"
        assert result["name"] == "x"

    def test_worktree_suffix(self):
        with patch(
            "jacked.web.analytics_paths._probe_filesystem"
        ) as mock_probe:
            mock_probe.return_value = [
                "Users", "jack", "Github", "claude-jacked"
            ]
            result = decode_project_hash(
                "-Users-jack-Github-claude-jacked"
                "--claude-worktrees-agent-a7c6b155"
            )
        assert "claude-jacked" in result["name"]
        assert result["path"] == (
            "Users/jack/Github/claude-jacked"
            "/.claude-worktrees/agent-a7c6b155"
        )

    def test_skips_common_prefixes(self):
        """users, home, documents, desktop are stripped from display name."""
        with patch(
            "jacked.web.analytics_paths._probe_filesystem"
        ) as mock_probe:
            mock_probe.return_value = [
                "Users", "alice", "Documents", "projects", "cool-app"
            ]
            result = decode_project_hash(
                "-Users-alice-Documents-projects-cool-app"
            )
        assert result["name"] == "cool-app"

    def test_preserves_full_path(self):
        """The path field always contains the complete decoded path."""
        with patch(
            "jacked.web.analytics_paths._probe_filesystem"
        ) as mock_probe:
            mock_probe.return_value = [
                "Users", "alice", "Documents", "projects", "cool-app"
            ]
            result = decode_project_hash(
                "-Users-alice-Documents-projects-cool-app"
            )
        assert result["path"] == "Users/alice/Documents/projects/cool-app"

    def test_probe_fallback_on_exception(self):
        """If filesystem probing raises, falls back to naive split."""
        with patch(
            "jacked.web.analytics_paths._probe_filesystem",
            side_effect=RuntimeError("boom"),
        ):
            result = decode_project_hash("-a-b-c")
        # Falls back to naive: each dash becomes a separator
        assert result["path"] == "a/b/c"


# ------------------------------------------------------------------
# _probe_filesystem (integration with real fs)
# ------------------------------------------------------------------

class TestProbeFilesystem:
    def test_merges_dashed_dirs(self, tmp_path):
        """Correctly merges segments when a dashed directory exists."""
        from jacked.web.analytics_paths import _probe_filesystem

        (tmp_path / "my-project").mkdir()
        (tmp_path / "my-project" / "src").mkdir()

        resolved = _probe_filesystem(str(tmp_path), ["my", "project", "src"])
        assert resolved == ["my-project", "src"]

    def test_no_merge_when_separate_dirs_exist(self, tmp_path):
        """Keeps segments separate when each is a real directory."""
        from jacked.web.analytics_paths import _probe_filesystem

        (tmp_path / "my").mkdir()
        (tmp_path / "my" / "project").mkdir()

        resolved = _probe_filesystem(str(tmp_path), ["my", "project"])
        assert resolved == ["my", "project"]

    def test_empty_segments(self, tmp_path):
        from jacked.web.analytics_paths import _probe_filesystem

        assert _probe_filesystem(str(tmp_path), []) == []


# ------------------------------------------------------------------
# find_active_jsonl_files
# ------------------------------------------------------------------

class TestFindActiveJsonlFiles:
    def test_finds_recent_files(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-jack-proj"
        proj.mkdir(parents=True)
        recent = proj / "conversation.jsonl"
        recent.write_text('{"role":"user"}\n')
        files = find_active_jsonl_files(
            [tmp_path / "projects"], max_age_seconds=600
        )
        assert recent in files

    def test_ignores_old_files(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-jack-proj"
        proj.mkdir(parents=True)
        old = proj / "conversation.jsonl"
        old.write_text('{"role":"user"}\n')
        old_mtime = time.time() - 7200
        os.utime(old, (old_mtime, old_mtime))
        files = find_active_jsonl_files(
            [tmp_path / "projects"], max_age_seconds=600
        )
        assert old not in files

    def test_finds_subagent_files(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-jack-proj"
        subagents = proj / "subagents"
        subagents.mkdir(parents=True)
        sub_file = subagents / "agent-abc123.jsonl"
        sub_file.write_text('{"role":"assistant"}\n')
        files = find_active_jsonl_files(
            [tmp_path / "projects"], max_age_seconds=600
        )
        assert sub_file in files

    def test_handles_permission_error(self, tmp_path):
        """Should not raise even if a directory is unreadable."""
        proj = tmp_path / "projects" / "-Users-jack-proj"
        proj.mkdir(parents=True)
        bad_dir = tmp_path / "nonexistent"
        files = find_active_jsonl_files(
            [bad_dir, tmp_path / "projects"], max_age_seconds=600
        )
        assert isinstance(files, list)

    def test_empty_data_dirs(self):
        files = find_active_jsonl_files([], max_age_seconds=600)
        assert files == []

    def test_ignores_non_jsonl_files(self, tmp_path):
        proj = tmp_path / "projects" / "-Users-jack-proj"
        proj.mkdir(parents=True)
        txt = proj / "notes.txt"
        txt.write_text("not a jsonl")
        jsonl = proj / "conversation.jsonl"
        jsonl.write_text('{"ok":true}\n')
        files = find_active_jsonl_files(
            [tmp_path / "projects"], max_age_seconds=600
        )
        assert jsonl in files
        assert txt not in files

    def test_multiple_data_dirs(self, tmp_path):
        dir_a = tmp_path / "a" / "projects"
        dir_b = tmp_path / "b" / "projects"
        proj_a = dir_a / "-proj-a"
        proj_b = dir_b / "-proj-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        file_a = proj_a / "conv.jsonl"
        file_b = proj_b / "conv.jsonl"
        file_a.write_text("{}\n")
        file_b.write_text("{}\n")
        files = find_active_jsonl_files([dir_a, dir_b], max_age_seconds=600)
        assert file_a in files
        assert file_b in files
