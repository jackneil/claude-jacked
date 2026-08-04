"""Unit tests for required-plugin auto-install and the Firecrawl CLI rec."""

import subprocess
from unittest import mock

from jacked.cli import (
    _REQUIRED_PLUGINS,
    _install_required_plugins,
    _recommend_external_tools,
    _run_claude_plugin,
)


def _ok(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


# ---------------------------------------------------------------------------
# _REQUIRED_PLUGINS — the ids jacked actually depends on
# ---------------------------------------------------------------------------


class TestRequiredPluginIds:
    def test_review_plugin_is_the_real_code_review_id(self):
        # pr-review-toolkit@claude-plugins-official does not exist; the real
        # review plugin is code-review@claude-plugins-official.
        assert "code-review@claude-plugins-official" in _REQUIRED_PLUGINS
        assert not any("pr-review-toolkit" in p for p in _REQUIRED_PLUGINS)

    def test_firecrawl_is_no_longer_a_required_plugin(self):
        # We use the firecrawl CLI, not the buggy MCP plugin.
        assert not any("firecrawl" in p for p in _REQUIRED_PLUGINS)


# ---------------------------------------------------------------------------
# _run_claude_plugin
# ---------------------------------------------------------------------------


class TestRunClaudePlugin:
    def test_returns_none_when_claude_missing(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: None)
        assert _run_claude_plugin("list") is None

    def test_invokes_claude_plugin_with_closed_stdin(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
        captured = {}

        def fake_run(cmd, **kwargs):
            captured["cmd"] = cmd
            captured["kwargs"] = kwargs
            return _ok()

        monkeypatch.setattr("subprocess.run", fake_run)
        _run_claude_plugin("install", "x@y", "-s", "user")

        assert captured["cmd"][:2] == ["/usr/bin/claude", "plugin"]
        assert captured["cmd"][2:] == ["install", "x@y", "-s", "user"]
        # stdin closed so a prompt can't hang install; window suppressed on win
        assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
        assert "creationflags" in captured["kwargs"]


# ---------------------------------------------------------------------------
# _install_required_plugins
# ---------------------------------------------------------------------------


class TestInstallRequiredPlugins:
    def test_skips_when_claude_missing(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: None)
        calls = []
        monkeypatch.setattr("jacked.cli._run_claude_plugin", lambda *a, **k: calls.append(a))
        _install_required_plugins()
        assert calls == []  # never shells out without claude

    def test_installs_every_missing_plugin(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
        monkeypatch.setattr("jacked.cli._installed_plugins", set)  # none configured
        installed_ids = []

        def fake_plugin(*args, **kwargs):
            assert args[0] == "install"
            assert args[2:] == ("-s", "user")
            installed_ids.append(args[1])
            return _ok()

        monkeypatch.setattr("jacked.cli._run_claude_plugin", fake_plugin)
        _install_required_plugins()
        assert set(installed_ids) == set(_REQUIRED_PLUGINS)
        assert "code-review@claude-plugins-official" in installed_ids

    def test_skips_already_configured(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
        monkeypatch.setattr("jacked.cli._installed_plugins", lambda: set(_REQUIRED_PLUGINS))
        calls = []
        monkeypatch.setattr("jacked.cli._run_claude_plugin", lambda *a, **k: calls.append(a))
        _install_required_plugins(force=False)
        assert calls == []  # all present -> nothing installed

    def test_force_reinstalls_even_if_configured(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
        monkeypatch.setattr("jacked.cli._installed_plugins", lambda: set(_REQUIRED_PLUGINS))
        calls = []
        monkeypatch.setattr(
            "jacked.cli._run_claude_plugin",
            lambda *a, **k: (calls.append(a), _ok())[1],
        )
        _install_required_plugins(force=True)
        assert len(calls) == len(_REQUIRED_PLUGINS)

    def test_install_failure_is_non_fatal(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
        monkeypatch.setattr("jacked.cli._installed_plugins", set)
        monkeypatch.setattr(
            "jacked.cli._run_claude_plugin",
            lambda *a, **k: _ok(returncode=1, stderr="boom"),
        )
        # Must not raise even when every install fails.
        _install_required_plugins()

    def test_none_result_is_non_fatal(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda _name: "/usr/bin/claude")
        monkeypatch.setattr("jacked.cli._installed_plugins", set)
        monkeypatch.setattr("jacked.cli._run_claude_plugin", lambda *a, **k: None)
        _install_required_plugins()  # timeout/OSError path -> warn, no crash


# ---------------------------------------------------------------------------
# _recommend_external_tools — Firecrawl CLI recommendation
# ---------------------------------------------------------------------------


class TestFirecrawlRecommendation:
    def test_recommends_cli_when_firecrawl_missing(self, monkeypatch):
        # firecrawl absent, everything else present (isolate the firecrawl rec)
        monkeypatch.setattr(
            "shutil.which", lambda n: None if n == "firecrawl" else f"/usr/bin/{n}"
        )
        fake_console = mock.MagicMock()
        monkeypatch.setattr("jacked.cli.console", fake_console)
        _recommend_external_tools()
        printed = " ".join(str(c) for c in fake_console.print.call_args_list)
        assert "firecrawl-cli" in printed

    def test_no_recommendation_when_firecrawl_present(self, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda n: f"/usr/bin/{n}")
        fake_console = mock.MagicMock()
        monkeypatch.setattr("jacked.cli.console", fake_console)
        _recommend_external_tools()
        printed = " ".join(str(c) for c in fake_console.print.call_args_list)
        assert "firecrawl-cli" not in printed
