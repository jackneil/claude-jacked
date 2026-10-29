"""Linux POSIX ACLs on the trust path (findbin and the service spec).

With an ACL mask the group mode bits show the MASK, so a named grant to
another user can hide behind a trusted owning group. These tests set real
ACLs with setfacl; they run on Linux where setfacl and an ACL-capable
filesystem are present, and skip elsewhere.
"""

import shutil
import subprocess
import sys

import pytest

from jacked import findbin
from jacked.service import spec

pytestmark = [
    pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux POSIX ACLs"),
    pytest.mark.skipif(shutil.which("setfacl") is None, reason="setfacl not installed"),
    # setfacl is an ordinary local tool, not a supervisor or the service.
    pytest.mark.real_process,
]

OTHER_UID = 4242  # an id that is neither this user nor root


def _setfacl(*args):
    result = subprocess.run(["setfacl", *args], capture_output=True, text=True, check=False)
    if result.returncode != 0 and "Operation not supported" in result.stderr:
        pytest.skip("filesystem without POSIX ACL support")
    assert result.returncode == 0, result.stderr


def _chain(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(mode=0o755)
    tool = bin_dir / "codex"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    return bin_dir, tool


def test_a_named_write_grant_on_the_holding_directory_is_refused(tmp_path):
    bin_dir, tool = _chain(tmp_path)
    assert findbin._resolve_trusted(str(tool))[1] is None
    _setfacl("-m", f"u:{OTHER_UID}:rwx", str(bin_dir))
    assert findbin._resolve_trusted(str(tool)) == (None, "a path component has a mutating ACL")
    with pytest.raises(ValueError, match="mutating POSIX ACL"):
        spec._validate_linux_acl(bin_dir)


def test_a_named_write_grant_on_the_binary_is_refused(tmp_path):
    _, tool = _chain(tmp_path)
    _setfacl("-m", f"u:{OTHER_UID}:rw", str(tool))
    assert findbin._resolve_trusted(str(tool)) == (None, "a path component has a mutating ACL")
    with pytest.raises(ValueError, match="trusted regular file|mutating POSIX ACL"):
        spec._validate_posix_file(tool, "runtime_path")


def test_a_mask_without_write_keeps_the_path_trusted(tmp_path):
    bin_dir, tool = _chain(tmp_path)
    _setfacl("-m", f"u:{OTHER_UID}:rwx,m::r-x", str(bin_dir))
    assert findbin._resolve_trusted(str(tool))[1] is None


def test_a_read_only_named_grant_keeps_the_path_trusted(tmp_path):
    bin_dir, tool = _chain(tmp_path)
    _setfacl("-m", f"u:{OTHER_UID}:r-x", str(bin_dir))
    assert findbin._resolve_trusted(str(tool))[1] is None
