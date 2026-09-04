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

import re
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


def upgrade_command(extras: str = "tray") -> list[str]:
    """Return the argv list that should be run to upgrade jacked in place.

    Callers MUST gate on can_auto_upgrade() first — it refuses pip and
    editable with recovery messages.  If this function is reached with
    method='pip' or 'editable' it raises ValueError loudly; the old pip
    fallback shipped a `python -m pip install` command that crashed
    with 'No module named pip' in uv-managed venvs (the 0.41.17 bug).
    """
    method = detect_install_method()
    if method == "uv":
        # `--refresh` is critical: without it uv may return cached package
        # metadata and a `--force` reinstall stays on the cached version even
        # when a newer one was just published. That made the tray "Update"
        # button silently no-op when run within uv's cache TTL of a release.
        return ["uv", "tool", "install", f"claude-jacked[{extras}]", "--force", "--refresh"]
    if method == "pipx":
        return [
            "pipx", "install", f"claude-jacked[{extras}]",
            "--force",
        ]
    raise ValueError(
        f"pip auto-upgrade is not supported (detected method: {method}). "
        "Caller must refuse via can_auto_upgrade() with migration guidance."
    )


_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+!_-]{0,63}$")


def _validated_version(version: str) -> str:
    """Return `version` if it looks like a PEP 440 release, else raise.

    Rollback commands are built from a version string that reaches us from a
    running build (``jacked.__version__``) or a caller. It is interpolated into
    a requirement specifier and, on Windows, into a batch line — so anything
    with whitespace, quotes or shell metacharacters is refused outright rather
    than quoted and hoped for.
    """
    if not isinstance(version, str) or not _VERSION_RE.match(version):
        raise ValueError(
            f"refusing to build a rollback command for version {version!r}: "
            "expected a PEP 440 version (digits, letters, '.', '-', '+', '_', '!')"
        )
    return version


def rollback_command(extras: str = "tray", version: str = "") -> list[str]:
    """Return the argv list that reinstalls an EXACT previous version.

    Mirrors :func:`upgrade_command` — same install-method detection, same
    refusal contract. pip and editable installs are refused here too: an
    upgrade never starts on those (``can_auto_upgrade`` gates it), so a
    rollback can never be owed to one.
    """
    _validated_version(version)
    method = detect_install_method()
    if method == "uv":
        # `--refresh` for the same reason the upgrade path needs it: a cached
        # index entry must not decide which artifact we pin back to.
        return [
            "uv", "tool", "install", f"claude-jacked[{extras}]=={version}",
            "--force", "--refresh",
        ]
    if method == "pipx":
        return [
            "pipx", "install", f"claude-jacked[{extras}]=={version}",
            "--force",
        ]
    raise ValueError(
        f"pip rollback is not supported (detected method: {method}). "
        "Caller must refuse via can_auto_upgrade() with migration guidance."
    )


def rollback_command_label(extras: str = "tray", version: str = "") -> str:
    """Human-readable rollback command for logs, error messages, and docs.

    Raises ValueError for non-uv/pipx methods — same contract as rollback_command.
    """
    _validated_version(version)
    method = detect_install_method()
    if method == "uv":
        return (
            f'uv tool install "claude-jacked[{extras}]=={version}" --force --refresh'
        )
    if method == "pipx":
        return f'pipx install "claude-jacked[{extras}]=={version}" --force'
    raise ValueError(
        f"pip rollback is not supported (detected method: {method}). "
        "Caller must refuse via can_auto_upgrade() with migration guidance."
    )


def upgrade_command_label(extras: str = "tray") -> str:
    """Human-readable upgrade command for logs, error messages, and docs.

    Raises ValueError for non-uv/pipx methods — same contract as upgrade_command.
    """
    method = detect_install_method()
    if method == "uv":
        return f'uv tool install "claude-jacked[{extras}]" --force --refresh'
    if method == "pipx":
        return f'pipx install "claude-jacked[{extras}]" --force'
    raise ValueError(
        f"pip auto-upgrade is not supported (detected method: {method}). "
        "Caller must refuse via can_auto_upgrade() with migration guidance."
    )
