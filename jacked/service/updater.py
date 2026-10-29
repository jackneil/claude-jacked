"""Detached auto-updater.

Run via `python -m jacked.service.updater <parent_pid> [extras]`.
Waits for the parent tray to exit, runs `uv tool install --force`,
migrates settings.json via `jacked install`, then spawns a fresh
`jacked service start`.
"""

from __future__ import annotations

import http.client
import json
import logging
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import NamedTuple

from jacked.findbin import find_bin
from jacked.service import CLAUDE_DIR
from jacked.service.process import is_process_alive, is_port_listening
from jacked.winproc import NO_WINDOW

UPDATE_LOG = CLAUDE_DIR / "jacked-update.log"
RECOVERY_FILE = CLAUDE_DIR / "jacked-update-failed.txt"

# Rollback step names. A partial restoration must say WHICH step failed, so the
# names reach the update status, the recovery file and the Windows batch.
ROLLBACK_STEP_PACKAGE = "package rollback"
ROLLBACK_STEP_INSTALL = "settings migration"
ROLLBACK_STEP_RESTART = "service restart"

# How long the preflight gate may run before it counts as a refusal. A build
# that cannot answer in two minutes cannot be trusted with the old service.
PREFLIGHT_TIMEOUT_SECONDS = 120

# How long one identification probe may take. The listener is on loopback and
# /api/health is a pure local handler, so a jacked service answers in
# milliseconds; anything slower is, for this decision, a stranger.
IDENTIFY_TIMEOUT_SECONDS = 2.0

# How long the old listener has to release the port before it has to identify
# itself instead.
PORT_FREE_TIMEOUT_SECONDS = 10.0

# How long the restarted service has to answer as the expected build.
VERIFY_TIMEOUT_SECONDS = 20.0

# Ceiling on ONE version read. It has to be generous - /api/version calls PyPI
# on a cold cache, up to ~6s - but never the whole verify window: a listener
# that accepts TCP and then says nothing would eat the window and leave the
# health fingerprint no budget, which then read as "nothing ever bound" for a
# port that was demonstrably bound.
VERSION_READ_TIMEOUT_SECONDS = 8.0

# Gap between polls in both waits.
POLL_INTERVAL_SECONDS = 0.5

# Ceiling on a probe body. A jacked health answer is a few dozen bytes; the
# cap stops a hostile listener streaming a body at the updater.
_PROBE_BODY_LIMIT = 4_096


def _health_fingerprint(body: bytes) -> "tuple[bool, str]":
    """Judge a /api/health body. Exactly jacked's shape, or it is a stranger."""
    if len(body) > _PROBE_BODY_LIMIT:
        return False, "/api/health answered a body too large to be jacked's"
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return False, "/api/health did not answer JSON"
    if (
        isinstance(payload, dict)
        and set(payload) == {"status", "db"}
        and payload["status"] == "ok"
        and isinstance(payload["db"], bool)
    ):
        return True, "answered /api/health as a jacked service"
    return False, "/api/health answered a shape that is not jacked's"


# The three answers of :func:`classify_jacked_listener`.
LISTENER_JACKED = "jacked"
LISTENER_FOREIGN = "foreign"
LISTENER_UNKNOWN = "unknown"

# Errors that come from a listener that refuses or drops an HTTP request. A
# jacked service always speaks HTTP, so these listeners are not jacked.
_STRANGER_ERRORS = (
    ConnectionRefusedError,
    ConnectionResetError,
    ConnectionAbortedError,
    http.client.BadStatusLine,
)


def classify_jacked_listener(port: int, budget: float) -> "tuple[str, str]":
    """Ask /api/health which kind of listener is on *port*.

    Return ``(verdict, reason)``. The verdict is one of these values:

    * ``LISTENER_JACKED``: the body has the exact jacked health shape.
    * ``LISTENER_FOREIGN``: the listener is clearly a different program. It
      sent an HTTP error status, a body that is not JSON, a body with the
      wrong shape, or it refused or reset the connection.
    * ``LISTENER_UNKNOWN``: the probe proves nothing. For example, the
      listener did not answer in the time limit.

    The reason names the failure class and contains no body text. The
    function never raises.

    >>> classify_jacked_listener(8321, 0)
    ('unknown', 'no time left to identify the listener')
    """
    if budget <= 0:
        return LISTENER_UNKNOWN, "no time left to identify the listener"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(
            f"http://127.0.0.1:{port}/api/health", timeout=budget
        ) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                return LISTENER_FOREIGN, f"/api/health answered HTTP {status}"
            body = response.read(_PROBE_BODY_LIMIT + 1)
    except urllib.error.HTTPError as exc:
        return LISTENER_FOREIGN, f"/api/health answered HTTP {exc.code}"
    except urllib.error.URLError as exc:
        stranger = isinstance(exc.reason, _STRANGER_ERRORS)
        verdict = LISTENER_FOREIGN if stranger else LISTENER_UNKNOWN
        return verdict, f"/api/health unreachable ({exc.reason})"
    except TimeoutError:
        return LISTENER_UNKNOWN, f"/api/health did not answer within {budget:.1f}s"
    except _STRANGER_ERRORS as exc:
        return LISTENER_FOREIGN, f"/api/health dropped the request ({type(exc).__name__})"
    except Exception as exc:  # noqa: BLE001 - an unreadable listener is the answer
        return LISTENER_UNKNOWN, f"/api/health probe failed ({type(exc).__name__})"

    is_jacked, reason = _health_fingerprint(body)
    return (LISTENER_JACKED if is_jacked else LISTENER_FOREIGN), reason


def _identify_jacked_listener(port: int, budget: float) -> "tuple[bool, str]":
    """Ask whether the listener on *port* is a jacked service.

    Deliberately /api/health, NOT /api/version: the version route calls PyPI
    on a cold cache (two fetches, 3s each), so a perfectly healthy jacked
    service would miss this budget and read as a stranger - the exact misread
    this probe exists to prevent. Health is a pure local handler answering
    exactly ``{"status": "ok", "db": <bool>}``, the same fingerprint
    ``service.legacy.probe_legacy_health`` pins, and the body read is bounded
    the same way so a hostile listener cannot stream at us.

    Proxy handlers are stripped: an ``http_proxy`` in the environment must
    never route a 127.0.0.1 probe through someone else's server.

    Returns ``(is_jacked, reason)``; the reason names the failure class so a
    refused update can say WHY the port holder was not recognised. Never
    raises. An unknown verdict counts as "not jacked" here: for the squatter
    decision, a listener that cannot prove it is jacked is a stranger.
    """
    verdict, reason = classify_jacked_listener(port, budget)
    return verdict == LISTENER_JACKED, reason


def _read_reported_build(port: int, budget: float) -> "str | None":
    """Ask whatever listens on *port* which jacked build it is.

    A jacked service answers ``/api/version`` with JSON carrying ``current``.
    Anything else - no answer within *budget*, a listener that is not jacked,
    malformed JSON - reads as None. Never raises: callers use this to identify
    a port owner or to enrich a log line, never to fail a phase.
    """
    if budget <= 0:
        return None
    # Same proxy-free, bounded read as the health probe: this answer decides
    # whether the update verified, so an http_proxy in the environment must
    # not get to answer for 127.0.0.1.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(
            f"http://127.0.0.1:{port}/api/version", timeout=budget
        ) as response:
            payload = json.loads(response.read(_PROBE_BODY_LIMIT + 1))
    except Exception:  # noqa: BLE001 - an unidentifiable listener is the answer
        return None
    current = payload.get("current") if isinstance(payload, dict) else None
    return current if isinstance(current, str) and current else None


class RollbackResult(NamedTuple):
    """Outcome of one rollback attempt.

    `package_ok` - the previous version is back on disk.
    `install_ok` - the restored build migrated its settings.
    `step`       - the first step that failed, or None when both succeeded.
    """

    package_ok: bool
    install_ok: bool
    step: "str | None"

    @property
    def ok(self) -> bool:
        return self.package_ok and self.install_ok

# One-second sleep for a detached batch. NOT `timeout /t 1 /nobreak`: timeout
# reads the keyboard, so the moment stdin is anything but a real console — and
# every helper here is spawned with stdin=DEVNULL — it bails instantly with
# "ERROR: Input redirection is not supported, exiting the process immediately."
# That turns a bounded 120-second poll into 120 back-to-back no-ops, so the
# batch stops waiting for the tray to die and races `uv tool install --force`
# against a live python.exe (the classic Windows "Access denied" upgrade).
# `ping -n 2 127.0.0.1` needs no console and sleeps ~1s between its two pings.
_SLEEP_1S = "ping -n 2 127.0.0.1 >NUL\r\n"

# About three seconds, the same console-free idiom: four pings, one second
# apart. The pause between two package attempts.
_SLEEP_3S = "ping -n 4 127.0.0.1 >NUL\r\n"

# How many times a Windows batch runs the package step before it rolls back.
# One failed attempt is not proof of a broken update: Claude Code runs the
# statusline as `<tool venv>\Scripts\python.exe -m jacked.statusline` on every
# refresh, and that short-lived process locks venv files for a moment.
PACKAGE_ATTEMPTS = 3

# The text both batches put in FAILREASON, the log and the status record when
# the package step fails on its last attempt.
PACKAGE_FAILED_REASON = (
    f"the package step failed after {PACKAGE_ATTEMPTS} attempts"
)


def release_install_line(
    *,
    check_only: bool = False,
    port: int = 8321,
    wait_process: str = "",
) -> str:
    """Return the batch line that frees the install before the package step.

    The line runs ``jacked _release_install`` with the OLD build. Exit 0 means
    that no jacked service holds the install. Exit 1 means that the command
    did not stop the service. Exit 3 means that the service accepted the stop
    but did not exit in time. Use ``check_only`` when the batch must not stop
    the service: the command then refuses when a live service holds the
    install. ``port`` is the service port that the command checks when no
    manifest exists. ``wait_process`` is a ``PID:CREATION_ID`` value (from
    :func:`jacked.service.install_release.format_wait_process`) for a process
    that must exit first. An empty value adds no option.

    >>> release_install_line()
    'jacked _release_install --timeout 60 --port 8321 2>&1\\r\\n'
    >>> release_install_line(check_only=True, port=9000)
    'jacked _release_install --check-only --timeout 60 --port 9000 2>&1\\r\\n'
    >>> release_install_line(wait_process="7:windows-filetime:1")
    'jacked _release_install --timeout 60 --port 8321 --wait-process 7:windows-filetime:1 2>&1\\r\\n'
    >>> release_install_line(wait_process="7:a b")
    Traceback (most recent call last):
    ...
    ValueError: the process identity is not valid
    """
    from jacked.service.install_release import (
        RELEASE_TIMEOUT_SECONDS,
        parse_wait_process,
    )

    flag = " --check-only" if check_only else ""
    wait = ""
    if wait_process:
        # Validate again here: this text goes into a cmd.exe line.
        parse_wait_process(wait_process)
        wait = f" --wait-process {wait_process}"
    return (
        f"jacked _release_install{flag} --timeout {RELEASE_TIMEOUT_SECONDS}"
        f" --port {int(port)}{wait} 2>&1\r\n"
    )


def goto_on_failure(label: str) -> str:
    """Return the batch lines that jump to *label* when a step did not succeed.

    Put these lines directly after the step. ``if errorlevel 1`` means "the
    exit code is 1 or more". A crash exits with a negative NTSTATUS code,
    which passes that check. The second line compares the exact value, so
    every exit code that is not 0 goes to *label*. ``if`` does not change
    ERRORLEVEL, so the second line still reads the exit code of the step.
    Both lines are flat: ``goto`` out of a parenthesised block is a cmd.exe
    parsing trap.

    >>> goto_on_failure("preflight_failed").split("\\r\\n")
    ['if errorlevel 1 goto preflight_failed', 'if not "%ERRORLEVEL%"=="0" goto preflight_failed', '']
    """
    return (
        f"if errorlevel 1 goto {label}\r\n"
        f'if not "%ERRORLEVEL%"=="0" goto {label}\r\n'
    )


def package_retry_block(upgrade_line: str, failed_label: str) -> str:
    """Return batch lines that run the package step up to PACKAGE_ATTEMPTS times.

    The block logs each attempt and pauses about three seconds between two
    attempts. When an attempt succeeds, the batch continues after the block.
    When the last attempt fails, the batch jumps to ``failed_label``. The
    lines are flat: ``goto`` out of a parenthesised block is a cmd.exe
    parsing trap.

    >>> block = package_retry_block('"uv" "tool" "install"', "package_failed")
    >>> block.count('"uv" "tool" "install" 2>&1')
    1
    >>> "if %JACKED_PKG_TRY% GEQ 3 goto package_failed" in block
    True
    >>> 'if "%ERRORLEVEL%"=="0" goto package_done' in block
    True
    """
    return (
        "set /a JACKED_PKG_TRY=0\r\n"
        ":package_attempt\r\n"
        "set /a JACKED_PKG_TRY+=1\r\n"
        "echo [%date% %time%] package step: attempt %JACKED_PKG_TRY% of "
        f"{PACKAGE_ATTEMPTS}\r\n"
        + upgrade_line + " 2>&1\r\n"
        # An exact compare, not "if not errorlevel 1": a negative NTSTATUS
        # exit (a crash) is below 1 and would read as success.
        'if "%ERRORLEVEL%"=="0" goto package_done\r\n'
        "echo [%date% %time%] WARN: package step attempt %JACKED_PKG_TRY% of "
        f"{PACKAGE_ATTEMPTS} failed\r\n"
        f"if %JACKED_PKG_TRY% GEQ {PACKAGE_ATTEMPTS} goto {failed_label}\r\n"
        + _SLEEP_3S
        + "goto package_attempt\r\n"
        ":package_done\r\n"
    )

# Absolute paths for the two tools whose EXIT CODE steers the wait loop. Git
# Bash, Cygwin and GnuWin32 all put a `find` on PATH that takes different flags
# and returns different codes; if one shadows System32's, `if errorlevel 1`
# reads as "parent is gone" on the very first pass and the batch stops waiting
# for the tray to die. Same failure the `timeout` bug caused, different cause.
_TASKLIST = "%SystemRoot%\\System32\\tasklist.exe"
_FIND = "%SystemRoot%\\System32\\find.exe"

logger = logging.getLogger(__name__)

# Install methods whose upgrade argv starts with a user-installed tool. The
# updater runs under the service's system-only PATH, so argv[0] must be the
# path find_bin resolved (``~/.local/bin/uv``, ``~/.local/bin/pipx``), never
# the bare name. Value: the install hint for the "tool missing" recovery.
_TOOL_INSTALL_HINTS = {
    "uv": "Install uv from https://docs.astral.sh/uv/",
    "pipx": "Install pipx from https://pipx.pypa.io/",
}


def _verify_service_block(port: int) -> str:
    """Batch line that polls ``/api/version`` for up to 20s. Exit 0 = healthy."""
    return (
        'powershell -NoProfile -Command "for ($i=0;$i -lt '
        f"{int(VERIFY_TIMEOUT_SECONDS / POLL_INTERVAL_SECONDS)};$i++)"
        "{try{$r=Invoke-WebRequest -UseBasicParsing "
        f"http://127.0.0.1:{port}/api/version"
        " -TimeoutSec 1 -ErrorAction Stop; if($r.StatusCode -eq 200){exit 0}}catch{}"
        f'Start-Sleep -Milliseconds {int(POLL_INTERVAL_SECONDS * 1000)}'
        '} exit 1"\r\n'
    )


def wait_for_parent_block(pid: int) -> str:
    """Batch lines that block until *pid* exits, capped at ~120s.

    Shared by BOTH Windows helpers (the tray updater here and
    ``jacked upgrade``'s helper in cli.py) so the two cannot drift apart —
    they have independently regressed on the same poll loop twice already.

    A bare ``find "<pid>"`` matches any process that later reuses this PID, so
    an unbounded loop can spin forever after the real parent died. Cap it and
    proceed, mirroring the POSIX :func:`wait_for_exit` timeout.

    Emits progress on stdout; callers must NOT redirect it to the update log
    (see the warning in ``_spawn_windows_tray_updater``).
    """
    return (
        "set /a JACKED_WAITED=0\r\n"
        ":wait\r\n"
        f'{_TASKLIST} /FI "PID eq {pid}" 2>NUL | {_FIND} "{pid}" >NUL\r\n'
        # Only "if errorlevel 1" here, on purpose: exit 1 from find.exe means
        # "the PID is not listed". A crashed find.exe (a negative exit) proves
        # nothing, so the loop keeps waiting, up to the 120 s cap below.
        "if errorlevel 1 goto waitdone\r\n"
        "set /a JACKED_WAITED+=1\r\n"
        "if %JACKED_WAITED% GEQ 120 (\r\n"
        f"    echo [%date% %time%] WARNING: parent {pid} still listed after 120s; proceeding (PID may be reused)\r\n"
        "    goto waitdone\r\n"
        ")\r\n" + _SLEEP_1S + "goto wait\r\n"
        ":waitdone\r\n"
    )


def wait_for_exit(pid: int, timeout: float = 30.0) -> bool:
    """Poll until process exits or timeout. Returns True if exited."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.5)
    return False


def _force_kill_pid(pid: int) -> None:
    """Compatibility no-op: integer PIDs never authorize force termination."""
    logger.warning(
        "Refused force-kill of PID %d without a v2 creation-identity handle", pid
    )


def _pids_bound_to_port(port: int) -> list[int]:
    """Compatibility shim: port ownership is deliberately not enumerated."""
    logger.warning("Refused PID discovery from port %d; ownership is ambiguous", port)
    return []


def _spawn_detached(cmd: list, log_fh=None) -> "subprocess.Popen":
    """Spawn a subprocess that survives this process dying."""
    kwargs: dict = {
        "stdin": subprocess.DEVNULL,
        "stdout": log_fh if log_fh is not None else subprocess.DEVNULL,
        "stderr": log_fh if log_fh is not None else subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        # CREATE_NO_WINDOW, not DETACHED_PROCESS: callers hand us the jacked.exe
        # console trampoline (and python.exe), which pop a visible console under
        # DETACHED (no inherited console). A hidden console suppresses the flash.
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(cmd, **kwargs)


def _release_update_lock(handle) -> None:
    """Release the exclusive update lock. Never raises."""
    if handle is None:
        return
    try:
        handle.close()
    except Exception:
        logger.exception("Could not release the update lock")


def _write_recovery(message: str) -> None:
    """Write a human-readable recovery file so the user sees what broke."""
    try:
        RECOVERY_FILE.parent.mkdir(parents=True, exist_ok=True)
        RECOVERY_FILE.write_text(message)
    except Exception:
        logger.exception("Could not write recovery file")


def run_update(
    parent_pid: int,
    extras: str = "tray",
    target_version: "str | None" = None,
    port: int = 8321,
) -> None:
    """Main update sequence. Called in the detached helper process."""
    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_fh = open(UPDATE_LOG, "a", buffering=1, encoding="utf-8", errors="replace")

    def log(msg: str) -> None:
        log_fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

    # Defense-in-depth: refuse non-upgradable installs even here, in case
    # someone invokes this entrypoint directly without tray/CLI pre-flight.
    from jacked.install_method import (
        can_auto_upgrade as _can_upgrade,
        detect_install_method as _detect,
        rollback_command,
        rollback_command_label,
        upgrade_command,
        upgrade_command_label,
    )
    from jacked.service import update_status as _us
    from jacked import __version__ as _current_version

    _ok, _reason = _can_upgrade()
    if not _ok:
        log(f"REFUSED: {_reason}")
        _write_recovery(f"Jacked auto-update refused:\n{_reason}\n")
        log_fh.close()
        return

    # --- Status file lifecycle
    _target = target_version or "next"
    _method = _detect()
    # Exclusive lock FIRST. `init_or_adopt_status` is a read-check-write on
    # mtime, so two updaters that start inside the same second both read "no
    # updater in flight" and both proceed. The lock is held for the whole
    # update and released in the finally below.
    try:
        _update_lock = _us.acquire_update_lock(_us.UPDATE_STATUS_FILE)
    except Exception as exc:
        # Same posture as the CLI: a machine that cannot take the lock must
        # not start a package install.
        logger.exception("Could not take the update lock")
        log(f"REFUSED: could not take the update lock ({type(exc).__name__})")
        _write_recovery(
            "Jacked auto-update refused: the update lock could not be taken "
            f"({type(exc).__name__}). Check ~/.claude is writable, then retry.\n"
        )
        log_fh.close()
        return
    else:
        if _update_lock is None:
            log("REFUSED: another updater holds the update lock")
            _write_recovery(
                "Jacked auto-update refused: another jacked update is already "
                "running. Wait for it to finish, then retry.\n"
            )
            log_fh.close()
            return
    try:
        outcome = _us.init_or_adopt_status(
            _us.UPDATE_STATUS_FILE,
            from_version=_current_version,
            to_version=_target,
            method=_method,
            log_path=str(UPDATE_LOG),
        )
        if outcome == "adopted":
            log("REUSING tray pre-init status file")
    except _us.LockBusy as exc:
        log(f"REFUSED: another updater active: {exc}")
        _release_update_lock(_update_lock)
        log_fh.close()
        return
    except Exception:
        logger.exception("Could not initialize update status file")

    def _begin(phase: str) -> None:
        try:
            _us.begin_phase(_us.UPDATE_STATUS_FILE, phase)
        except Exception:
            logger.exception("begin_phase failed: %s", phase)

    def _end(
        phase: str,
        status: str,
        error: "str | None" = None,
        recovery: "str | None" = None,
    ) -> None:
        try:
            _us.end_phase(
                _us.UPDATE_STATUS_FILE,
                phase,
                status=status,
                error=error,
                recovery=recovery,
            )
        except Exception:
            logger.exception("end_phase failed: %s", phase)

    # Set to True by any restart path (success or fallback) so the
    # finally-guard skips a duplicate restart.
    _restart_attempted = [False]

    try:
        # Phase: waiting_for_parent
        _begin("waiting_for_parent")
        log(f"Waiting for parent PID {parent_pid} to exit")
        if not wait_for_exit(parent_pid, timeout=15.0):
            log(
                f"Parent {parent_pid} still alive after 15s; refusing PID-only termination"
            )
            _end(
                "waiting_for_parent",
                "failed",
                error="parent process did not exit; ownership cannot be revalidated",
                recovery="stop the owned service normally, then retry the update",
            )
            _write_recovery(
                "Jacked auto-update stopped safely because the old service did not exit.\n"
                "No process was killed from PID-only evidence. Stop jacked normally, then retry.\n"
            )
            return
        else:
            _end("waiting_for_parent", "ok")

        method = _method
        try:
            cmd = upgrade_command(extras)
            label = upgrade_command_label(extras)
        except ValueError as exc:
            # Defense in depth: _can_upgrade() gate above should already
            # refuse pip/editable, but if a future code path skips the gate
            # the raise here keeps us from crashing the detached helper.
            log(f"ERROR: {exc}")
            try:
                _us.mark_failed(
                    _us.UPDATE_STATUS_FILE,
                    error=str(exc),
                    recovery='uv tool install "claude-jacked[tray]" --force',
                )
            except Exception:
                logger.exception("mark_failed after upgrade_command ValueError")
            _write_recovery(
                f"Jacked auto-update failed: {exc}\n\n"
                "Manual recovery:\n"
                '  uv tool install "claude-jacked[tray]" --force\n'
            )
            return

        if method in _TOOL_INSTALL_HINTS:
            tool = find_bin(method)
            if not tool:
                hint = _TOOL_INSTALL_HINTS[method]
                msg = f"Could not find `{method}` on PATH. {hint}"
                log(f"ERROR: {msg}")
                try:
                    _us.mark_failed(
                        _us.UPDATE_STATUS_FILE,
                        error=f"{method} not found on PATH",
                        recovery=f"{hint} and re-run",
                    )
                except Exception:
                    logger.exception("mark_failed after %s-missing failed", method)
                _write_recovery(
                    f"Jacked auto-update failed:\n{msg}\n\n"
                    "Manual recovery:\n"
                    f"  {label}\n"
                    "  jacked install --force\n"
                    "  jacked service start\n"
                )
                return
            cmd[0] = tool

        # Phase: installing_package
        _begin("installing_package")
        log(f"Install method: {method}")
        log(f"Running: {label}")
        result = subprocess.run(
            cmd,
            stdout=log_fh,
            stderr=log_fh,
            check=False,
            creationflags=NO_WINDOW,
        )
        log(f"upgrade command returncode: {result.returncode}")

        if result.returncode != 0:
            _end(
                "installing_package",
                "failed",
                error=f"upgrade command exit {result.returncode}",
                recovery=label,
            )
            _write_recovery(
                f"Jacked auto-update failed: upgrade command returned {result.returncode}.\n"
                f"See {UPDATE_LOG} for details.\n\n"
                "Manual recovery:\n"
                f"  {label}\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return
        _end("installing_package", "ok")

        jacked = find_bin("jacked")
        if not jacked:
            log("Could not locate jacked after install - NOT restarting")
            try:
                _us.mark_failed(
                    _us.UPDATE_STATUS_FILE,
                    error="jacked binary missing after install",
                    recovery="jacked install --force && jacked service start",
                )
            except Exception:
                logger.exception("mark_failed after jacked-missing failed")
            _write_recovery(
                "Jacked auto-update: install succeeded but the `jacked` binary "
                "is no longer on PATH. Run manually:\n"
                "  jacked install --force\n"
                "  jacked service start\n"
            )
            return

        # The restart + verify tail. It runs for the successful path AND for
        # the rolled-back path, so the machine always ends with a service.
        _diagnosed: set = set()

        def _log_once(message: str) -> None:
            """Log a diagnosis the first time it is seen.

            Both waits poll, so an unchanged reason would repeat dozens of
            times; it must still be logged, because for a refused update this
            line is the only account of WHY the port holder was rejected.
            """
            if message not in _diagnosed:
                _diagnosed.add(message)
                log(message)

        def _wait_port_free() -> bool:
            """Wait until the port is ours to take. True when the start may run.

            Free is the easy answer. A port that is still LISTENING is not
            automatically a squatter: `jacked install --force` activates the
            tray, so the build this update just installed is usually already
            serving by the time this phase runs, and under launchd KeepAlive
            the previous build can be too. A listener that fingerprints as a
            jacked service is one of those, and the restart replaces it. Only
            a listener that will not identify itself by the deadline stops the
            update, and no pid is looked up or signalled either way.
            """
            _begin("waiting_port_free")
            log("Waiting for port to become available")
            deadline = time.monotonic() + PORT_FREE_TIMEOUT_SECONDS
            while time.monotonic() < deadline:
                if not is_port_listening("127.0.0.1", port):
                    log(f"Port {port} is free")
                    _end("waiting_port_free", "ok")
                    return True
                is_jacked, reason = _identify_jacked_listener(
                    port, min(IDENTIFY_TIMEOUT_SECONDS, deadline - time.monotonic())
                )
                if is_jacked:
                    log(
                        f"Port {port} is held by a jacked service; "
                        "a native restart will be attempted"
                    )
                    _end("waiting_port_free", "ok")
                    return True
                _log_once(f"Port {port} holder is not identified as jacked: {reason}")
                time.sleep(POLL_INTERVAL_SECONDS)
            _end(
                "waiting_port_free",
                "failed",
                error=f"port {port} remains occupied by an unverified listener",
                recovery="run `jacked service status`; v2 services use discoverable quarantine",
            )
            log(f"ABORT: port {port} is ambiguous; no process was signalled")
            return False

        def _spawn_service() -> None:
            """Start the service through the platform's own lifecycle manager."""
            _begin("starting_service")
            # On macOS the tray may be managed by launchd with KeepAlive - a
            # plain detached `jacked service start` races launchd's respawn to
            # bind :port and usually loses. Delegate to the platform's native
            # lifecycle manager when present (launchctl kickstart on macOS;
            # systemctl --user on Linux if user-installed). Fall back to
            # detached Popen when no manager is present (Windows or unmanaged
            # Linux).
            from jacked.service.platform import ensure_native_lifecycle, native_restart

            ens_ok, ens_state, ens_reason = ensure_native_lifecycle()
            if ens_ok:
                if ens_state == "just_installed":
                    log(
                        f"Native lifecycle freshly installed (RunAtLoad booted service): {ens_reason}"
                    )
                    _restart_attempted[0] = True
                else:
                    # already_installed -> atomic kickstart
                    native_ok, native_reason = native_restart()
                    _restart_attempted[0] = True
                    if native_ok:
                        log(f"Native lifecycle restart: {native_reason}")
                    else:
                        log(
                            f"Native kickstart failed ({native_reason}); fallback to manual spawn"
                        )
                        _spawn_detached([jacked, "service", "start"], log_fh=log_fh)
            else:
                log(f"Native lifecycle unavailable ({ens_reason}); manual spawn")
                _spawn_detached([jacked, "service", "start"], log_fh=log_fh)
                _restart_attempted[0] = True
            _end("starting_service", "ok")

        def _verified_build(
            expected: "str | None", deadline: float, known_mismatch: "str | None"
        ) -> "tuple[bool, str | None]":
            """Identity-check the listener once. Returns (accepted, build seen).

            Accepted means this listener ends verification ok: it reports the
            build this update installed, or - when its version route will not
            answer in time, which a cold PyPI cache alone can cause - it at
            least fingerprints as a jacked service. `expected` None means the
            caller has no version to compare (the tray can update to "next"),
            so any readable build counts. `known_mismatch` is the build this
            port already reported, if any: once it has named itself as the
            wrong build, the fingerprint fallback is closed.
            """
            # Always leave the health fingerprint its own slice: when less
            # than the identify budget remains, skip straight to it.
            seen = _read_reported_build(
                port,
                min(
                    VERSION_READ_TIMEOUT_SECONDS,
                    deadline - time.monotonic() - IDENTIFY_TIMEOUT_SECONDS,
                ),
            )
            if seen is not None:
                if expected is None or seen == expected:
                    log(f"Service reports build {seen}")
                    return True, seen
                _log_once(
                    f"Port {port} answers as build {seen}, expected {expected}; "
                    "still waiting for the restart"
                )
                return False, seen
            if known_mismatch is not None:
                # This port has already told us which build it is, and it is
                # the wrong one. A health fingerprint the wrong build passes
                # just as well is not evidence that the restart landed.
                return False, known_mismatch
            is_jacked, reason = _identify_jacked_listener(
                port, min(IDENTIFY_TIMEOUT_SECONDS, deadline - time.monotonic())
            )
            if is_jacked:
                log(
                    f"Service is listening on :{port} as a jacked service, "
                    "but its build could not be read"
                )
                return True, None
            _log_once(f"Listener on :{port} is not identified as jacked: {reason}")
            return False, None

        def _verify_service(expected: "str | None") -> bool:
            """Wait for the EXPECTED build to answer on the port. True when it does.

            Connect-based, because with remote access on the service listens
            on ``*:port`` and a 127.0.0.1 bind probe succeeds right next to
            that wildcard listener. 127.0.0.1 is always the right probe
            address: every bind plan covers loopback (see BindPlan.probe_host
            in service/bind.py).

            Identity-aware, because a listener alone proves nothing: the
            previous build under launchd KeepAlive, a hand-run `jacked webux`,
            or the failed new build can all be answering here, and accepting
            any of them let the rollback claim a restore that never happened.
            """
            _begin("verifying_service")
            log("Verifying service came up")
            deadline = time.monotonic() + VERIFY_TIMEOUT_SECONDS
            mismatch = None
            was_listening = False
            while time.monotonic() < deadline:
                if is_port_listening("127.0.0.1", port):
                    was_listening = True
                    accepted, seen = _verified_build(expected, deadline, mismatch)
                    if accepted:
                        _end("verifying_service", "ok")
                        return True
                    mismatch = seen if seen is not None else mismatch
                time.sleep(POLL_INTERVAL_SECONDS)

            if mismatch is not None:
                error = f"port {port} answers as build {mismatch}, expected {expected}"
                recovery = "jacked service status"
            elif was_listening:
                # Never call a bound port dead: the operator needs to know
                # something IS there and would not say what it is.
                error = (
                    f"port {port} is bound but the listener never "
                    "identified itself as jacked"
                )
                recovery = "jacked service status"
            else:
                error = (
                    f"service did not bind :{port} "
                    f"within {VERIFY_TIMEOUT_SECONDS:g}s"
                )
                recovery = "jacked service start"
            _end("verifying_service", "failed", error=error, recovery=recovery)
            log(f"WARNING: {error}")
            return False

        def _start_and_verify(expected: "str | None") -> str:
            """Start the service and wait for *expected* to answer.

            'ok'        - the expected build is serving the port.
            'port_busy' - the port stayed occupied by an unverified listener.
            'not_ready' - the expected build never answered in time.

            `expected` is the build this tail is starting: the target version
            on the upgrade path, the restored version on a rollback tail, or
            None when the update had no pinned target to compare against.
            """
            # Per tail: the rollback tail must log its own reasons even when
            # they read the same as the upgrade tail's.
            _diagnosed.clear()
            if not _wait_port_free():
                return "port_busy"
            _spawn_service()
            return "ok" if _verify_service(expected) else "not_ready"

        _rolled_back = [False]

        def _run_rollback_step(argv: list) -> "tuple[int, str]":
            """Run one rollback step. Returns (returncode, failure detail).

            A step that cannot even SPAWN (uv or jacked deleted, or not
            executable) raises OSError. Letting that escape aborted the
            rollback before the failed step was ever recorded, so the user got
            no recovery file at all. Treat it as that step's failure instead.
            """
            try:
                completed = subprocess.run(
                    argv,
                    stdout=log_fh,
                    stderr=log_fh,
                    check=False,
                    creationflags=NO_WINDOW,
                )
            except OSError as exc:
                return -1, f"could not run {argv[0]}: {exc}"
            return completed.returncode, f"exit {completed.returncode}"

        def _rollback_package(reason: str) -> "str | None":
            """Reinstall the previous build. Returns a failure detail or None."""
            try:
                rb_cmd = rollback_command(extras, _current_version)
                rb_label = rollback_command_label(extras, _current_version)
            except ValueError as exc:
                log(f"ERROR: cannot build a rollback command: {exc}")
                return str(exc)
            if method in _TOOL_INSTALL_HINTS:
                rb_tool = find_bin(method)
                if rb_tool:
                    rb_cmd[0] = rb_tool
            _begin("rolling_back")
            log(f"Rolling back to v{_current_version} ({reason}): {rb_label}")
            code, detail = _run_rollback_step(rb_cmd)
            if code == 0:
                return None
            _end(
                "rolling_back",
                "failed",
                error=f"rollback command {detail}",
                recovery=rb_label,
            )
            log(f"ERROR: rollback command failed: {detail}")
            return detail

        def _rollback_settings() -> "str | None":
            """Migrate the restored build's settings. Detail on failure."""
            restored = find_bin("jacked") or jacked
            code, detail = _run_rollback_step([restored, "install", "--force"])
            if code == 0:
                return None
            _end(
                "rolling_back",
                "failed",
                error=(
                    "the restored build could not migrate its settings "
                    f"(jacked install --force {detail})"
                ),
                recovery="jacked install --force",
            )
            log(
                "ERROR: jacked install --force failed for the restored build: "
                f"{detail}"
            )
            return detail

        def _rollback(reason: str) -> "RollbackResult":
            """Reinstall the version that was running. At most once per run.

            Returns a :class:`RollbackResult`. The package reinstall and the
            settings migration are checked SEPARATELY: a rollback that put the
            package back but could not migrate its settings has not restored
            the machine, and callers must not report it as if it had.
            """
            if _rolled_back[0]:
                log("Rollback already attempted once; refusing a second pass")
                return RollbackResult(False, False, ROLLBACK_STEP_PACKAGE)
            _rolled_back[0] = True
            if _rollback_package(reason) is not None:
                return RollbackResult(False, False, ROLLBACK_STEP_PACKAGE)
            if _rollback_settings() is not None:
                return RollbackResult(True, False, ROLLBACK_STEP_INSTALL)
            _end("rolling_back", "ok")
            log(f"Rollback to v{_current_version} reinstalled the package")
            return RollbackResult(True, True, None)

        def _fail_and_recover(
            what: str, rolled: bool, failed_step: "str | None" = None
        ) -> None:
            """Record a failed update and tell the user what state they are in.

            `rolled` is True ONLY when the package rollback, the settings
            migration AND the restarted service all succeeded. Anything less
            names the step that failed and the command that repairs it.
            """
            if rolled:
                error = f"{what}; rolled back to v{_current_version}"
                recovery = (
                    f"v{_current_version} is restored. Run `jacked service status`, "
                    "then report the preflight output."
                )
            else:
                step = failed_step or ROLLBACK_STEP_PACKAGE
                error = (
                    f"{what}; the rollback to v{_current_version} stopped at "
                    f"the {step} step"
                )
                recovery = (
                    f'uv tool install "claude-jacked[{extras}]=={_current_version}" '
                    "--force --refresh && jacked install --force && jacked service start"
                )
            try:
                _us.mark_failed(
                    _us.UPDATE_STATUS_FILE, error=error, recovery=recovery
                )
            except Exception:
                logger.exception("mark_failed after %s", what)
            _write_recovery(
                f"Jacked auto-update failed: {error}\n\n"
                f"See {UPDATE_LOG} for details.\n\n"
                "Recovery:\n"
                f"  {recovery}\n"
            )
            log(f"UPDATE FAILED: {error}")

        # Phase: preflight - the transaction gate. The new build must prove it
        # can provision its service contract before the old service is gone.
        _begin("preflight")
        log(f"Running: {jacked} service preflight")
        # A hung or unlaunchable preflight is a REFUSAL, not a crash: the old
        # service is still up at this point, and the rollback path is what puts
        # the machine back. Without the timeout the detached helper could wait
        # forever with the tray already gone.
        try:
            preflight = subprocess.run(
                [jacked, "service", "preflight"],
                capture_output=True,
                text=True,
                check=False,
                timeout=PREFLIGHT_TIMEOUT_SECONDS,
                creationflags=NO_WINDOW,
            )
            preflight_code = preflight.returncode
            preflight_output = (
                (preflight.stdout or "") + (preflight.stderr or "")
            ).strip()
        except subprocess.TimeoutExpired:
            preflight_code = -1
            preflight_output = (
                f"preflight did not answer within {PREFLIGHT_TIMEOUT_SECONDS}s"
            )
        except OSError as exc:
            preflight_code = -1
            preflight_output = f"preflight could not run: {exc}"
        if preflight_output:
            log(f"preflight output: {preflight_output}")
        log(f"preflight returncode: {preflight_code}")
        if preflight_code != 0:
            first_line = (
                preflight_output.splitlines()[0]
                if preflight_output
                else f"exit {preflight_code}"
            )
            _end(
                "preflight",
                "failed",
                error=f"new build refused to provision its service contract: {first_line}",
                recovery=f'uv tool install "claude-jacked[{extras}]=={_current_version}" --force --refresh',
            )
            rollback = _rollback("preflight refused")
            # Bring the restored version back up through the native lifecycle.
            # A restored build that never binds the port has not restored the
            # machine, so its verdict decides whether this was a rollback.
            restored_ready = _start_and_verify(_current_version) == "ok"
            _fail_and_recover(
                f"v{_target} refused to start: {first_line}",
                rollback.ok and restored_ready,
                rollback.step or (None if restored_ready else ROLLBACK_STEP_RESTART),
            )
            return
        _end("preflight", "ok")

        # Phase: migrating_settings
        _begin("migrating_settings")
        log(f"Running: {jacked} install --force")
        migrate_result = subprocess.run(
            [jacked, "install", "--force"],
            stdout=log_fh,
            stderr=log_fh,
            check=False,
            creationflags=NO_WINDOW,
        )
        log(f"jacked install returncode: {migrate_result.returncode}")

        if migrate_result.returncode != 0:
            _end(
                "migrating_settings",
                "failed",
                error=f"jacked install exit {migrate_result.returncode}",
                recovery="jacked install --force",
            )
            log(
                "Settings migration failed. Rolling back to "
                f"v{_current_version}"
            )
            # The old tray is already gone at this point. Returning here left
            # the machine with a build whose settings never migrated AND no
            # running service, so this is a transaction failure like any other.
            rollback = _rollback("settings migration failed")
            restored_ready = _start_and_verify(_current_version) == "ok"
            _fail_and_recover(
                f"v{_target} could not migrate settings "
                f"(jacked install exit {migrate_result.returncode}); "
                "backups are at ~/.claude/settings.json.bak-*",
                rollback.ok and restored_ready,
                rollback.step or (None if restored_ready else ROLLBACK_STEP_RESTART),
            )
            return
        _end("migrating_settings", "ok")

        outcome = _start_and_verify(target_version)
        if outcome == "ok":
            log(f"Updater done - new service is listening on :{port}")
            if RECOVERY_FILE.exists():
                try:
                    RECOVERY_FILE.unlink()
                except Exception:
                    pass
            try:
                _us.mark_succeeded(_us.UPDATE_STATUS_FILE)
            except Exception:
                logger.exception("mark_succeeded failed - writing mark_failed fallback")
                log("WARNING: mark_succeeded raised - attempting mark_failed fallback")
                try:
                    _us.mark_failed(
                        _us.UPDATE_STATUS_FILE,
                        error="mark_succeeded raised - service came up but final status write failed",
                        recovery="Reload the dashboard: http://127.0.0.1:8321/",
                    )
                except Exception:
                    logger.exception("mark_failed fallback also raised")
                    log("ERROR: mark_failed fallback also raised - disk likely full")
        elif outcome == "port_busy":
            _write_recovery(
                f"Jacked auto-update stopped safely because port {port} is occupied by an "
                "unverified listener. No port owner was killed. Run `jacked service status` "
                "for ownership and quarantine guidance.\n"
            )
        else:
            # The new service never came up. Put the previous version back and
            # restart it, so the user keeps a working tray.
            rollback = _rollback("new service never became ready")
            # Start whatever is on disk either way: a machine with no service
            # is the worst outcome. Only the verdict decides "rolled back".
            restored_ready = _start_and_verify(_current_version) == "ok"
            _fail_and_recover(
                f"v{_target} never became ready on :{port}",
                rollback.ok and restored_ready,
                rollback.step or (None if restored_ready else ROLLBACK_STEP_RESTART),
            )
    finally:
        # Best-effort recovery: ensure the service comes back up even when an
        # upgrade phase bailed early via `return`. Previously a SameFileError
        # in `jacked install --force` (or any other failure path) left the
        # tray permanently dead — the parent process had already exited and
        # nothing kicked launchd back into life. Skip when the success path
        # already attempted a restart (avoids duplicate launchctl calls).
        if not _restart_attempted[0]:
            try:
                from jacked.service.platform import native_restart

                log("Final guard: no restart attempted by upgrade flow. Kickstart.")
                ok, reason = native_restart()
                log(
                    f"Final guard: native_restart {'OK' if ok else 'FAILED'} ({reason})"
                )
            except Exception:
                logger.exception("Final-guard native_restart raised")
        _release_update_lock(_update_lock)
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


def spawn_updater_from_tray(
    parent_pid: int,
    extras: str = "tray",
    target_version: "str | None" = None,
    port: int = 8321,
) -> None:
    """Called by the tray on update click. Spawns the detached helper.

    POSIX: detached Python subprocess running this module. `uv tool install`
    can atomically replace the venv while our interpreter stays valid via
    its open file descriptor on the binary.

    Windows: Python subprocess doesn't work — `uv tool install --force`
    can't replace python.exe while this interpreter is using it (classic
    Windows exclusive-file-lock). Instead spawn a detached cmd.exe batch
    that:
      1. Waits for the tray PID to exit.
      2. Runs `jacked _release_install` (old build): no service may hold
         the tool venv. A failure starts the previous service again.
      3. Runs `uv tool install --force`, up to PACKAGE_ATTEMPTS times; the
         last failure rolls back to the running version.
      4. Runs `jacked install --force`.
      5. Runs `jacked service start` (detached).
    cmd.exe is a system binary we don't own, so it survives whatever uv
    does to the jacked venv. Same trick as `jacked upgrade` on Windows.
    """
    if sys.platform == "win32":
        _spawn_windows_tray_updater(
            parent_pid, extras, target_version=target_version, port=port
        )
        return

    py = _find_updater_python()
    if not py:
        raise SystemExit("No Python executable found for updater spawn")

    _spawn_detached(
        [
            py,
            "-m",
            "jacked.service.updater",
            str(parent_pid),
            extras,
            "--target-version",
            target_version or "",
            "--port",
            str(port),
        ],
        log_fh=None,
    )


_FAILED_FILE_PATH = '"%USERPROFILE%\\.claude\\jacked-update-failed.txt"'


def _tray_rollback_failed_label(
    label: str, step: str, repair: str, drift_guard: str
) -> str:
    """Return the cmd.exe label that reports ONE failed rollback step.

    Reached only by `goto`, from a rollback step that returned a non-zero
    errorlevel. It marks the phase failed, names the step, gives the command
    that repairs it, and exits 1. It never writes "rolled back".
    """
    return (
        ":" + label + "\r\n"
        'jacked _update_status rolling_back failed --error "the rollback '
        + 'stopped at the ' + step + ' step" --recovery "' + repair + '"\r\n'
        + drift_guard
        + "echo Jacked tray update failed, and the rollback stopped at the "
        + step + " step. See %LOGFILE%. > " + _FAILED_FILE_PATH + "\r\n"
        "echo Run this first: " + repair + " >> " + _FAILED_FILE_PATH + "\r\n"
        "echo Then: jacked install --force ^&^& jacked service start >> "
        + _FAILED_FILE_PATH + "\r\n"
        "exit /b 1\r\n"
    )


def _tray_start_previous_service(port: int, failed_label: str) -> str:
    """Return batch lines that start the service again and verify it.

    The batch uses these lines on an abort path where nothing was installed,
    or where the install did not change. A second start is safe: the port
    guard makes it a no-op when a service already serves the port. When the
    service does not answer, the batch jumps to ``failed_label``.
    """
    return (
        "echo [%date% %time%] starting the previous service again\r\n"
        'start "" /B jacked service start\r\n'
        + _verify_service_block(port)
        + goto_on_failure(failed_label)
    )


def _tray_release_failed_block(
    port: int, to_version: str, drift_guard: str
) -> str:
    """Return the tray batch tail for a release step that did not succeed.

    The batch reaches ``:release_failed`` before the package step, so nothing
    was installed. The old tray stopped itself before the batch started, so
    the batch starts the previous service again for every non-zero exit. The
    port guard makes that start a no-op when a different service still runs.
    """
    return (
        ":release_failed\r\n"
        'jacked _update_status waiting_for_parent failed --error "the old '
        'service did not release the install" --recovery "jacked service '
        'start"\r\n'
        + drift_guard
        + "echo [%date% %time%] ERROR: the old service did not release the "
        "install; the package step did not run\r\n"
        + _tray_start_previous_service(port, "release_restart_failed")
        + "echo Jacked tray update to v" + to_version + " stopped before the "
        "package step: the old service did not release the install. Nothing "
        "was installed, and the previous service is running again. See "
        "%LOGFILE%. > " + _FAILED_FILE_PATH + "\r\n"
        "echo Recovery: close every jacked process, then update again from "
        "the tray. >> " + _FAILED_FILE_PATH + "\r\n"
        "exit /b 1\r\n"
        ":release_restart_failed\r\n"
        "echo [%date% %time%] ERROR: the previous service did not start "
        "again\r\n"
        "echo Jacked tray update to v" + to_version + " stopped before the "
        "package step, and the service start step failed. Nothing was "
        "installed. See %LOGFILE%. > " + _FAILED_FILE_PATH + "\r\n"
        "echo Run this first: jacked service start >> " + _FAILED_FILE_PATH
        + "\r\n"
        "exit /b 1\r\n"
    )


def _tray_package_failed_block(
    *, port: int, label: str, drift_guard: str, has_rollback: bool
) -> str:
    """Return the tray batch tail for a package step that failed every attempt.

    With a rollback command, the block goes to the shared rollback, which
    reinstalls the previous version, migrates its settings, and starts and
    verifies its service. Without one, the block tries to start the previous
    service (the failed step can leave the install unchanged) and writes a
    failure message that does not claim a rollback.
    """
    block = (
        ":package_failed\r\n"
        'jacked _update_status installing_package failed --error "'
        + PACKAGE_FAILED_REASON + '" --recovery "' + label + '"\r\n'
        + drift_guard
        + "echo [%date% %time%] ERROR: " + PACKAGE_FAILED_REASON + "\r\n"
        "set FAILREASON=" + PACKAGE_FAILED_REASON + "\r\n"
    )
    if has_rollback:
        return block + "goto rollback_now\r\n"
    return (
        block
        + "echo [%date% %time%] no rollback command is available\r\n"
        + _tray_start_previous_service(port, "package_restart_failed")
        + "echo Jacked tray update failed (%FAILREASON%), and no rollback "
        "command is available. The previous service is running again. See "
        "%LOGFILE%. > " + _FAILED_FILE_PATH + "\r\n"
        "echo Recovery: " + label + " ^&^& jacked install --force ^&^& "
        "jacked service start >> " + _FAILED_FILE_PATH + "\r\n"
        "exit /b 1\r\n"
        ":package_restart_failed\r\n"
        "echo [%date% %time%] ERROR: the previous service did not start "
        "again\r\n"
        "echo Jacked tray update failed (%FAILREASON%), no rollback command "
        "is available, and the service start step failed. The install can be "
        "incomplete. See %LOGFILE%. > " + _FAILED_FILE_PATH + "\r\n"
        "echo Run this first: " + label + " >> " + _FAILED_FILE_PATH + "\r\n"
        "echo Then: jacked install --force ^&^& jacked service start >> "
        + _FAILED_FILE_PATH + "\r\n"
        "exit /b 1\r\n"
    )


def _tray_wait_process(parent_pid: int) -> str:
    """Return the ``--wait-process`` value for the tray process, or "".

    The tray is the service process, and it deletes its manifest before its
    interpreter exits. The release step must therefore wait for the tray
    process itself, by its creation identity, so that a reused PID cannot
    look like the tray. When the identity cannot be read, or cannot go
    safely into a batch line, return "": the release step then still waits
    until the service port is free.

    Only Windows runs this batch, and only the Windows identity
    (``windows-filetime:<ticks>``) is safe in a batch line. On other
    platforms the function returns "" and starts no probe process.
    """
    if sys.platform != "win32":
        return ""
    from jacked.service.install_release import format_wait_process
    from jacked.service.instance import process_identity

    try:
        identity = process_identity(parent_pid)
        return format_wait_process(identity.pid, identity.creation_id)
    except Exception as exc:  # noqa: BLE001 - the port check still applies
        logger.warning(
            "Tray updater: the tray process identity is not available (%s); "
            "the release step checks only the port",
            type(exc).__name__,
        )
        return ""


def _spawn_windows_tray_updater(
    parent_pid: int,
    extras: str,
    target_version: "str | None" = None,
    port: int = 8321,
) -> None:
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
        can_auto_upgrade,
        detect_install_method,
        rollback_command,
        rollback_command_label,
        safe_version_label,
        upgrade_command,
        upgrade_command_label,
    )

    # Gate: same refusal as run_update / jacked upgrade.  Prevents
    # ValueError from upgrade_command(pip) crashing the detached helper
    # at batch-script-generation time.
    _ok, _reason = can_auto_upgrade()
    if not _ok:
        logger.warning("Windows tray updater refused: %s", _reason)
        _write_recovery(
            f"Jacked auto-update refused: {_reason}\n\n"
            "Manual recovery:\n"
            '  uv tool install "claude-jacked[tray]" --force\n'
        )
        return

    method = detect_install_method()
    cmd = upgrade_command(extras)

    if method in _TOOL_INSTALL_HINTS:
        resolved_tool = find_bin(method)
        if resolved_tool:
            cmd[0] = resolved_tool

    label = upgrade_command_label(extras)
    # Recovery string goes inside a cmd.exe argument between double quotes;
    # internal " (present in uv's label) would terminate the arg. Swap for '.
    label_for_batch = label.replace('"', "'")
    upgrade_line = " ".join(f'"{arg}"' for arg in cmd)
    to_version = safe_version_label(target_version or "next", fallback="next")
    # Read the tray's creation identity NOW, while the tray is alive: the
    # release step waits for this exact process to exit.
    wait_process = _tray_wait_process(parent_pid)
    # Validate the running version ONCE, before any batch line is built, and
    # use the validated label everywhere below - including the status-init line
    # and every echo. A version that fails the check must never reach cmd.exe.
    running_version = __import__("jacked").__version__
    current_version = safe_version_label(running_version)

    # Rollback argv for the version running right now. Built in Python so the
    # batch never composes a requirement specifier itself. It is built from the
    # RAW version: an unusable one raises here and leaves the batch with no
    # rollback path at all, which is the safe outcome.
    rollback_line = ""
    rollback_label = ""
    try:
        rb_cmd = rollback_command(extras, running_version)
        rollback_label = rollback_command_label(extras, running_version)
    except ValueError as exc:
        logger.warning("No rollback command available: %s", exc)
        rb_cmd = []
    if rb_cmd:
        if method in _TOOL_INSTALL_HINTS:
            rb_tool = find_bin(method)
            if rb_tool:
                rb_cmd[0] = rb_tool
        rollback_line = " ".join(f'"{arg}"' for arg in rb_cmd)
    rollback_label_for_batch = rollback_label.replace('"', "'")

    UPDATE_LOG.parent.mkdir(parents=True, exist_ok=True)
    log_path = str(UPDATE_LOG)

    # Each phase block follows the pattern:
    #   jacked _update_status <phase> in_progress
    #   if errorlevel 1 (... abort — phase-name drift detected)
    #   <work step>
    #   if errorlevel 1 (jacked _update_status <phase> failed ... ; exit 1)
    #   jacked _update_status <phase> ok
    # Status tracking is OBSERVABILITY, not the update itself. A drifted or
    # crashed `_update_status` shim must NEVER abort the update or leave the
    # service unstarted — previously this `exit /b 1`'d, so a single status
    # hiccup could silently kill the whole tray update (service left down, no
    # further log). Log it and keep going; the real work steps (upgrade,
    # install, verify) have their own dedicated error handling below.
    # NEVER redirect a step to %LOGFILE% in THIS batch. cmd.exe is spawned with
    # its stdout already bound to an inherited handle on that same file (see the
    # Popen below), and cmd opens redirection targets with a share mode that
    # collides with an existing writer. Every `>> "%LOGFILE%"` therefore fails
    # with "The process cannot access the file because it is being used by
    # another process" — and cmd SKIPS the command while leaving ERRORLEVEL at 0,
    # so `if errorlevel 1` never fires and the batch marches on marking each
    # phase "ok". That is exactly how a tray update silently no-opped the
    # upgrade, the settings migration AND the service start, leaving the user on
    # the old version with no tray icon at all.
    #
    # Bare output is correct here: it lands on cmd's stdout, which IS the log.
    # %LOGFILE% survives only as text inside the recovery-file messages, and the
    # recovery file itself is a DIFFERENT path that nothing else holds open.
    # An exact compare, so a crashed shim (a negative exit) is logged too.
    DRIFT_GUARD = (
        'if not "%ERRORLEVEL%"=="0" (\r\n'
        "    echo [%date% %time%] WARN: _update_status shim returned non-zero (continuing)\r\n"
        ")\r\n"
    )
    # Rollback path. Reached only by `goto`, from a package step that failed
    # every attempt, a refused preflight, a failed settings migration, or a
    # new service that never bound the port. Flat lines only: nesting
    # parentheses inside a parenthesised block is a cmd.exe parsing trap.
    _FAILED_FILE = '"%USERPROFILE%\\.claude\\jacked-update-failed.txt"'
    # A failed settings migration is a transaction failure like any other: the
    # old tray is already gone, so returning here left the machine on a build
    # whose settings never migrated, with no service and no breadcrumb. Route
    # it into the shared rollback, naming its own step for the failed file.
    package_failed_block = _tray_package_failed_block(
        port=port,
        label=label_for_batch,
        drift_guard=DRIFT_GUARD,
        has_rollback=bool(rollback_line),
    )
    migrate_failed_block = (
        # With a rollback command this is a short entry label that goes to
        # :rollback_now; without one it is a full block in the tail.
        (package_failed_block if rollback_line else "")
        + ":preflight_failed\r\n"
        "echo [%date% %time%] ERROR: jacked service preflight failed for the "
        "new build\r\n"
        "set FAILREASON=the new build refused to provision its service "
        "contract\r\n"
        "goto rollback_now\r\n"
        ":migrate_failed\r\n"
        'jacked _update_status migrating_settings failed --error "jacked '
        'install --force failed" --recovery "jacked install --force"\r\n'
        + DRIFT_GUARD
        + "echo [%date% %time%] ERROR: the settings migration failed for the "
        "new build\r\n"
        "set FAILREASON=the " + ROLLBACK_STEP_INSTALL + " failed\r\n"
        "goto rollback_now\r\n"
    )
    rollback_block = (
        migrate_failed_block
        + ":rollback_now\r\n"
        "echo [%date% %time%] the update cannot finish (%FAILREASON%); "
        "rolling back to v" + current_version + "\r\n"
        "jacked _update_status rolling_back in_progress\r\n"
        + DRIFT_GUARD
    )
    if rollback_line:
        # Every rollback step is checked, and `rolling_back ok` is written ONLY
        # after the restored service answers /api/version. A batch that marked
        # the phase ok before the restart claimed a restoration it had not made.
        rollback_block += (
            rollback_line + " 2>&1\r\n"
            + goto_on_failure("rollback_failed_package")
            + "jacked install --force 2>&1\r\n"
            + goto_on_failure("rollback_failed_install")
            + 'start "" /B jacked service start\r\n'
            + _verify_service_block(port)
            + goto_on_failure("rollback_failed_verify")
            + "jacked _update_status rolling_back ok\r\n"
            + DRIFT_GUARD
            + "echo Jacked tray update to v" + to_version
            + " failed (%FAILREASON%); rolled back to v" + current_version
            + ". See %LOGFILE%. > " + _FAILED_FILE + "\r\n"
            "echo Recovery: " + rollback_label_for_batch
            + " ^&^& jacked install --force ^&^& jacked service start >> "
            + _FAILED_FILE + "\r\n"
            "exit /b 1\r\n"
            + _tray_rollback_failed_label(
                "rollback_failed_package", ROLLBACK_STEP_PACKAGE,
                rollback_label_for_batch, DRIFT_GUARD,
            )
            + _tray_rollback_failed_label(
                "rollback_failed_install", ROLLBACK_STEP_INSTALL,
                "jacked install --force", DRIFT_GUARD,
            )
            + _tray_rollback_failed_label(
                "rollback_failed_verify", ROLLBACK_STEP_RESTART,
                "jacked service start", DRIFT_GUARD,
            )
        )
    else:
        rollback_block += (
            'jacked _update_status rolling_back failed --error "no rollback command available" '
            '--recovery "jacked install --force"\r\n'
            + DRIFT_GUARD
            + "echo Jacked tray update failed (%FAILREASON%) and no rollback "
            "command is available. See %LOGFILE%. > " + _FAILED_FILE + "\r\n"
            "exit /b 1\r\n"
        )

    batch_body = (
        "@echo off\r\n"
        "set LOGFILE=" + log_path + "\r\n"
        # Default reason for the rollback breadcrumb. Every entry point into
        # :rollback_now that has a more specific one overwrites this first.
        "set FAILREASON=the new version could not start\r\n"
        "echo [%date% %time%] tray update helper starting (parent PID "
        + str(parent_pid)
        + ", method "
        + method
        + ")\r\n"
        "echo [%date% %time%] upgrade command: " + label + "\r\n"
        'jacked _update_status_init "'
        + current_version
        + '" "'
        + to_version
        + '" '
        + method
        + ' --log-path "'
        + log_path
        + '"\r\n'
        "if errorlevel 2 (\r\n"
        '    echo Another jacked updater is already in progress. Aborting. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        "    exit /b 2\r\n"
        ")\r\n"
        # Phase: waiting_for_parent
        "jacked _update_status waiting_for_parent in_progress\r\n"
        + DRIFT_GUARD
        + wait_for_parent_block(parent_pid)
        + "echo [%date% %time%] parent exited; releasing the install\r\n"
        # The parent is the tray, and the tray stops its own service before
        # it exits. This step still proves, through the owned-instance
        # manifest, the port, and the tray's own creation identity, that no
        # service holds the tool venv: a service that is still alive locks
        # venv files, and uv then deletes half the venv and stops with
        # "Access is denied". The tray deletes its manifest before its
        # interpreter exits, and the parent wait above gives up after 120 s,
        # so the identity wait is what proves the tray is gone. Any non-zero
        # exit goes to :release_failed, which starts the previous service
        # again.
        + release_install_line(port=port, wait_process=wait_process)
        + "if errorlevel 1 goto release_failed\r\n"
        # "if errorlevel 1" is "at least 1", so a negative NTSTATUS exit (a
        # crash) passes it. No command runs between the release line and
        # this line, so ERRORLEVEL is still the release command's.
        + 'if not "%ERRORLEVEL%"=="0" goto release_failed\r\n'
        "jacked _update_status waiting_for_parent ok\r\n"
        + DRIFT_GUARD
        + "echo [%date% %time%] install released\r\n"
        # Phase: installing_package. Retried, then rolled back: a failed
        # attempt can already have deleted part of the venv.
        "jacked _update_status installing_package in_progress\r\n"
        + DRIFT_GUARD
        + package_retry_block(upgrade_line, "package_failed")
        + "jacked _update_status installing_package ok\r\n"
        + DRIFT_GUARD
        +
        # Phase: preflight - the transaction gate. The new build must prove it
        # can provision its service contract before the old service is gone.
        "jacked _update_status preflight in_progress\r\n"
        + DRIFT_GUARD
        + "jacked service preflight --timeout 120 2>&1\r\n"
        + goto_on_failure("preflight_failed")
        + "jacked _update_status preflight ok\r\n"
        + DRIFT_GUARD
        +
        # Phase: migrating_settings
        "jacked _update_status migrating_settings in_progress\r\n"
        + DRIFT_GUARD
        + "jacked install --force 2>&1\r\n"
        + goto_on_failure("migrate_failed")
        + "jacked _update_status migrating_settings ok\r\n"
        + DRIFT_GUARD
        +
        # Phase: waiting_port_free
        "jacked _update_status waiting_port_free in_progress\r\n"
        + DRIFT_GUARD
        + _SLEEP_1S
        + "jacked _update_status waiting_port_free ok\r\n"
        + DRIFT_GUARD
        +
        # Phase: starting_service
        "jacked _update_status starting_service in_progress\r\n"
        + DRIFT_GUARD
        + 'start "" /B jacked service start\r\n'
        "jacked _update_status starting_service ok\r\n"
        + DRIFT_GUARD
        +
        # Phase: verifying_service
        "jacked _update_status verifying_service in_progress\r\n"
        + DRIFT_GUARD
        + _verify_service_block(port)
        +
        # One retry before declaring defeat. A tray update that ends with NO
        # service is the worst possible outcome — the user loses the icon and
        # every entry point to fix it. Re-issuing `service start` is safe: the
        # port-bind guard makes a second instance a no-op if one is already up.
        # Plain labels, not a nested `if (...)` block: the verify line is a
        # powershell one-liner stuffed with parentheses, and burying it inside a
        # parenthesised block is exactly the kind of cmd.exe parsing trap that
        # produced this bug in the first place.
        # An exact compare, not "if not errorlevel 1": a crashed verify (a
        # negative exit) is below 1 and would read as success.
        'if "%ERRORLEVEL%"=="0" goto verifyok\r\n'
        "echo [%date% %time%] verify failed; retrying service start once\r\n"
        'start "" /B jacked service start\r\n'
        + _verify_service_block(port)
        + ":verifyok\r\n"
        # Neither a label nor a skipped line changes ERRORLEVEL, so this
        # reads the exit code of the last verify that ran.
        'if not "%ERRORLEVEL%"=="0" (\r\n'
        '    jacked _update_status verifying_service failed --error "service did not bind :'
        + str(port)
        + f' in {VERIFY_TIMEOUT_SECONDS:g}s" --recovery "jacked service start"\r\n'
        '    echo Jacked tray update: service did not come up. See %LOGFILE%. > "%USERPROFILE%\\.claude\\jacked-update-failed.txt"\r\n'
        "    set FAILREASON=the new service never bound the port\r\n"
        "    goto rollback_now\r\n"
        ")\r\n"
        "jacked _update_status verifying_service ok\r\n"
        + DRIFT_GUARD
        + "jacked _update_status_succeed\r\n"
        # A clean update retires the breadcrumb an earlier failure left behind,
        # so the next tray boot never warns about a repaired failure.
        + "if exist " + _FAILED_FILE + " del " + _FAILED_FILE + "\r\n"
        + "echo [%date% %time%] tray update complete\r\n"
        '(goto) 2>nul & del "%~f0"\r\n'
        + rollback_block
        + ("" if rollback_line else package_failed_block)
        + _tray_release_failed_block(port, to_version, DRIFT_GUARD)
    )

    fd, batch_path = tempfile.mkstemp(suffix=".bat", prefix="jacked-tray-update-")
    try:
        # newline="" — batch_body already carries explicit \r\n. Translating on
        # top of that wrote \r\r\n; cmd.exe tolerates the stray CR but nothing
        # here should depend on that.
        with os.fdopen(fd, "w", newline="") as f:
            f.write(batch_body)
    except Exception:
        try:
            os.unlink(batch_path)
        except OSError:
            pass
        raise

    # CREATE_NO_WINDOW, NOT DETACHED_PROCESS. This batch is spawned by the tray,
    # which runs under the windowless pythonw.exe — so there's no console to
    # inherit. Under DETACHED_PROCESS every console child (the `tasklist | find`
    # + `timeout` poll loop running once a second, the `jacked _update_status`
    # shims, the uv upgrade, and the powershell verify loop) auto-allocates its
    # own visible console window. CREATE_NO_WINDOW hands cmd.exe a hidden console
    # the whole batch shares, so nothing pops. CREATE_BREAKAWAY_FROM_JOB lets the
    # helper outlive the tray/job dying; fall back to CREATE_NO_WINDOW alone if
    # the job forbids breakaway.
    NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    BREAKAWAY = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    # Capture the batch's stdout/stderr into the update log. Previously these
    # were DEVNULL, so if the batch — or any `jacked` subcommand it calls —
    # crashed or exited non-zero OUTSIDE a `>> %LOGFILE%`-redirected step, it
    # died SILENTLY (exactly what made this failure impossible to diagnose: the
    # log stopped dead after the two opening echoes). cmd.exe inherits this
    # handle and keeps writing to it after the tray parent exits.
    try:
        _lf = open(UPDATE_LOG, "a", encoding="utf-8", errors="replace")
    except OSError:
        _lf = subprocess.DEVNULL
    _kwargs = dict(
        stdin=subprocess.DEVNULL,
        stdout=_lf,
        stderr=subprocess.STDOUT,
        close_fds=True,
    )
    try:
        try:
            subprocess.Popen(
                ["cmd.exe", "/c", batch_path],
                creationflags=NO_WINDOW | BREAKAWAY,
                **_kwargs,
            )
        except OSError:
            subprocess.Popen(
                ["cmd.exe", "/c", batch_path],
                creationflags=NO_WINDOW,
                **_kwargs,
            )
    finally:
        # cmd.exe holds its own inherited copy of the handle; close ours.
        if _lf is not subprocess.DEVNULL:
            try:
                _lf.close()
            except OSError:
                pass


def _cli() -> None:
    """Entry point for `python -m jacked.service.updater <pid> [extras] [--target-version V] [--port P]`."""
    import argparse

    ap = argparse.ArgumentParser(prog="python -m jacked.service.updater")
    ap.add_argument("parent_pid", type=int)
    ap.add_argument("extras", nargs="?", default="tray")
    ap.add_argument("--target-version", default=None)
    ap.add_argument("--port", type=int, default=8321)
    try:
        args = ap.parse_args()
    except SystemExit:
        sys.exit(2)
    target = args.target_version or None  # empty string -> None
    run_update(args.parent_pid, args.extras, target_version=target, port=args.port)


if __name__ == "__main__":
    _cli()
