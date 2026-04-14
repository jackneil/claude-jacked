"""Platform-specific auto-start install/uninstall for macOS and Windows."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from jacked.service import CLAUDE_DIR, DEFAULT_HOST, DEFAULT_PORT, LAUNCHD_LABEL


def _get_launchd_plist_path() -> Path:
    """Return path to the launchd plist file."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _get_windows_startup_path() -> Path:
    """Return path to the Windows startup VBS script."""
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


def _generate_launchd_plist(
    jacked_bin: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Generate launchd plist XML for macOS auto-start."""
    log_path = str(CLAUDE_DIR / "jacked-service.log")
    current_path = os.environ.get("PATH", "/usr/bin:/bin:/usr/local/bin")

    return f"""\
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{jacked_bin}</string>
        <string>service</string>
        <string>start</string>
        <string>--host</string>
        <string>{host}</string>
        <string>--port</string>
        <string>{port}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{current_path}</string>
    </dict>
</dict>
</plist>
"""


def _generate_windows_vbs(
    jacked_bin: str,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Generate VBScript for Windows startup folder."""
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """{jacked_bin}"" service start'
        f" --host {host} --port {port}\", 0, False\n"
    )


def detect_autostart() -> bool:
    """Check if auto-start is currently configured."""
    if sys.platform == "darwin":
        return _get_launchd_plist_path().exists()
    elif sys.platform == "win32":
        return _get_windows_startup_path().exists()
    return False


def install_autostart(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
) -> str:
    """Install platform auto-start configuration.

    Returns a human-readable status message.
    """
    jacked_bin = shutil.which("jacked")
    if not jacked_bin:
        return "Could not find 'jacked' binary on PATH. Is it installed?"

    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_content = _generate_launchd_plist(jacked_bin, host, port)
        plist_path.write_text(plist_content, encoding="utf-8")
        subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
        )
        return f"Installed launchd agent: {plist_path}"

    elif sys.platform == "win32":
        vbs_path = _get_windows_startup_path()
        vbs_path.parent.mkdir(parents=True, exist_ok=True)
        vbs_content = _generate_windows_vbs(jacked_bin, host, port)
        vbs_path.write_text(vbs_content, encoding="utf-8")
        return f"Installed startup script: {vbs_path}"

    else:
        return (
            "Auto-start not supported on this platform. "
            "Run `jacked service start` manually."
        )


def uninstall_autostart() -> str:
    """Remove platform auto-start configuration.

    Returns a human-readable status message.
    """
    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        if plist_path.exists():
            subprocess.run(
                ["launchctl", "unload", str(plist_path)],
                capture_output=True,
            )
            plist_path.unlink()
            return f"Removed launchd agent: {plist_path}"
        return "No launchd agent found — nothing to remove."

    elif sys.platform == "win32":
        vbs_path = _get_windows_startup_path()
        if vbs_path.exists():
            vbs_path.unlink()
            return f"Removed startup script: {vbs_path}"
        return "No startup script found — nothing to remove."

    else:
        return "Auto-start not supported on this platform."
