"""Tests for the chain-of-command SessionStart auto-load hook.

Covers the hook module (context injection + silence when the skill file is
absent) and the cli.py remove/registration plumbing. INSTALL coverage lives in
tests/unit/test_session_start_hook.py: the policy is injected by the single
combined SessionStart entry now, not by an entry of its own.
"""

import json
from pathlib import Path
from unittest.mock import patch

FRONTMATTER_SKILL = (
    "---\n"
    "name: chain-of-command\n"
    "description: test dispatch policy\n"
    "---\n"
    "\n"
    "SENTINEL_BODY_LINE the policy body starts here\n"
)


def _write_fake_skill(home: Path) -> Path:
    """Create a fake ~/.claude/skills/chain-of-command/SKILL.md under `home`."""
    skill_dir = home / ".claude" / "skills" / "chain-of-command"
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    skill_file.write_text(FRONTMATTER_SKILL, encoding="utf-8")
    return skill_file


class TestStripFrontmatter:
    def test_strips_leading_yaml_block(self):
        from jacked.data.hooks.chain_of_command_context import _strip_frontmatter

        assert _strip_frontmatter("---\nname: x\n---\nbody") == "body"

    def test_returns_unchanged_without_frontmatter(self):
        from jacked.data.hooks.chain_of_command_context import _strip_frontmatter

        assert _strip_frontmatter("no frontmatter here") == "no frontmatter here"

    def test_no_closing_fence_returns_unchanged(self):
        from jacked.data.hooks.chain_of_command_context import _strip_frontmatter

        text = "---\nname: x\nstill going"
        assert _strip_frontmatter(text) == text


class TestHookMain:
    def test_prints_preamble_and_body_strips_frontmatter(
        self, tmp_path, capsys, monkeypatch
    ):
        from jacked.data.hooks import chain_of_command_context as hook

        _write_fake_skill(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = '{"hook_event_name": "SessionStart"}'
            hook.main()

        out = capsys.readouterr().out
        # Preamble framing present.
        assert "=== CHAIN OF COMMAND (auto-loaded by jacked) ===" in out
        assert "do NOT announce or acknowledge the policy" in out
        # Body present.
        assert "SENTINEL_BODY_LINE the policy body starts here" in out
        # Frontmatter stripped: the YAML keys must not leak into the output.
        assert "name: chain-of-command" not in out
        assert "description: test dispatch policy" not in out

    def test_prints_nothing_when_skill_absent(self, tmp_path, capsys, monkeypatch):
        from jacked.data.hooks import chain_of_command_context as hook

        # tmp_path has no .claude/skills/chain-of-command/SKILL.md.
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = '{"hook_event_name": "SessionStart"}'
            hook.main()

        assert capsys.readouterr().out == ""

    def test_tolerates_garbage_stdin(self, tmp_path, capsys, monkeypatch):
        from jacked.data.hooks import chain_of_command_context as hook

        _write_fake_skill(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = "this is not json at all {{{"
            hook.main()

        out = capsys.readouterr().out
        # Garbage stdin is discarded; the body is still injected.
        assert "SENTINEL_BODY_LINE the policy body starts here" in out

    def test_tolerates_empty_stdin(self, tmp_path, capsys, monkeypatch):
        from jacked.data.hooks import chain_of_command_context as hook

        _write_fake_skill(tmp_path)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        with patch("sys.stdin") as mock_stdin:
            mock_stdin.read.return_value = ""
            hook.main()

        out = capsys.readouterr().out
        assert "SENTINEL_BODY_LINE the policy body starts here" in out


class TestRemove:
    def test_removes_only_our_entry(self, tmp_path):
        from jacked.cli import _remove_chain_of_command_hook

        settings_path = tmp_path / "settings.json"
        foreign_cmd = "/usr/local/bin/my-own-startup-script.sh"
        ours_cmd = '"/fake/bin/jacked" _hook chain_of_command_context'
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": foreign_cmd}],
                    },
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": ours_cmd}],
                    },
                ]
            }
        }
        settings_path.write_text(json.dumps(settings))

        assert _remove_chain_of_command_hook(settings_path) is True

        remaining = json.loads(settings_path.read_text())["hooks"]["SessionStart"]
        assert len(remaining) == 1
        assert remaining[0]["hooks"][0]["command"] == foreign_cmd

    def test_leaves_session_tracker_entry_intact(self, tmp_path):
        from jacked.cli import _remove_chain_of_command_hook

        settings_path = tmp_path / "settings.json"
        tracker_cmd = '"/fake/bin/jacked" _hook session_account_tracker'
        ours_cmd = '"/fake/bin/jacked" _hook chain_of_command_context'
        settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": tracker_cmd}],
                    },
                    {
                        "matcher": "",
                        "hooks": [{"type": "command", "command": ours_cmd}],
                    },
                ]
            }
        }
        settings_path.write_text(json.dumps(settings))

        assert _remove_chain_of_command_hook(settings_path) is True

        remaining = json.loads(settings_path.read_text())["hooks"]["SessionStart"]
        assert len(remaining) == 1
        assert "session_account_tracker" in remaining[0]["hooks"][0]["command"]

    def test_noop_when_no_entry(self, tmp_path):
        from jacked.cli import _remove_chain_of_command_hook

        settings_path = tmp_path / "settings.json"
        settings_path.write_text(json.dumps({"hooks": {"SessionStart": []}}))
        assert _remove_chain_of_command_hook(settings_path) is False

    def test_noop_when_settings_missing(self, tmp_path):
        from jacked.cli import _remove_chain_of_command_hook

        settings_path = tmp_path / "does-not-exist.json"
        assert _remove_chain_of_command_hook(settings_path) is False


class TestHookRegistration:
    def test_hook_is_in_valid_hook_names(self):
        from jacked.cli import _valid_hook_names

        assert "chain_of_command_context" in _valid_hook_names()
