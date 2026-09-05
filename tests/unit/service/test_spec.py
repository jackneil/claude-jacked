import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from jacked.service.spec import ServiceSpec, SupervisorKind


def _spec(**overrides):
    values = {
        "service_id": "ai.hank.jacked",
        "protocol_version": 2,
        "build_version": "0.99.0",
        "runtime_path": os.path.realpath(sys.executable),
        "launcher_path": os.path.join(
            tempfile.gettempdir(), "jacked", "launcher-v2"
        ),
        "launcher_sha256": "a" * 64,
        "supervisor": SupervisorKind.LAUNCHD,
        "arguments": ("-I", "-m", "jacked", "service", "start"),
    }
    values.update(overrides)
    return ServiceSpec(**values)


def test_generation_is_deterministic_and_covers_runtime_identity():
    first = _spec()
    assert first.generation == _spec().generation
    assert first.generation != _spec(build_version="0.99.1").generation
    other_runtime = os.path.realpath(
        os.path.join(tempfile.gettempdir(), "jacked", "other-python")
    )
    assert first.generation != _spec(runtime_path=other_runtime).generation


def test_runtime_path_rejects_relative():
    with pytest.raises(ValueError, match="absolute"):
        _spec(runtime_path="python")


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_runtime_path_accepts_virtualenv_symlink(tmp_path):
    foreign_link = tmp_path / "python"
    foreign_link.symlink_to(Path(sys.executable))
    with pytest.raises(ValueError):
        _spec(runtime_path=str(foreign_link))

    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / "python3.99"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)
    spec = _spec(runtime_path=str(link))
    assert spec.runtime_path == str(link)
    assert spec.runtime_target_path == str(target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
@pytest.mark.parametrize("target_name", ["python3.13t", "pypy3", "graalpy"])
def test_virtualenv_runtime_accepts_supported_python_targets(tmp_path, target_name):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / target_name
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)

    assert _spec(runtime_path=str(link)).runtime_target_path == str(target)


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_virtualenv_runtime_rejects_broken_non_python_and_writable_paths(tmp_path):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")

    broken = venv / "bin" / "python"
    broken.symlink_to(venv / "missing" / "python3")
    with pytest.raises(ValueError):
        _spec(runtime_path=str(broken))
    broken.unlink()

    non_python = tmp_path / "not-an-interpreter"
    non_python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    non_python.chmod(0o700)
    broken.symlink_to(non_python)
    with pytest.raises(ValueError, match="Python executable"):
        _spec(runtime_path=str(broken))
    broken.unlink()

    broken.symlink_to(Path(sys.executable))
    venv.joinpath("bin").chmod(0o775)
    with pytest.raises(ValueError, match="untrusted writable directory"):
        _spec(runtime_path=str(broken))


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_virtualenv_runtime_rejects_symlinked_ancestor(tmp_path):
    real_venv = tmp_path / "real-venv"
    real_venv.joinpath("bin").mkdir(parents=True)
    real_venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    real_venv.joinpath("bin", "python").symlink_to(Path(sys.executable))
    alias = tmp_path / "alias"
    alias.symlink_to(real_venv, target_is_directory=True)

    with pytest.raises(ValueError, match="virtualenv Python entrypoint"):
        _spec(runtime_path=str(alias / "bin" / "python"))


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_runtime_target_is_bound_and_retargeting_changes_fresh_generation(tmp_path):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    original_target = tmp_path / "python3.11"
    original_target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    original_target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(original_target)
    original = _spec(runtime_path=str(link))
    original_generation = original.generation

    replacement = tmp_path / "python3.99"
    replacement.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    replacement.chmod(stat.S_IRWXU)
    link.unlink()
    link.symlink_to(replacement)

    assert original.generation == original_generation
    assert not original.runtime_target_matches()
    assert _spec(runtime_path=str(link)).generation != original_generation


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin extended ACL model")
@pytest.mark.parametrize(
    ("acl_target", "acl_rule"),
    [
        ("runtime-parent", "everyone allow add_file,delete_child"),
        ("target", "everyone allow write,delete"),
    ],
)
def test_virtualenv_runtime_rejects_mutating_darwin_acls(
    tmp_path, acl_target, acl_rule
):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / "python3.99"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)
    path = venv / "bin" if acl_target == "runtime-parent" else target
    subprocess.run(["/bin/chmod", "+a", acl_rule, str(path)], check=True)

    with pytest.raises(ValueError, match="mutating extended ACL"):
        _spec(runtime_path=str(link))


def _group_writable_venv(tmp_path, mode=0o770):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("bin").chmod(mode)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    target = tmp_path / "python3.99"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o700)
    link = venv / "bin" / "python"
    link.symlink_to(target)
    return link


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_group_writable_runtime_directory_rejected_for_untrusted_group(
    tmp_path, monkeypatch
):
    from jacked.service import spec as spec_module

    link = _group_writable_venv(tmp_path)
    monkeypatch.setattr(spec_module, "_trusted_group_ids", lambda: frozenset({0}))
    with pytest.raises(ValueError, match="untrusted writable directory"):
        _spec(runtime_path=str(link))


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_group_writable_runtime_directory_accepted_for_root_equivalent_group(
    tmp_path, monkeypatch
):
    from jacked.service import spec as spec_module

    link = _group_writable_venv(tmp_path)
    gid = link.parent.stat().st_gid
    monkeypatch.setattr(
        spec_module, "_trusted_group_ids", lambda: frozenset({0, gid})
    )
    assert _spec(runtime_path=str(link)).runtime_target_path == str(
        tmp_path / "python3.99"
    )


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_world_writable_runtime_directory_rejected_even_for_trusted_group(
    tmp_path, monkeypatch
):
    from jacked.service import spec as spec_module

    link = _group_writable_venv(tmp_path, mode=0o777)
    gid = link.parent.stat().st_gid
    monkeypatch.setattr(
        spec_module, "_trusted_group_ids", lambda: frozenset({0, gid})
    )
    with pytest.raises(ValueError, match="untrusted writable directory"):
        _spec(runtime_path=str(link))


def _admin_gid_if_member():
    if sys.platform != "darwin":
        return None
    import grp

    try:
        admin = grp.getgrnam("admin")
    except KeyError:
        return None
    return admin.gr_gid if admin.gr_gid in os.getgroups() else None


@pytest.mark.skipif(
    _admin_gid_if_member() is None, reason="requires macOS admin membership"
)
def test_homebrew_admin_group_writable_prefix_is_trusted(tmp_path):
    """Regression: Homebrew's prefix is mode 0775 group admin (2026-09-04).

    The 0.100.0 tray died at boot on every Mac whose uv tool venv sits on a
    Homebrew or cask Python because this layout was rejected outright.
    """
    caskroom = tmp_path / "Caskroom"
    caskroom.mkdir()
    os.chown(caskroom, -1, _admin_gid_if_member())
    caskroom.chmod(0o775)
    base_bin = caskroom / "miniconda" / "base" / "bin"
    base_bin.mkdir(parents=True)
    target = base_bin / "python3.12"
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    link = venv / "bin" / "python"
    link.symlink_to(target)

    assert _spec(runtime_path=str(link)).runtime_target_path == str(target)


class _Group:
    def __init__(self, name, gid, members=()):
        self.gr_name, self.gr_gid, self.gr_mem = name, gid, list(members)


class _User:
    def __init__(self, name, uid, gid):
        self.pw_name, self.pw_uid, self.pw_gid = name, uid, gid


def _fake_nss(monkeypatch, *, groups, users, uid, platform, os_release=""):
    """Install a fake user database and platform; the real one is never read."""
    import grp
    import pwd

    from jacked.service import spec as spec_module

    by_name = {g.gr_name: g for g in groups}
    by_gid = {g.gr_gid: g for g in groups}

    def _getgrnam(name):
        if name not in by_name:
            raise KeyError(name)
        return by_name[name]

    def _getgrgid(gid):
        if gid not in by_gid:
            raise KeyError(gid)
        return by_gid[gid]

    def _getpwnam(name):
        for user in users:
            if user.pw_name == name:
                return user
        raise KeyError(name)

    def _getpwuid(value):
        for user in users:
            if user.pw_uid == value:
                return user
        raise KeyError(value)

    def _never_enumerate():
        raise AssertionError("the boot gate must not enumerate the user database")

    monkeypatch.setattr(os, "getuid", lambda: uid)
    monkeypatch.setattr(grp, "getgrnam", _getgrnam)
    monkeypatch.setattr(grp, "getgrgid", _getgrgid)
    monkeypatch.setattr(pwd, "getpwnam", _getpwnam)
    monkeypatch.setattr(pwd, "getpwuid", _getpwuid)
    monkeypatch.setattr(pwd, "getpwall", _never_enumerate)
    monkeypatch.setattr(spec_module, "_HOST_IS_DARWIN", platform == "darwin")
    monkeypatch.setattr(spec_module.sys, "platform", platform)
    monkeypatch.setattr(
        spec_module, "_linux_family_ids", lambda: frozenset(os_release.split())
    )
    return spec_module


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_darwin_trusts_admin_and_wheel_members_without_enumerating(monkeypatch):
    spec_module = _fake_nss(
        monkeypatch,
        groups=[_Group("admin", 80, ["root", "jack", "alice"]), _Group("wheel", 0), _Group("staff", 20)],
        users=[_User("root", 0, 0), _User("jack", 501, 20), _User("alice", 502, 20)],
        uid=501,
        platform="darwin",
    )
    assert spec_module._trusted_group_ids() == frozenset({0, 80})
    # alice is an admin member: a path she owns cannot escalate anyone.
    assert spec_module._trusted_owner_ids() == frozenset({0, 501, 502})
    # staff is shared, so a staff-writable path stays untrusted.
    assert 20 not in spec_module._trusted_group_ids()
    assert spec_module._user_private_group_id() is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
@pytest.mark.parametrize(
    ("os_release", "wheel_trusted"),
    [("debian", False), ("ubuntu debian", False), ("arch", False), ("alpine", False),
     ("rhel fedora", True), ("fedora", True), ("amzn rhel fedora", True), ("opensuse-leap suse opensuse", True)],
)
def test_linux_wheel_is_trusted_only_where_the_family_grants_sudo(
    monkeypatch, os_release, wheel_trusted
):
    spec_module = _fake_nss(
        monkeypatch,
        groups=[_Group("sudo", 27, ["jack"]), _Group("wheel", 10, ["bob"]), _Group("admin", 900, ["mallory"]), _Group("jack", 1001)],
        users=[_User("root", 0, 0), _User("jack", 1001, 1001), _User("bob", 1002, 100), _User("mallory", 1003, 100)],
        uid=1001,
        platform="linux",
        os_release=os_release,
    )
    gids = spec_module._trusted_group_ids()
    owners = spec_module._trusted_owner_ids()
    assert 27 in gids and 1001 in gids  # sudo + the user-private group
    assert (10 in gids) is wheel_trusted
    assert (1002 in owners) is wheel_trusted
    # A Linux "admin" group is an ordinary group and never trusted by name.
    assert 900 not in gids and 1003 not in owners


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_user_private_group_requires_same_name_no_members_and_a_user_range_gid(monkeypatch):
    base = dict(users=[_User("jack", 1001, 1001)], uid=1001, platform="linux")
    assert _fake_nss(monkeypatch, groups=[_Group("jack", 1001)], **base)._user_private_group_id() == 1001
    assert _fake_nss(monkeypatch, groups=[_Group("jack", 1001, ["bob"])], **base)._user_private_group_id() is None
    assert _fake_nss(monkeypatch, groups=[_Group("users", 1001)], **base)._user_private_group_id() is None
    low = dict(users=[_User("jack", 501, 501)], uid=501, platform="linux")
    assert _fake_nss(monkeypatch, groups=[_Group("jack", 501)], **low)._user_private_group_id() is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_trust_sets_are_not_cached_across_calls(monkeypatch):
    spec_module = _fake_nss(
        monkeypatch,
        groups=[_Group("admin", 80, ["jack", "alice"]), _Group("wheel", 0)],
        users=[_User("jack", 501, 20), _User("alice", 502, 20)],
        uid=501,
        platform="darwin",
    )
    assert 502 in spec_module._trusted_owner_ids()
    _fake_nss(
        monkeypatch,
        groups=[_Group("admin", 80, ["jack"]), _Group("wheel", 0)],
        users=[_User("jack", 501, 20), _User("alice", 502, 20)],
        uid=501,
        platform="darwin",
    )
    # A demoted administrator loses trust on the next validation, not on restart.
    assert 502 not in spec_module._trusted_owner_ids()


def test_linux_family_ids_parses_id_and_id_like(tmp_path, monkeypatch):
    from jacked.service import spec as spec_module

    release = tmp_path / "os-release"
    release.write_text('NAME="Rocky Linux"\nID="rocky"\nID_LIKE="rhel centos fedora"\n', encoding="utf-8")
    release.chmod(0o644)

    class _RootOwned:
        def __init__(self, real):
            self._real = real

        def stat(self):
            real = self._real.stat()
            return os.stat_result((real.st_mode, real.st_ino, real.st_dev, real.st_nlink, 0, 0, real.st_size, real.st_atime, real.st_mtime, real.st_ctime))

        def read_text(self, encoding="utf-8"):
            return self._real.read_text(encoding=encoding)

    monkeypatch.setattr(spec_module, "Path", lambda _p: _RootOwned(release))
    assert spec_module._linux_family_ids() == frozenset({"rocky", "rhel", "centos", "fedora"})


def test_artifact_marker_requires_exact_owner_and_generation():
    spec = _spec()
    marker = spec.artifact_marker()
    assert spec.matches_artifact_marker(marker)
    assert not spec.matches_artifact_marker({**marker, "generation": "0" * 64})
    assert not spec.matches_artifact_marker({**marker, "owner": "foreign"})


def test_linux_family_ids_fail_closed_on_a_writable_or_unowned_os_release(tmp_path, monkeypatch):
    from jacked.service import spec as spec_module

    release = tmp_path / "os-release"
    release.write_text('ID="rhel"\n', encoding="utf-8")
    monkeypatch.setattr(spec_module, "Path", lambda _p: release)
    release.chmod(0o666)
    assert spec_module._linux_family_ids() == frozenset()
    release.chmod(0o644)
    if os.getuid() != 0:
        # Not root-owned in this test, so still refused; root-owned is the
        # positive case pinned by test_linux_family_ids_parses_id_and_id_like.
        assert spec_module._linux_family_ids() == frozenset()

