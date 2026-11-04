"""The subprocess steps of the dashboard upgrade (``POST /api/upgrade``).

``jacked.api.routes.system`` runs the steps and re-exports these names. They
live here so that the route module stays inside its line budget.
"""

from __future__ import annotations

import asyncio
import os

from jacked.public_errors import PublicError


class UpgradeStepError(PublicError, RuntimeError):
    """An upgrade step failed with a report that is written for the user.

    The message is the installer's own output (its last 500 characters) or a
    fixed sentence. The dashboard upgrade dialog shows it as it is, so the
    user can see why ``uv`` or ``jacked install`` failed.
    """


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
        raise UpgradeStepError(f"{os.path.basename(cmd[0])} timed out after 120s")
    except asyncio.CancelledError:
        await _stop_child(proc)
        raise
    if proc.returncode != 0:
        raise UpgradeStepError((stdout or b"").decode(errors="replace")[-500:] or "Unknown error")


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
