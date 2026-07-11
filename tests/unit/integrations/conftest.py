"""Shared isolation for the integration unit tests.

``AgentReachRunner.install``/``.remove`` now write a best-effort Codex rules block
into ``~/.codex/AGENTS.md`` when Codex is present (M5). Point ``CODEX_HOME`` at a
per-test path that does NOT exist so the Codex side resolves to "skipped" by
default: no test may touch a developer's real ``~/.codex``. A test that wants to
exercise the Codex path opts in explicitly (create the dir, or pass an explicit
``env``/``home``), overriding this default.
"""
import pytest


@pytest.fixture(autouse=True)
def _isolate_codex_home(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-home-absent"))
