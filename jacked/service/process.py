"""PID file management, port checking, and process lifecycle."""

import os
import signal
import socket
import sys
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
    """Check if a TCP port is available for binding."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
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
