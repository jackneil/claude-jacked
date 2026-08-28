"""Platform capability probes for the test suite.

These exist so the local gate MEANS something. Before this module, a Windows
run reported 46 failures that had nothing to do with the code under test, which
trains everyone to ignore a red suite -- and a red suite nobody reads cannot
catch a real regression.

Every probe here tests the CAPABILITY, never the platform name. A Windows box
with Developer Mode enabled can create symlinks, and those tests should run
there rather than being skipped on a `sys.platform` guess. `sys.platform` is
only consulted as a fast path for the case where the answer is unconditional.
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path

import pytest

_WIN = sys.platform == "win32"


def symlinks_supported() -> bool:
    """True when this process can actually create a symlink.

    On Windows this needs Administrator or Developer Mode; without it,
    ``os.symlink`` raises ``OSError [WinError 1314] A required privilege is not
    held by the client``.
    """
    if not _WIN:
        return True
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "s"
        src.write_text("x", encoding="utf-8")
        try:
            (Path(d) / "l").symlink_to(src)
            return True
        except OSError:
            return False


def posix_dir_permissions_enforced() -> bool:
    """True when clearing the write bit on a DIRECTORY actually blocks writes.

    POSIX honors ``chmod 0o500``. Windows does not: the mode bits are stored but
    the ACL is what governs, so a "read-only" directory still accepts new files
    and any test simulating an unwritable HOME silently gets a writable one, then
    fails asserting on an error that was never raised.
    """
    if not _WIN:
        return True
    with tempfile.TemporaryDirectory() as d:
        locked = Path(d) / "locked"
        locked.mkdir()
        try:
            locked.chmod(stat.S_IRUSR | stat.S_IXUSR)
            probe = locked / "probe"
            try:
                probe.write_text("x", encoding="utf-8")
            except OSError:
                return True
            return False
        finally:
            try:
                locked.chmod(stat.S_IRWXU)
            except OSError:
                pass


def posix_file_modes_enforced() -> bool:
    """True when a file's permission bits round-trip through ``stat``.

    Windows reports 0o666/0o777 regardless of what was set, so an assertion like
    ``mode == 0o700`` compares 511 to 448 and fails for reasons unrelated to the
    code being tested.
    """
    if not _WIN:
        return True
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "f"
        f.write_text("x", encoding="utf-8")
        f.chmod(0o600)
        return stat.S_IMODE(os.stat(f).st_mode) == 0o600


def shebang_exec_supported() -> bool:
    """True when a text file with a ``#!`` line can be exec'd directly.

    Windows has no shebang support: exec'ing such a file raises
    ``[WinError 193] %1 is not a valid Win32 application``. Tests that fake an
    external binary this way need a ``.cmd`` wrapper instead, which is a fix
    rather than a skip -- see ``_write_npx_shim`` in ``tests/test_packs.py``.
    """
    return not _WIN


def posix_file_read_permissions_enforced() -> bool:
    """True when clearing a FILE's read bit actually blocks reading it.

    POSIX honors ``chmod 0o000``. Windows does not for the owning user, so a
    test simulating an unreadable config or database silently gets a readable
    one and then fails asserting on the error path it never reached.
    """
    if not _WIN:
        return True
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "f"
        f.write_text("x", encoding="utf-8")
        try:
            f.chmod(0o000)
            try:
                f.read_text(encoding="utf-8")
            except OSError:
                return True
            return False
        finally:
            try:
                f.chmod(stat.S_IRWXU)
            except OSError:
                pass


def posix_exec_bit_enforced() -> bool:
    """True when ``os.access(path, os.X_OK)`` reflects a missing exec bit.

    Windows has no exec bit: X_OK is True for any existing file, so code that
    falls back when a target is not executable never takes that branch and the
    test asserting the fallback fails.
    """
    if not _WIN:
        return True
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "f"
        f.write_text("x", encoding="utf-8")
        f.chmod(stat.S_IRUSR | stat.S_IWUSR)
        return not os.access(f, os.X_OK)


def write_python_shim(directory, name: str, body: str) -> Path:
    """Write ``body`` as a directly-executable stand-in for an external binary.

    Tests fake binaries (npx, codex) so they can drive the real subprocess path
    without the real tool. POSIX does that with a shebang; Windows has none, and
    exec'ing such a file raises ``[WinError 193] %1 is not a valid Win32
    application`` -- which is why ~20 pack tests and both codex round-trip tests
    failed here for reasons unrelated to the code under test.

    On Windows, write the body as a ``.py`` and drive it from a ``.cmd``
    launcher. Callers invoke these as a list argv with ``shell=False``;
    CreateProcess runs a ``.cmd`` through cmd.exe with arguments forwarded
    intact (``%*``) and stdin/stdout piping preserved, so the shim sees exactly
    the argv and stdin the caller intended.

    Returns the path to hand to the code under test.
    """
    directory = Path(directory)
    if _WIN:
        script = directory / f"{name}_shim.py"
        script.write_text(body, encoding="utf-8")
        launcher = directory / f"{name}.cmd"
        # Quote both paths (a space in a temp path must not split the command)
        # and use CRLF, which cmd.exe is fussy about.
        launcher.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
        )
        return launcher
    script = directory / name
    script.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return script


requires_symlinks = pytest.mark.skipif(
    not symlinks_supported(),
    reason="symlinks require admin or Developer Mode on Windows",
)

requires_posix_dir_permissions = pytest.mark.skipif(
    not posix_dir_permissions_enforced(),
    reason="Windows ignores directory mode bits, so an unwritable dir cannot be simulated",
)

requires_posix_file_modes = pytest.mark.skipif(
    not posix_file_modes_enforced(),
    reason="Windows does not round-trip POSIX file mode bits",
)

requires_posix_file_read_permissions = pytest.mark.skipif(
    not posix_file_read_permissions_enforced(),
    reason="Windows ignores a cleared read bit, so an unreadable file cannot be simulated",
)

requires_posix_exec_bit = pytest.mark.skipif(
    not posix_exec_bit_enforced(),
    reason="Windows has no exec bit, so os.access(X_OK) is always True",
)
