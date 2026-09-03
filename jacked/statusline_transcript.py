"""Bounded transcript-derived statusline segments."""

from __future__ import annotations

import json
import os

from jacked.statusline_common import RED, RESET


def _latest_transcript_cost(payload) -> float | None:
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path:
        return None
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as source:
            source.seek(max(0, size - 262144))
            lines = source.read().decode("utf-8", "replace").splitlines()
    except (OSError, ValueError):
        return None
    from jacked.usage_normalizer import normalize_usage

    for line in reversed(lines):
        try:
            record = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(record, dict) or record.get("type") != "assistant":
            continue
        message = record.get("message")
        usage = message.get("usage") if isinstance(message, dict) else None
        if not isinstance(usage, dict):
            return None
        return normalize_usage(usage)["cost_usd"]
    return None


def _cost_segment(payload) -> str:
    cost = _latest_transcript_cost(payload)
    return "" if cost is None else f"cost ${cost:.4f}"


def _models_in(chunk: str) -> list:
    out = []
    for line in chunk.split("\n"):
        if '"model"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        model = message.get("model") if isinstance(message, dict) else None
        if isinstance(model, str) and model and model != "<synthetic>":
            out.append(model)
    return out


def _configured_model(payload) -> str:
    model = payload.get("model") if isinstance(payload, dict) else None
    raw = model.get("id") if isinstance(model, dict) else None
    return raw.strip() if isinstance(raw, str) else ""


def _model_key(name) -> str:
    if not isinstance(name, str):
        return ""
    return name.strip().rsplit("/", 1)[-1].strip().casefold()


def _served_segment(payload) -> str:
    """Warn only when the newest served model differs from current config."""
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path:
        return ""
    expected = _configured_model(payload)
    tail_bytes = 262144
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as source:
            first = []
            read_bytes = 0
            if not expected:
                for window in (65536, 262144, 1048576, 4194304):
                    if window > size and read_bytes >= size:
                        break
                    source.seek(0)
                    read_bytes = min(size, window)
                    first = _models_in(
                        source.read(read_bytes).decode("utf-8", "replace")
                    )
                    if first or read_bytes >= size:
                        break
            if size > read_bytes:
                source.seek(max(0, size - tail_bytes))
                last = _models_in(source.read().decode("utf-8", "replace"))
            else:
                last = first
    except (OSError, ValueError):
        return ""
    if not last:
        return ""
    if not expected:
        if not first:
            return ""
        expected = first[0]
    served = last[-1]
    if _model_key(served) == _model_key(expected):
        return ""
    return (
        f"{RED}{served.rsplit('/', 1)[-1]} "
        f"(FALLBACK, not {expected.rsplit('/', 1)[-1]}){RESET}"
    )
