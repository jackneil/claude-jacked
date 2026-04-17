"""Detached auto-updater.

Run via `python -m jacked.service.updater <parent_pid> [extras]`.
Waits for the parent tray to exit, runs `uv tool install --force`,
migrates settings.json via `jacked install`, then spawns a fresh
`jacked service start`.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from jacked.findbin import find_bin
from jacked.service import CLAUDE_DIR
from jacked.service.process import is_process_alive, is_port_available

UPDATE_LOG = CLAUDE_DIR / "jacked-update.log"
RECOVERY_FILE = CLAUDE_DIR / "jacked-update-failed.txt"

logger = logging.getLogger(__name__)


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    """Poll until process exits or timeout. Returns True if exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.5)
    return False


def _force_kill_pid(pid: int) -> None:
    """SIGKILL the PID if it's still alive. No exceptions leak out.

    pystray's AppKit runloop on macOS can swallow Python signals, so the
    tray's own `icon.stop()` call may not actually end the process. Without
    this we'd waste 30+ seconds of wait_for_exit and still hit "port in use"
    on the detached start.
    """
    if pid <= 0 or not is_process_alive(pid):
        return
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/F", "/T"],
                capture_output=True, check=False,
            )
        else:
            import signal as _signal
            os.kill(pid, _signal.SIGKILL)
    except Exception:
        logger.exception("force-kill of PID %d failed", pid)


def _pids_bound_to_port(port: int) -> list[int]:
    """Return PIDs holding a LISTEN socket on *port*. Uses lsof on POSIX,
    netstat on Windows. Best-effort — returns [] on any failure."""
    try:
        if sys.platform == "win32":
            out = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, check=False,
            ).stdout
            pids: set[int] = set()
            for line in out.splitlines():
                if f":{port}" in line and "LISTENING" in line.upper():
                    parts = line.split()
                    try:
                        pids.add(int(parts[-1]))
                    except (ValueError, IndexError):
                        pass
            return list(pids)
        out = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", f"-iTCP:{port}"],
            capture_output=True, text=True, check=False,
        ).stdout
        pids = set()
        for line in out.splitlines()[1:]:  # skip header
            parts = line.split()
            if len(parts) >= 2:
                try:
                    pids.add(int(parts[1]))
                except ValueError:
                    pass
        return list(pids)
    except Exception:
        return []


def _spawn_detached(cmd: list, log_fh=None) -> "subprocess.Popen":
    """Spawn a subprocess that survives this process dying."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh if log_fh is not None else subprocess.DEVNULL,
        "stderr": log_fh if log_fh is not None else subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _write_recovery(message: str) -> None:
    """Write a human-readable recovery file so the user sees what broke."""
    try:
        RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_FILE.write_text(message)
    except Exception:
        logger.exception("Could not write recovery file")


def run_update(parent_pid: int, extras: str = "tray") -> None:
    """Main update sequence. Called in the detached helper process."""
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(UPDATE_LOG, "a", buffering=1, encoding="utf-8", errors="replace")

    def log(msg: str) -> None:
        log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    try:
        log(f"Waiting for parent PID {parent_pid} to exit")
        if not wait_for_exit(parent_pid, timeout=15.0):
            # Tray is still alive — probably pystray's AppKit runloop didn't
            # react to icon.stop(). Force-kill so we can reclaim the port.
            log(f"Parent {parent_pid} still alive after 15s — SIGKILL")
            _force_kill_pid(parent_pid)
            if not wait_for_exit(parent_pid, timeout=5.0):
                log(f"Parent {parent_pid} still alive after SIGKILL — continuing anyway")

        from jacked.install_method import (
            detect_install_method,
            upgrade_command,
            upgrade_command_label,
        )
        method = detect_install_method()
        cmd = upgrade_command(extras)
        label = upgrade_command_label(extras)

        if method == "uv":
            uv = find_bin("uv")
            if not uv:
                msg = "Could not find `uv` on PATH. Install uv from https://docs.astral.sh/uv/"
                log(f"ERROR: {msg}")
                _write_recovery(
                    f"Jacked auto-update failed:\n{msg}\n\n"
                    "Manual recovery:\n"
                    f"  {label}\n"
                    "  jacked install --force\n"
                    "  jacked service start\n"
                )
                return
            cmd[0] = uv

        log(f"Install method: {method}")
        log(f"Running: {label}")
        result = subprocess.run(cmd, stdout=log_fh, stderr=log_fh, check=False)
        log(f"upgrade command returncode: {result.returncode}")

        if result.returncode != 0:
            _write_recovery(
                f"Jacked auto-update failed: upgrade command returned {result.returncode}.\n"
                f"See {UPDATE_LOG} for details.\n\n"
                "Manual recovery:\n"
                f"  {label}\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return

        jacked = find_bin("jacked")
        if not jacked:
            log("Could not locate jacked after install - NOT restarting")
            _write_recovery(
                "Jacked auto-update: install succeeded but the `jacked` binary "
                "is no longer on PATH. Run manually:\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return

        log(f"Running: {jacked} install --force")
        migrate_result = subprocess.run(
            [jacked, "install", "--force"],
            stdout=log_fh, stderr=log_fh, check=False,
        )
        log(f"jacked install returncode: {migrate_result.returncode}")

        if migrate_result.returncode != 0:
            _write_recovery(
                f"Jacked auto-update: package upgrade succeeded but "
                f"`jacked install --force` returned {migrate_result.returncode}.\n"
                f"settings.json may be in a partial state. A backup was saved "
                f"at ~/.claude/settings.json.bak-*.\n"
                f"See {UPDATE_LOG} for details.\n\n"
                "Recovery:\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            log("NOT restarting service — jacked install failed")
            return

        # Wait for the old tray's port to actually release before starting
        # the replacement. wait_for_exit above only confirms the PID is gone;
        # on POSIX the kernel reclaims the socket almost immediately, but
        # uvicorn's graceful shutdown path can leave a brief window where
        # bind fails. Polling here avoids the spawned service hitting
        # "Port 8321 is already in use" and exiting silently.
        log("Waiting for port to become available")
        port_deadline = time.monotonic() + 10.0
        while time.monotonic() < port_deadline:
            if is_port_available("127.0.0.1", 8321):
                break
            time.sleep(0.5)
        if not is_port_available("127.0.0.1", 8321):
            # Port is stuck. Find who's holding it and force-kill.
            squatters = _pids_bound_to_port(8321)
            if squatters:
                log(f"Port 8321 still bound by PIDs {squatters} — SIGKILL")
                for pid in squatters:
                    _force_kill_pid(pid)
                # Brief grace for the kernel to release the socket.
                kill_deadline = time.monotonic() + 3.0
                while time.monotonic() < kill_deadline:
                    if is_port_available("127.0.0.1", 8321):
                        break
                    time.sleep(0.2)
            if not is_port_available("127.0.0.1", 8321):
                _write_recovery(
                    "Jacked auto-update: port 8321 could not be freed after "
                    "the update. Some other process is holding it.\n\n"
                    "Check with: lsof -iTCP:8321 -sTCP:LISTEN   (macOS/Linux)\n"
                    "            netstat -ano | findstr :8321   (Windows)\n\n"
                    "Recovery:\n"
                    "  kill -9 <PID>  (or taskkill /PID <PID> /F on Windows)\n"
                    "  jacked service start\n"
                )
                log("ABORT: port 8321 could not be freed — see recovery file")
                return

        log(f"Restarting service: {jacked} service start")
        _spawn_detached([jacked, "service", "start"], log_fh=log_fh)

        # Verify the new service actually came up. Without this we silently
        # claim success when the child died (bad PATH, missing extras, etc.).
        log("Verifying new service came up")
        verify_deadline = time.monotonic() + 20.0
        came_up = False
        while time.monotonic() < verify_deadline:
            if not is_port_available("127.0.0.1", 8321):
                came_up = True
                break
            time.sleep(0.5)

        if came_up:
            log("Updater done — new service is listening on :8321")
            if RECOVERY_FILE.exists():
                try:
                    RECOVERY_FILE.unlink()
                except Exception:
                    pass
        else:
            log("WARNING: new service did not bind :8321 within 20s")
            _write_recovery(
                "Jacked auto-update: package install + migrate succeeded, but "
                "the new tray never came up.\n\n"
                f"See {UPDATE_LOG} for details. Most likely uv installed into\n"
                "an unexpected PATH location.\n\n"
                "Recovery:\n"
                "  jacked service start\n"
                "If that fails:\n"
                "  uv tool install 'claude-jacked[tray]' --force\n"
                "  jacked service start\n"
            )
    finally:
        log_fh.close()


def _find_updater_python() -> str | None:
    """Pick the Python to run the detached updater helper.

    Must be an interpreter that can `import jacked.service.updater` —
    which means the tool venv's Python (only interpreter with jacked
    installed). An earlier version tried a "system Python" to avoid
    being clobbered by `uv tool install --force`, but system Python
    has no jacked module on sys.path, so the helper can't even start.

    POSIX: `uv tool install --force` can atomically replace the venv
    while the running interpreter stays valid via its open file
    descriptor. All imports we need are already resolved before install.

    Windows: python.exe file locks can block uv. uv now hardlinks and
    retries, and the helper loads all modules before kicking off the
    install, so no fresh imports are needed during the replace window.
    """
    return sys.executable


def spawn_updater_from_tray(parent_pid: int, extras: str = "tray") -> None:
    """Called by the tray on update click. Spawns the detached helper.

    POSIX: detached Python subprocess running this module. `uv tool install`
    can atomically replace the venv while our interpreter stays valid via
    its open file descriptor on the binary.

    Windows: Python subprocess doesn't work — `uv tool install --force`
    can't replace python.exe while this interpreter is using it (classic
    Windows exclusive-file-lock). Instead spawn a detached cmd.exe batch
    that:
      1. Waits for the tray PID to exit.
      2. Runs `uv tool install --force`.
      3. Runs `jacked install --force`.
      4. Runs `jacked service start` (detached).
    cmd.exe is a system binary we don't own, so it survives whatever uv
    does to the jacked venv. Same trick as `jacked upgrade` on Windows.
    """
    if sys.platform == "win32":
        _spawn_windows_tray_updater(parent_pid, extras)
        return

    py = _find_updater_python()
    if not py:
        raise SystemExit("No Python executable found for updater spawn")

    _spawn_detached(
        [py, "-m", "jacked.service.updater", str(parent_pid), extras],
        log_fh=None,
    )


def _spawn_windows_tray_updater(parent_pid: int, extras: str) -> None:
    """Spawn a detached cmd.exe batch that does the full Windows update.

    Uses the same install-method detection as `jacked upgrade`, so a user
    who installed via `pip install --user claude-jacked` gets upgraded via
    `python -m pip install --upgrade --user`, not `uv tool install` (which
    would install the package a second time in a different location).
    """
    import os
    import tempfile

    from jacked.findbin import find_bin
    from jacked.install_method import (
        detect_install_method,
        upgrade_command,
        upgrade_command_label,
    )

    method = detect_install_method()
    cmd = upgrade_command(extras)

    # If uv flow, resolve uv's absolute path so cmd.exe doesn't depend on
    # PATH resolution working the same inside a detached batch.
    if method == "uv":
        resolved_uv = find_bin("uv")
        if resolved_uv:
            cmd[0] = resolved_uv

    label = upgrade_command_label(extras)
    upgrade_line = " ".join(f'"{arg}"' for arg in cmd)

    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_path = str(UPDATE_LOG)

    batch_body = (
        '@echo off\r\n'
        'set LOGFILE=' + log_path + '\r\n'
        'echo [%date% %time%] tray update helper starting (parent PID ' + str(parent_pid) + ', method ' + method + ') >> "%LOGFILE%"\r\n'
        'echo [%date% %time%] upgrade command: ' + label + ' >> "%LOGFILE%"\r\n'
        ':wait\r\n'
        'tasklist /FI "PID eq ' + str(parent_pid) + '" 2>NUL | find "' + str(parent_pid) + '" >NUL\r\n'
        'if not errorlevel 1 (\r\n'
        '    timeout /t 1 /nobreak >NUL\r\n'
        '    goto wait\r\n'
        ')\r\n'
        'echo [%date% %time%] parent exited >> "%LOGFILE%"\r\n'
        + upgrade_line + ' >> "%LOGFILE%" 2>&1\r\n'
        'if errorlevel 1 (\r\n'
        '    echo [%date% %time%] ERROR: upgrade command failed >> "%LOGFILE%"\r\n'
        '    echo Jacked tray update failed. See %LOGFILE%. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    echo Recovery: ' + label + ' ^&^& jacked install --force >> "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        '    exit /b 1\r\n'
        ')\r\n'
        'echo [%date% %time%] running jacked install --force >> "%LOGFILE%"\r\n'
        'jacked install --force >> "%LOGFILE%" 2>&1\r\n'
        'echo [%date% %time%] starting service (detached) >> "%LOGFILE%"\r\n'
        'start "" /B jacked service start >> "%LOGFILE%" 2>&1\r\n'
        'echo [%date% %time%] tray update complete >> "%LOGFILE%"\r\n'
        '(goto) 2>nul & del "%~f0"\r\n'
    )

    fd, batch_path = tempfile.mkstemp(suffix=".bat", prefix="jacked-tray-update-")
    try:
        with os.fdopen(fd, "w", newline="\r\n") as f:
            f.write(batch_body)
    except Exception:
        try:
            os.unlink(batch_path)
        except OSError:
            pass
        raise

    DETACHED_PROCESS = getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
    subprocess.Popen(
        ["cmd.exe", "/c", batch_path],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=DETACHED_PROCESS,
        close_fds=True,
    )


def _cli() -> None:
    """Entry point for `python -m jacked.service.updater <pid> [extras]`."""
    if len(sys.argv) < 2:
        sys.stderr.write("Usage: python -m jacked.service.updater <parent_pid> [extras]\n")
        sys.exit(2)
    try:
        pid = int(sys.argv[1])
    except ValueError:
        sys.stderr.write(f"Invalid PID: {sys.argv[1]}\n")
        sys.exit(2)
    extras = sys.argv[2] if len(sys.argv) >= 3 else "tray"
    run_update(pid, extras)


if __name__ == "__main__":
    _cli()
