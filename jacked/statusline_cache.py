"""Tiny atomic cache helpers for the statusline."""

from __future__ import annotations

import json


def _read_cache(cache_path: str, version: int, source: dict) -> dict | None:
    try:
        with open(cache_path, encoding="utf-8", errors="replace") as source_file:
            cached = json.load(source_file)
    except (OSError, RecursionError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("version") != version or cached.get("source") != source:
        return None
    return cached


def _write_cache(cache_path: str, version: int, source: dict, payload: dict) -> None:
    """Persist one cache record. A failed write is a cache miss next time.

    The shared writer is imported here, on the cache-miss path only: it loads
    ``logging`` and ``tempfile``, which a cache-hit render never needs. The
    replace never waits (``retry_budget=0``): the statusline prints after
    this call, and a lost write only costs one recompute.
    """
    from jacked.atomic_io import atomic_write_text

    record = {"version": version, "source": source, **payload}
    try:
        atomic_write_text(
            cache_path,
            json.dumps(record, separators=(",", ":")) + "\n",
            retry_budget=0,
        )
    except OSError:
        pass
