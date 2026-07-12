"""Restart-hook registry decoupling the API layer from the tray runner.

The settings API must restart the dashboard when the operator flips the
remote-access toggle, but it cannot import the tray: that would couple the API
to a GUI dependency, and the tray may not even be running (``jacked webux`` has
no tray). This module is the thin seam. Whoever owns the process lifecycle
registers a handler; the API calls :func:`restart_service_now` and doesn't care
who answers.

Two worlds:

- **Service / tray:** ``ServiceRunner`` registers ``_on_restart`` — an
  IN-PROCESS uvicorn restart (same PID, the tray survives, works identically on
  every platform). ``_on_restart`` re-reads the settings DB on the way back up,
  so a GUI toggle takes effect without replacing the process.
- **Runner-less ``jacked webux`` foreground:** no handler is registered, so we
  fall back to ``os.execv`` — a full process replacement, the same proven
  pattern the upgrade flow uses. Acceptable there because there is no tray to
  orphan (on Windows ``execv`` is spawn-and-exit emulation, still fine for a
  path with no tray).
"""

from __future__ import annotations

import logging
import os
import sys
from typing import Callable

logger = logging.getLogger(__name__)

_restart_handler: Callable[[], None] | None = None


def set_restart_handler(fn: Callable[[], None] | None) -> None:
    """Register (or clear, with ``None``) the process-restart handler."""
    global _restart_handler
    _restart_handler = fn


def get_restart_handler() -> Callable[[], None] | None:
    """The currently registered restart handler, or ``None``."""
    return _restart_handler


def restart_service_now() -> None:
    """Restart the dashboard process.

    If a handler is registered (the tray), call it — wrapped in
    ``try/except BaseException``. The handler runs on a daemon thread with no
    one watching its exceptions, and a ``SystemExit`` raised deep inside it (a
    bind conflict on the way back up) would otherwise vanish silently, leaving
    the dashboard dead with no trace. We log it LOUDLY instead; the tray menu's
    Restart stays available for manual recovery.

    With no handler (the ``webux`` foreground path), replace the process via
    ``os.execv``. The service/artifact launch path no longer bakes ``--host``
    into argv, so a re-exec there re-resolves the bind from the DB. (A ``webux
    --host X`` foreground launch replays its argv verbatim and stays pinned to
    X, which is the documented CLI-override precedence; see :mod:`jacked.service.bind`.)
    """
    handler = _restart_handler
    if handler is not None:
        try:
            handler()
        except BaseException:  # noqa: BLE001 — a swallowed SystemExit kills the dashboard silently
            logger.exception(
                "Restart handler raised; the dashboard may still be serving "
                "the OLD bind. Use the tray menu's Restart to recover."
            )
        return
    # No handler: full process replacement (webux foreground path). execv only
    # returns on failure; when it does, log LOUDLY (matching the handler path)
    # rather than letting the OSError die on stderr where the tray-log reader
    # never sees why the restart stranded.
    prog, argv = _exec_target()
    try:
        os.execv(prog, argv)
    except OSError:
        logger.exception(
            "execv restart failed (%s); the dashboard is still on the OLD bind. "
            "Restart jacked manually to apply the change.",
            prog,
        )
        raise


def _exec_target() -> tuple[str, list[str]]:
    """The (program, argv) pair for the execv process replacement.

    ``sys.argv[0]`` is directly executable for console-script launches
    (``jacked webux``), but under ``python -m jacked`` it is the package's
    ``__main__.py``, which has no shebang and no exec bit; execv'ing it raises
    OSError inside the restart thread and strands the dashboard on the old
    bind. In that case re-exec through the interpreter with ``-m jacked``.
    """
    argv0 = sys.argv[0]
    if os.path.basename(argv0) == "__main__.py" or not os.access(argv0, os.X_OK):
        return sys.executable, [sys.executable, "-m", "jacked", *sys.argv[1:]]
    return argv0, list(sys.argv)
