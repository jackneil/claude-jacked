"""Supervisor directories and artifacts under a user-private group umask.

Debian and Ubuntu give a user whose primary group is their own private group
umask 002. The supervisor code created its directory under that umask and
then refused it as group-writable, so ``jacked service install`` failed on a
stock desktop install. CI runners use umask 022 and never saw it.
"""

import os
import stat
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


def _status(mode, uid_offset=0, gid_offset=0):
    return SimpleNamespace(
        st_mode=stat.S_IFDIR | mode,
        st_uid=os.getuid() + uid_offset,
        st_gid=PRIVATE_GID + gid_offset,
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
    status = _status(mode, uid_offset, gid_offset)
    assert privately_controlled(tmp_path, status) is expected


@posix_only
@pytest.mark.usefixtures("no_private_group")
def test_group_write_is_refused_without_a_user_private_group():
    assert privately_controlled(Path("."), _status(0o775)) is False


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
