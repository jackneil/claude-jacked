"""Tests for QA suggestion Stop hook (qa_suggest.py)."""

import json
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest


class TestHasUiChanges:
    """Test _has_ui_changes() file filtering logic."""

    def test_detects_js_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["src/app.js"]) is True

    def test_detects_tsx_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["components/Button.tsx"]) is True

    def test_detects_css_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["styles/main.css"]) is True

    def test_detects_html_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["index.html"]) is True

    def test_detects_vue_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["App.vue"]) is True

    def test_detects_svelte_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["Page.svelte"]) is True

    def test_detects_scss_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["theme.scss"]) is True

    def test_detects_jinja_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["templates/base.jinja2"]) is True

    def test_ignores_python_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["server.py"]) is False

    def test_ignores_markdown_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["README.md"]) is False

    def test_ignores_yaml_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["config.yaml"]) is False

    def test_empty_list(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes([]) is False

    def test_excludes_node_modules(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["node_modules/react/index.js"]) is False

    def test_excludes_dist(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["dist/bundle.js"]) is False

    def test_excludes_build(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["build/static/app.css"]) is False

    def test_excludes_test_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["Button.test.tsx"]) is False

    def test_excludes_spec_files(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["Button.spec.js"]) is False

    def test_excludes_pycache(self):
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["__pycache__/template.js"]) is False

    def test_mixed_files_with_one_ui(self):
        """Returns True if at least one file is a non-excluded UI file."""
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert _has_ui_changes(["server.py", "README.md", "app.jsx"]) is True

    def test_all_excluded_ui_files(self):
        """All UI-extension files are in excluded dirs."""
        from jacked.data.hooks.qa_suggest import _has_ui_changes

        assert (
            _has_ui_changes(["node_modules/x.js", "dist/y.css", "z.test.tsx"]) is False
        )


class TestShouldSuggest:
    """Test _should_suggest() gating logic."""

    def test_returns_true_with_ui_changes(self, tmp_path):
        """Suggests when UI changes exist and no dedup file."""
        from jacked.data.hooks.qa_suggest import _should_suggest

        cwd = str(tmp_path)
        # Create a fake .git dir
        (tmp_path / ".git").mkdir()

        # Explicitly clear subagent env vars to prevent flakes in CI
        env_clear = {
            "CLAUDE_CODE_PARENT_SESSION_ID": "",
            "CLAUDE_CODE_AGENT_TYPE": "",
        }
        with patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ), patch.dict(os.environ, env_clear, clear=False):
            # Remove keys (empty string still passes the `or` check, so pop them)
            os.environ.pop("CLAUDE_CODE_PARENT_SESSION_ID", None)
            os.environ.pop("CLAUDE_CODE_AGENT_TYPE", None)
            assert _should_suggest("test-session", cwd, tmp_path / "dedup") is True

    def test_returns_false_when_no_git_dir(self, tmp_path):
        """Early-exits when cwd has no .git directory."""
        from jacked.data.hooks.qa_suggest import _should_suggest

        cwd = str(tmp_path)
        # No .git dir

        result = _should_suggest("test-session", cwd, tmp_path / "dedup")
        assert result is False

    def test_returns_false_when_dedup_file_exists(self, tmp_path):
        """Skips when already suggested this session."""
        from jacked.data.hooks.qa_suggest import _should_suggest

        cwd = str(tmp_path)
        (tmp_path / ".git").mkdir()
        dedup_path = tmp_path / "dedup"
        dedup_path.write_text("already suggested")

        with patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ):
            assert _should_suggest("test-session", cwd, dedup_path) is False

    def test_returns_false_for_subagent(self, tmp_path):
        """Suppresses for subagents (CLAUDE_CODE_PARENT_SESSION_ID set)."""
        from jacked.data.hooks.qa_suggest import _should_suggest

        cwd = str(tmp_path)
        (tmp_path / ".git").mkdir()

        with patch.dict(
            os.environ, {"CLAUDE_CODE_PARENT_SESSION_ID": "parent-123"}
        ), patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ):
            assert _should_suggest("test-session", cwd, tmp_path / "dedup") is False

    def test_returns_false_for_agent_type(self, tmp_path):
        """Suppresses for subagents (CLAUDE_CODE_AGENT_TYPE set)."""
        from jacked.data.hooks.qa_suggest import _should_suggest

        cwd = str(tmp_path)
        (tmp_path / ".git").mkdir()

        with patch.dict(os.environ, {"CLAUDE_CODE_AGENT_TYPE": "reviewer"}), patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ):
            assert _should_suggest("test-session", cwd, tmp_path / "dedup") is False

    def test_returns_false_when_no_ui_changes(self, tmp_path):
        """No suggestion when only non-UI files changed."""
        from jacked.data.hooks.qa_suggest import _should_suggest

        cwd = str(tmp_path)
        (tmp_path / ".git").mkdir()

        with patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["server.py", "README.md"],
        ):
            assert _should_suggest("test-session", cwd, tmp_path / "dedup") is False


class TestGetChangedFiles:
    """Test _get_changed_files() subprocess handling."""

    def test_returns_file_list(self, tmp_path):
        from jacked.data.hooks.qa_suggest import _get_changed_files

        mock_result = MagicMock()
        mock_result.stdout = "src/app.js\nstyles/main.css\n"
        mock_result.returncode = 0

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            files = _get_changed_files(str(tmp_path))

        assert files == ["src/app.js", "styles/main.css"]
        # Verify timeout is set
        call_kwargs = mock_run.call_args
        assert call_kwargs.kwargs.get("timeout") == 5 or (
            len(call_kwargs.args) > 0 and "timeout" in str(call_kwargs)
        )

    def test_returns_empty_on_subprocess_error(self, tmp_path):
        from jacked.data.hooks.qa_suggest import _get_changed_files

        with patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5)
        ):
            files = _get_changed_files(str(tmp_path))

        assert files == []

    def test_returns_empty_on_nonzero_exit(self, tmp_path):
        from jacked.data.hooks.qa_suggest import _get_changed_files

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.returncode = 128

        # Both HEAD and --cached fail
        with patch("subprocess.run", return_value=mock_result):
            files = _get_changed_files(str(tmp_path))

        assert files == []

    def test_falls_back_to_cached_on_head_failure(self, tmp_path):
        """When HEAD doesn't exist (zero-commit repo), falls back to --cached."""
        from jacked.data.hooks.qa_suggest import _get_changed_files

        head_fail = MagicMock()
        head_fail.stdout = ""
        head_fail.returncode = 128

        cached_ok = MagicMock()
        cached_ok.stdout = "src/index.tsx\n"
        cached_ok.returncode = 0

        with patch("subprocess.run", side_effect=[head_fail, cached_ok]) as mock_run:
            files = _get_changed_files(str(tmp_path))

        assert files == ["src/index.tsx"]
        assert mock_run.call_count == 2
        # Second call should use --cached
        second_call_args = mock_run.call_args_list[1][0][0]
        assert "--cached" in second_call_args


class TestSuggestionMessageRuntime:
    """Test the runtime-specific systemMessage wording (Claude vs Codex)."""

    def test_default_message_unchanged(self):
        """The default (Claude) message keeps the /qa slash-command wording."""
        from jacked.data.hooks.qa_suggest import _suggestion_message

        msg = _suggestion_message("claude")
        assert msg == (
            "UI files were modified in this session. "
            "Run /qa to browser-test the changes, "
            "or /qa <url> to specify the app URL."
        )

    def test_codex_message_uses_dollar_qa_not_slash_qa(self):
        """Codex has no /qa slash command; skills are invoked as $qa."""
        from jacked.data.hooks.qa_suggest import _suggestion_message

        msg = _suggestion_message("codex")
        assert "$qa" in msg
        assert "/qa" not in msg
        assert msg == (
            "UI files were modified in this session. "
            "Run the $qa skill to browser-test the changes "
            "(or pass the app URL as its argument)."
        )

    def test_unknown_runtime_falls_back_to_claude(self):
        from jacked.data.hooks.qa_suggest import _suggestion_message

        assert _suggestion_message("codex") != _suggestion_message("borg")
        assert "/qa" in _suggestion_message("borg")


class TestParseRuntime:
    """Test --runtime argv parsing (tolerant, never crashes the hook)."""

    def test_parses_codex(self):
        from jacked.data.hooks.qa_suggest import _parse_runtime

        assert _parse_runtime(["--runtime", "codex"]) == "codex"

    def test_defaults_to_claude(self):
        from jacked.data.hooks.qa_suggest import _parse_runtime

        assert _parse_runtime([]) == "claude"

    def test_ignores_unknown_extra_argv(self):
        """The shim may forward stray tokens; unknowns are ignored, not fatal."""
        from jacked.data.hooks.qa_suggest import _parse_runtime

        assert _parse_runtime(["_hook", "qa_suggest", "--runtime", "codex"]) == "codex"
        assert _parse_runtime(["_hook", "qa_suggest"]) == "claude"

    def test_unrecognized_value_falls_back_to_claude(self):
        from jacked.data.hooks.qa_suggest import _parse_runtime

        assert _parse_runtime(["--runtime", "nonsense"]) == "claude"


class TestMainEndToEnd:
    """Test main() with mocked stdin and stdout."""

    @pytest.fixture(autouse=True)
    def _clear_subagent_env(self):
        """Prevent flakes when tests run inside a Claude Code subagent."""
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CLAUDE_CODE_PARENT_SESSION_ID", None)
            os.environ.pop("CLAUDE_CODE_AGENT_TYPE", None)
            yield

    def test_emits_suggestion_on_ui_changes(self, tmp_path, capsys):
        from jacked.data.hooks.qa_suggest import main

        input_data = json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "test-session-e2e",
                "cwd": str(tmp_path),
            }
        )
        (tmp_path / ".git").mkdir()
        dedup_dir = tmp_path / "dedup_dir"
        dedup_dir.mkdir()

        with patch("sys.stdin") as mock_stdin, patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ), patch(
            "jacked.data.hooks.qa_suggest.DEDUP_DIR", str(dedup_dir)
        ):
            mock_stdin.read.return_value = input_data
            main()

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert "systemMessage" in output
        assert "/qa" in output["systemMessage"]

    def test_emits_codex_message_with_runtime_flag(self, tmp_path, capsys):
        """main(["--runtime", "codex"]) emits the $qa (Codex) wording."""
        from jacked.data.hooks.qa_suggest import main

        input_data = json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "test-session-codex",
                "cwd": str(tmp_path),
            }
        )
        (tmp_path / ".git").mkdir()
        dedup_dir = tmp_path / "dedup_dir"
        dedup_dir.mkdir()

        with patch("sys.stdin") as mock_stdin, patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ), patch(
            "jacked.data.hooks.qa_suggest.DEDUP_DIR", str(dedup_dir)
        ):
            mock_stdin.read.return_value = input_data
            main(["--runtime", "codex"])

        output = json.loads(capsys.readouterr().out)
        assert "$qa" in output["systemMessage"]
        assert "/qa" not in output["systemMessage"]

    def test_dedup_prevents_second_suggestion(self, tmp_path, capsys):
        """Second call with same session ID should not emit suggestion."""
        from jacked.data.hooks.qa_suggest import main

        input_data = json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": "test-dedup-session",
                "cwd": str(tmp_path),
            }
        )
        (tmp_path / ".git").mkdir()
        dedup_dir = tmp_path / "dedup_dir"
        dedup_dir.mkdir()

        with patch("sys.stdin") as mock_stdin, patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ), patch(
            "jacked.data.hooks.qa_suggest.DEDUP_DIR", str(dedup_dir)
        ):
            # First call — should emit
            mock_stdin.read.return_value = input_data
            main()

        first = capsys.readouterr()
        assert json.loads(first.out)["systemMessage"]

        with patch("sys.stdin") as mock_stdin, patch(
            "jacked.data.hooks.qa_suggest._get_changed_files",
            return_value=["src/app.js"],
        ), patch(
            "jacked.data.hooks.qa_suggest.DEDUP_DIR", str(dedup_dir)
        ):
            # Second call — dedup file exists, should NOT emit
            mock_stdin.read.return_value = input_data
            main()

        second = capsys.readouterr()
        assert second.out.strip() == ""

    def test_ignores_non_stop_events(self, tmp_path, capsys):
        from jacked.data.hooks.qa_suggest import main

        input_data = json.dumps(
            {
                "hook_event_name": "SessionStart",
                "session_id": "test-session-ignore",
                "cwd": str(tmp_path),
            }
        )

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = input_data
            main()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_handles_empty_stdin(self, capsys):
        from jacked.data.hooks.qa_suggest import main

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = ""
            main()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""

    def test_handles_invalid_json(self, capsys):
        from jacked.data.hooks.qa_suggest import main

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "not json"
            main()

        captured = capsys.readouterr()
        assert captured.out.strip() == ""
