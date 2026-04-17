"""Detect how this jacked install was set up so we pick the right upgrade command.

Supported methods, in priority order:

  uv   — `uv tool install claude-jacked[...]`. Binary lives in an isolated
         venv under `*/uv/tools/claude-jacked/`. Upgrade via
         `uv tool install 'claude-jacked[...]' --force` (or `uv tool upgrade`).
  pipx — `pipx install claude-jacked`. Binary lives under `pipx/venvs/claude-jacked`.
         Upgrade via `pipx upgrade claude-jacked`.
  pip  — `pip install [--user] claude-jacked`. No unique path marker, so this is
         the fallback assumption. Upgrade via
         `<sys.executable> -m pip install --upgrade claude-jacked[...]`
         (with `--user` if the current install is under the user site).

The same logic drives both the CLI `jacked upgrade` command and the
tray-icon auto-update helper — so whichever install method the user picked,
we don't hand them a command that would install the package a second time
in a different place.
"""

from __future__ import annotations

import site
import sys
from pathlib import Path


def detect_install_method() -> str:
    """Return 'uv', 'pipx', or 'pip' based on where sys.executable lives."""
    try:
        exe = Path(sys.executable).resolve()
    except (OSError, RuntimeError):
        return "pip"

    parts_lower = [p.lower() for p in exe.parts]

    # uv tool venv path fingerprint: .../uv/tools/<pkg>/...
    for i, part in enumerate(parts_lower):
        if part == "tools" and i > 0 and parts_lower[i - 1] == "uv":
            return "uv"

    # pipx venv path fingerprint: .../pipx/venvs/<pkg>/...
    for i, part in enumerate(parts_lower):
        if part == "venvs" and i > 0 and parts_lower[i - 1] == "pipx":
            return "pipx"

    return "pip"


def is_user_site_install() -> bool:
    """True if the running interpreter is in / near the user site-packages.

    Used when we fall back to `pip install` — we pass `--user` if the existing
    install is a user-site install so the upgrade lands in the same location
    instead of requiring sudo or failing on a read-only system-Python.
    """
    try:
        user_site = Path(site.getusersitepackages()).resolve()
        exe = Path(sys.executable).resolve()
    except (OSError, RuntimeError):
        return False

    # Check if sys.executable lives under the user-base dir. On Windows that's
    # typically %APPDATA%\Python\Python3XX\; on POSIX it's ~/.local or similar.
    try:
        user_base = Path(site.getuserbase()).resolve()
    except (OSError, AttributeError):
        user_base = user_site.parent

    try:
        exe.relative_to(user_base)
        return True
    except ValueError:
        pass

    # Also: check if user-site is on this interpreter's sys.path AND looks active
    try:
        user_site_str = str(user_site)
        return any(
            Path(p).resolve() == user_site for p in sys.path if p
        ) and "site-packages" in user_site_str
    except (OSError, RuntimeError):
        return False


def upgrade_command(extras: str = "tray") -> list[str]:
    """Return the argv list that should be run to upgrade jacked in place.

    The returned command is appropriate for the detected install method.
    Caller should still catch non-zero exit codes and surface the output.
    """
    method = detect_install_method()
    if method == "uv":
        return ["uv", "tool", "install", f"claude-jacked[{extras}]", "--force"]
    if method == "pipx":
        # pipx upgrade doesn't let you add extras to an existing venv, but
        # install --force does replace + can re-declare extras.
        return [
            "pipx", "install", f"claude-jacked[{extras}]",
            "--force",
        ]
    # pip fallback. Use the current interpreter so we land in the same env.
    cmd = [sys.executable, "-m", "pip", "install", "--upgrade"]
    if is_user_site_install():
        cmd.append("--user")
    cmd.append(f"claude-jacked[{extras}]")
    return cmd


def upgrade_command_label(extras: str = "tray") -> str:
    """Human-readable upgrade command for logs, error messages, and docs."""
    method = detect_install_method()
    if method == "uv":
        return f'uv tool install "claude-jacked[{extras}]" --force'
    if method == "pipx":
        return f'pipx install "claude-jacked[{extras}]" --force'
    user_flag = "--user " if is_user_site_install() else ""
    return f'{sys.executable} -m pip install --upgrade {user_flag}"claude-jacked[{extras}]"'
