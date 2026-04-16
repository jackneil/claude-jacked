"""Tests for `jacked _hook <name>` shim command."""

from unittest.mock import patch, MagicMock
from click.testing import CliRunner


class TestHookShim:
    def test_dispatches_to_named_hook_module(self):
        from jacked.cli import main
        runner = CliRunner()
        mock_module = MagicMock()
        mock_module.main = MagicMock()
        with patch("importlib.import_module", return_value=mock_module) as mock_import:
            result = runner.invoke(main, ["_hook", "security_gatekeeper"], input="{}")
        mock_import.assert_called_once_with("jacked.data.hooks.security_gatekeeper")
        mock_module.main.assert_called_once()

    def test_unknown_hook_name_rejected_before_import(self):
        from jacked.cli import main
        runner = CliRunner()
        with patch("importlib.import_module") as mock_import:
            result = runner.invoke(main, ["_hook", "nonexistent_xyz"], input="{}")
        assert result.exit_code != 0
        mock_import.assert_not_called()

    def test_path_traversal_rejected(self):
        from jacked.cli import main
        runner = CliRunner()
        with patch("importlib.import_module") as mock_import:
            result = runner.invoke(main, ["_hook", "../etc"], input="{}")
        assert result.exit_code != 0
        mock_import.assert_not_called()

    def test_known_hook_names_accepted(self):
        from jacked.cli import _valid_hook_names
        names = _valid_hook_names()
        assert "security_gatekeeper" in names
        assert "session_account_tracker" in names
        assert "qa_suggest" in names
