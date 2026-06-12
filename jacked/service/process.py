"""PID file management, port checking, and process lifecycle."""

import os
import signal
import socket
import sys
import time
from pathlib import Path

from jacked.service import DEFAULT_PORT


def write_pid(pid_file: Path, port: int = DEFAULT_PORT) -> None:
    """Write current PID and port to the PID file."""
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(f"{os.getpid()}\n{port}")


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


def remove_pid(pid_file: Path) -> None:
    """Remove PID file if it exists."""
    pid_file.unlink(missing_ok=True)


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
        subprocess.run(["taskkill", "/PID", str(pid)], capture_output=True)
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
