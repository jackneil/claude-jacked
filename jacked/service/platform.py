"""Platform-specific auto-start install/uninstall for macOS and Windows."""

import os
import subprocess
import sys
from pathlib import Path

from jacked.service import CLAUDE_DIR, DEFAULT_HOST, DEFAULT_PORT, LAUNCHD_LABEL


def _get_launchd_plist_path() -> Path:
    """Return path to the launchd plist file."""
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _get_systemd_user_unit_path() -> Path:
    """Path to a potential user-managed systemd unit for jacked on Linux."""
    return Path.home() / ".config" / "systemd" / "user" / "jacked.service"


def native_restart() -> tuple[bool, str]:
    """Restart jacked via the platform's native lifecycle manager, if any.

    Returns (ok, reason):
      - (True, "...")  native restart initiated successfully; caller should
                       stop its own stop+start dance and let the manager
                       handle respawn.
      - (False, "...") no native manager available OR manager command failed;
                       caller should fall back to manual stop+start.

    Per-platform behavior:
      - macOS:   if ai.hank.jacked.plist is installed, `launchctl kickstart -k`.
      - Linux:   if user-installed systemd unit exists, `systemctl --user restart`.
      - Windows: always returns (False, ...) — the Startup-folder VBS doesn't
                 supervise lifecycle.

    Rationale: on macOS, launchd's `KeepAlive: SuccessfulExit=false` racing
    against a manual `service stop && service start` causes the "Port 8321
    is already in use" error users keep hitting. Delegation to kickstart is
    atomic.
    """
    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        if not plist_path.exists():
            return (False, "launchd plist not installed")
        uid = os.getuid()
        try:
            result = subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/{LAUNCHD_LABEL}"],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return (False, f"launchctl kickstart failed: {exc}")
        if result.returncode == 0:
            return (True, f"launchctl kickstart gui/{uid}/{LAUNCHD_LABEL}")
        return (
            False,
            f"launchctl exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}",
        )

    if sys.platform.startswith("linux"):
        unit_path = _get_systemd_user_unit_path()
        if not unit_path.exists():
            return (False, "no user systemd unit installed")
        try:
            result = subprocess.run(
                ["systemctl", "--user", "restart", "jacked"],
                capture_output=True, text=True, timeout=15,
            )
        except (subprocess.SubprocessError, OSError) as exc:
            return (False, f"systemctl restart failed: {exc}")
        if result.returncode == 0:
            return (True, "systemctl --user restart jacked")
        return (
            False,
            f"systemctl exit {result.returncode}: {result.stderr.strip() or result.stdout.strip()}",
        )

    # Windows: no supervising manager — VBS only fires on login.
    return (False, "no native lifecycle manager on this platform")


def ensure_native_lifecycle() -> tuple[bool, str, str]:
    """Ensure the platform's native lifecycle manager is configured.

    Returns a 3-tuple: (ok, state, reason).

    - (True, "already_installed", "..."):  plist/unit was present; caller
      should run native_restart() to atomically restart the job.
    - (True, "just_installed", "..."):     we just wrote the plist and
      launchd already started the service via RunAtLoad=true.  Caller
      should SKIP native_restart (the job is already fresh and running).
    - (False, "unavailable", reason):      no plist/unit and we can't
      create one (Linux systemd: user DIYs; Windows: no manager).
      Caller falls through to manual stop+start.

    macOS: auto-creates the plist via in-process call to install_autostart()
    — no subprocess shell-out.  Eliminates the class of bugs that bit us
    when 0.41.17 detection mis-classified and the ad-hoc "jacked install
    --tray" command didn't exist.

    Before creating the plist, if an ad-hoc jacked service is running on
    DEFAULT_PORT (held by some other user-initiated `jacked service start`),
    stop it first so launchctl load's RunAtLoad can bind the port cleanly.
    """
    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        if plist_path.exists():
            return (True, "already_installed", "launchd plist already installed")

        # Ad-hoc service may be holding :DEFAULT_PORT.  Stop it so the
        # RunAtLoad inside install_autostart can bind cleanly.
        from jacked.service import PID_FILE
        from jacked.service.process import stop_process_graceful
        stop_process_graceful(PID_FILE)

        status = install_autostart()
        if plist_path.exists():
            return (True, "just_installed", status)
        return (False, "unavailable", f"install_autostart did not produce plist: {status}")

    if sys.platform.startswith("linux"):
        unit_path = _get_systemd_user_unit_path()
        if unit_path.exists():
            return (True, "already_installed", "systemd user unit already installed")
        return (
            False, "unavailable",
            "no systemd user unit installed — create one manually to enable "
            "native restart.  See docs.",
        )

    return (False, "unavailable", "no native lifecycle manager on this platform")


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
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
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
    """Generate VBScript for the Windows startup folder.

    Prefer the GUI-subsystem ``pythonw.exe -m jacked`` (never touches a console)
    over the ``jacked.exe`` console trampoline — same reasoning as
    _spawn_service_detached. Window style 0 keeps it hidden either way.
    """
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.Run """{pythonw}"" -m jacked service start'
            f' --host {host} --port {port}", 0, False\n'
        )
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """{jacked_bin}"" service start'
        f' --host {host} --port {port}", 0, False\n'
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
    from jacked.findbin import find_bin

    jacked_bin = find_bin("jacked")
    if not jacked_bin:
        return "Could not find 'jacked' binary on PATH. Is it installed?"

    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        plist_content = _generate_launchd_plist(jacked_bin, host, port)
        plist_path.write_text(plist_content, encoding="utf-8")
        # Load now so the service starts immediately.
        # KeepAlive is scoped to SuccessfulExit=false, so clean stops
        # (user-initiated via tray/CLI) will NOT trigger respawn.
        # Only crashes will restart.
        subprocess.run(
            ["launchctl", "load", str(plist_path)],
            capture_output=True,
        )
        return (
            f"Installed and started launchd agent: {plist_path}\n"
            "  Service is running now and will auto-start on login."
        )

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
