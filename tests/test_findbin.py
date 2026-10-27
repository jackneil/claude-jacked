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


# ---------------------------------------------------------------------------
# Name guard: a lookup name is a bare command name, never a path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["", "a/b", "../codex", "/usr/bin/codex", "a\\b", "co\x00dex"])
def test_rejects_names_that_are_not_bare(bad):
    """A path-shaped name would let PATH lookup resolve an arbitrary file."""
    with patch("jacked.findbin.shutil.which") as which:
        assert find_bin(bad) is None
    which.assert_not_called()


# ---------------------------------------------------------------------------
# Fallback trust: off-PATH candidates must sit in a directory only the
# current user (or root) can change, exactly like the service's own runtime
# validation in jacked.service.spec
# ---------------------------------------------------------------------------

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")


def _local_bin_exe(home: Path, name: str) -> Path:
    local_bin = home / ".local" / "bin"
    local_bin.mkdir(parents=True, exist_ok=True)
    exe = local_bin / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


def test_home_override_is_used_for_fallback_dirs(tmp_path):
    target_name = "uv.exe" if sys.platform == "win32" else "uv"
    exe = _local_bin_exe(tmp_path, target_name)
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value="/nonexistent"):
        assert find_bin("uv", home=str(tmp_path)) == str(exe)


@posix_only
def test_refuses_world_writable_fallback_dir(tmp_path, caplog):
    exe = _local_bin_exe(tmp_path, "codex")
    exe.parent.chmod(0o777)
    try:
        with patch("jacked.findbin.shutil.which", return_value=None), \
             patch("jacked.findbin._home_dir", return_value=str(tmp_path)), \
             caplog.at_level("WARNING", logger="jacked.findbin"):
            assert find_bin("codex") is None
    finally:
        exe.parent.chmod(0o755)
    text = caplog.text
    assert "codex" in text and "world-writable" in text
    assert str(tmp_path) not in text  # values-free: no user path in the log


@posix_only
def test_refuses_group_writable_fallback_dir_with_untrusted_group(tmp_path, caplog):
    exe = _local_bin_exe(tmp_path, "codex")
    exe.parent.chmod(0o775)
    try:
        with patch("jacked.findbin.shutil.which", return_value=None), \
             patch("jacked.findbin._home_dir", return_value=str(tmp_path)), \
             patch("jacked.findbin._trusted_group_ids", return_value=frozenset()), \
             caplog.at_level("WARNING", logger="jacked.findbin"):
            assert find_bin("codex") is None
    finally:
        exe.parent.chmod(0o755)
    assert "group-writable" in caplog.text


@posix_only
def test_refuses_fallback_dir_owned_by_another_user(tmp_path, caplog):
    _local_bin_exe(tmp_path, "codex")
    other_uid = os.getuid() + 4242
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(tmp_path)), \
         patch("jacked.findbin._current_uid", return_value=other_uid), \
         caplog.at_level("WARNING", logger="jacked.findbin"):
        assert find_bin("codex") is None
    text = caplog.text
    assert "codex" in text and "not owned" in text
    assert str(tmp_path) not in text


@posix_only
def test_refuses_world_writable_binary_file(tmp_path, caplog):
    exe = _local_bin_exe(tmp_path, "codex")
    exe.chmod(0o777)
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(tmp_path)), \
         caplog.at_level("WARNING", logger="jacked.findbin"):
        assert find_bin("codex") is None
    assert "world-writable" in caplog.text


@posix_only
def test_follows_symlink_and_checks_target_directory(tmp_path):
    """~/.local/bin/codex is a symlink into ~/.codex/packages/...: a trusted
    link into a world-writable target directory must still be refused."""
    real_dir = tmp_path / "pkg" / "bin"
    real_dir.mkdir(parents=True)
    real = real_dir / "codex"
    real.write_text("#!/bin/sh\nexit 0\n")
    real.chmod(0o755)
    local_bin = tmp_path / ".local" / "bin"
    local_bin.mkdir(parents=True)
    (local_bin / "codex").symlink_to(real)

    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(tmp_path)):
        assert find_bin("codex") == str(local_bin / "codex")
        real_dir.chmod(0o777)
        try:
            assert find_bin("codex") is None
        finally:
            real_dir.chmod(0o755)


@posix_only
def test_path_hit_is_not_subject_to_fallback_trust(tmp_path):
    """PATH is the caller's own trusted environment; only the off-PATH
    fallback widens the search, so only it carries the extra check."""
    with patch("jacked.findbin.shutil.which", return_value="/usr/bin/codex"), \
         patch("jacked.findbin._current_uid", return_value=os.getuid() + 4242):
        assert find_bin("codex") == "/usr/bin/codex"


@posix_only
def test_refuses_a_world_writable_ancestor_directory(tmp_path, caplog):
    """~/.local/bin itself is sound, but a writable ancestor lets another
    local user swap the whole directory for a symlink: refused."""
    home = tmp_path / "home"
    exe = _local_bin_exe(home, "codex")
    home.chmod(0o777)
    try:
        with patch("jacked.findbin.shutil.which", return_value=None), \
             patch("jacked.findbin._home_dir", return_value=str(home)), \
             caplog.at_level("WARNING", logger="jacked.findbin"):
            assert find_bin("codex") is None
    finally:
        home.chmod(0o755)
    assert "ancestor is world-writable" in caplog.text
    assert str(exe) not in caplog.text


@posix_only
def test_accepts_a_linux_style_symlink_launcher(tmp_path):
    """Linux reports mode 0777 on a symlink; those bits mean nothing (the
    directory decides who may replace the link). A user-owned link in a
    sound directory to a sound target is accepted."""
    home = tmp_path / "home"
    target = _local_bin_exe(home, "codex-real")
    link = target.parent / "codex"
    link.symlink_to(target)
    real_lstat = os.lstat

    def linux_lstat(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        if str(path) == str(link):
            return os.stat_result((0o120777,) + tuple(st)[1:])
        return st

    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(home)), \
         patch("jacked.findbin.os.lstat", side_effect=linux_lstat):
        assert find_bin("codex") == str(link)


@posix_only
def test_refuses_a_symlink_planted_by_another_user(tmp_path, caplog):
    home = tmp_path / "home"
    target = _local_bin_exe(home, "codex-real")
    link = target.parent / "codex"
    link.symlink_to(target)
    real_lstat = os.lstat

    def foreign_lstat(path, *a, **kw):
        st = real_lstat(path, *a, **kw)
        if str(path) == str(link):
            return os.stat_result(tuple(st)[:4] + (st.st_uid + 4242,) + tuple(st)[5:])
        return st

    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch("jacked.findbin._home_dir", return_value=str(home)), \
         patch("jacked.findbin.os.lstat", side_effect=foreign_lstat), \
         caplog.at_level("WARNING", logger="jacked.findbin"):
        assert find_bin("codex") is None
    assert "symlink is not owned" in caplog.text
