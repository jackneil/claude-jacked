"""Tests for jacked.findbin."""

import os
import stat
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from jacked.findbin import find_bin


@pytest.fixture
def fake_bin(tmp_path):
    """Create a fake executable in a temp directory."""
    def _make(name: str) -> Path:
        suffix = ".exe" if sys.platform == "win32" else ""
        p = tmp_path / f"{name}{suffix}"
        p.write_text("fake")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return p
    return _make


def test_finds_via_shutil_which():
    """If shutil.which succeeds, return its result immediately."""
    with patch("jacked.findbin.shutil.which", return_value="/usr/bin/uv"):
        assert find_bin("uv") == "/usr/bin/uv"


def test_falls_back_to_local_bin(fake_bin, tmp_path):
    """When PATH lookup fails, probe ~/.local/bin/."""
    fake_bin("uv")
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    target = local_bin / ("uv.exe" if sys.platform == "win32" else "uv")
    target.write_text("fake")
    target.chmod(target.stat().st_mode | stat.S_IEXEC)

    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(tmp_path)):
        result = find_bin("uv")
        assert result is not None
        assert result == str(target)


def test_respects_uv_tool_bin_dir(fake_bin, tmp_path):
    """UV_TOOL_BIN_DIR env var takes priority over default paths."""
    target_name = "jacked.exe" if sys.platform == "win32" else "jacked"
    target = tmp_path / target_name
    target.write_text("fake")
    target.chmod(target.stat().st_mode | stat.S_IEXEC)

    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch.dict(os.environ, {"UV_TOOL_BIN_DIR": str(tmp_path)}), \
         patch("jacked.findbin._home_dir", return_value="/nonexistent"):
        result = find_bin("jacked")
        assert result is not None
        assert result == str(target)


def test_returns_none_when_not_found():
    """When binary doesn't exist anywhere, return None."""
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value="/nonexistent"):
        assert find_bin("nonexistent_binary_xyz") is None


def test_xdg_bin_home_override(tmp_path):
    """XDG_BIN_HOME env var is checked before default paths."""
    target_name = "uv.exe" if sys.platform == "win32" else "uv"
    target = tmp_path / target_name
    target.write_text("fake")
    target.chmod(target.stat().st_mode | stat.S_IEXEC)

    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch.dict(os.environ, {"XDG_BIN_HOME": str(tmp_path)}), \
         patch("jacked.findbin._home_dir", return_value="/nonexistent"):
        result = find_bin("uv")
        assert result is not None
        assert result == str(target)
