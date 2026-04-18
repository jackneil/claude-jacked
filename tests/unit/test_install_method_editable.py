"""Tests for editable-install detection and the can_auto_upgrade() gate."""

import sys
from pathlib import Path
from unittest.mock import patch

from jacked.install_method import (
    detect_install_method,
    can_auto_upgrade,
)


class TestDetectEditable:
    def test_detects_pth_editable_marker(self, tmp_path, monkeypatch):
        sp = tmp_path / "site-packages"
        sp.mkdir()
        (sp / "_editable_impl_claude_jacked.pth").write_text("/tmp/repo\n")
        monkeypatch.setattr(sys, "path", [str(sp), *sys.path])
        with patch("sys.executable", str(tmp_path / ".venv" / "bin" / "python3")):
            assert detect_install_method() == "editable"

    def test_detects_setuptools_editable_marker(self, tmp_path, monkeypatch):
        sp = tmp_path / "site-packages"
        sp.mkdir()
        (sp / "__editable__.claude_jacked-0.41.18.pth").write_text("/tmp/repo\n")
        monkeypatch.setattr(sys, "path", [str(sp), *sys.path])
        with patch("sys.executable", str(tmp_path / ".venv" / "bin" / "python3")):
            assert detect_install_method() == "editable"

    def test_uv_tool_still_beats_editable(self, tmp_path, monkeypatch):
        sp = tmp_path / "site-packages"
        sp.mkdir()
        (sp / "_editable_impl_claude_jacked.pth").write_text("/tmp/repo\n")
        monkeypatch.setattr(sys, "path", [str(sp), *sys.path])
        with patch(
            "sys.executable",
            "/home/u/.local/share/uv/tools/claude-jacked/bin/python3",
        ):
            assert detect_install_method() == "uv"


class TestUvDetectionThroughSymlinks:
    """Regression: on macOS, uv-tool venvs symlink bin/python to the real
    Python (e.g. miniconda). `Path(sys.executable).resolve()` follows that
    symlink and loses the `uv/tools/<pkg>/` fingerprint. Detection must
    match via `sys.prefix` too."""

    def test_detects_uv_via_sys_prefix_when_exe_resolves_elsewhere(
        self, monkeypatch,
    ):
        """sys.executable resolves to an unrelated path (miniconda-like),
        but sys.prefix is the uv-tool venv root."""
        monkeypatch.setattr(
            "sys.prefix",
            "/Users/u/.local/share/uv/tools/claude-jacked",
        )
        monkeypatch.setattr(
            "sys.executable",
            "/Users/u/.local/share/uv/tools/claude-jacked/bin/python",
        )
        # Symlink target (the 'resolve' path)
        from unittest.mock import patch
        import pathlib
        real_resolve = pathlib.Path.resolve
        def fake_resolve(self, *a, **kw):
            if "uv/tools" in str(self):
                return pathlib.Path("/opt/homebrew/Caskroom/miniconda/base/bin/python3.12")
            return real_resolve(self, *a, **kw)
        with patch("pathlib.Path.resolve", fake_resolve):
            assert detect_install_method() == "uv"


class TestCanAutoUpgrade:
    def test_uv_is_auto_upgradable(self):
        with patch("jacked.install_method.detect_install_method", return_value="uv"):
            ok, reason = can_auto_upgrade()
        assert ok is True
        assert reason == ""

    def test_pipx_is_auto_upgradable(self):
        with patch("jacked.install_method.detect_install_method", return_value="pipx"):
            ok, reason = can_auto_upgrade()
        assert ok is True

    def test_editable_refused_with_git_pull_recovery(self):
        with patch("jacked.install_method.detect_install_method", return_value="editable"):
            ok, reason = can_auto_upgrade()
        assert ok is False
        assert "editable" in reason.lower()
        assert "git pull" in reason
        assert "uv sync" in reason

    def test_pip_refused_recommending_uv(self):
        with patch("jacked.install_method.detect_install_method", return_value="pip"):
            ok, reason = can_auto_upgrade()
        assert ok is False
        assert "pip" in reason.lower()
        assert "uv tool install" in reason

    def test_gate_defensive_when_detection_raises(self):
        with patch(
            "jacked.install_method.detect_install_method",
            side_effect=RuntimeError("boom"),
        ):
            ok, reason = can_auto_upgrade()
        assert ok is False
        assert reason
