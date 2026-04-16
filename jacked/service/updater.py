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
from jacked.service.process import is_process_alive

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
        if not wait_for_exit(parent_pid, timeout=30.0):
            log(f"Parent {parent_pid} still alive after 30s - continuing anyway")

        uv = find_bin("uv")
        if not uv:
            msg = "Could not find `uv` on PATH. Install uv from https://docs.astral.sh/uv/"
            log(f"ERROR: {msg}")
            _write_recovery(
                f"Jacked auto-update failed:\n{msg}\n\n"
                "Manual recovery:\n"
                f"  uv tool install 'claude-jacked[{extras}]' --force\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return

        log(f"Running: {uv} tool install claude-jacked[{extras}] --force")
        result = subprocess.run(
            [uv, "tool", "install", f"claude-jacked[{extras}]", "--force"],
            stdout=log_fh, stderr=log_fh, check=False,
        )
        log(f"uv install returncode: {result.returncode}")

        if result.returncode != 0:
            _write_recovery(
                f"Jacked auto-update failed: `uv tool install` returned {result.returncode}.\n"
                f"See {UPDATE_LOG} for details.\n\n"
                "Manual recovery:\n"
                f"  uv tool install 'claude-jacked[{extras}]' --force\n"
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

        log(f"Restarting service: {jacked} service start")
        _spawn_detached([jacked, "service", "start"], log_fh=log_fh)
        log("Updater done")

        if RECOVERY_FILE.exists():
            try:
                RECOVERY_FILE.unlink()
            except Exception:
                pass
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
    """Called by the tray on update click. Spawns the detached helper."""
    py = _find_updater_python()
    if not py:
        raise SystemExit("No Python executable found for updater spawn")

    _spawn_detached(
        [py, "-m", "jacked.service.updater", str(parent_pid), extras],
        log_fh=None,
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
