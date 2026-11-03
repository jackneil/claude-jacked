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
    """Compatibility adapter for exact-generation native restart."""
    from jacked.service.lifecycle import (
        native_artifact_path,
        provision_service_contract,
        restart_native_owned,
    )

    try:
        spec, environment = provision_service_contract()
        result = restart_native_owned(
            spec, native_artifact_path(spec), environment=environment
        )
    except (OSError, ValueError) as exc:
        return False, type(exc).__name__
    return result.ok, result.reason


def ensure_native_lifecycle() -> tuple[bool, str, str]:
    """Compatibility adapter for exact ServiceSpec reconciliation/activation."""
    from jacked.service.lifecycle import (
        install_native_owned,
        provision_service_contract,
    )

    try:
        spec, environment = provision_service_contract()
        result = install_native_owned(spec, environment=environment)
    except (OSError, ValueError) as exc:
        return False, "unavailable", type(exc).__name__
    return result.ok, ("just_installed" if result.ok else "unavailable"), result.reason


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
    """Compatibility boolean backed by native-manager inspection."""
    return inspect_autostart().enabled


def inspect_autostart(run=None):
    """Return typed native autostart state for safe UI and CLI decisions.

    ``run`` replaces ``subprocess.run`` for the native manager queries.
    """
    from jacked.service.autostart import inspect_autostart as inspect

    task = CLAUDE_DIR / "jacked-service-v2" / "supervisors" / "jacked-task.xml"
    return inspect(
        run=run,
        launchd_path=_get_launchd_plist_path(),
        task_path=task,
        legacy_vbs_path=_get_windows_startup_path(),
        systemd_path=_get_systemd_user_unit_path(),
    )


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
            capture_output=True,
            text=True,
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
            capture_output=True,
            text=True,
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
        return "No launchd agent found. Nothing to remove."

    elif sys.platform == "win32":
        vbs_path = _get_windows_startup_path()
        if vbs_path.exists():
            vbs_path.unlink()
            return f"Removed startup script: {vbs_path}"
        return "No startup script found. Nothing to remove."

    else:
        return "Auto-start not supported on this platform."


# The reason that supervision_drift gives when launchd does not have the
# owned job loaded. It is the only reason that needs no live service.
LAUNCHD_JOB_NOT_LOADED = "launchd job not loaded"


class _NotGiven:
    """Marks a ``probe=`` argument that the caller did not give."""

    def __repr__(self) -> str:
        return "<not given>"


_PROBE_NOT_GIVEN = _NotGiven()


def launchd_job_probe(run=None, uid: "int | None" = None):
    """Ask launchd for the jacked job in the GUI domain of the user.

    Returns the completed ``launchctl print`` call, or None when launchd
    cannot be asked: there is no ``os.getuid`` (Windows), launchctl does not
    start, or it times out. A launchctl call that cannot run logs one
    WARNING with the exception class name, because the caller then skips a
    check. Exit 113 ("Could not find service") is an answer: launchd does
    not have the job loaded. Any other non-zero exit is no answer.

    ``supervision_drift`` and ``launchd_job_never_ran`` ask the same
    question. A caller that runs both (``jacked service status``, ``jacked
    doctor``) calls this once and gives the result to both as ``probe=``,
    so launchctl runs once and a failure logs one WARNING. ``run`` replaces
    ``subprocess.run`` and is resolved at call time, so the test tripwire on
    ``subprocess.run`` still applies.
    """
    if uid is None:
        getuid = getattr(os, "getuid", None)  # absent on Windows
        if getuid is None:
            return None
        uid = getuid()
    runner = subprocess.run if run is None else run
    try:
        return runner(
            ["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # The class name only: the text of an OSError can carry a path.
        logger.warning(
            "launchctl could not be asked about the jacked job (%s); "
            "autostart supervision was not checked",
            type(exc).__name__,
        )
        return None


def launchd_job_never_ran(
    run=None, uid: "int | None" = None, *, probe=_PROBE_NOT_GIVEN
) -> bool:
    """True when the launchd job is loaded but launchd never spawned it.

    Seen 2026-10-22 after an interrupted upgrade: `launchctl print` showed
    ``state = not running`` and ``runs = 0`` for a job loaded with RunAtLoad,
    while `jacked service status` said "Autostart: enabled" and nothing was
    going to start it (#178i). Only the job's top-level fields count (one tab
    deep; nested dictionaries carry their own ``state``). Never raises; any
    other platform or a probe failure is False. ``probe`` is a result of
    ``launchd_job_probe`` that the caller already has (None when launchctl
    could not be asked); when it is given, this asks launchctl nothing.

    >>> class _R:
    ...     returncode = 0
    ...     stdout = "gui/501/x = {\\n\\tstate = not running\\n\\truns = 0\\n\\t\\tstate = active\\n}"
    >>> launchd_job_never_ran(run=lambda *a, **k: _R(), uid=501) if sys.platform == "darwin" else True
    True
    """
    if sys.platform != "darwin":
        return False
    import re

    if probe is _PROBE_NOT_GIVEN:
        probe = launchd_job_probe(run, uid)
    if probe is None or probe.returncode != 0:
        return False
    fields = dict(re.findall(r"^\t(state|runs) = (.+)$", probe.stdout or "", re.MULTILINE))
    return fields.get("runs", "").strip() == "0" and fields.get("state", "").strip() != "running"


def supervision_drift(
    run=None,
    uid: "int | None" = None,
    paths=None,
    *,
    inspection=None,
    probe=_PROBE_NOT_GIVEN,
) -> "str | None":
    """Return why the enabled autostart does not supervise the running service.

    `jacked service status` said "Autostart: enabled" when an owned
    definition existed. After one upgrade, launchd did not have the job
    loaded, the plist named the previous generation, and a manual service
    ran in its place. Nothing restarted that service after a crash, and at
    the next login launchd would start the previous generation (#128i).
    This probe compares the definition with the running
    service and returns the first mismatch as a short reason. It checks in
    this order:

    1. The definition must be owned and enabled. If it is not, there is
       nothing to supervise, and the result is None.
    2. macOS only: launchd does not have the owned job loaded
       (``launchctl print`` exits 113), reason ``LAUNCHD_JOB_NOT_LOADED``.
       This check needs no running service: until launchd loads the plist
       again at the next login, nothing starts the service and nothing
       restarts it after a crash. Any other non-zero exit is no answer and
       gives one DEBUG line. A launchctl call that cannot run gives one
       WARNING. In both cases the probe continues with the checks below.
    3. A manual supervisor started the running service.
    4. The definition on disk records a different generation.

    Checks 3 and 4 need a live service. A manifest that is missing,
    unreadable or names a dead process gives None there. The result is
    also None when the definition supervises the running service.
    ``paths`` is the ``ServicePaths`` of the running service (default:
    ``default_service_paths()``). ``inspection`` is the
    ``inspect_autostart()`` result of the caller, if it has one: on Windows
    each query of the native manager is a PowerShell COM call. ``probe`` is
    a ``launchd_job_probe`` result that the caller already has (None when
    launchctl could not be asked); when it is given, this asks launchctl
    nothing. ``run`` replaces ``subprocess.run`` and is resolved at call
    time.
    """
    from jacked.service.autostart import AutostartState
    from jacked.service.instance import process_is_stale, read_manifest
    from jacked.service.spec import SupervisorKind
    from jacked.service.supervisors.launchd import LAUNCHCTL_NOT_FOUND

    if inspection is None:
        inspection = inspect_autostart(run=run)
    if inspection.state is not AutostartState.OWNED_ENABLED:
        return None
    if sys.platform == "darwin":
        if probe is _PROBE_NOT_GIVEN:
            probe = launchd_job_probe(run, uid)
        if probe is not None and probe.returncode == LAUNCHCTL_NOT_FOUND:
            return LAUNCHD_JOB_NOT_LOADED
        if probe is not None and probe.returncode != 0:
            logger.debug(
                "launchctl print exited %d; the loaded state of the jacked "
                "job is unknown",
                probe.returncode,
            )
    if paths is None:
        from jacked.service.lifecycle import default_service_paths

        paths = default_service_paths()
    try:
        running = read_manifest(paths.manifest)
    except (OSError, ValueError):
        return None
    # The shared staleness rule (manifest_is_proven_stale), applied to the
    # manifest that was just read instead of to a second read of the file.
    if process_is_stale(running.process):
        return None
    if running.supervisor == SupervisorKind.MANUAL.value:
        return "the running service is under a manual supervisor"
    on_disk = inspection.generation
    if on_disk is not None and on_disk != running.generation:
        return f"generation {on_disk[:12]} on disk, {running.generation[:12]} running"
    return None


def drift_consequence(drift: str, *, service_live: bool) -> str:
    """Return the words that lead a drift reason in status and doctor.

    ``service_live`` is the verdict that the caller printed: True when it
    found a live service. Only ``LAUNCHD_JOB_NOT_LOADED`` can describe a
    stopped service. The other reasons come from a live manifest, so they
    always read as "not supervising the running service", even when the
    caller's own check missed that service (doctor probes port 8321 only).

    >>> drift_consequence(LAUNCHD_JOB_NOT_LOADED, service_live=True)
    'not supervising the running service'
    >>> drift_consequence(LAUNCHD_JOB_NOT_LOADED, service_live=False)
    'will not start the service until the next login'
    >>> drift_consequence(
    ...     "the running service is under a manual supervisor", service_live=False)
    'not supervising the running service'
    """
    if service_live or drift != LAUNCHD_JOB_NOT_LOADED:
        return "not supervising the running service"
    return "will not start the service until the next login"
