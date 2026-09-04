"""Bounded retry memory for supervised service starts."""

from __future__ import annotations

import json
from pathlib import Path

from jacked.service import START_FAILURE_WINDOW_SECONDS


def record_start_failure(
    path: Path, now: float, *, window: float = START_FAILURE_WINDOW_SECONDS
) -> int:
    """Append one failure timestamp and return how many fall inside the window."""
    stamps: list[float] = []
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        stamps = [float(item) for item in loaded if isinstance(item, (int, float))]
    except (OSError, ValueError, TypeError):
        stamps = []
    # A stamp in the future (clock stepped back after a reboot) must not
    # count forever, so both bounds are enforced.
    stamps = [stamp for stamp in stamps if 0 <= now - stamp <= window]
    stamps.append(now)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(stamps), encoding="utf-8")
    return len(stamps)


def clear_start_failures(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
