"""Robust binary lookup with fallback to known install locations.

shutil.which() only searches PATH, which may be incomplete when the
dashboard server runs in a shell (e.g., Git Bash on Windows) that
doesn't inherit paths from other shells (e.g., PowerShell).

Usage:
    from jacked.findbin import find_bin
    uv = find_bin("uv")  # returns full path or None
"""

import os
import shutil
import sys
from pathlib import Path


def _home_dir() -> str:
    """Return the user's home directory. Extracted for testability."""
    return str(Path.home())


def find_bin(name: str) -> str | None:
    """Find a binary by name, searching PATH then known install locations."""
    found = shutil.which(name)
    if found:
        return found

    is_win = sys.platform == "win32"
    suffix = ".exe" if is_win and not name.endswith(".exe") else ""
    target = f"{name}{suffix}"
    home = _home_dir()

    candidates: list[str] = []

    uv_tool_bin = os.environ.get("UV_TOOL_BIN_DIR")
    if uv_tool_bin:
        candidates.append(os.path.join(uv_tool_bin, target))

    xdg_bin = os.environ.get("XDG_BIN_HOME")
    if xdg_bin:
        candidates.append(os.path.join(xdg_bin, target))

    # All platforms: uv's default tool bin dir
    candidates.append(os.path.join(home, ".local", "bin", target))

    # All platforms: cargo bin (uv can be installed via cargo)
    candidates.append(os.path.join(home, ".cargo", "bin", target))

    if is_win:
        local_app = os.environ.get("LOCALAPPDATA", "")
        if local_app:
            candidates.append(os.path.join(local_app, "uv", "bin", target))
            candidates.append(os.path.join(local_app, "Programs", "claude", target))

    for path in candidates:
        if os.path.isfile(path):
            # On Windows, os.access(path, os.X_OK) is unreliable — it can
            # return False for valid .exe files depending on UAC, security
            # policies, or cloud/network drives.  If the file exists and has
            # an executable extension, that's sufficient.
            if is_win or os.access(path, os.X_OK):
                return path

    return None
