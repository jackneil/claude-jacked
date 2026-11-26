"""One short probe process that proves the service runtime can boot.

``jacked service preflight`` is the upgrade transaction gate. Provisioning
alone does not prove that the service can start. On Windows a service
environment with no SystemRoot provisioned correctly, but the service
stopped at ``import asyncio`` with WinError 10106. The probe closes that gap.

The probe goes through the launch shape that the supervisors use:

- On Windows, a probe launcher from ``render_windows_launcher`` runs in
  ``powershell.exe -File``, as Task Scheduler runs the real launcher. So
  PowerShell decodes the environment, as it does for the service.
- On POSIX, ``posix_preinterpreter_command`` runs the probe through
  ``/usr/bin/env -i`` and the versioned launcher, as launchd and systemd do.

The probe has these steps, in this order:

1. ``asyncio_event_loop``: make and close an asyncio event loop.
2. ``socket``: open and close a socket.
3. ``ssl``: make the default TLS context (this reads the trust store).
4. ``tempfile``: find the temporary directory. It must not be the current
   directory.
5. ``tray_import``: import ``jacked.service.tray``.
6. ``tray_backend``: do the checks that the real start does. uvicorn must
   be available. The menu-bar backend must be available, except on a Linux
   host with no display.
7. ``cli_import``: import ``jacked.cli``.
8. ``api_import``: import ``jacked.api.main``. Its import opens no
   database, starts no thread, writes no file and makes no connection.

The probe starts no service and changes no supervisor. It reports only step
names, exception type names, module names and error numbers. It never
reports an environment value.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jacked.service.environment import (
    posix_preinterpreter_command,
    render_windows_launcher,
)

#: How long the preflight gate may run before it counts as a refusal. A
#: build that cannot answer in two minutes cannot be trusted with the old
#: service. Each upgrade path gives the command this ``--timeout`` budget, it
#: is the command's default, and the Windows batches write the same number.
PREFLIGHT_TIMEOUT_SECONDS = 120
#: The caller waits this much longer than the budget, so the command reports
#: its own named failure before the caller's timeout stops it.
PREFLIGHT_EXIT_GRACE_SECONDS = 30


def preflight_argv(jacked: str) -> list[str]:
    """Return the preflight command line that the upgrade paths run.

    >>> preflight_argv("jacked")
    ['jacked', 'service', 'preflight', '--timeout', '120']
    """
    return [jacked, "service", "preflight", "--timeout", str(PREFLIGHT_TIMEOUT_SECONDS)]


#: The probe steps, in the order that the probe runs them.
PROBE_STEPS = (
    "asyncio_event_loop",
    "socket",
    "ssl",
    "tempfile",
    "tray_import",
    "tray_backend",
    "cli_import",
    "api_import",
)
#: A failed step exits with this code plus its index. The exit code is the
#: verdict, because a windowless pythonw.exe can lose its stdout. Codes 40 to
#: 63 are free: Python uses 1 and 2, sysexits starts at 64 (EX_TEMPFAIL, 75,
#: tells a supervisor to restart), and shells use 126 and higher.
PROBE_EXIT_BASE = 40
#: The name of the file that holds the stderr of the last failed probe.
PROBE_STDERR_NAME = "preflight-probe-stderr.log"

STEP_MARK = "jacked-probe-step:"
FAIL_MARK = "jacked-probe-fail:"
OK_MARK = "jacked-probe-ok"

_PRELUDE = r'''
import os
import sys


class ProbeRefusal(Exception):
    """A check of the real start failed. args[0] is a fixed token."""


def _say(line):
    # pythonw.exe can start with no usable stdout. The exit code carries
    # the verdict, so a lost marker line never changes the result.
    try:
        print(line, flush=True)
    except Exception:
        pass


def _detail(exc):
    detail = type(exc).__name__
    if isinstance(exc, ProbeRefusal):
        return detail + " " + str(exc.args[0])
    if isinstance(exc, ImportError) and exc.name:
        detail += " module " + exc.name
    winerror = getattr(exc, "winerror", None)
    number = getattr(exc, "errno", None)
    if isinstance(winerror, int):
        detail += " winerror " + str(winerror)
    elif isinstance(number, int):
        detail += " errno " + str(number)
    return detail


def _step(code, name, action):
    _say(STEP_MARK + name)
    try:
        action()
    except BaseException as exc:
        _say(FAIL_MARK + name + ":" + _detail(exc))
        sys.exit(code)


def asyncio_event_loop():
    import asyncio

    asyncio.new_event_loop().close()


def socket():
    import socket

    socket.socket().close()


def ssl():
    import ssl

    ssl.create_default_context()


def tempfile():
    import tempfile

    here = os.path.normcase(os.path.abspath(os.getcwd()))
    temp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    if temp == here:
        raise ProbeRefusal("temp_is_cwd")


def tray_import():
    import jacked.service.tray  # noqa: F401


def tray_backend():
    # The checks of ServiceRunner.run, in its order.
    import jacked.service.tray as tray

    if tray.select_menubar_backend(sys.platform, tray._mac_menubar_available()) != "mac":
        headless = sys.platform.startswith("linux") and not (
            os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
        )
        if not tray._TRAY_AVAILABLE and not headless:
            raise ProbeRefusal("tray_backend_missing")
    if not tray._UVICORN_AVAILABLE:
        raise ProbeRefusal("uvicorn_missing")


def cli_import():
    import jacked.cli  # noqa: F401


def api_import():
    import jacked.api.main  # noqa: F401
'''


def _probe_code() -> str:
    """Build the probe code from the step and marker constants."""
    lines = [
        f"STEP_MARK = {STEP_MARK!r}",
        f"FAIL_MARK = {FAIL_MARK!r}",
        _PRELUDE,
    ]
    for index, step in enumerate(PROBE_STEPS):
        lines.append(f"_step({PROBE_EXIT_BASE + index}, {step!r}, {step})")
    lines.append(f"_say({OK_MARK!r})")
    return "\n".join(lines) + "\n"


PROBE_CODE = _probe_code()


@dataclass(frozen=True)
class ProbeResult:
    """The probe outcome. ``step`` names the failed step, else it is None.

    ``stderr_file`` is the path of the private file that holds the stderr of
    a failed probe, when the probe wrote one.
    """

    ok: bool
    step: str | None = None
    detail: str = ""
    stderr_file: str | None = None


@dataclass(frozen=True)
class ProbeOptions:
    """How to run the probe. ``code`` and ``runner`` are test seams."""

    timeout: float
    code: str = PROBE_CODE
    runner: Any = None


class ServiceProbeError(RuntimeError):
    """The service runtime failed its start probe. The text starts with the step."""

    def __init__(self, result: ProbeResult):
        self.result = result
        text = f"at {result.step}: {result.detail}. The service runtime failed its start probe."
        if result.stderr_file:
            text += f" Its stderr is in {result.stderr_file}"
        super().__init__(text)


def _last_step(stdout: str) -> str | None:
    steps = [
        line[len(STEP_MARK):].strip()
        for line in stdout.splitlines()
        if line.startswith(STEP_MARK)
    ]
    return steps[-1] if steps else None


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return value


def parse_probe_output(returncode: int, stdout: str) -> ProbeResult:
    """Read the probe result. The exit code is the verdict.

    Exit code 0 is a pass. ``PROBE_EXIT_BASE`` plus a step index names the
    failed step. The marker lines on stdout only add the detail.

    >>> parse_probe_output(0, "")
    ProbeResult(ok=True, step=None, detail='', stderr_file=None)
    >>> parse_probe_output(41, "jacked-probe-fail:socket:OSError winerror 10106\\n").detail
    'OSError winerror 10106'
    >>> parse_probe_output(40, "").detail
    'exit 40'
    >>> parse_probe_output(-11, "jacked-probe-step:ssl\\n").step
    'ssl'
    """
    if returncode == 0:
        return ProbeResult(True)
    index = returncode - PROBE_EXIT_BASE
    if 0 <= index < len(PROBE_STEPS):
        step = PROBE_STEPS[index]
        detail = f"exit {returncode}"
        prefix = f"{FAIL_MARK}{step}:"
        for line in stdout.splitlines():
            if line.startswith(prefix):
                detail = line[len(prefix):].strip() or detail
        return ProbeResult(False, step, detail)
    step = _last_step(stdout) or "interpreter_start"
    return ProbeResult(False, step, f"exit {returncode}")


def _os_error_detail(exc: OSError) -> str:
    detail = type(exc).__name__
    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int):
        return f"{detail} winerror {winerror}"
    if isinstance(exc.errno, int):
        return f"{detail} errno {exc.errno}"
    return detail


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with owner-only access (mode 0600)."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(text)


def _run(argv: list[str], cwd: Path, options: ProbeOptions):
    if options.runner is not None:
        return options.runner(argv, timeout=options.timeout, cwd=str(cwd))
    from jacked.proctree import run_tree

    return run_tree(argv, timeout=options.timeout, capture=True, cwd=str(cwd))


def _windows_argv(spec, environment, cwd: Path, code: str) -> tuple[list[str], Path]:
    from jacked.winproc import system_program

    script = render_windows_launcher(
        runtime=spec.runtime_path, argv=("-I", "-c", code), environment=environment
    )
    descriptor, name = tempfile.mkstemp(prefix="jacked-probe-", suffix=".ps1", dir=cwd)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(script.encode("ascii"))
    argv = [
        system_program("powershell"),
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        name,
        "-Generation",
        spec.generation,
    ]
    return argv, Path(name)


def probe_argv(spec, environment: dict[str, str], cwd: Path, code: str):
    """Return ``(argv, temporary_file)`` for the delivered launch shape.

    ``temporary_file`` is the Windows probe launcher (the caller removes it),
    or None on POSIX.
    """
    if sys.platform == "win32":
        return _windows_argv(spec, environment, cwd, code)
    command = posix_preinterpreter_command(
        runtime=spec.runtime_path,
        argv=("-I", "-c", code),
        environment={**environment, "JACKED_SERVICE_GENERATION": spec.generation},
        launcher=spec.launcher_path,
        runtime_target=spec.runtime_target_path,
    )
    return list(command), None


def run_service_probe(
    spec, environment: dict[str, str], cwd: Path, options: ProbeOptions
) -> ProbeResult:
    """Run the probe once, through the delivered launch shape.

    The probe environment is ``environment`` plus
    ``JACKED_SERVICE_GENERATION``, as the supervisors give it. Nothing comes
    from ``os.environ``. A failed probe writes its stderr to the private
    file ``cwd / PROBE_STDERR_NAME``.
    """
    stderr_path = Path(cwd) / PROBE_STDERR_NAME
    temporary = None
    try:
        argv, temporary = probe_argv(spec, environment, Path(cwd), options.code)
        completed = _run(argv, Path(cwd), options)
    except subprocess.TimeoutExpired as exc:
        stdout = _text(exc.stdout)
        step = "interpreter_exit" if OK_MARK in stdout.splitlines() else (
            _last_step(stdout) or "interpreter_start"
        )
        result = ProbeResult(False, step, f"timeout after {options.timeout:g}s")
        return _with_stderr(result, _text(exc.stderr), stderr_path)
    except OSError as exc:
        return ProbeResult(False, "interpreter_start", _os_error_detail(exc))
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    result = parse_probe_output(completed.returncode, _text(completed.stdout))
    if result.ok:
        stderr_path.unlink(missing_ok=True)
        return result
    return _with_stderr(result, _text(completed.stderr), stderr_path)


def _with_stderr(result: ProbeResult, stderr: str, path: Path) -> ProbeResult:
    try:
        _write_private(path, stderr)
    except OSError:
        return result
    return ProbeResult(result.ok, result.step, result.detail, str(path))


__all__ = [
    "PREFLIGHT_EXIT_GRACE_SECONDS",
    "PREFLIGHT_TIMEOUT_SECONDS",
    "preflight_argv",
    "FAIL_MARK",
    "OK_MARK",
    "PROBE_CODE",
    "PROBE_EXIT_BASE",
    "PROBE_STDERR_NAME",
    "PROBE_STEPS",
    "STEP_MARK",
    "ProbeOptions",
    "ProbeResult",
    "ServiceProbeError",
    "parse_probe_output",
    "probe_argv",
    "run_service_probe",
]
