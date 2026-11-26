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
   be available. A missing tray package (pystray or Pillow) is a broken
   build, and the probe refuses it on every platform. When pystray is
   installed but cannot load on this host (no display, a missing typelib),
   the probe does not refuse, because the old build fails the same way.
   The result then holds a note that names the error type.
7. ``cli_import``: import ``jacked.cli``.
8. ``api_import``: import ``jacked.api.main``. Its import opens no
   database, starts no thread, writes no file and makes no connection.

The probe starts no service and changes no supervisor. It reports only step
names, exception type names, module names and error numbers. It never
reports an environment value.
"""

from __future__ import annotations

import base64
import logging
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
# The probe program and its markers live in a sibling module; re-exported here.
from jacked.service.probe_code import (
    FAIL_MARK,
    NOTE_MARK,
    OK_MARK,
    PROBE_CODE,
    PROBE_EXIT_BASE,
    PROBE_STEPS,
    STEP_MARK,
)

logger = logging.getLogger(__name__)

#: How long the preflight gate may run before it counts as a refusal. A
#: build that cannot answer in two minutes cannot be trusted with the old
#: service. Each upgrade path gives the command this ``--timeout`` budget, it
#: is the command's default, and the Windows batches write the same number.
PREFLIGHT_TIMEOUT_SECONDS = 120
#: The caller waits this much longer than the budget, so the command reports
#: its own named failure before the caller's timeout stops it.
PREFLIGHT_EXIT_GRACE_SECONDS = 30
#: The budget of ``jacked service preflight`` with no ``--timeout``. A 0.111.x
#: upgrade path runs the command with no ``--timeout`` and stops it after its
#: own 120 seconds, counted from the spawn. The command's clock starts after
#: ``jacked.cli`` imports, which on a cold Windows host takes seconds, and the
#: probe tree stop takes more. So the default ends 20 seconds before that,
#: and the command stops its probe and reports before the old caller kills
#: it (which would leave the probe process behind). The new callers pass
#: ``--timeout``.
PREFLIGHT_DEFAULT_BUDGET_SECONDS = PREFLIGHT_TIMEOUT_SECONDS - 20


def preflight_argv(jacked: str) -> list[str]:
    """Return the preflight command line that the upgrade paths run.

    >>> preflight_argv("jacked")
    ['jacked', 'service', 'preflight', '--timeout', '120']
    """
    return [jacked, "service", "preflight", "--timeout", str(PREFLIGHT_TIMEOUT_SECONDS)]


#: The name of the file that holds the stderr of the last failed probe.
PROBE_STDERR_NAME = "preflight-probe-stderr.log"
#: The name of the file that the probe writes its markers and traceback to.
PROBE_REPORT_NAME = "preflight-probe-report.log"


@dataclass(frozen=True)
class ProbeResult:
    """The probe outcome. ``step`` names the failed step, else it is None.

    ``stderr_file`` is the path of the private file that holds the stderr of
    a failed probe, when the probe wrote one. ``note`` is a fact about this
    host that is not a build defect (for example, the tray cannot load here).
    """

    ok: bool
    step: str | None = None
    detail: str = ""
    stderr_file: str | None = None
    note: str | None = None


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
    ProbeResult(ok=True, step=None, detail='', stderr_file=None, note=None)
    >>> parse_probe_output(41, "jacked-probe-fail:socket:OSError winerror 10106\\n").detail
    'OSError winerror 10106'
    >>> parse_probe_output(40, "").detail
    'exit 40'
    >>> parse_probe_output(-11, "jacked-probe-step:ssl\\n").step
    'ssl'
    """
    notes = [line[len(NOTE_MARK):].strip() for line in stdout.splitlines() if line.startswith(NOTE_MARK)]
    note = notes[0] if notes else None
    if returncode == 0:
        return ProbeResult(True, note=note)
    index = returncode - PROBE_EXIT_BASE
    if 0 <= index < len(PROBE_STEPS):
        step = PROBE_STEPS[index]
        detail = f"exit {returncode}"
        prefix = f"{FAIL_MARK}{step}:"
        for line in stdout.splitlines():
            if line.startswith(prefix):
                detail = line[len(prefix):].strip() or detail
        return ProbeResult(False, step, detail, note=note)
    step = _last_step(stdout) or "interpreter_start"
    return ProbeResult(False, step, f"exit {returncode}", note=note)


def _os_error_detail(exc: OSError) -> str:
    detail = type(exc).__name__
    winerror = getattr(exc, "winerror", None)
    if isinstance(winerror, int):
        return f"{detail} winerror {winerror}"
    if isinstance(exc.errno, int):
        return f"{detail} errno {exc.errno}"
    return detail


def _secure(path: Path) -> None:
    """Give ``path`` owner-only access: mode 0600, or the private ACL on Windows."""
    if os.name == "nt":
        from jacked.service.instance_storage import _secure_windows_path

        _secure_windows_path(path)
    else:
        os.chmod(path, 0o600)


def _write_private(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` with owner-only access."""
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", errors="replace") as handle:
        handle.write(text)
    _secure(path)


def _run(argv: list[str], cwd: Path, options: ProbeOptions):
    if options.runner is not None:
        return options.runner(argv, timeout=options.timeout, cwd=str(cwd))
    from jacked.proctree import run_tree

    return run_tree(argv, timeout=options.timeout, capture=True, cwd=str(cwd))


def _windows_argv(spec, environment, cwd: Path, argv: tuple[str, ...]):
    from jacked.winproc import system_program

    script = render_windows_launcher(
        runtime=spec.runtime_path, argv=argv, environment=environment
    )
    descriptor, name = tempfile.mkstemp(prefix="jacked-probe-", suffix=".ps1", dir=cwd)
    with os.fdopen(descriptor, "wb") as handle:
        handle.write(script.encode("ascii"))
    _secure(Path(name))
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


def probe_python_argv(code: str, report: Path | None = None) -> tuple[str, ...]:
    """Return the interpreter argv that runs ``code``, on one line.

    Every launcher refuses a value with a newline, and the probe code has
    many lines. So the code goes as base64 of its UTF-8 bytes, and a
    one-line ``-c`` program decodes and runs it. The Windows and the POSIX
    probe paths both use this helper, so both run the same bytes.

    >>> argv = probe_python_argv("print(1)\\nprint(2)\\n")
    >>> argv[:2], "\\n" in argv[2]
    (('-I', '-c'), False)
    """
    encoded = base64.b64encode(code.encode("utf-8")).decode("ascii")
    program = (
        "import base64;exec(compile(base64.b64decode('"
        + encoded
        + "'),'<jacked-probe>','exec'))"
    )
    return ("-I", "-c", program, *((str(report),) if report else ()))


def probe_argv(
    spec, environment: dict[str, str], cwd: Path, code: str, report: Path | None = None
):
    """Return ``(argv, temporary_file)`` for the delivered launch shape.

    ``report`` is the file that the probe writes its markers to (its argv[1]).
    ``temporary_file`` is the Windows probe launcher (the caller removes it),
    or None on POSIX.
    """
    python_argv = probe_python_argv(code, report)
    if sys.platform == "win32":
        return _windows_argv(spec, environment, cwd, python_argv)
    command = posix_preinterpreter_command(
        runtime=spec.runtime_path,
        argv=python_argv,
        environment={**environment, "JACKED_SERVICE_GENERATION": spec.generation},
        launcher=spec.launcher_path,
        runtime_target=spec.runtime_target_path,
    )
    return list(command), None


def _remove(path: Path) -> None:
    """Remove ``path``. A failure is logged, never raised.

    On Windows a killed probe child can still hold the file open (with no
    FILE_SHARE_DELETE), so the removal can fail with PermissionError.
    """
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("could not remove %s: %s", path.name, type(exc).__name__)


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
    report = Path(cwd) / PROBE_REPORT_NAME
    temporary = None
    try:
        _write_private(report, "")
        argv, temporary = probe_argv(spec, environment, Path(cwd), options.code, report)
        completed = _run(argv, Path(cwd), options)
    except subprocess.TimeoutExpired as exc:
        output = _report_text(report) + _text(exc.stdout)
        step = "interpreter_exit" if OK_MARK in output.splitlines() else (
            _last_step(output) or "interpreter_start"
        )
        result = ProbeResult(False, step, f"timeout after {options.timeout:g}s")
        return _with_stderr(result, _trace(output) + _text(exc.stderr), stderr_path)
    except OSError as exc:
        return ProbeResult(False, "interpreter_start", _os_error_detail(exc))
    except ValueError as exc:
        # A launcher that refuses to render (a value it cannot carry) is a
        # named refusal, never a traceback.
        return ProbeResult(False, "interpreter_start", f"ValueError: {exc}")
    finally:
        # Every path removes the report; the normal path reads it here first.
        if temporary is not None:
            _remove(temporary)
        output_before = _report_text(report)
        _remove(report)
    output = output_before + _text(completed.stdout)
    result = parse_probe_output(completed.returncode, output)
    if result.ok:
        stderr_path.unlink(missing_ok=True)
        return result
    return _with_stderr(result, _trace(output) + _text(completed.stderr), stderr_path)


def _report_text(report: Path) -> str:
    try:
        return report.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _trace(output: str) -> str:
    """The lines of the probe report that are not marker lines (the traceback)."""
    marks = (STEP_MARK, FAIL_MARK, OK_MARK, NOTE_MARK)
    return "".join(line for line in output.splitlines(True) if not line.startswith(marks))


def _with_stderr(result: ProbeResult, stderr: str, path: Path) -> ProbeResult:
    try:
        _write_private(path, stderr)
    except OSError:
        return result
    return ProbeResult(result.ok, result.step, result.detail, str(path), result.note)


__all__ = [
    "PREFLIGHT_DEFAULT_BUDGET_SECONDS",
    "PREFLIGHT_EXIT_GRACE_SECONDS",
    "PREFLIGHT_TIMEOUT_SECONDS",
    "preflight_argv",
    "FAIL_MARK",
    "NOTE_MARK",
    "OK_MARK",
    "PROBE_CODE",
    "PROBE_EXIT_BASE",
    "PROBE_REPORT_NAME",
    "PROBE_STDERR_NAME",
    "PROBE_STEPS",
    "STEP_MARK",
    "ProbeOptions",
    "ProbeResult",
    "ServiceProbeError",
    "parse_probe_output",
    "probe_argv",
    "probe_python_argv",
    "run_service_probe",
]
