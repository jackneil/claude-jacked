"""PID file management, port checking, and process lifecycle."""

import os
import signal
import socket
import sys
import tempfile
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from jacked.service import DEFAULT_PORT
from jacked.winproc import NO_WINDOW


@dataclass(frozen=True)
class OwnedProcess:
    """Process evidence copied from a validated private instance manifest."""

    pid: int
    creation_id: str
    executable: str
    managed: bool


class TerminationResult(str, Enum):
    SIGNALLED = "signalled"
    REFUSED_IDENTITY = "refused_identity"
    REFUSED_UNMANAGED = "refused_unmanaged"
    REFUSED_SUPERVISOR_REQUIRED = "refused_supervisor_required"
    UNSUPPORTED = "unsupported"


def verify_owned_process(process: OwnedProcess) -> bool:
    """Re-read creation identity and executable immediately before control."""

    try:
        from jacked.service.instance import process_identity

        observed = process_identity(process.pid)
    except (OSError, ProcessLookupError, ValueError):
        return False
    return observed.creation_id == process.creation_id and os.path.realpath(
        observed.executable
    ) == os.path.realpath(process.executable)


def _linux_pidfd_signal(pid: int, sig: int) -> bool:
    """Signal the exact Linux process object, not a reusable integer PID."""

    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        return False
    descriptor = os.pidfd_open(pid, 0)
    try:
        signal.pidfd_send_signal(descriptor, sig)
    finally:
        os.close(descriptor)
    return True


def _windows_handle_terminate(process: OwnedProcess, *, force: bool) -> bool:
    """Retain a Win32 process handle and verify creation time before control."""

    if not force:
        return False
    import ctypes
    from ctypes import wintypes

    query = 0x1000
    terminate = 0x0001
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(query | terminate, False, process.pid)
    if not handle:
        return False
    try:
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not kernel32.GetProcessTimes(
            handle,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        ):
            return False
        creation_id = f"windows-filetime:{(creation.dwHighDateTime << 32) | creation.dwLowDateTime}"
        if creation_id != process.creation_id:
            return False
        return bool(kernel32.TerminateProcess(handle, 1))
    finally:
        kernel32.CloseHandle(handle)


def terminate_owned_process(
    process: OwnedProcess,
    *,
    force: bool = False,
) -> TerminationResult:
    """Apply the fail-closed platform termination policy.

    Managed services must be stopped through a separately verified supervisor
    artifact.  macOS unmanaged processes have no race-free force primitive and
    must use authenticated IPC for graceful shutdown.
    """

    if process.managed:
        return TerminationResult.REFUSED_SUPERVISOR_REQUIRED
    if not verify_owned_process(process):
        return TerminationResult.REFUSED_IDENTITY
    requested_signal = signal.SIGKILL if force else signal.SIGTERM
    if sys.platform.startswith("linux"):
        try:
            if _linux_pidfd_signal(process.pid, requested_signal):
                return TerminationResult.SIGNALLED
        except OSError:
            return TerminationResult.REFUSED_IDENTITY
        return TerminationResult.UNSUPPORTED
    if sys.platform == "win32":
        return (
            TerminationResult.SIGNALLED
            if _windows_handle_terminate(process, force=force)
            else TerminationResult.REFUSED_UNMANAGED
        )
    if sys.platform == "darwin":
        return TerminationResult.REFUSED_UNMANAGED
    return TerminationResult.UNSUPPORTED


def write_pid(pid_file: Path, port: int = DEFAULT_PORT) -> bool:
    """Publish compatibility evidence unless another live process is named.

    This file is refusal-only. It never authorizes process control or cleanup.
    The v2 lease is the actual singleton authority.
    """
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    current_pid = os.getpid()
    existing = read_pid(pid_file)
    if existing is not None:
        if is_v2_pid_evidence(pid_file):
            return True
        if existing["pid"] != current_pid and is_process_alive(existing["pid"]):
            return False
        # Compatibility evidence is refusal-only. Never replace an existing
        # pathname, even when it is stale or belongs to this process: a legacy
        # writer could claim it between inspection and replacement.
        return True
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{pid_file.name}.", dir=pid_file.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(f"{current_pid}\n{port}\njacked-v2\n")
            handle.flush()
            os.fsync(handle.fileno())
        try:
            # link(2) is an atomic create-if-absent publication. os.replace()
            # would let a racing legacy process lose its live PID evidence.
            os.link(temporary, pid_file)
        except FileExistsError:
            winner = read_pid(pid_file)
            if (
                winner is not None
                and winner["pid"] != current_pid
                and is_process_alive(winner["pid"])
            ):
                return False
            return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def is_v2_pid_evidence(pid_file: Path) -> bool:
    """Identify compatibility evidence emitted by a lease-owning v2 service."""
    try:
        with pid_file.open("r", encoding="utf-8") as handle:
            return handle.read(256).splitlines()[2:3] == ["jacked-v2"]
    except (OSError, UnicodeError):
        return False


def read_pid(pid_file: Path) -> dict | None:
    """Read PID and port from PID file. Returns None if missing/corrupt."""
    if not pid_file.exists():
        return None
    try:
        text = pid_file.read_text().strip()
        lines = text.split("\n")
        pid = int(lines[0])
        port = int(lines[1]) if len(lines) > 1 else DEFAULT_PORT
        return {"pid": pid, "port": port}
    except (ValueError, IndexError):
        return None


def remove_pid(pid_file: Path, *, expected_pid: int | None = None) -> bool:
    """Legacy helper retained for old callers; v2 ownership never uses it."""
    if expected_pid is not None:
        existing = read_pid(pid_file)
        if existing is None or existing["pid"] != expected_pid:
            return False
    try:
        pid_file.unlink()
    except FileNotFoundError:
        return False
    return True


def is_process_alive(pid: int) -> bool:
    """Cross-platform check if a PID is running.

    POSIX: `os.kill(pid, 0)` probes process existence.
    Windows: `os.kill(pid, 0)` is not a valid probe — use the Win32 API
    via ctypes. WaitForSingleObject with 0 timeout avoids the
    STILL_ACTIVE==259 false-positive that bites GetExitCodeProcess.
    """
    if pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        SYNCHRONIZE = 0x00100000
        WAIT_TIMEOUT = 0x00000102

        kernel32 = ctypes.windll.kernel32
        # Explicit argtypes/restype — default int marshalling truncates
        # 64-bit HANDLE values and yields false results.
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not handle:
            return False
        try:
            return kernel32.WaitForSingleObject(handle, 0) == WAIT_TIMEOUT
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def is_port_available(host: str, port: int) -> bool:
    """Check if a TCP port is available for binding.

    Uses SO_REUSEADDR so a port in TIME_WAIT (recently-closed by a
    previous server) probes as available — matching what uvicorn
    actually does when binding (it sets reuse_address=True on POSIX).
    Without this, ``is_port_available`` returned False for ~30s after
    a tray restart even though the new server would bind cleanly,
    causing the auto-updater to abort with "port could not be freed"
    and leave the service down.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def stop_process(pid_file: Path) -> bool:
    """Stop the service by reading PID file and sending signal.

    Returns True if a signal was sent, False if no process found.
    Removes stale PID files.
    """
    info = read_pid(pid_file)
    if info is None:
        return False

    pid = info["pid"]
    if not is_process_alive(pid):
        remove_pid(pid_file)
        return False

    if sys.platform == "win32":
        import subprocess

        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F"],
            capture_output=True,
            creationflags=NO_WINDOW,
        )
    else:
        os.kill(pid, signal.SIGTERM)

    return True


def _wait_for_exit(pid: int, timeout: float) -> bool:
    """Poll is_process_alive() until the PID exits or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.25)
    return not is_process_alive(pid)


def stop_process_graceful(
    pid_file: Path,
    term_timeout: float = 10.0,
    kill_timeout: float = 3.0,
) -> dict:
    """Stop the service, waiting for actual exit and escalating to SIGKILL.

    Why this exists: pystray on macOS runs the AppKit NSRunLoop on the main
    thread, which blocks Python signal delivery until the runloop yields.
    A plain SIGTERM can be silently ignored for the life of the process,
    which is exactly what bit `jacked upgrade` — the old tray kept holding
    port 8321 while the upgrade proceeded anyway.

    Sequence:
      1. SIGTERM (POSIX) / `taskkill /PID` without /F (Windows) — graceful.
      2. Wait up to term_timeout for the PID to exit.
      3. If still alive, SIGKILL (POSIX) / `taskkill /F /T` (Windows).
      4. Wait up to kill_timeout for the PID to exit.

    Returns a dict with keys:
      was_running (bool) — the PID existed and was alive at entry.
      died (bool)        — the process is confirmed dead at return.
      killed (bool)      — we had to escalate to force-kill.
    """
    info = read_pid(pid_file)
    if info is None:
        return {"was_running": False, "died": False, "killed": False}

    pid = info["pid"]
    if not is_process_alive(pid):
        remove_pid(pid_file)
        return {"was_running": False, "died": True, "killed": False}

    if sys.platform == "win32":
        import subprocess

        # Graceful first — no /F. Sends WM_CLOSE to GUI procs / CTRL_BREAK to consoles.
        subprocess.run(
            ["taskkill", "/PID", str(pid)],
            capture_output=True,
            creationflags=NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            remove_pid(pid_file)
            return {"was_running": True, "died": True, "killed": False}

    if _wait_for_exit(pid, term_timeout):
        remove_pid(pid_file)
        return {"was_running": True, "died": True, "killed": False}

    # Escalate.
    if sys.platform == "win32":
        import subprocess

        subprocess.run(
            ["taskkill", "/PID", str(pid), "/F", "/T"],
            capture_output=True,
            creationflags=NO_WINDOW,
        )
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            remove_pid(pid_file)
            return {"was_running": True, "died": True, "killed": False}

    died = _wait_for_exit(pid, kill_timeout)
    if died:
        remove_pid(pid_file)
    return {"was_running": True, "died": died, "killed": True}


def wait_for_port_free(host: str, port: int, timeout: float = 10.0) -> bool:
    """Poll is_port_available() until the port is bindable or timeout elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_port_available(host, port):
            return True
        time.sleep(0.25)
    return is_port_available(host, port)
