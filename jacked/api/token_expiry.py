"""Read a Claude Code ``expiresAt`` value.

``credential_helpers`` re-exports both names.
"""

import math


def expiry_seconds(raw) -> int | None:
    """A Claude Code ``expiresAt`` as epoch seconds, or None when absent or
    not a real time. Claude Code writes milliseconds; older payloads and
    jacked's own rows use seconds.

    >>> expiry_seconds(1_900_000_000_000), expiry_seconds(1_900_000_000)
    (1900000000, 1900000000)
    >>> [expiry_seconds(x) for x in (None, 0, -5, True, "1900000000", float("inf"), float("nan"))]
    [None, None, None, None, None, None, None]
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if not math.isfinite(raw) or raw <= 0:
        return None
    return int(raw / 1000) if raw > 1e12 else int(raw)


def _malformed_expiry(raw) -> bool:
    """True when ``expiresAt`` is present but is not a real time: such a
    payload is not a Claude Code rotation, so nothing in it is imported.

    >>> [_malformed_expiry(x) for x in (None, 0, 1_900_000_000_000)]
    [False, False, False]
    >>> [_malformed_expiry(x) for x in ("soon", float("nan"), True, False, -1)]
    [True, True, True, True, True]
    """
    if raw is None or (raw == 0 and not isinstance(raw, bool)):
        return False  # absent: nothing claims a time
    return expiry_seconds(raw) is None
