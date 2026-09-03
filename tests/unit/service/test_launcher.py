import hashlib
import os

import pytest

from jacked.service.launcher import install_versioned_launcher, verify_launcher


def test_launcher_install_is_content_addressed_and_idempotent(tmp_path):
    content = b"#!/bin/sh\nexec /usr/bin/env -i /opt/jacked/python -I -m jacked\n"
    digest = hashlib.sha256(content).hexdigest()
    path = install_versioned_launcher(
        tmp_path,
        version="v2",
        name="jacked-launch",
        content=content,
        expected_sha256=digest,
        executable=True,
    )
    assert verify_launcher(path, digest)
    assert (
        install_versioned_launcher(
            tmp_path,
            version="v2",
            name="jacked-launch",
            content=content,
            expected_sha256=digest,
            executable=True,
        )
        == path
    )
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o700


def test_launcher_never_overwrites_changed_version_slot(tmp_path):
    good = b"fixed-launcher"
    digest = hashlib.sha256(good).hexdigest()
    path = install_versioned_launcher(
        tmp_path,
        version="v2",
        name="launcher",
        content=good,
        expected_sha256=digest,
    )
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="foreign or altered"):
        install_versioned_launcher(
            tmp_path,
            version="v2",
            name="launcher",
            content=good,
            expected_sha256=digest,
        )


def test_launcher_rejects_source_hash_mismatch(tmp_path):
    with pytest.raises(ValueError, match="source hash"):
        install_versioned_launcher(
            tmp_path,
            version="v2",
            name="launcher",
            content=b"content",
            expected_sha256="0" * 64,
        )
