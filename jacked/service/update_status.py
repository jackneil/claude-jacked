"""Atomic reader/writer for the update-status JSON file.

Used by the detached POSIX updater, the Windows cmd.exe batch (via the
`jacked _update_status` CLI shim), and the `/api/update/status` endpoint.

Lifecycle contract:
  1. `init_status()` — clobbers any prior file unless another updater is
     actively in-flight (raises LockBusy). Stale `in_progress` files
     (older than STALE_IN_PROGRESS_SECONDS) are considered abandoned.
  2. `begin_phase` / `end_phase` throughout the update.
  3. `mark_succeeded()` OR end_phase with status="failed" finalizes.
  4. A `succeeded` file older than STALE_SUCCEEDED_SECONDS is reported as
     missing by `read_status()` so the UI doesn't resurrect old banners.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from jacked.service import CLAUDE_DIR

UPDATE_STATUS_FILE: Path = CLAUDE_DIR / "jacked-update-status.json"

STALE_SUCCEEDED_SECONDS: int = 600  # 10 minutes
STALE_IN_PROGRESS_SECONDS: int = 300  # 5 minutes


class LockBusy(Exception):
    """Another updater is actively in-flight — refuse to clobber its state."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_raw(path: Path) -> Optional[dict]:
    try:
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return None


def read_status(path: Path) -> Optional[dict]:
    """Public read. Returns None on missing, corrupt, or stale-succeeded."""
    data = _read_raw(path)
    if data is None:
        return None
    if data.get("overall") == "succeeded":
        try:
            age = time.time() - path.stat().st_mtime
            if age > STALE_SUCCEEDED_SECONDS:
                return None
        except OSError:
            return None
    return data


def read_status_with_mtime(path: Path) -> tuple[Optional[dict], Optional[str]]:
    """Returns (data, mtime_iso). Used by /api/update/status."""
    data = read_status(path)
    if data is None:
        return None, None
    try:
        mtime = path.stat().st_mtime
        mtime_iso = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
    except OSError:
        mtime_iso = None
    return data, mtime_iso


def init_status(
    path: Path,
    from_version: str,
    to_version: str,
    method: str,
    log_path: Optional[str] = None,
) -> None:
    """Create a fresh status file. Raises LockBusy if another updater is active.

    Clobbers:
      - no prior file
      - prior succeeded/failed file
      - prior in_progress file older than STALE_IN_PROGRESS_SECONDS
    Refuses:
      - prior in_progress file fresher than that
    """
    prior = _read_raw(path)
    if prior is not None and prior.get("overall") == "in_progress":
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            age = 0
        if age <= STALE_IN_PROGRESS_SECONDS:
            raise LockBusy(
                "Another updater is in-flight (started "
                f"{prior.get('started_at', '?')}, last updated "
                f"{int(age)}s ago)"
            )

    data = {
        "started_at": _now_iso(),
        "from_version": from_version,
        "to_version": to_version,
        "method": method,
        "current_phase": None,
        "phases": [],
        "overall": "in_progress",
        "error": None,
        "recovery": None,
        "log_path": log_path,
    }
    _atomic_write(path, data)


def begin_phase(path: Path, phase: str) -> None:
    data = _read_raw(path) or {}
    phases = data.get("phases", [])
    phases.append({
        "name": phase,
        "started_at": _now_iso(),
        "finished_at": None,
        "status": "in_progress",
    })
    data["phases"] = phases
    data["current_phase"] = phase
    _atomic_write(path, data)


def end_phase(
    path: Path,
    phase: str,
    status: str,
    error: Optional[str] = None,
    recovery: Optional[str] = None,
) -> None:
    """Close the most recent in-progress phase with the given name.

    Raises ValueError if no such open phase exists — loud failure > silent drift.
    """
    data = _read_raw(path) or {}
    phases = data.get("phases", [])
    matched = False
    for entry in reversed(phases):
        if entry["name"] == phase and entry["status"] == "in_progress":
            entry["status"] = status
            entry["finished_at"] = _now_iso()
            matched = True
            break
    if not matched:
        raise ValueError(
            f"end_phase({phase!r}) called but no in_progress entry with that name"
        )
    data["phases"] = phases
    if status == "failed":
        data["overall"] = "failed"
        if error:
            data["error"] = error
        if recovery:
            data["recovery"] = recovery
    _atomic_write(path, data)


def mark_succeeded(path: Path) -> None:
    data = _read_raw(path) or {}
    data["overall"] = "succeeded"
    data["current_phase"] = None
    _atomic_write(path, data)


def mark_failed(
    path: Path,
    error: str,
    recovery: Optional[str] = None,
) -> None:
    """Mark overall=failed with an explicit error/recovery.

    Use when a failure happens OUTSIDE any active phase (pre-flight failure,
    post-phase exception). No-op if the status file doesn't exist.
    """
    data = _read_raw(path)
    if data is None:
        return
    data["overall"] = "failed"
    data["error"] = error
    if recovery:
        data["recovery"] = recovery
    _atomic_write(path, data)


def clear_status(path: Path) -> None:
    """Delete the status file. Used by the tray's refusal path."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
