"""Windows console-window suppression for background subprocesses.

The jacked service/tray runs under the windowless ``pythonw.exe``. Any console
program it shells out to (``taskkill``, ``netstat``, ``git`` ...) is otherwise
handed its OWN fresh, visible console window by Windows — a flash on screen,
even with ``capture_output=True`` (that redirects the pipes, not the window).
``CREATE_NO_WINDOW`` gives the child a hidden console instead.

``NO_WINDOW`` is ``0`` on POSIX (no such concept, and ``creationflags=0`` is the
accepted no-op default everywhere), so callers can pass it unconditionally:

    >>> import subprocess
    >>> from jacked.winproc import NO_WINDOW
    >>> # subprocess.run([...], capture_output=True, creationflags=NO_WINDOW)
"""

import subprocess
import sys

#: Pass as ``creationflags=`` to any background console subprocess. Hidden
#: console on Windows; harmless ``0`` no-op on POSIX.
NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if sys.platform == "win32"
    else 0
)
