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
    assert "a directory is world-writable" in caplog.text
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


# ---------------------------------------------------------------------------
# Hop-aware resolution (#186i, #185i): every directory, every symlink and the
# final file on the way are checked, intermediate hops included.
# ---------------------------------------------------------------------------

def _executable(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


def _lookup(home, caplog, name="codex", **env):
    with patch("jacked.findbin.shutil.which", return_value=None), \
         patch.dict(os.environ, env), \
         caplog.at_level("WARNING", logger="jacked.findbin"):
        return find_bin(name, home=str(home))


@posix_only
def test_refuses_a_hop_through_a_world_writable_directory(tmp_path, caplog):
    """~/.local/bin/codex -> /shared/codex -> ~/pkg/codex: the first link's
    directory and the final target are sound, but the MIDDLE link sits in a
    world-writable directory where another user could repoint it. The old two
    walks never looked at /shared."""
    home = tmp_path / "home"
    real = _executable(home / "pkg" / "codex")
    shared = tmp_path / "shared"
    shared.mkdir()
    (shared / "codex").symlink_to(real)
    shared.chmod(0o777)
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "bin" / "codex").symlink_to(shared / "codex")
    try:
        assert _lookup(home, caplog) is None
    finally:
        shared.chmod(0o755)
    assert "a directory is world-writable" in caplog.text
    assert str(tmp_path) not in caplog.text
    # Once the middle directory is sound the same chain is accepted.
    assert _lookup(home, caplog) == str(home / ".local" / "bin" / "codex")


@posix_only
def test_refuses_a_user_link_to_a_world_writable_bin(tmp_path, caplog):
    """~/.local/bin is the user's own symlink, but it points at a 0o777
    directory that holds the candidate."""
    home = tmp_path / "home"
    shared_bin = tmp_path / "shared-bin"
    _executable(shared_bin / "codex")
    (home / ".local").mkdir(parents=True)
    (home / ".local" / "bin").symlink_to(shared_bin)
    shared_bin.chmod(0o777)
    try:
        assert _lookup(home, caplog) is None
    finally:
        shared_bin.chmod(0o755)
    assert "world-writable" in caplog.text


@posix_only
def test_refuses_a_link_to_a_world_writable_parent_of_bin(tmp_path, caplog):
    """~/.local is a link to a 0o777 directory with a real bin/ below it:
    another user could replace bin/ there."""
    home = tmp_path / "home"
    shared = tmp_path / "shared-local"
    _executable(shared / "bin" / "codex")
    home.mkdir()
    (home / ".local").symlink_to(shared)
    shared.chmod(0o777)
    try:
        assert _lookup(home, caplog) is None
    finally:
        shared.chmod(0o755)
    assert "a directory is world-writable" in caplog.text


@posix_only
def test_accepts_a_home_under_the_sticky_tmp():
    """Containers set HOME under /tmp. /tmp is root-owned, sticky and
    world-writable: others may not rename or delete the user's own real
    directory there, so a home below it keeps the fallback."""
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="jk-findbin-home-", dir="/tmp"))
    try:
        exe = _executable(home / ".local" / "bin" / "codex")
        with patch("jacked.findbin.shutil.which", return_value=None):
            assert find_bin("codex", home=str(home)) == str(exe)
    finally:
        import shutil as _shutil
        _shutil.rmtree(home)


@posix_only
def test_refuses_a_link_planted_directly_in_tmp(caplog):
    """A symlink (or file) directly in the shared /tmp is refused whoever owns
    it: the directory that holds the binary gets the strict rule."""
    import tempfile

    work = Path(tempfile.mkdtemp(prefix="jk-findbin-", dir=os.path.expanduser("~")
                                 if os.access(os.path.expanduser("~"), os.W_OK) else None))
    real = _executable(work / "pkg" / "codex")
    link = Path(tempfile.mktemp(prefix="jk-findbin-link-", dir="/tmp"))
    link.symlink_to(real)
    try:
        bin_dir = os.path.dirname(str(link))
        with patch("jacked.findbin.shutil.which", return_value=None), \
             patch.dict(os.environ, {"UV_TOOL_BIN_DIR": bin_dir}), \
             caplog.at_level("WARNING", logger="jacked.findbin"):
            assert find_bin(link.name, home="/nonexistent") is None
        assert "shared sticky directory" in caplog.text
        # Planted by another user: refused just the same.
        caplog.clear()
        with patch("jacked.findbin.shutil.which", return_value=None), \
             patch("jacked.findbin._current_uid", return_value=os.getuid() + 4242), \
             patch.dict(os.environ, {"UV_TOOL_BIN_DIR": bin_dir}), \
             caplog.at_level("WARNING", logger="jacked.findbin"):
            assert find_bin(link.name, home="/nonexistent") is None
        assert "refused" in caplog.text
    finally:
        link.unlink()
        import shutil as _shutil
        _shutil.rmtree(work)


@posix_only
def test_refuses_a_candidate_directly_in_tmp_via_uv_tool_bin_dir(caplog):
    """UV_TOOL_BIN_DIR=/tmp (on macOS /tmp is itself a link to /private/tmp):
    a binary directly in the shared directory is refused."""
    import tempfile

    fd, name = tempfile.mkstemp(prefix="jk-findbin-exe-", dir="/tmp")
    os.close(fd)
    os.chmod(name, 0o755)
    try:
        with patch("jacked.findbin.shutil.which", return_value=None), \
             patch.dict(os.environ, {"UV_TOOL_BIN_DIR": "/tmp"}), \
             caplog.at_level("WARNING", logger="jacked.findbin"):
            assert find_bin(os.path.basename(name), home="/nonexistent") is None
        assert "shared sticky directory" in caplog.text
    finally:
        os.unlink(name)


@posix_only
def test_refuses_a_home_with_dot_dot(tmp_path, caplog):
    home = tmp_path / "home"
    _executable(home / ".local" / "bin" / "codex")
    dotted = f"{tmp_path}/other/../home"
    (tmp_path / "other").mkdir()
    with patch("jacked.findbin.shutil.which", return_value=None), \
         caplog.at_level("WARNING", logger="jacked.findbin"):
        assert find_bin("codex", home=dotted) is None
    assert "not an absolute path without '..'" in caplog.text


@posix_only
def test_refuses_a_symlink_loop(tmp_path, caplog):
    home = tmp_path / "home"
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "a").symlink_to(bin_dir / "b")
    (bin_dir / "b").symlink_to(bin_dir / "a")
    (bin_dir / "codex").symlink_to(bin_dir / "a")
    with patch("jacked.findbin.os.path.isfile", return_value=True), \
         patch("jacked.findbin.os.access", return_value=True):
        assert _lookup(home, caplog) is None
    assert "too many symlink hops" in caplog.text


@posix_only
def test_refuses_a_component_swapped_between_its_two_reads(tmp_path, caplog):
    """Each step is read with lstat and again through a descriptor; if the
    two disagree (a swap in between), the candidate is refused."""
    import jacked.findbin as findbin

    home = tmp_path / "home"
    exe = _executable(home / ".local" / "bin" / "codex")
    real_secure = findbin._secure_lstat

    def swapped(path, kind):
        status = real_secure(path, kind)
        if path == str(exe):
            fields = list(status)
            fields[stat.ST_INO] += 1
            return os.stat_result(fields)
        return status

    with patch.object(findbin, "_secure_lstat", side_effect=swapped):
        assert _lookup(home, caplog) is None
    assert "changed while it was being checked" in caplog.text


@posix_only
@pytest.mark.parametrize("kind", ["dir", "file", "link"])
def test_refuses_a_mutating_macos_acl_on_any_step(tmp_path, caplog, kind):
    """#185i: on macOS every step goes through the service spec's ACL check
    (an O_NOFOLLOW descriptor and acl_get_fd_np, acl_get_link_np for links),
    so an `everyone allow add_file` entry on a 0755 directory is refused."""
    import jacked.findbin as findbin
    from jacked.service import spec

    home = tmp_path / "home"
    real = _executable(home / "pkg" / "codex")
    bin_dir = home / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "codex").symlink_to(real)
    flagged = {"dir": str(bin_dir), "file": str(real), "link": str(bin_dir / "codex")}[kind]

    def secure_status(path, *, directory):
        if str(path) == flagged:
            raise ValueError("runtime_path has a mutating extended ACL")
        return os.lstat(path)

    def link_acl(path):
        if str(path) == flagged:
            raise ValueError("runtime_path symlink has a mutating extended ACL")

    with patch.object(findbin, "_HOST_IS_DARWIN", True), \
         patch.object(spec, "_darwin_secure_status", side_effect=secure_status), \
         patch.object(spec, "_validate_darwin_link_acl", side_effect=link_acl):
        assert _lookup(home, caplog) is None
        assert "mutating ACL" in caplog.text
        caplog.clear()
        flagged = "/nowhere"
        assert _lookup(home, caplog) == str(bin_dir / "codex")
