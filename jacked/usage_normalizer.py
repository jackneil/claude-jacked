"""Normalize token usage from Claude and OpenRouter response shapes."""

from __future__ import annotations

from numbers import Real
import math


_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_create_tokens",
    "total_tokens",
)


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, Real):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0
    return max(0, int(value))


def _cost(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    value = float(value)
    return value if math.isfinite(value) and value >= 0 else None


def normalize_usage(usage) -> dict:
    """Return one stable usage shape for Claude and OpenRouter records.

    OpenRouter's ``prompt_tokens`` includes cached prompt tokens in its total,
    so cached tokens are reported as a breakdown and are not added twice.
    Anthropic's input counter excludes cache counters, so its total includes
    each available counter once.
    """
    if not isinstance(usage, dict):
        usage = {}

    is_openrouter = any(
        key in usage for key in ("prompt_tokens", "completion_tokens")
    )
    if is_openrouter:
        prompt_tokens = _count(usage.get("prompt_tokens"))
        output_tokens = _count(usage.get("completion_tokens"))
        details = usage.get("prompt_tokens_details")
        details = details if isinstance(details, dict) else {}
        cache_read = _count(usage.get("cache_read_input_tokens"))
        if not cache_read:
            cache_read = _count(details.get("cached_tokens"))
        cache_create = _count(usage.get("cache_creation_input_tokens"))
        if not cache_create:
            cache_create = _count(details.get("cache_write_tokens"))
        total = _count(usage.get("total_tokens")) or prompt_tokens + output_tokens
        input_tokens = prompt_tokens
    else:
        input_tokens = _count(usage.get("input_tokens"))
        output_tokens = _count(usage.get("output_tokens"))
        cache_read = _count(usage.get("cache_read_input_tokens"))
        cache_create = _count(usage.get("cache_creation_input_tokens"))
        total = input_tokens + output_tokens + cache_read + cache_create

    provider_cost = _cost(usage.get("cost"))
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_read_tokens": cache_read,
        "cache_create_tokens": cache_create,
        "total_tokens": total,
        "cost_usd": provider_cost if provider_cost is not None else None,
        "cost_source": "provider" if provider_cost is not None else None,
    }
