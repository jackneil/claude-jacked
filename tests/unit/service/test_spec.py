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


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_trusted_group_ids_cover_root_equivalent_and_private_groups(monkeypatch):
    import grp
    import pwd

    from jacked.service import spec as spec_module

    class _Group:
        def __init__(self, name, gid, members=()):
            self.gr_name, self.gr_gid, self.gr_mem = name, gid, list(members)

    class _User:
        def __init__(self, name, uid, gid):
            self.pw_name, self.pw_uid, self.pw_gid = name, uid, gid

    groups = {"admin": _Group("admin", 80, ["alice"]), "jack": _Group("jack", 1001)}
    by_gid = {80: groups["admin"], 1001: groups["jack"], 20: _Group("staff", 20)}
    users = [_User("jack", 1001, 1001), _User("alice", 1002, 20)]

    monkeypatch.setattr(os, "getuid", lambda: 1001)
    monkeypatch.setattr(grp, "getgrnam", lambda name: groups[name])
    monkeypatch.setattr(grp, "getgrgid", lambda gid: by_gid[gid])
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: next(u for u in users if u.pw_uid == uid))
    monkeypatch.setattr(pwd, "getpwnam", lambda name: next(u for u in users if u.pw_name == name))
    monkeypatch.setattr(pwd, "getpwall", lambda: list(users))
    spec_module._trusted_group_ids.cache_clear()
    spec_module._trusted_owner_ids.cache_clear()
    try:
        assert spec_module._trusted_group_ids() == frozenset({0, 80, 1001})
        # alice is an admin member: a path she owns cannot escalate anyone.
        assert spec_module._trusted_owner_ids() == frozenset({0, 1001, 1002})
        # staff (20) is shared, so a staff-writable path stays untrusted.
        assert 20 not in spec_module._trusted_group_ids()
    finally:
        spec_module._trusted_group_ids.cache_clear()
        spec_module._trusted_owner_ids.cache_clear()


@pytest.mark.skipif(os.name != "posix", reason="POSIX runtime trust model")
def test_private_group_is_not_trusted_when_shared(monkeypatch):
    import grp
    import pwd

    from jacked.service import spec as spec_module

    class _Group:
        def __init__(self, name, gid, members=()):
            self.gr_name, self.gr_gid, self.gr_mem = name, gid, list(members)

    class _User:
        def __init__(self, name, uid, gid):
            self.pw_name, self.pw_uid, self.pw_gid = name, uid, gid

    users = [_User("jack", 1001, 1001), _User("bob", 1002, 1001)]
    monkeypatch.setattr(os, "getuid", lambda: 1001)
    monkeypatch.setattr(grp, "getgrgid", lambda gid: _Group("jack", 1001))
    monkeypatch.setattr(pwd, "getpwuid", lambda uid: users[0])
    monkeypatch.setattr(pwd, "getpwall", lambda: list(users))
    assert spec_module._user_private_group_id() is None


def test_artifact_marker_requires_exact_owner_and_generation():
    spec = _spec()
    marker = spec.artifact_marker()
    assert spec.matches_artifact_marker(marker)
    assert not spec.matches_artifact_marker({**marker, "generation": "0" * 64})
    assert not spec.matches_artifact_marker({**marker, "owner": "foreign"})
