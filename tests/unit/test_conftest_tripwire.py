"""The real-process tripwire in tests/conftest.py must hold at every entry point.

On 2026-09-04 a test spawned a real detached tray and another drove the real
launchctl. Default arguments bound at import time (``run: Any = subprocess.run``)
would bypass a fixture-time patch, so the supervisor entry points resolve the
runner at call time and these tests pin both halves.
"""

from __future__ import annotations

import subprocess

import pytest


def test_run_refuses_supervisor_binaries():
    with pytest.raises(RuntimeError, match="real supervisor"):
        subprocess.run(["launchctl", "print", "gui/1"], capture_output=True)
    with pytest.raises(RuntimeError, match="real supervisor"):
        subprocess.run(["/bin/launchctl", "bootstrap", "gui/1", "x.plist"])


def test_popen_refuses_supervisor_binaries_and_service_lifecycle_commands():
    with pytest.raises(RuntimeError, match="real supervisor"):
        subprocess.Popen(["systemctl", "--user", "daemon-reload"])
    with pytest.raises(RuntimeError, match="real jacked service"):
        subprocess.Popen(["/x/bin/jacked", "service", "start", "--port", "8321"])


def test_run_refuses_every_service_lifecycle_verb():
    for verb in ("start", "restart", "preflight", "recover", "install"):
        with pytest.raises(RuntimeError, match="real jacked service"):
            subprocess.run(["/x/bin/jacked", "service", verb])


def test_check_output_routes_through_the_guard():
    with pytest.raises(RuntimeError, match="real supervisor"):
        subprocess.check_output(["schtasks.exe", "/Query"])


def test_supervisor_entry_points_do_not_capture_the_real_runner(tmp_path, monkeypatch):
    """A def-time default would bind the real subprocess.run before the fixture."""
    import inspect

    from jacked.service.supervisors import (
        install_owned_supervisor,
        restart_owned_supervisor,
        uninstall_owned_supervisor,
    )
    from jacked.service.supervisors.uninstall import (
        uninstall_owned_supervisor as uninstall_impl,
    )

    for function in (
        install_owned_supervisor,
        restart_owned_supervisor,
        uninstall_owned_supervisor,
        uninstall_impl,
    ):
        default = inspect.signature(function).parameters["run"].default
        assert default is None, function.__qualname__


def test_run_refuses_a_real_jacked_install():
    with pytest.raises(RuntimeError, match="real jacked install"):
        subprocess.run(["/x/bin/jacked", "install", "--force"])
    with pytest.raises(RuntimeError, match="real jacked install"):
        subprocess.Popen([r"C:\\x\\jacked.exe", "install"])


def test_run_refuses_windows_style_supervisor_paths():
    with pytest.raises(RuntimeError, match="real supervisor"):
        subprocess.run([r"C:\\Windows\\System32\\schtasks.exe", "/Query"])

