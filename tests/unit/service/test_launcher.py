import hashlib
import os
import subprocess

import pytest

from jacked.service.launcher import (
    LauncherInstall,
    POSIX_LAUNCHER_SOURCE,
    install_versioned_launcher,
    verify_launcher,
)
from jacked.service.spec import ServiceSpec, SupervisorKind


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX launcher boundary")
def test_launcher_refuses_retargeted_runtime_without_running_replacement(tmp_path):
    venv = tmp_path / "venv"
    venv.joinpath("bin").mkdir(parents=True)
    venv.joinpath("pyvenv.cfg").write_text("home = test\n", encoding="utf-8")
    original = tmp_path / "python3.11"
    original.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    original.chmod(0o700)
    runtime = venv / "bin" / "python"
    runtime.symlink_to(original)
    spec = ServiceSpec(
        service_id="ai.hank.jacked",
        protocol_version=2,
        build_version="0.99.2",
        runtime_path=str(runtime),
        launcher_path=str(tmp_path / "launcher"),
        launcher_sha256=hashlib.sha256(POSIX_LAUNCHER_SOURCE).hexdigest(),
        supervisor=SupervisorKind.MANUAL,
        arguments=("-I", "-m", "jacked", "service", "start"),
    )
    launcher = install_versioned_launcher(
        tmp_path / "launchers",
        LauncherInstall(
            version="runtime-bound",
            name="launcher",
            content=POSIX_LAUNCHER_SOURCE,
            expected_sha256=hashlib.sha256(POSIX_LAUNCHER_SOURCE).hexdigest(),
            executable=True,
        ),
    )
    sentinel = tmp_path / "sentinel"
    replacement = tmp_path / "python3.99"
    replacement.write_text(
        f"#!/bin/sh\nprintf compromised > {sentinel}\n", encoding="utf-8"
    )
    replacement.chmod(0o700)
    runtime.unlink()
    runtime.symlink_to(replacement)

    result = subprocess.run(
        [
            str(launcher),
            spec.runtime_path,
            spec.runtime_target_path,
            *spec.arguments,
        ],
        check=False,
        timeout=10,
    )
    assert result.returncode == 78
    assert not sentinel.exists()
