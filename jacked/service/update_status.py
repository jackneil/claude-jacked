"""Atomic reader/writer for the update-status JSON file.

Used by the detached POSIX updater, the Windows cmd.exe batch (via the
`jacked _update_status` CLI shim), and the `/api/update/status` endpoint.

Lifecycle contract:
  1. `init_status()` — clobbers any prior file unless another updater is
     actively in-flight (raises LockBusy). Stale `in_progress` files
     (older than STALE_IN_PROGRESS_SECONDS) are considered abandoned.
     `init_or_adopt_status()` wraps this to tolerate the tray's own
     pre-init file (metadata written, no phases opened yet).
  2. `begin_phase` / `end_phase` throughout the update.
  3. `mark_succeeded()` OR end_phase with status="failed" finalizes.
  4. `read_status()` reports as missing (None): a `succeeded` file older
     than STALE_SUCCEEDED_SECONDS, AND an `in_progress` file older than
     STALE_IN_PROGRESS_SECONDS (an aborted updater that never finalized),
     so the UI doesn't resurrect old banners or a zombie "updating" state.
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
    """Public read. Returns None on missing, corrupt, stale-succeeded, or
    stale-in_progress."""
    data = _read_raw(path)
    if data is None:
        return None
    overall = data.get("overall")
    if overall == "succeeded":
        try:
            age = time.time() - path.stat().st_mtime
            if age > STALE_SUCCEEDED_SECONDS:
                return None
        except OSError:
            return None
    elif overall == "in_progress":
        # An aborted updater leaves in_progress forever; without this the
        # dashboard would show a zombie "updating..." banner indefinitely.
        # init_status already treats a file of this age as abandoned — mirror
        # that so the read side and the write side agree on staleness.
        try:
            age = time.time() - path.stat().st_mtime
            if age > STALE_IN_PROGRESS_SECONDS:
                return None
        except OSError:
            return None
    return data


def abandoned_status(path: Path) -> Optional[dict]:
    """Return the raw record of an update that was interrupted, else None.

    `read_status` hides an `in_progress` file older than
    STALE_IN_PROGRESS_SECONDS so the dashboard shows no zombie banner. Hiding
    it is not the same as telling the user: the machine may be running a build
    whose settings never migrated. The tray reads this at boot and turns it
    into a recovery file.

    >>> import json, os, tempfile, time
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "status.json"
    ...     _ = p.write_text(json.dumps({"overall": "in_progress",
    ...                                  "current_phase": "migrating_settings"}))
    ...     old = time.time() - STALE_IN_PROGRESS_SECONDS - 60
    ...     os.utime(p, (old, old))
    ...     abandoned_status(p)["current_phase"]
    'migrating_settings'
    """
    data = _read_raw(path)
    if data is None or data.get("overall") != "in_progress":
        return None
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return None
    if age <= STALE_IN_PROGRESS_SECONDS:
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


def init_or_adopt_status(
    path: Path,
    from_version: str,
    to_version: str,
    method: str,
    log_path: Optional[str] = None,
) -> str:
    """Init a fresh status file, or adopt the tray's pre-init one.

    The tray pre-creates the status file (metadata, no phases) the instant
    the user clicks update so the dashboard has something from t=0. The
    detached updater — POSIX Python or the Windows cmd.exe shim — then races
    to init again moments later and hits LockBusy on the tray's own
    seconds-old file. That's not a real collision: a file with no phases and
    no current_phase is the tray's pre-init, so adopt it without rewriting
    (its metadata is already correct). A file with an open phase IS a real
    concurrent updater — re-raise LockBusy so callers refuse.

    Returns ``"initialized"`` (wrote a fresh file) or ``"adopted"`` (reused
    the tray's pre-init metadata).

    >>> import tempfile
    >>> from pathlib import Path
    >>> with tempfile.TemporaryDirectory() as d:
    ...     p = Path(d) / "status.json"
    ...     init_or_adopt_status(p, "1.0.0", "1.1.0", "uv")
    ...     init_or_adopt_status(p, "1.0.0", "1.1.0", "uv")
    'initialized'
    'adopted'
    """
    try:
        init_status(path, from_version, to_version, method, log_path=log_path)
        return "initialized"
    except LockBusy:
        prior = _read_raw(path) or {}
        if not (prior.get("phases") or []) and prior.get("current_phase") is None:
            return "adopted"
        raise


def begin_phase(path: Path, phase: str) -> None:
    data = _read_raw(path) or {}
    phases = data.get("phases", [])
    phases.append(
        {
            "name": phase,
            "started_at": _now_iso(),
            "finished_at": None,
            "status": "in_progress",
        }
    )
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
