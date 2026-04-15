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
    """Check if a process with the given PID is running."""
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
