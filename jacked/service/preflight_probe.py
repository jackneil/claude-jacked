"""One short probe process that proves the service runtime can boot.

``jacked service preflight`` is the upgrade transaction gate. Provisioning
alone does not prove that the service can start: on Windows a service
environment with no SystemRoot provisioned correctly, but the service died at
``import asyncio`` with WinError 10106. The probe closes that gap.

The probe runs the exact runtime of the spec with the exact service
environment (the mapping that the launcher and ``spawn_exact_service`` use,
plus ``JACKED_SERVICE_GENERATION``). It runs ``-I -c`` code that does what the
service does at start: it creates and closes an asyncio event loop, opens a
socket, makes a TLS context, resolves the temporary directory, and imports
``jacked.service.tray``. The probe starts no service and changes no
supervisor. It reports only step names, exception type names and error
numbers. It never reports an environment value.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from jacked.winproc import NO_WINDOW

#: How long the probe may run. The upgrade helper gives the whole preflight
#: 120 seconds, and the probe imports take under one second on a normal host.
PROBE_TIMEOUT_SECONDS = 45.0

#: The probe steps, in the order that the probe runs them.
PROBE_STEPS = ("asyncio_event_loop", "socket", "ssl", "tempfile", "tray_import")
#: The probe exits with this code plus the step index when a step fails.
#: The exit code is the verdict. The marker lines on stdout give the detail.
PROBE_EXIT_BASE = 70

_STEP_MARK = "jacked-probe-step:"
_FAIL_MARK = "jacked-probe-fail:"

# The probe code must not hold the words that the test tripwire reads as a
# service lifecycle command (" service start" and the like). The module path
# below has no spaces, so it does not match.
PROBE_CODE = r'''
import sys


def _say(line):
    # pythonw.exe can start with no usable stdout. The exit code carries the
    # verdict, so a lost marker line never changes the result.
    try:
        print(line, flush=True)
    except Exception:
        pass


def _step(code, name, action):
    _say("jacked-probe-step:" + name)
    try:
        action()
    except BaseException as exc:
        detail = type(exc).__name__
        number = getattr(exc, "winerror", None) or getattr(exc, "errno", None)
        if isinstance(number, int):
            detail += " " + str(number)
        _say("jacked-probe-fail:" + name + ":" + detail)
        sys.exit(code)


def _event_loop():
    import asyncio

    asyncio.new_event_loop().close()


def _socket():
    import socket

    socket.socket().close()


def _ssl():
    import ssl

    ssl.create_default_context()


def _tempfile():
    import os
    import tempfile

    here = os.path.normcase(os.path.abspath(os.getcwd()))
    temp = os.path.normcase(os.path.abspath(tempfile.gettempdir()))
    if temp == here:
        raise RuntimeError("the temporary directory is the current directory")


def _tray():
    import jacked.service.tray  # noqa: F401


_step(70, "asyncio_event_loop", _event_loop)
_step(71, "socket", _socket)
_step(72, "ssl", _ssl)
_step(73, "tempfile", _tempfile)
_step(74, "tray_import", _tray)
_say("jacked-probe-ok")
'''


@dataclass(frozen=True)
class ProbeResult:
    """The probe outcome. ``step`` names the failed step, else it is None."""

    ok: bool
    step: str | None = None
    detail: str = ""


class ServiceProbeError(RuntimeError):
    """The service runtime failed its start probe."""

    def __init__(self, result: ProbeResult):
        self.result = result
        super().__init__(
            f"the service runtime failed its start probe at {result.step}: "
            f"{result.detail}"
        )


def _last_step(stdout: str) -> str | None:
    steps = [
        line[len(_STEP_MARK):].strip()
        for line in stdout.splitlines()
        if line.startswith(_STEP_MARK)
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
    failed step. The marker lines on stdout only add the detail, because a
    windowless runtime can lose its stdout.

    >>> parse_probe_output(0, "")
    ProbeResult(ok=True, step=None, detail='')
    >>> parse_probe_output(71, "jacked-probe-step:socket\\n"
    ...                        "jacked-probe-fail:socket:OSError 10106\\n")
    ProbeResult(ok=False, step='socket', detail='OSError 10106')
    >>> parse_probe_output(70, "")
    ProbeResult(ok=False, step='asyncio_event_loop', detail='exit 70')
    >>> parse_probe_output(-11, "jacked-probe-step:ssl\\n")
    ProbeResult(ok=False, step='ssl', detail='exit -11')
    """
    if returncode == 0:
        return ProbeResult(True)
    index = returncode - PROBE_EXIT_BASE
    if 0 <= index < len(PROBE_STEPS):
        step = PROBE_STEPS[index]
        detail = f"exit {returncode}"
        for line in stdout.splitlines():
            if line.startswith(f"{_FAIL_MARK}{step}:"):
                detail = line[len(_FAIL_MARK) + len(step) + 1:].strip() or detail
        return ProbeResult(False, step, detail)
    step = _last_step(stdout) or "interpreter_start"
    return ProbeResult(False, step, f"exit {returncode}")


def run_service_probe(
    *,
    runtime: str,
    environment: Mapping[str, str],
    generation: str,
    cwd: Path,
    timeout: float = PROBE_TIMEOUT_SECONDS,
    code: str = PROBE_CODE,
    run: Callable[..., subprocess.CompletedProcess] | None = None,
) -> ProbeResult:
    """Run the probe once and return its result.

    The probe environment is ``environment`` plus ``JACKED_SERVICE_GENERATION``,
    as ``spawn_exact_service`` and the Windows launcher give it. Nothing comes
    from ``os.environ``. ``code`` and ``run`` are test seams.
    """

    runner = subprocess.run if run is None else run
    child_environment = {**environment, "JACKED_SERVICE_GENERATION": generation}
    try:
        completed = runner(
            [runtime, "-I", "-c", code],
            env=child_environment,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout,
            check=False,
            creationflags=NO_WINDOW,
        )
    except subprocess.TimeoutExpired as exc:
        step = _last_step(_text(exc.stdout)) or "interpreter_start"
        return ProbeResult(False, step, f"timeout after {timeout:g}s")
    except OSError as exc:
        return ProbeResult(False, "interpreter_start", type(exc).__name__)
    return parse_probe_output(completed.returncode, completed.stdout or "")


__all__ = [
    "PROBE_CODE",
    "PROBE_EXIT_BASE",
    "PROBE_STEPS",
    "PROBE_TIMEOUT_SECONDS",
    "ProbeResult",
    "ServiceProbeError",
    "parse_probe_output",
    "run_service_probe",
]
