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

``system_program`` gives the absolute path of a Windows system program.
CreateProcess looks for a bare program name in the folder of the calling
program, then in the process cwd, and only then in System32. jacked runs in a
cloned repository (a hook, or the CLI in a project folder), so a bare
``cmd.exe`` or ``schtasks.exe`` could run a file that the repository holds.
Every spawn of a Windows system program starts with ``system_program(name)``;
``tests/unit/test_windows_program_guard.py`` fails on a bare name. The paths are built
with ``ntpath``, so they are Windows paths on every platform.
"""

import ntpath
import os
import subprocess
import sys
from collections.abc import Mapping

#: Pass as ``creationflags=`` to any background console subprocess. Hidden
#: console on Windows; harmless ``0`` no-op on POSIX.
NO_WINDOW = (
    getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    if sys.platform == "win32"
    else 0
)

_DEFAULT_ROOT = "C:\\Windows"
# Programs in %SystemRoot%\System32, by name without ".exe".
_SYSTEM32 = frozenset({"cmd", "schtasks", "taskkill", "reg", "sc", "cscript", "wscript"})


def _absolute(value: str | None) -> bool:
    """True for a Windows path with a drive (or a UNC share) and a root.

    >>> _absolute(r"C:\\Windows"), _absolute(r"\\Windows"), _absolute("Windows")
    (True, False, False)
    """
    return bool(value) and bool(ntpath.splitdrive(value)[0]) and ntpath.isabs(value)


def system_root(env: Mapping[str, str] | None = None) -> str:
    """Return ``%SystemRoot%`` (else ``%windir%``) when it is absolute, else ``C:\\Windows``."""
    env = os.environ if env is None else env
    for key in ("SystemRoot", "windir"):
        value = env.get(key)
        if _absolute(value):
            return value
    return _DEFAULT_ROOT


def system_program(name: str, env: Mapping[str, str] | None = None) -> str:
    """Return the absolute path of the Windows system program ``name``.

    ``cmd`` is ``%ComSpec%`` when that is an absolute path to a ``cmd.exe``,
    else ``%SystemRoot%\\System32\\cmd.exe``. An unknown name raises
    ``ValueError``, so a new program gets a deliberate home here.

    >>> system_program("schtasks.exe", {"SystemRoot": r"C:\\Windows"})
    'C:\\\\Windows\\\\System32\\\\schtasks.exe'
    """
    env = os.environ if env is None else env
    base = name.lower().removesuffix(".exe")
    root = system_root(env)
    if base == "cmd":
        comspec = env.get("ComSpec")
        if _absolute(comspec) and ntpath.basename(comspec).lower() == "cmd.exe":
            return comspec
    if base == "powershell":
        return ntpath.join(root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")
    if base == "explorer":
        return ntpath.join(root, "explorer.exe")
    if base in _SYSTEM32:
        return ntpath.join(root, "System32", f"{base}.exe")
    raise ValueError(f"{name} is not a known Windows system program")


def run_batch_detached(batch_path: str, **kwargs) -> None:
    """Start ``cmd.exe /c <batch_path>`` with a hidden console that outlives the caller.

    ``CREATE_NO_WINDOW`` (never ``DETACHED_PROCESS``) gives cmd.exe a hidden
    console that every child of the batch shares, so no window pops up.
    ``CREATE_BREAKAWAY_FROM_JOB`` lets the batch survive the end of the
    caller's job; when the job forbids breakaway, the start is tried again
    with ``CREATE_NO_WINDOW`` alone. ``kwargs`` go to ``subprocess.Popen``.
    """
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000)
    argv = [system_program("cmd"), "/c", batch_path]
    try:
        subprocess.Popen(argv, creationflags=no_window | breakaway, **kwargs)
    except OSError:
        subprocess.Popen(argv, creationflags=no_window, **kwargs)
