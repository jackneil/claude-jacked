"""Dependency-light formatting primitives for the Claude statusline."""

from __future__ import annotations

import time

RESET = "\033[0m"
BOLD_CYAN = "\033[1;36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CAVE = "\033[38;5;172m"
ARROW = "→"
MIDDOT = "·"
SEP = f" {DIM}|{RESET} "

_TIER_LABELS = {
    "max_5x": "Max 5x",
    "max_20x": "Max 20x",
    "pro": "Pro",
    "free": "Free",
}


def _round_pct(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value)


def _pct_color(pct: int) -> str:
    if pct >= 85:
        return RED
    if pct >= 60:
        return YELLOW
    return GREEN


def _fmt_tokens(value: int) -> str:
    if value >= 999500:
        m10 = (value + 50000) // 100000
        return f"{m10 // 10}.{m10 % 10}M"
    if value >= 1000:
        return f"{(value + 500) // 1000}k"
    return str(value)


def _fmt_reset(epoch, now: float | None = None) -> str:
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return ""
    now = time.time() if now is None else now
    try:
        return time.strftime(
            "%H:%M" if (epoch - now) < 86400 else "%a %H:%M",
            time.localtime(epoch),
        )
    except (OverflowError, OSError, ValueError):
        return ""


def _sum_usage(usage) -> int | None:
    if not isinstance(usage, dict):
        return None
    if "total_tokens" in usage:
        value = usage.get("total_tokens")
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return int(value)
    total = 0
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ):
        value = usage.get(key) or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = 0
        total += value
    return int(total)


def _tier_label(raw) -> str:
    if not isinstance(raw, str) or not raw:
        return ""
    key = raw.removeprefix("default_claude_")
    return _TIER_LABELS.get(key, key)
