"""Tiny atomic cache helpers for the statusline."""

from __future__ import annotations

import json
import os
import tempfile


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


def _write_cache(
    cache_path: str,
    prefix: str,
    version: int,
    source: dict,
    payload: dict,
) -> None:
    record = {"version": version, "source": source, **payload}
    temporary = None
    try:
        descriptor, temporary = tempfile.mkstemp(
            dir=os.path.dirname(cache_path), prefix=prefix, suffix=".tmp"
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(record, output, separators=(",", ":"))
            output.write("\n")
        os.replace(temporary, cache_path)
        temporary = None
    except OSError:
        pass
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
