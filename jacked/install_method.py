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


def _is_path_under_any_site_packages(target: Path) -> bool:
    """Return True if `target` lives under a site-packages/ dir on sys.path."""
    try:
        target = target.resolve()
    except (OSError, RuntimeError):
        return False
    for entry in sys.path:
        if not entry:
            continue
        try:
            p = Path(entry).resolve()
        except (OSError, RuntimeError):
            continue
        if p.name != "site-packages":
            continue
        try:
            target.relative_to(p)
            return True
        except ValueError:
            continue
    return False


def detect_install_method() -> str:
    """Return 'uv', 'pipx', 'editable', or 'pip' based on install markers.

    Detection order: uv -> pipx -> editable -> pip (fallback).
    """
    # IMPORTANT: use `sys.prefix` (venv root), not `Path(sys.executable).resolve()`.
    # `.resolve()` follows symlinks; uv-tool venvs on macOS typically symlink
    # `bin/python` to the real interpreter binary (e.g. miniconda's). After
    # resolve, the path no longer contains `uv/tools/<pkg>/`. `sys.prefix`
    # always points to the active venv root regardless of symlinks.
    #
    # We also check the raw (unresolved) `sys.executable` as a belt-and-
    # suspenders for environments where sys.prefix is overridden.
    candidate_paths = []
    try:
        candidate_paths.append(Path(sys.prefix))
    except (OSError, RuntimeError):
        pass
    try:
        candidate_paths.append(Path(sys.executable))
    except (OSError, RuntimeError):
        pass
    # Resolved exe as a last fingerprint option (can still match if uv-tool
    # venv stores the real python bin inside uv/tools/<pkg>/).
    try:
        candidate_paths.append(Path(sys.executable).resolve())
    except (OSError, RuntimeError):
        pass

    for p in candidate_paths:
        parts_lower = [part.lower() for part in p.parts]
        # uv tool venv path fingerprint: .../uv/tools/<pkg>/...
        for i, part in enumerate(parts_lower):
            if part == "tools" and i > 0 and parts_lower[i - 1] == "uv":
                return "uv"
        # pipx venv path fingerprint: .../pipx/venvs/<pkg>/...
        for i, part in enumerate(parts_lower):
            if part == "venvs" and i > 0 and parts_lower[i - 1] == "pipx":
                return "pipx"

    # Editable install: look for marker .pth files on sys.path.
    for entry in sys.path:
        if not entry:
            continue
        try:
            d = Path(entry)
            if not d.is_dir():
                continue
            if any(d.glob("_editable_impl_*.pth")):
                return "editable"
            if any(d.glob("__editable__.*.pth")):
                return "editable"
        except (OSError, RuntimeError):
            continue

    # Fallback: if jacked/__init__.py resolves to a path NOT under any
    # site-packages/ on sys.path, treat as editable.
    try:
        import jacked
        if jacked.__file__:
            jacked_init = Path(jacked.__file__).resolve()
            if not _is_path_under_any_site_packages(jacked_init):
                return "editable"
    except Exception:
        pass

    return "pip"


def can_auto_upgrade() -> tuple[bool, str]:
    """Return (ok, reason) — is it safe to auto-upgrade this install?

    uv / pipx: True, empty reason.
    editable:  False with a git-pull/uv-sync recovery hint.
    pip:       False with a 'migrate to uv' recovery hint.
    any error: False with a defensive message.
    """
    try:
        method = detect_install_method()
    except Exception:
        return (
            False,
            "Could not detect install method — auto-update disabled. "
            "Try: `uv tool install \"claude-jacked[tray]\" --force`.",
        )
    if method in ("uv", "pipx"):
        return True, ""
    if method == "editable":
        return (
            False,
            "This is an editable (dev-clone) install — auto-update disabled. "
            "Upgrade manually from the repo: `cd <repo> && git pull && uv sync`.",
        )
    return (
        False,
        "pip install detected — auto-update disabled (uv is the supported "
        "install method). Migrate with: "
        "`uv tool install \"claude-jacked[tray]\"`.",
    )


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
