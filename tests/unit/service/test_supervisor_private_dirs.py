"""Supervisor directories and artifacts under a user-private group umask.

Debian and Ubuntu give a user whose primary group is their own private group
umask 002. The supervisor code created its directory under that umask and
then refused it as group-writable, so ``jacked service install`` failed on a
stock desktop install. CI runners use umask 022 and never saw it.
"""

import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from jacked.service.supervisors import _helpers
from jacked.service.supervisors._helpers import privately_controlled
from jacked.service.supervisors._transition import _ensure_safe_parent

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX mode bits")

PRIVATE_GID = 4321


@pytest.fixture
def private_group(monkeypatch):
    monkeypatch.setattr(
        "jacked.service.spec._user_private_group_id", lambda: PRIVATE_GID
    )


@pytest.fixture
def no_private_group(monkeypatch):
    monkeypatch.setattr("jacked.service.spec._user_private_group_id", lambda: None)


def _status(mode, uid_offset=0, gid_offset=0, path=None):
    real = path.lstat() if path is not None else None
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=os.getuid() + uid_offset,
        st_gid=PRIVATE_GID + gid_offset,
        st_dev=real.st_dev if real else 0,
        st_ino=real.st_ino if real else 0,
    )


@posix_only
@pytest.mark.usefixtures("private_group")
@pytest.mark.parametrize(
    ("mode", "uid_offset", "gid_offset", "expected"),
    [
        (0o700, 0, 0, True),
        (0o755, 0, 0, True),
        (0o775, 0, 0, True),  # group write by the user's own private group
        (0o775, 0, 1, False),  # another group
        (0o757, 0, 0, False),  # anyone can write
        (0o700, 1, 0, False),  # another owner
    ],
)
def test_privately_controlled(tmp_path, mode, uid_offset, gid_offset, expected):
    status = _status(mode, uid_offset, gid_offset, path=tmp_path)
    assert privately_controlled(tmp_path, status) is expected


@posix_only
@pytest.mark.usefixtures("no_private_group")
def test_group_write_is_refused_without_a_user_private_group():
    assert privately_controlled(Path("."), _status(0o775, path=Path("."))) is False


@posix_only
@pytest.mark.usefixtures("no_private_group")
def test_a_directory_created_under_umask_002_is_private(tmp_path):
    previous = os.umask(0o002)
    try:
        _ensure_safe_parent(tmp_path / "fresh" / "LaunchAgents")
        _helpers.atomic_write(tmp_path / "unit" / "user" / "jacked.service", b"[Unit]\n")
    finally:
        os.umask(previous)
    assert (tmp_path / "fresh").stat().st_mode & 0o077 == 0
    assert (tmp_path / "fresh" / "LaunchAgents").stat().st_mode & 0o077 == 0
    assert (tmp_path / "unit").stat().st_mode & 0o077 == 0
    assert (tmp_path / "unit" / "user").stat().st_mode & 0o077 == 0
    assert (tmp_path / "unit" / "user" / "jacked.service").stat().st_mode & 0o077 == 0


@posix_only
def test_an_existing_directory_writable_by_the_private_group_is_accepted(
    tmp_path, monkeypatch
):
    existing = tmp_path / "systemd" / "user"
    existing.mkdir(parents=True)
    existing.chmod(0o775)
    gid = existing.stat().st_gid
    monkeypatch.setattr("jacked.service.spec._user_private_group_id", lambda: gid)
    _ensure_safe_parent(existing)

    monkeypatch.setattr("jacked.service.spec._user_private_group_id", lambda: None)
    with pytest.raises(OSError, match="not privately controlled"):
        _ensure_safe_parent(existing)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS extended ACLs")
def test_a_mutating_macos_acl_entry_is_not_private(tmp_path):
    """Mode bits cannot show an extended ACL entry: a LaunchAgents folder that
    grants everyone add_file is writable by another user."""
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(mode=0o700)
    assert privately_controlled(agents, agents.lstat())
    subprocess.run(["/bin/chmod", "+a", "everyone allow add_file,delete_child", str(agents)], check=True)
    assert not privately_controlled(agents, agents.lstat())
    with pytest.raises(OSError, match="not privately controlled"):
        _ensure_safe_parent(agents)
