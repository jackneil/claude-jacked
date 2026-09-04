import hashlib
import os

import pytest

from jacked.service.launcher import (
    LauncherInstall,
    install_versioned_launcher,
    verify_launcher,
)


def _request(content, digest, *, executable=False):
    return LauncherInstall(
        version="v2",
        name="jacked-launch" if executable else "launcher",
        content=content,
        expected_sha256=digest,
        executable=executable,
    )


def test_launcher_install_is_content_addressed_and_idempotent(tmp_path):
    content = b"#!/bin/sh\nexec /usr/bin/env -i /opt/jacked/python -I -m jacked\n"
    digest = hashlib.sha256(content).hexdigest()
    path = install_versioned_launcher(
        tmp_path,
        _request(content, digest, executable=True),
    )
    assert verify_launcher(path, digest)
    assert (
        install_versioned_launcher(
            tmp_path,
            _request(content, digest, executable=True),
        )
        == path
    )
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o700


def test_launcher_recovers_interrupted_hardlink_publication(tmp_path):
    content = b"fixed-launcher"
    digest = hashlib.sha256(content).hexdigest()
    request = _request(content, digest)
    path = install_versioned_launcher(tmp_path, request)
    temporary = path.with_name(".launcher.interrupted")
    os.link(path, temporary)
    assert path.stat().st_nlink == 2

    assert install_versioned_launcher(tmp_path, request) == path
    assert path.stat().st_nlink == 1
    assert not temporary.exists()


def test_launcher_never_overwrites_changed_version_slot(tmp_path):
    good = b"fixed-launcher"
    digest = hashlib.sha256(good).hexdigest()
    path = install_versioned_launcher(
        tmp_path,
        _request(good, digest),
    )
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="foreign or altered"):
        install_versioned_launcher(
            tmp_path,
            _request(good, digest),
        )


def test_launcher_rejects_source_hash_mismatch(tmp_path):
    with pytest.raises(ValueError, match="source hash"):
        install_versioned_launcher(
            tmp_path,
            _request(b"content", "0" * 64),
        )
