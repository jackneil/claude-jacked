"""The usage-block verdict that a provider reports for an account.

A Codex rate-limit read says whether the account may send requests.
:func:`usage_block` reads that verdict from a cached payload, and
:func:`usage_block_of_row` reads it from a database account row.
:mod:`jacked.service.menubar_summary` re-exports each function of this
module and ``USAGE_STALE_SECS`` for its callers and tests.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Optional

from jacked.service.per_model_sources import _epoch_to_iso


# ---------------------------------------------------------------------------
# Provider-reported usage block (Codex)
# ---------------------------------------------------------------------------
#
# A Codex ``account/rateLimits/read`` result says outright whether the account
# may send requests: ``ordinaryUsageAllowed`` is the backend's own verdict, and
# ``rateLimits.rateLimitReachedType`` / ``spendControlReached`` name why. Only
# an explicit False blocks: once the included usage runs out, credits can keep
# ordinary requests allowed while a reached type is still set. The verdict can
# hold below 100% (a spend control or a workspace limit), so it is surfaced
# as-is instead of inferred from percents.

# A reading older than this is stale: six 5-minute poll intervals. Mirror of
# USAGE_STALE_SECS in jacked/data/web/js/components/usage.js.
USAGE_STALE_SECS = 30 * 60


def usage_block(raw: Optional[dict], now: Optional[float] = None) -> Optional[dict]:
    """The provider's "usage is blocked" verdict from a cached payload, or None.

    Returns ``{"reason": str, "resets_at": iso|None}`` where reason is
    ``limit_reached``, ``spend_limit`` or ``blocked``. Only a Codex payload
    with ``ordinaryUsageAllowed: false`` blocks; any other shape returns None.
    A block whose reset time has passed returns None: the reading is older
    than the reset, so it no longer says anything about the account.

    >>> usage_block(None) is None
    True
    >>> usage_block({"ordinaryUsageAllowed": True,
    ...              "rateLimits": {"rateLimitReachedType": "rate_limit_reached"}}) is None
    True
    >>> usage_block({"ordinaryUsageAllowed": False,
    ...              "rateLimits": {"rateLimitReachedType": "rate_limit_reached"},
    ...              "rateLimitUpsell": {"reset_at": 4102444800}}, now=0)
    {'reason': 'limit_reached', 'resets_at': '2100-01-01T00:00:00+00:00'}
    >>> usage_block({"ordinaryUsageAllowed": False,
    ...              "rateLimits": {"spendControlReached": True}}, now=0)
    {'reason': 'spend_limit', 'resets_at': None}
    >>> usage_block({"ordinaryUsageAllowed": False, "rateLimits": {},
    ...              "rateLimitUpsell": {"reset_at": 100}}, now=200) is None
    True
    """
    if not isinstance(raw, dict) or raw.get("ordinaryUsageAllowed") is not False:
        return None
    rl = raw.get("rateLimits") if isinstance(raw.get("rateLimits"), dict) else {}
    if rl.get("spendControlReached") is True:
        reason = "spend_limit"
    elif rl.get("rateLimitReachedType"):
        reason = "limit_reached"
    else:
        reason = "blocked"
    upsell = raw.get("rateLimitUpsell") if isinstance(raw.get("rateLimitUpsell"), dict) else {}
    reset = upsell.get("reset_at")
    if not isinstance(reset, (int, float)) or isinstance(reset, bool):
        # No upsell reset: the latest reset among exhausted windows.
        resets = [
            w.get("resetsAt")
            for w in (rl.get("primary"), rl.get("secondary"))
            if isinstance(w, dict)
            and isinstance(w.get("usedPercent"), (int, float))
            and w["usedPercent"] >= 100
            and isinstance(w.get("resetsAt"), (int, float))
        ]
        reset = max(resets) if resets else None
    current = datetime.now(timezone.utc).timestamp() if now is None else now
    if reset is not None and reset <= current:
        return None
    return {"reason": reason, "resets_at": _epoch_to_iso(reset)}


def usage_block_of_row(row: dict, now: Optional[float] = None) -> Optional[dict]:
    """:func:`usage_block` for a DB account row (parses ``cached_usage_raw``).

    Adds ``read_at`` (the row's ``usage_cached_at``). A block with no reset
    time counts only while the reading is fresh (:data:`USAGE_STALE_SECS`):
    nothing else can end it, and a row the poller no longer reads would
    otherwise say "blocked" forever. A block with a reset time stands until
    that reset. A malformed cached payload reads as "no block".

    >>> import json as _j
    >>> raw = _j.dumps({"ordinaryUsageAllowed": False,
    ...                 "rateLimits": {"spendControlReached": True}})
    >>> usage_block_of_row({"cached_usage_raw": raw, "usage_cached_at": 1000}, now=1060)
    {'reason': 'spend_limit', 'resets_at': None, 'read_at': 1000}
    >>> usage_block_of_row({"cached_usage_raw": raw, "usage_cached_at": 1000},
    ...                    now=1000 + USAGE_STALE_SECS + 1) is None
    True
    >>> usage_block_of_row({"cached_usage_raw": "not json"}) is None
    True
    """
    text = (row or {}).get("cached_usage_raw")
    if not text:
        return None
    try:
        block = usage_block(json.loads(text), now=now)
    except (TypeError, ValueError):
        return None
    if block is None:
        return None
    read_at = row.get("usage_cached_at")
    read_at = int(read_at) if isinstance(read_at, (int, float)) and not isinstance(read_at, bool) else None
    if block["resets_at"] is None:
        current = datetime.now(timezone.utc).timestamp() if now is None else now
        if read_at is None or current - read_at > USAGE_STALE_SECS:
            return None
    block["read_at"] = read_at
    return block
