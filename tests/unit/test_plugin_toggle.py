"""Unit tests for plugin enable/disable toggle in Claude Code settings."""

import asyncio
import json
from unittest import mock

import pytest
from pydantic import BaseModel

from jacked.api.routes.features import toggle_claude_plugin


def _run(coro):
    """Run an async coroutine synchronously."""
    return asyncio.new_event_loop().run_until_complete(coro)


class _Body(BaseModel):
    enabled: bool


@pytest.fixture()
def settings_file(tmp_path):
    """Create a temp settings.json and patch the module constant."""
    sf = tmp_path / "settings.json"
    sf.write_text("{}", encoding="utf-8")
    with mock.patch("jacked.api.routes.features.SETTINGS_JSON", sf):
        yield sf


class TestToggleClaudePlugin:
    """Verify toggle writes explicit true/false, not pop."""

    def test_disable_writes_false(self, settings_file):
        """Disabling a plugin sets enabledPlugins[name] = false (not removes key)."""
        result = _run(toggle_claude_plugin("stripe@claude-plugins-official", _Body(enabled=False)))

        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert "stripe@claude-plugins-official" in data["enabledPlugins"]
        assert data["enabledPlugins"]["stripe@claude-plugins-official"] is False
        assert result == {"name": "stripe@claude-plugins-official", "enabled": False}

    def test_enable_writes_true(self, settings_file):
        """Enabling a plugin sets enabledPlugins[name] = true."""
        result = _run(toggle_claude_plugin("stripe@claude-plugins-official", _Body(enabled=True)))

        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["enabledPlugins"]["stripe@claude-plugins-official"] is True
        assert result == {"name": "stripe@claude-plugins-official", "enabled": True}

    def test_disable_plugin_not_in_enabled_plugins(self, settings_file):
        """Disabling a plugin that has no enabledPlugins entry creates a false entry."""
        settings_file.write_text(
            json.dumps({"enabledPlugins": {"other@official": True}}),
            encoding="utf-8",
        )

        _run(toggle_claude_plugin("stripe@claude-plugins-official", _Body(enabled=False)))

        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["enabledPlugins"]["stripe@claude-plugins-official"] is False
        assert data["enabledPlugins"]["other@official"] is True

    def test_roundtrip_disable_then_enable(self, settings_file):
        """Disable then re-enable preserves the entry."""
        _run(toggle_claude_plugin("test@official", _Body(enabled=False)))
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["enabledPlugins"]["test@official"] is False

        _run(toggle_claude_plugin("test@official", _Body(enabled=True)))
        data = json.loads(settings_file.read_text(encoding="utf-8"))
        assert data["enabledPlugins"]["test@official"] is True
