"""Recoverable Windows state-path validation and hardening."""

from __future__ import annotations

from pathlib import Path

from jacked.service.windows_security import inspect_windows_path, secure_windows_path


def _reject_reparse_ancestors(path: Path) -> None:
    current = path
    while True:
        if current.exists() or current.is_symlink():
            if inspect_windows_path(current).is_reparse_point:
                raise ValueError("private Windows path has a reparse ancestor")
        if current.parent == current:
            return
        current = current.parent


def ensure_private_windows_file(path: Path) -> None:
    """Validate ownership/type and repair a current-user file's private DACL."""

    _reject_reparse_ancestors(path.parent)
    inspected = inspect_windows_path(path)
    if (
        inspected.is_directory
        or inspected.is_reparse_point
        or inspected.link_count != 1
        or not inspected.owner_matches
    ):
        raise ValueError("private file has unsafe Windows ownership or type")
    if not inspected.dacl_private:
        secure_windows_path(path)


def ensure_private_windows_directory(path: Path) -> None:
    """Create or validate a non-reparse, current-user-owned private directory."""

    _reject_reparse_ancestors(path.parent)
    existed = path.exists() or path.is_symlink()
    if not existed:
        path.mkdir(parents=True, exist_ok=True)
        secure_windows_path(path)
        return
    inspected = inspect_windows_path(path)
    if (
        not inspected.is_directory
        or inspected.is_reparse_point
        or not inspected.owner_matches
    ):
        raise ValueError("private directory has unsafe Windows ownership or type")
    if not inspected.dacl_private:
        secure_windows_path(path)
