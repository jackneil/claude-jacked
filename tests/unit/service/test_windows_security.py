"""Windows-only regression tests for native ownership primitives."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from unittest.mock import patch

import pytest

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows security API")


def test_current_process_sid_and_identity_are_pointer_safe():
    from jacked.service.instance_storage import process_identity
    from jacked.service.windows_security import current_user_sid, windows_libraries

    api = windows_libraries()
    assert api.kernel32.OpenProcess.argtypes is not None
    assert api.kernel32.OpenProcess.restype is not None
    assert api.kernel32.LocalFree.argtypes is not None
    assert api.advapi32.GetSecurityInfo.argtypes is not None
    assert current_user_sid().startswith("S-")
    identity = process_identity(os.getpid())
    assert identity.pid == os.getpid()
    assert identity.creation_id.startswith("windows-filetime:")
    expected_executable = getattr(sys, "_base_executable", sys.executable)
    assert os.path.samefile(identity.executable, expected_executable)


def test_private_dacl_rejects_an_additional_everyone_ace(tmp_path):
    from jacked.service.windows_security import (
        inspect_windows_path,
        secure_windows_path,
    )

    path = tmp_path / "private.json"
    path.write_text("{}", encoding="utf-8")
    secure_windows_path(path)
    assert inspect_windows_path(path).private_for(directory=False)
    directory = tmp_path / "private-directory"
    directory.mkdir()
    secure_windows_path(directory)
    assert inspect_windows_path(directory).private_for(directory=True)

    changed = subprocess.run(
        ["icacls.exe", str(path), "/grant", "*S-1-1-0:(W)"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert changed.returncode == 0, changed.stderr or changed.stdout
    assert not inspect_windows_path(path).private_for(directory=False)


def test_launcher_reuse_requires_private_current_user_file(tmp_path):
    from jacked.service.launcher import verify_launcher
    from jacked.service.windows_security import secure_windows_path

    path = tmp_path / "launcher.ps1"
    content = b"Write-Output jacked\r\n"
    path.write_bytes(content)
    digest = hashlib.sha256(content).hexdigest()
    secure_windows_path(path)
    assert verify_launcher(path, digest)

    changed = subprocess.run(
        ["icacls.exe", str(path), "/grant", "*S-1-1-0:(W)"],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert changed.returncode == 0, changed.stderr or changed.stdout
    assert not verify_launcher(path, digest)


def test_private_directory_rejects_junction(tmp_path):
    from jacked.service.windows_state import ensure_private_windows_directory

    target = tmp_path / "target"
    target.mkdir()
    junction = tmp_path / "junction"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(created.stderr or created.stdout)
    with pytest.raises(ValueError, match="unsafe Windows ownership or type"):
        ensure_private_windows_directory(junction)


def test_private_directory_rejects_an_ancestor_junction(tmp_path):
    from jacked.service.windows_state import ensure_private_windows_directory

    target = tmp_path / "target-parent"
    target.mkdir()
    junction = tmp_path / "junction-parent"
    created = subprocess.run(
        ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(target)],
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    if created.returncode != 0:
        pytest.skip(created.stderr or created.stdout)
    with pytest.raises(ValueError, match="reparse ancestor"):
        ensure_private_windows_directory(junction / "service")


def test_incomplete_fixed_name_locks_recover_to_private_dacls(tmp_path):
    from jacked.service.instance import ServiceLease
    from jacked.service.supervisors._transition import SupervisorTransitionLease
    from jacked.service.windows_security import inspect_windows_path

    root = tmp_path / "service"
    root.mkdir()
    lease_path = root / "instance.lock"
    lease_path.write_bytes(b"")
    lease = ServiceLease(lease_path)
    lease.acquire()
    try:
        assert inspect_windows_path(lease_path).private_for(directory=False)
    finally:
        lease.release()

    artifact = root / "supervisors" / "jacked.xml"
    artifact.parent.mkdir()
    lock_path = artifact.parent / ".ai.hank.jacked.transition.lock"
    lock_path.write_bytes(b"")
    with SupervisorTransitionLease(artifact, "ai.hank.jacked"):
        assert inspect_windows_path(lock_path).private_for(directory=False)


@pytest.mark.parametrize("failure", [OSError("denied"), ValueError("unsafe")])
def test_legacy_hardening_failure_is_logged_and_refused(
    tmp_path, caplog, failure
):
    from jacked.service.supervisors.task_scheduler import (
        _harden_legacy_windows_file,
    )

    path = tmp_path / "jacked.vbs"
    path.write_bytes(b"known")
    with patch(
        "jacked.service.windows_state.ensure_private_windows_file",
        side_effect=failure,
    ):
        assert _harden_legacy_windows_file(path, b"known") is None
    assert "Legacy Windows artifact hardening failed" in caplog.text


def test_force_termination_retains_and_checks_the_process_handle():
    from jacked.service.instance_storage import process_identity
    from jacked.service.process import (
        OwnedProcess,
        TerminationResult,
        terminate_owned_process,
    )

    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        identity = process_identity(child.pid)
        owned = OwnedProcess(
            pid=identity.pid,
            creation_id=identity.creation_id,
            executable=identity.executable,
            managed=False,
        )
        assert terminate_owned_process(owned, force=True) is TerminationResult.SIGNALLED
        child.wait(timeout=10)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)
