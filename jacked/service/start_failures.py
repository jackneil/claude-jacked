"""Bounded retry memory for supervised service starts."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from jacked.service import START_FAILURE_WINDOW_SECONDS


@dataclass(frozen=True)
class StartFailure:
    """One refused or unready start, with the reason when one was captured."""

    at: float
    reason: str | None = None


def _coerce(item) -> StartFailure | None:
    """Read one on-disk entry: a legacy float, or the object form."""
    if isinstance(item, bool):
        return None
    if isinstance(item, (int, float)):
        return StartFailure(float(item), None)
    if isinstance(item, dict):
        at = item.get("at")
        if isinstance(at, bool) or not isinstance(at, (int, float)):
            return None
        reason = item.get("reason")
        return StartFailure(float(at), reason if isinstance(reason, str) else None)
    return None


def _load(path: Path, now: float, window: float) -> list[StartFailure]:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(loaded, list):
        return []
    failures = [_coerce(item) for item in loaded]
    # A stamp in the future (clock stepped back after a reboot) must not
    # count forever, so both bounds are enforced.
    return [
        failure
        for failure in failures
        if failure is not None and 0 <= now - failure.at <= window
    ]


def read_start_failures(
    path: Path, now: float, *, window: float = START_FAILURE_WINDOW_SECONDS
) -> list[StartFailure]:
    """Return the failures inside the window, oldest first.

    A missing or corrupt file reads as no failures: the breaker is advisory
    memory, so an unreadable file must never block a status report.
    """
    return _load(path, now, window)


def record_start_failure(
    path: Path,
    now: float,
    *,
    window: float = START_FAILURE_WINDOW_SECONDS,
    reason: str | None = None,
) -> int:
    """Append one failure and return how many fall inside the window."""
    failures = _load(path, now, window)
    failures.append(StartFailure(now, reason))
    payload = [
        {"at": failure.at, "reason": failure.reason} for failure in failures
    ]
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return len(failures)


def clear_start_failures(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
