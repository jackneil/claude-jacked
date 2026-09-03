"""Typed, evidence-qualified inspection of native login startup state."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from jacked.service import CLAUDE_DIR, LAUNCHD_LABEL
from jacked.service.spec import SupervisorKind
from jacked.service.supervisors import extract_marker


class AutostartState(str, Enum):
    ABSENT = "absent"
    OWNED_ENABLED = "owned_enabled"
    OWNED_DISABLED = "owned_disabled"
    LEGACY = "legacy"
    FOREIGN = "foreign"
    DUPLICATE = "duplicate"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class AutostartInspection:
    state: AutostartState
    reason: str = ""

    @property
    def enabled(self) -> bool:
        return self.state in {
            AutostartState.OWNED_ENABLED,
            AutostartState.LEGACY,
            AutostartState.DUPLICATE,
        }

    @property
    def toggle_safe(self) -> bool:
        return self.state in {
            AutostartState.ABSENT,
            AutostartState.OWNED_ENABLED,
            AutostartState.OWNED_DISABLED,
        }


def inspect_autostart(
    *,
    platform: str | None = None,
    run: Any | None = None,
    launchd_path: Path | None = None,
    task_path: Path | None = None,
    legacy_vbs_path: Path | None = None,
    systemd_path: Path | None = None,
) -> AutostartInspection:
    current = sys.platform if platform is None else platform
    manager_run = subprocess.run if run is None else run
    if current == "darwin":
        return _inspect_launchd(launchd_path or _launchd_path(), manager_run)
    if current == "win32":
        return _inspect_windows(
            task_path or _task_path(),
            legacy_vbs_path or _windows_vbs_path(),
            manager_run,
        )
    if current.startswith("linux"):
        return _inspect_systemd(systemd_path or _systemd_path(), manager_run)
    return AutostartInspection(AutostartState.ABSENT)


def _inspect_launchd(path: Path, run: Any) -> AutostartInspection:
    content = _read(path)
    if content is None:
        return AutostartInspection(AutostartState.ABSENT)
    marker = extract_marker(content, SupervisorKind.LAUNCHD)
    if _owned(marker):
        try:
            result = run(
                ["launchctl", "print-disabled", f"gui/{os.getuid()}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return AutostartInspection(AutostartState.UNKNOWN, "launchd unavailable")
        if result.returncode != 0:
            return AutostartInspection(AutostartState.UNKNOWN, "launchd indeterminate")
        disabled = re.search(
            rf'"{re.escape(LAUNCHD_LABEL)}"\s*=>\s*true\b', result.stdout
        )
        return AutostartInspection(
            AutostartState.OWNED_DISABLED
            if disabled
            else AutostartState.OWNED_ENABLED
        )
    try:
        payload = plistlib.loads(content)
        if not isinstance(payload, dict):
            raise TypeError("launchd plist root must be a dictionary")
        arguments = payload.get("ProgramArguments", [])
        if payload.get("Label") == LAUNCHD_LABEL and _legacy_command(arguments):
            return AutostartInspection(
                AutostartState.LEGACY, "legacy launchd definition requires recovery"
            )
    except (plistlib.InvalidFileException, ValueError, TypeError):
        pass
    return AutostartInspection(AutostartState.FOREIGN, "unowned launchd definition")


def _inspect_windows(path: Path, legacy: Path, run: Any) -> AutostartInspection:
    legacy_present = legacy.exists()
    common = {"capture_output": True, "text": True, "timeout": 5, "check": False}
    try:
        result = run(
            ["schtasks.exe", "/Query", "/TN", LAUNCHD_LABEL, "/XML"], **common
        )
    except (OSError, subprocess.SubprocessError):
        return AutostartInspection(AutostartState.UNKNOWN, "Task Scheduler unavailable")
    registered = result.returncode == 0
    if result.returncode not in {0, 1}:
        return AutostartInspection(AutostartState.UNKNOWN, "Task Scheduler indeterminate")
    marker = (
        extract_marker(result.stdout.encode(), SupervisorKind.TASK_SCHEDULER)
        if registered
        else None
    )
    if registered and not _owned(marker):
        return AutostartInspection(AutostartState.FOREIGN, "registered task is unowned")
    if registered and legacy_present:
        return AutostartInspection(AutostartState.DUPLICATE, "task and legacy VBS coexist")
    if registered:
        enabled = _task_enabled(result.stdout)
        return AutostartInspection(
            AutostartState.OWNED_ENABLED if enabled else AutostartState.OWNED_DISABLED,
            "registered task is disabled" if not enabled else "",
        )
    if legacy_present:
        return AutostartInspection(AutostartState.LEGACY, "legacy Startup VBS exists")
    content = _read(path)
    if content is not None:
        if _owned(extract_marker(content, SupervisorKind.TASK_SCHEDULER)):
            return AutostartInspection(
                AutostartState.OWNED_DISABLED, "task is staged only"
            )
        return AutostartInspection(AutostartState.FOREIGN, "staged task is unowned")
    return AutostartInspection(AutostartState.ABSENT)


def _task_enabled(content: str) -> bool:
    """Treat any explicit disabled task or trigger flag as disabled."""
    try:
        root = ElementTree.fromstring(content)
    except ElementTree.ParseError:
        return False
    return not any(
        item.tag.rsplit("}", 1)[-1] == "Enabled"
        and (item.text or "").strip().lower() == "false"
        for item in root.iter()
    )


def _inspect_systemd(path: Path, run: Any) -> AutostartInspection:
    content = _read(path)
    try:
        result = run(
            ["systemctl", "--user", "is-enabled", "jacked.service"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return AutostartInspection(AutostartState.UNKNOWN, "systemd unavailable")
    enabled = result.returncode == 0
    if content is None:
        return AutostartInspection(
            AutostartState.FOREIGN if enabled else AutostartState.ABSENT,
            "enabled unit has no owned artifact" if enabled else "",
        )
    marker = extract_marker(content, SupervisorKind.SYSTEMD_USER)
    if not _owned(marker):
        if b"jacked" in content and b"service start" in content:
            return AutostartInspection(AutostartState.LEGACY, "legacy systemd unit")
        return AutostartInspection(AutostartState.FOREIGN, "unowned systemd unit")
    return AutostartInspection(
        AutostartState.OWNED_ENABLED if enabled else AutostartState.OWNED_DISABLED
    )


def _owned(marker: dict | None) -> bool:
    return bool(
        marker
        and marker.get("owner") == "claude-jacked"
        and marker.get("service_id") == LAUNCHD_LABEL
    )


def _legacy_command(arguments: object) -> bool:
    return isinstance(arguments, list) and "service" in arguments and "start" in arguments


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _launchd_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / "jacked.service"


def _task_path() -> Path:
    return CLAUDE_DIR / "jacked-service-v2" / "supervisors" / "jacked-task.xml"


def _windows_vbs_path() -> Path:
    appdata = os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
    return (
        Path(appdata)
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "jacked.vbs"
    )
