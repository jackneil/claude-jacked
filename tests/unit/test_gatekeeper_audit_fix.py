"""Tests for `jacked gatekeeper audit --fix` — interactive permission pruning."""

import json
from pathlib import Path
from unittest.mock import patch
from click.testing import CliRunner


def _write_settings(path: Path, allow_list: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"permissions": {"allow": allow_list}}, indent=2))


class TestRemovePermissionPatterns:
    def test_removes_matching_pattern(self, tmp_path):
        from jacked.cli import _remove_permission_patterns

        settings = tmp_path / "settings.json"
        _write_settings(settings, [
            "Bash(curl:*)",
            "Bash(ls:*)",
            "Bash(python -m pytest:*)",
        ])

        count, removed = _remove_permission_patterns(
            settings, {"Bash(curl:*)", "Bash(python -m pytest:*)"}
        )

        assert count == 2
        assert set(removed) == {"Bash(curl:*)", "Bash(python -m pytest:*)"}
        content = json.loads(settings.read_text())
        assert content["permissions"]["allow"] == ["Bash(ls:*)"]

    def test_noop_when_no_matches(self, tmp_path):
        from jacked.cli import _remove_permission_patterns

        settings = tmp_path / "settings.json"
        _write_settings(settings, ["Bash(ls:*)"])
        count, removed = _remove_permission_patterns(settings, {"Bash(curl:*)"})
        assert count == 0
        assert removed == []

    def test_no_file_is_noop(self, tmp_path):
        from jacked.cli import _remove_permission_patterns
        count, removed = _remove_permission_patterns(
            tmp_path / "nonexistent.json", {"Bash(curl:*)"}
        )
        assert count == 0

    def test_corrupt_file_is_noop(self, tmp_path):
        from jacked.cli import _remove_permission_patterns
        settings = tmp_path / "settings.json"
        settings.write_text("not valid json {{{")
        count, removed = _remove_permission_patterns(settings, {"Bash(curl:*)"})
        assert count == 0

    def test_writes_backup_before_mutation(self, tmp_path):
        from jacked.cli import _remove_permission_patterns

        settings = tmp_path / "settings.json"
        _write_settings(settings, ["Bash(curl:*)", "Bash(ls:*)"])
        original = settings.read_text()

        _remove_permission_patterns(settings, {"Bash(curl:*)"})

        backups = sorted(tmp_path.glob("settings.json.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_text() == original


class TestGatekeeperAuditFix:
    @patch("jacked.cli._settings_files_to_search")
    @patch("jacked.cli._scan_permission_rules")
    def test_fix_yes_removes_all_warn_patterns(
        self, mock_scan, mock_files, tmp_path,
    ):
        from jacked.cli import main

        settings = tmp_path / "settings.json"
        _write_settings(settings, [
            "Bash(curl:*)",
            "Bash(python -m pytest:*)",
            "Bash(ls:*)",  # not dangerous
        ])
        mock_files.return_value = [settings]
        mock_scan.return_value = [
            ("Bash(curl:*)", "WARN", "curl", "potential data exfiltration"),
            ("Bash(python -m pytest:*)", "WARN", "python", "arbitrary code execution via -c"),
            ("Bash(ls:*)", "OK", "ls", "safe"),
        ]

        runner = CliRunner()
        result = runner.invoke(main, ["gatekeeper", "audit", "--fix", "--yes"])

        assert result.exit_code == 0
        content = json.loads(settings.read_text())
        assert "Bash(curl:*)" not in content["permissions"]["allow"]
        assert "Bash(python -m pytest:*)" not in content["permissions"]["allow"]
        assert "Bash(ls:*)" in content["permissions"]["allow"]  # safe one preserved

    @patch("jacked.cli._settings_files_to_search")
    @patch("jacked.cli._scan_permission_rules")
    def test_fix_without_yes_prompts_interactively(
        self, mock_scan, mock_files, tmp_path,
    ):
        from jacked.cli import main

        settings = tmp_path / "settings.json"
        _write_settings(settings, ["Bash(curl:*)", "Bash(wget:*)"])
        mock_files.return_value = [settings]
        mock_scan.return_value = [
            ("Bash(curl:*)", "WARN", "curl", "potential data exfiltration"),
            ("Bash(wget:*)", "WARN", "wget", "potential data exfiltration"),
        ]

        runner = CliRunner()
        # "y" for first (remove curl), "n" for second (keep wget)
        result = runner.invoke(
            main, ["gatekeeper", "audit", "--fix"], input="y\nn\n"
        )

        assert result.exit_code == 0
        content = json.loads(settings.read_text())
        allow = content["permissions"]["allow"]
        assert "Bash(curl:*)" not in allow
        assert "Bash(wget:*)" in allow

    @patch("jacked.cli._settings_files_to_search")
    @patch("jacked.cli._scan_permission_rules")
    def test_fix_all_option_removes_remaining(
        self, mock_scan, mock_files, tmp_path,
    ):
        from jacked.cli import main

        settings = tmp_path / "settings.json"
        _write_settings(settings, [
            "Bash(curl:*)",
            "Bash(wget:*)",
            "Bash(python -m pytest:*)",
        ])
        mock_files.return_value = [settings]
        mock_scan.return_value = [
            ("Bash(curl:*)", "WARN", "curl", "x"),
            ("Bash(wget:*)", "WARN", "wget", "x"),
            ("Bash(python -m pytest:*)", "WARN", "python", "x"),
        ]

        runner = CliRunner()
        # "n" for first, then "a" to remove all remaining
        result = runner.invoke(
            main, ["gatekeeper", "audit", "--fix"], input="n\na\n"
        )

        assert result.exit_code == 0
        content = json.loads(settings.read_text())
        allow = content["permissions"]["allow"]
        # curl kept (user said no), wget + pytest removed (user said "a")
        assert "Bash(curl:*)" in allow
        assert "Bash(wget:*)" not in allow
        assert "Bash(python -m pytest:*)" not in allow

    @patch("jacked.cli._settings_files_to_search")
    @patch("jacked.cli._scan_permission_rules")
    def test_fix_quit_stops_immediately(
        self, mock_scan, mock_files, tmp_path,
    ):
        from jacked.cli import main

        settings = tmp_path / "settings.json"
        _write_settings(settings, ["Bash(curl:*)", "Bash(wget:*)"])
        mock_files.return_value = [settings]
        mock_scan.return_value = [
            ("Bash(curl:*)", "WARN", "curl", "x"),
            ("Bash(wget:*)", "WARN", "wget", "x"),
        ]

        runner = CliRunner()
        result = runner.invoke(
            main, ["gatekeeper", "audit", "--fix"], input="q\n"
        )

        assert result.exit_code == 0
        content = json.loads(settings.read_text())
        allow = content["permissions"]["allow"]
        # Nothing removed — quit before any answer
        assert "Bash(curl:*)" in allow
        assert "Bash(wget:*)" in allow

    @patch("jacked.cli._settings_files_to_search")
    @patch("jacked.cli._scan_permission_rules")
    def test_fix_with_no_warnings_is_noop(
        self, mock_scan, mock_files, tmp_path,
    ):
        from jacked.cli import main

        settings = tmp_path / "settings.json"
        _write_settings(settings, ["Bash(ls:*)"])
        mock_files.return_value = [settings]
        mock_scan.return_value = [
            ("Bash(ls:*)", "OK", "ls", "safe"),
        ]

        runner = CliRunner()
        result = runner.invoke(main, ["gatekeeper", "audit", "--fix"])
        assert result.exit_code == 0
        assert "Nothing to fix" in result.output
