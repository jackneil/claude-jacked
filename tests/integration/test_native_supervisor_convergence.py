"""Stateful cross-platform supervisor convergence regression."""

from __future__ import annotations

import hashlib
import os
import plistlib
import sys
from types import SimpleNamespace

import pytest

from jacked.service.spec import ServiceSpec, SupervisorKind
from jacked.service.supervisors import (
    extract_marker,
    install_owned_supervisor,
    render_for_spec,
)


def _spec(kind, launcher, *, build):
    return ServiceSpec(
        service_id="ai.hank.jacked",
        protocol_version=2,
        build_version=build,
        runtime_path=os.path.realpath(sys.executable),
        launcher_path=str(launcher),
        launcher_sha256=hashlib.sha256(b"launcher").hexdigest(),
        supervisor=kind,
        arguments=("-I", "-m", "jacked", "service", "start"),
    )


def _secure_windows_test_path(path):
    if os.name == "nt":
        from jacked.service.windows_security import secure_windows_path

        if path.parent.exists():
            secure_windows_path(path.parent)
        secure_windows_path(path)


class StatefulManager:
    """Small native-manager model with one process/listener/UI invariant."""

    def __init__(self, kind, path, registered):
        self.kind = kind
        self.path = path
        self.registered = registered
        self.running = False
        self.processes = 0
        self.listeners = 0
        self.icons = 0
        self.max_processes = 0
        self.max_listeners = 0
        self.max_icons = 0
        self.failed_starts = 0
        self._start()

    def _start(self):
        if self.running:
            self.failed_starts += 1
            return
        self.running = True
        self.processes += 1
        self.listeners += 1
        self.icons += 1
        self.max_processes = max(self.max_processes, self.processes)
        self.max_listeners = max(self.max_listeners, self.listeners)
        self.max_icons = max(self.max_icons, self.icons)

    def _stop(self):
        if not self.running:
            return False
        self.running = False
        self.processes -= 1
        self.listeners -= 1
        self.icons -= 1
        return True

    def __call__(self, command, **_kwargs):
        if self.kind is SupervisorKind.LAUNCHD:
            return self._launchd(command)
        if self.kind is SupervisorKind.SYSTEMD_USER:
            return self._systemd(command)
        return self._task(command)

    def _launchd(self, command):
        action = command[1]
        if action == "print":
            if self.registered is None:
                return SimpleNamespace(returncode=113, stdout="")
            payload = plistlib.loads(self.registered)
            marker = extract_marker(self.registered, self.kind)
            stdout = " ".join(
                [str(marker["generation"]), *payload["ProgramArguments"]]
            )
            return SimpleNamespace(returncode=0, stdout=stdout)
        if action == "enable":
            return SimpleNamespace(returncode=0, stdout="")
        if action == "bootout":
            self._stop()
            self.registered = None
            return SimpleNamespace(returncode=0, stdout="")
        if action == "bootstrap":
            self.registered = self.path.read_bytes()
            self._start()
            return SimpleNamespace(returncode=0, stdout="")
        if action == "kickstart":
            self._stop()
            self._start()
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(command)

    def _systemd(self, command):
        action = command[2]
        if action == "show":
            return SimpleNamespace(
                returncode=0,
                stdout=(
                    f"LoadState=loaded\nFragmentPath={self.path}\n"
                    f"ActiveState={'active' if self.running else 'inactive'}\n"
                    "UnitFileState=enabled\nNeedDaemonReload=no\n"
                ),
            )
        if action == "daemon-reload":
            self.registered = self.path.read_bytes()
        elif action == "restart":
            self._stop()
            self._start()
        return SimpleNamespace(returncode=0, stdout="")

    def _task(self, command):
        action = command[1]
        if action == "/Query":
            return SimpleNamespace(
                returncode=0 if self.registered is not None else 1,
                stdout=self.registered.decode() if self.registered else "",
            )
        if action == "/Create":
            xml_path = command[command.index("/XML") + 1]
            self.registered = type(self.path)(xml_path).read_bytes()
            return SimpleNamespace(returncode=0, stdout="")
        if action == "/End":
            was_running = self._stop()
            return SimpleNamespace(returncode=0 if was_running else 1, stdout="")
        if action == "/Run":
            self._start()
            return SimpleNamespace(returncode=0, stdout="")
        raise AssertionError(command)


@pytest.mark.parametrize(
    "kind",
    [
        SupervisorKind.LAUNCHD,
        SupervisorKind.SYSTEMD_USER,
        SupervisorKind.TASK_SCHEDULER,
    ],
)
def test_old_generation_and_repeated_installs_converge_to_one_native_service(
    tmp_path, monkeypatch, kind
):
    launcher = tmp_path / "launcher"
    launcher.write_bytes(b"launcher")
    launcher.chmod(0o700)
    _secure_windows_test_path(launcher)
    old_spec = _spec(kind, launcher, build="0.98.1")
    new_spec = _spec(kind, launcher, build="0.99.1")
    environment = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    if kind is SupervisorKind.TASK_SCHEDULER:
        environment["JACKED_SERVICE_USER"] = "sid:S-1-5-21-123"
        monkeypatch.setenv("APPDATA", str(tmp_path / "AppData"))
        path = tmp_path / "jacked-task.xml"
    elif kind is SupervisorKind.LAUNCHD:
        path = tmp_path / "LaunchAgents" / "ai.hank.jacked.plist"
    else:
        path = tmp_path / "systemd" / "jacked.service"
    old = render_for_spec(old_spec, environment=environment)
    expected = render_for_spec(new_spec, environment=environment)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(old.content)
    path.chmod(0o600)
    _secure_windows_test_path(path)
    manager = StatefulManager(kind, path, old.content)

    for _attempt in range(3):
        result = install_owned_supervisor(
            new_spec,
            path,
            environment=environment,
            run=manager,
            uid=501,
        )
        assert result.ok is True

    assert path.read_bytes() == expected.content
    assert extract_marker(manager.registered, kind) == expected.marker
    assert manager.running is True
    assert manager.processes == manager.listeners == manager.icons == 1
    assert manager.max_processes == manager.max_listeners == manager.max_icons == 1
    assert manager.failed_starts == 0
