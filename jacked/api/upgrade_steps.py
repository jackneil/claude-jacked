"""The subprocess steps of the dashboard upgrade (``POST /api/upgrade``).

``jacked.api.routes.system`` runs the steps and re-exports these names. They
live here so that the route module stays inside its line budget.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re

from jacked.public_errors import PublicError

logger = logging.getLogger(__name__)

SERVER_LOG_HINT = "See ~/.claude/jacked-server.log."

# A subcommand word that may name a step ("tool", "upgrade", "install").
_STEP_WORD = re.compile(r"[a-z][a-z-]{0,31}")


class UpgradeStepError(PublicError, RuntimeError):
    """An upgrade step failed. The message is fixed text written for the user.

    The ``upgrade_failed`` event sends the message to every dashboard client,
    remote ones too. So the message names the step and the exit code only.
    The installer's output goes to the host's log file (see
    :func:`_upgrade_run_cmd`), never into the message.
    """


class InstallerOutput(Exception):
    """The output of a failed installer, as an argument of a log record.

    The host's log file formats it in full. The dashboard log viewer names
    an exception argument by its type only (``jacked.api.log_capture``), so
    the output does not get to a client through the viewer either.
    """


def step_label(cmd: list[str]) -> str:
    """Name a step by its program and its first subcommand words.

    An argument that is not a plain word (an option, a path, code) is left out.

    >>> step_label(["/opt/bin/uv", "tool", "upgrade", "claude-jacked"])
    'uv tool upgrade'
    >>> step_label(["/opt/bin/jacked", "install", "--force"])
    'jacked install'
    >>> step_label(["/usr/bin/python3", "-c", "print('/Users/x')"])
    'python3'
    """
    words = [arg for arg in cmd[1:3] if _STEP_WORD.fullmatch(arg)]
    return " ".join([os.path.basename(cmd[0]), *words])


async def _upgrade_run_cmd(cmd: list[str]) -> None:
    """Run a subprocess command list, raising UpgradeStepError on failure or timeout.

    On cancellation (the service shutting down mid-upgrade) the child is
    terminated and awaited BEFORE the cancellation propagates, so the switch
    lease the caller holds is never released while an installer still runs.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        raise UpgradeStepError(f"The {step_label(cmd)} step timed out after 120 s. {SERVER_LOG_HINT}")
    except asyncio.CancelledError:
        await _stop_child(proc)
        raise
    if proc.returncode != 0:
        label = step_label(cmd)
        output = (stdout or b"").decode(errors="replace")
        logger.warning(
            "Upgrade step %s exited with code %s. Installer output:\n%s",
            label, proc.returncode, InstallerOutput(output or "(no output)"),
        )
        raise UpgradeStepError(
            f"The {label} step failed with exit code {proc.returncode}. {SERVER_LOG_HINT}"
        )


async def _stop_child(proc) -> None:
    """Terminate a child and wait for it, escalating to kill after 5 s.

    The cancelled ``communicate()`` no longer drains the child's stdout, and
    asyncio reports a child as finished only once its pipes close. A noisy
    child that ignores SIGTERM would fill the pipe and never be seen to
    exit, so a drain task reads the pipe to EOF while the child is stopped.
    """
    if proc.returncode is not None:
        return
    drain = asyncio.create_task(_drain(proc.stdout)) if proc.stdout is not None else None
    try:
        try:
            proc.terminate()
        except ProcessLookupError:
            return
        try:
            await asyncio.wait_for(asyncio.shield(proc.wait()), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
    finally:
        if drain is not None:
            try:
                await asyncio.wait_for(drain, timeout=5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001 - best effort
                drain.cancel()


async def _drain(stream) -> None:
    """Read a pipe to EOF, discarding the data."""
    while await stream.read(65536):
        pass
