"""Platform-specific auto-start install/uninstall for macOS and Windows."""

import logging
import os
import subprocess
import sys
from pathlib import Path

from jacked.service import CLAUDE_DIR, DEFAULT_PORT, LAUNCHD_LABEL

logger = logging.getLogger(__name__)


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
    stop it first so the RunAtLoad fired by install_autostart's launchctl
    bootstrap can bind the port cleanly.
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
    port: int = DEFAULT_PORT,
) -> str:
    """Generate launchd plist XML for macOS auto-start.

    Deliberately host-free: the bind host is resolved from the settings DB at
    every boot (jacked/service/bind.py), so the GUI Remote access toggle
    survives reboots and upgrades instead of being pinned by a baked ``--host``.
    """
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
    port: int = DEFAULT_PORT,
) -> str:
    """Generate VBScript for the Windows startup folder.

    Prefer the GUI-subsystem ``pythonw.exe -m jacked`` (never touches a console)
    over the ``jacked.exe`` console trampoline — same reasoning as
    _spawn_service_detached. Window style 0 keeps it hidden either way.

    Deliberately host-free: the bind host is resolved from the settings DB at
    every boot (jacked/service/bind.py), so the GUI Remote access toggle
    survives reboots and upgrades instead of being pinned by a baked ``--host``.
    """
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if pythonw.exists():
        return (
            'Set WshShell = CreateObject("WScript.Shell")\n'
            f'WshShell.Run """{pythonw}"" -m jacked service start'
            f' --port {port}", 0, False\n'
        )
    return (
        'Set WshShell = CreateObject("WScript.Shell")\n'
        f'WshShell.Run """{jacked_bin}"" service start'
        f' --port {port}", 0, False\n'
    )


def detect_autostart() -> bool:
    """Check if auto-start is currently configured."""
    if sys.platform == "darwin":
        return _get_launchd_plist_path().exists()
    elif sys.platform == "win32":
        return _get_windows_startup_path().exists()
    return False


def _service_is_running() -> bool:
    """True when the PID-file process is alive (the live jacked service)."""
    from jacked.service import PID_FILE
    from jacked.service.process import is_process_alive, read_pid

    info = read_pid(PID_FILE)
    return bool(info) and is_process_alive(info["pid"])


def _migrate_artifact_host(path: Path, kind: str) -> None:
    """Capture a baked ``--host`` from an existing artifact into the DB.

    Data-preserving: ``jacked install --force`` (which every upgrade runs)
    regenerates the artifact host-free, so a user who once ran ``service
    install --host 0.0.0.0`` must have that intent copied into the settings DB
    BEFORE the artifact is overwritten. Guarded inside
    ``migrate_baked_host_to_db``: an existing GUI choice is never clobbered.
    Never raises — migration is bookkeeping, not a gate.
    """
    from jacked.service.migrate import extract_baked_host, migrate_baked_host_to_db

    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    host = extract_baked_host(text, kind)
    if host is None:
        return
    logger.info(
        "Autostart artifact migration (%s): %s", kind, migrate_baked_host_to_db(host)
    )


def install_autostart(
    port: int = DEFAULT_PORT,
) -> str:
    """Install platform auto-start configuration.

    Artifacts are generated host-free: the bind host lives in the settings DB
    and is resolved at every service boot, so the GUI Remote access toggle
    survives reboots and upgrades. Any baked ``--host`` found in a pre-existing
    artifact is migrated into the DB first (never clobbering an existing GUI
    choice).

    Returns a human-readable status message.
    """
    from jacked.findbin import find_bin

    jacked_bin = find_bin("jacked")
    if not jacked_bin:
        return "Could not find 'jacked' binary on PATH. Is it installed?"

    if sys.platform == "darwin":
        plist_path = _get_launchd_plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        if plist_path.exists():
            _migrate_artifact_host(plist_path, "plist")
        plist_content = _generate_launchd_plist(jacked_bin, port)
        plist_path.write_text(plist_content, encoding="utf-8")

        if _service_is_running():
            # launchd keeps the LOADED job definition in memory: rewriting the
            # plist over a loaded label changes nothing until the label is
            # bootstrapped again, and booting it out now would KILL the running
            # service. `jacked install --force` runs inside BOTH upgrade paths
            # (web _do_upgrade and the tray updater), so this branch must never
            # interrupt the live service. File-only: the refreshed definition
            # takes effect at the next login/reboot or explicit bootstrap.
            logger.info(
                "launchd agent file refreshed while the service is running; "
                "the new definition takes effect at next login/reboot"
            )
            return (
                f"Updated launchd agent: {plist_path}\n"
                "  Service is already running; the refreshed definition takes "
                "effect at next login or reboot."
            )

        # Service is NOT running: boot out any stale loaded definition, then
        # bootstrap the fresh plist so it takes effect immediately. This
        # replaces the old blind `launchctl load`, which silently kept the old
        # in-memory definition when the label was already loaded.
        # KeepAlive is scoped to SuccessfulExit=false, so clean stops
        # (user-initiated via tray/CLI) will NOT trigger respawn.
        # Only crashes will restart.
        uid = os.getuid()
        bootout = subprocess.run(
            ["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True, text=True,
        )
        if bootout.returncode != 0:
            # "Boot-out failed: ... not loaded" is the normal case when the
            # label was never loaded — tolerate and proceed to bootstrap.
            logger.info(
                "launchctl bootout (pre-bootstrap) exit %d: %s",
                bootout.returncode,
                (bootout.stderr or bootout.stdout or "").strip(),
            )
        bootstrap = subprocess.run(
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)],
            capture_output=True, text=True,
        )
        if bootstrap.returncode != 0:
            err = (bootstrap.stderr or bootstrap.stdout or "").strip()
            logger.warning("launchctl bootstrap exit %d: %s", bootstrap.returncode, err)
            # Do NOT claim "running now" — the job never loaded, RunAtLoad never
            # fired, and the bind the user just set did not take effect. Report
            # the truth so the CLI/caller does not print a false success.
            return (
                f"Installed launchd agent: {plist_path}\n"
                f"  WARNING: it did not start (launchctl bootstrap exit "
                f"{bootstrap.returncode}: {err or 'no output'}). "
                "It will start at next login/reboot, or run "
                "`jacked service start` to start it now."
            )
        return (
            f"Installed and started launchd agent: {plist_path}\n"
            "  Service is running now and will auto-start on login."
        )

    elif sys.platform == "win32":
        vbs_path = _get_windows_startup_path()
        vbs_path.parent.mkdir(parents=True, exist_ok=True)
        if vbs_path.exists():
            # Startup-folder script only fires at next login — nothing is
            # loaded/cached, so migrate-then-rewrite unconditionally.
            _migrate_artifact_host(vbs_path, "vbs")
        vbs_content = _generate_windows_vbs(jacked_bin, port)
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
