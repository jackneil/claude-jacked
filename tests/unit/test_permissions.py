"""Tests for jacked.api.routes.permissions module."""

import asyncio
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
from jacked.api.project_paths import ProjectRefused
from tests._known_projects import KnownProjects, request_for
from tests._platform import requires_symlinks


def _refusal(repo_path, db):
    with pytest.raises(ProjectRefused) as refused:
        _validate_repo_path(repo_path, db)
    return refused.value.status_code, refused.value.code


class TestValidateRepoPath:
    def test_rejects_none(self):
        assert _refusal(None, KnownProjects()) == (422, "INVALID_REPO_PATH")

    def test_rejects_empty(self):
        assert _refusal("", KnownProjects()) == (422, "INVALID_REPO_PATH")

    def test_rejects_root(self):
        root = Path.cwd().anchor
        assert _refusal(root, KnownProjects(root)) == (422, "INVALID_REPO_PATH")

    def test_rejects_home(self):
        """``.claude`` in the home folder is the global settings folder."""
        home = str(Path.home())
        assert _refusal(home, KnownProjects(home)) == (422, "INVALID_REPO_PATH")

    def test_rejects_non_dir(self, tmp_path):
        f = tmp_path / "file.txt"
        f.write_text("x")
        assert _refusal(str(f), KnownProjects(f)) == (422, "NOT_DIRECTORY")

    def test_rejects_non_git_dir(self, tmp_path):
        assert _refusal(str(tmp_path), KnownProjects(tmp_path)) == (422, "NOT_GIT_REPO")

    def test_rejects_a_git_dir_without_a_db(self, tmp_path):
        """No project list: refuse, never open any git folder (fail closed)."""
        (tmp_path / ".git").mkdir()
        assert _refusal(str(tmp_path), None) == (503, "DB_UNAVAILABLE")

    def test_rejects_unknown_project_with_db(self, tmp_path):
        """DB check rejects git dirs not known to jacked."""
        (tmp_path / ".git").mkdir()
        assert _refusal(str(tmp_path), KnownProjects()) == (422, "UNKNOWN_PROJECT")

    def test_accepts_known_project_with_db(self, tmp_path):
        """DB check passes for projects with jacked activity."""
        (tmp_path / ".git").mkdir()
        assert _validate_repo_path(str(tmp_path), KnownProjects(tmp_path)) == tmp_path

    def test_accepts_a_git_worktree(self, tmp_path):
        """A linked worktree has a ``.git`` file, not a folder."""
        (tmp_path / ".git").write_text("gitdir: /elsewhere\n")
        assert _validate_repo_path(str(tmp_path), KnownProjects(tmp_path)) == tmp_path

    def test_fails_closed_on_db_error(self, tmp_path):
        """DB errors reject (fail closed)."""
        (tmp_path / ".git").mkdir()
        mock_db = MagicMock()
        mock_db.get_project_activity_summary.side_effect = Exception("DB down")
        assert _refusal(str(tmp_path), mock_db) == (503, "DB_ERROR")


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


def _project_routes(repo: Path) -> dict:
    """Each project-settings route, called as the dashboard calls it."""
    from jacked.api.routes import permissions as p

    def _request():
        return request_for(repo)

    async def put():
        return await p.set_project_permissions(
            p.ProjectPermissionsRequest(repo_path=str(repo), allow=["Bash(git status:*)"]),
            _request(),
        )

    async def rule():
        return await p.add_permission_rule(
            p.AddRuleRequest(pattern="Bash(git status:*)", scope="project", repo_path=str(repo)),
            _request(),
        )

    async def get():
        return p.get_project_permissions(str(repo), _request())

    return {"get": get, "put": put, "rule": rule}


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    return repo


class TestProjectSettingsStayInTheProject:
    """A repository can ship ``.claude`` (or its settings.local.json) as a
    link to a place outside the project. The routes refuse it with a 422:
    a write would leave the project, and a read would copy outside content
    (the user's global settings, for example) into the project's file."""

    @requires_symlinks
    @pytest.mark.parametrize("route", ["get", "put", "rule"])
    def test_a_claude_dir_linked_outside_is_refused(self, tmp_path, route):
        repo = _repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        (repo / ".claude").symlink_to(outside, target_is_directory=True)

        result = asyncio.run(_project_routes(repo)[route]())

        assert result.status_code == 422
        error = json.loads(result.body)["error"]
        assert error["code"] == "LINK_OUTSIDE_PROJECT"
        assert error["message"].startswith("The .claude path in this project is a link")
        assert list(outside.iterdir()) == []  # nothing written outside

    @requires_symlinks
    @pytest.mark.parametrize("route", ["get", "put", "rule"])
    def test_a_settings_file_linked_outside_is_refused(self, tmp_path, route):
        repo = _repo(tmp_path)
        (repo / ".claude").mkdir()
        global_settings = tmp_path / "home-settings.json"
        global_settings.write_text('{"env": {"API_KEY": "secret"}}', encoding="utf-8")
        link = repo / ".claude" / "settings.local.json"
        link.symlink_to(global_settings)

        result = asyncio.run(_project_routes(repo)[route]())

        assert result.status_code == 422
        error = json.loads(result.body)["error"]
        assert error["message"].startswith(
            "The .claude/settings.local.json path in this project is a link"
        )
        assert link.is_symlink()  # not replaced by a copy of the outside file
        assert global_settings.read_text(encoding="utf-8") == '{"env": {"API_KEY": "secret"}}'

    @pytest.mark.parametrize("route", ["put", "rule"])
    def test_a_normal_claude_dir_is_written(self, tmp_path, route):
        repo = _repo(tmp_path)

        result = asyncio.run(_project_routes(repo)[route]())

        assert not hasattr(result, "status_code"), result.body
        written = json.loads((repo / ".claude" / "settings.local.json").read_text())
        assert written["permissions"]["allow"] == ["Bash(git status:*)"]

    @requires_symlinks
    @pytest.mark.parametrize("route", ["put", "rule"])
    def test_a_claude_dir_linked_outside_after_the_check_is_refused(
        self, tmp_path, route, monkeypatch
    ):
        """The route checks the link before it takes the lock. A link made
        between that check and the write (another local process racing the
        dashboard) is caught by the writer's own check, right before the
        write, and nothing lands outside the project."""
        from jacked.api.routes import permissions as p

        repo = _repo(tmp_path)
        outside = tmp_path / "outside"
        outside.mkdir()
        read = p._read_project_settings

        def plant_the_link_then_read(repo_path):
            (repo / ".claude").symlink_to(outside, target_is_directory=True)
            return read(repo_path)

        monkeypatch.setattr(p, "_read_project_settings", plant_the_link_then_read)

        result = asyncio.run(_project_routes(repo)[route]())

        assert result.status_code == 422
        error = json.loads(result.body)["error"]
        assert error["code"] == "LINK_OUTSIDE_PROJECT"
        assert error["message"].startswith("The .claude path in this project is a link")
        assert list(outside.iterdir()) == []  # nothing written outside

    @requires_symlinks
    def test_a_claude_dir_linked_inside_the_project_is_allowed(self, tmp_path):
        repo = _repo(tmp_path)
        (repo / "config" / "claude").mkdir(parents=True)
        (repo / ".claude").symlink_to(repo / "config" / "claude", target_is_directory=True)

        result = asyncio.run(_project_routes(repo)["put"]())

        assert not hasattr(result, "status_code"), result.body
        assert (repo / "config" / "claude" / "settings.local.json").exists()


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
