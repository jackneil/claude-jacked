"""Fleet pacing summary for autonomous loops (`jacked usage --json`).

Pure Python, no UI imports — safe from the CLI, tests, and any service.
The one consumer contract: the night-shift skill reads
``summary.best_account_worst_window_pct``, ``best_account_cache_age_seconds``,
and ``pause_until`` to decide whether to keep working, pause until a window
resets, or fall back to error-driven pacing. That contract is pinned by
``tests/unit/test_usage_cmd.py``.

Staleness model (the subtle part, both directions matter):
- percent and ``resets_at`` are written INDEPENDENTLY (``database.py``
  ``update_account_usage_cache`` skips absent values; Codex payloads can
  carry a percent without a reset), so neither column dates the other;
- a window is stale-headroom ONLY when the percent PREDATES a turnover
  that has since happened: ``usage_cached_at < resets_at <= now``.
  A past reset alone is NOT staleness — a fresh 97% paired with the
  previous window's old reset must stay 97%, or the loop walks into a
  hard rate limit while every trust check reads green (the Wave-2
  CRITICAL). The reverse cut (reset passed AFTER the percent was cached)
  is the original stale-95%-blocks-a-ready-account bug.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Optional

from jacked.service.menubar_summary import _eligible


def parse_reset_ts(ts: object) -> Optional[datetime]:
    """Parse a stored ``resets_at`` string into an aware UTC datetime.

    Provider ``resets_at`` strings are stored VERBATIM (oauth.py / codex
    usage.py do no normalization), so their shape is the provider's to
    change. A naive value must never crash a comparison against an aware
    ``now`` — normalize to UTC like every other reader of this column
    (window_keeper, auto_swap selection/tiers/diagnostics/burn).

    >>> parse_reset_ts("2026-01-01T00:00:00Z").tzinfo is not None
    True
    >>> parse_reset_ts("2026-01-01T00:00:00").tzinfo is not None
    True
    >>> parse_reset_ts("not-a-date") is None
    True
    >>> parse_reset_ts(None) is None
    True
    """
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def coerce_pct(value: object) -> Optional[float]:
    """Defensively coerce a cached usage value to float, or None.

    SQLite's dynamic typing means a cached percent CAN be TEXT (an older or
    buggy writer); one hostile row must degrade to "unknown", never crash
    the pacing signal an unattended loop depends on.

    >>> coerce_pct(91), coerce_pct("91"), coerce_pct("91%"), coerce_pct(None)
    (91.0, 91.0, None, None)
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cache_age_seconds(acct: dict, now: datetime) -> Optional[int]:
    """SIGNED age of the account's usage cache, or None if unstamped.

    Negative means clock skew — visible on purpose, never clamped to
    "fresh" (an impossible timestamp is the opposite of fresh data).

    >>> now = datetime(2026, 1, 1, 0, 10, tzinfo=timezone.utc)
    >>> cache_age_seconds({"usage_cached_at": int(now.timestamp()) - 60}, now)
    60
    >>> cache_age_seconds({}, now) is None
    True
    """
    if not acct.get("usage_cached_at"):
        return None
    try:
        return int(now.timestamp()) - int(acct["usage_cached_at"])
    except (TypeError, ValueError):
        return None


def _effective_pct(
    pct: object, resets_at: object, cached_at: object, now: datetime
) -> Optional[float]:
    """A window's utilization with staleness applied (see module docstring).

    Stale-headroom (-> 0.0) requires the percent to PREDATE a turnover:
    ``cached_at < resets_at <= now``. With no cache stamp the percent's age
    is unknowable — conservatively treat a past reset as turnover (the
    pre-fix behavior), since a wrong pause self-corrects at the next
    refresh while the case is only reachable for rows written without
    ``usage_cached_at``.

    >>> now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    >>> past = "2026-01-01T00:00:00Z"
    >>> before_reset = int(datetime(2025, 12, 31, tzinfo=timezone.utc).timestamp())
    >>> after_reset = int(datetime(2026, 1, 1, 12, tzinfo=timezone.utc).timestamp())
    >>> _effective_pct(95, past, before_reset, now)   # percent predates turnover
    0.0
    >>> _effective_pct(97, past, after_reset, now)    # FRESH percent, old reset row
    97.0
    >>> _effective_pct(95, "2026-01-03T00:00:00Z", before_reset, now)
    95.0
    >>> _effective_pct(None, past, None, now) is None
    True
    """
    p = coerce_pct(pct)
    if p is None:
        return None
    reset = parse_reset_ts(resets_at)
    if reset is None or reset > now:
        return p
    # reset <= now: turnover happened. Stale only if the percent is older.
    try:
        cached_epoch = int(cached_at) if cached_at is not None else None
    except (TypeError, ValueError):
        cached_epoch = None
    if cached_epoch is None or cached_epoch < int(reset.timestamp()):
        return 0.0
    return p


def _pacing_eligible(acct: dict) -> bool:
    """Eligibility for the pacing summary: enabled, not deleted, not a
    known-dead login. Tests ``is_active`` for truthiness itself — SQLite
    hands back ``0``, not ``False``, and ``_eligible``'s ``is not False``
    lets a disabled row through when ``--include-inactive`` routes one here.
    ``validation_status == "invalid"`` accounts keep stale cached
    percentages forever (revocation does not touch ``is_active``), so
    counting them would report headroom that does not exist.

    >>> _pacing_eligible({"is_active": 1, "validation_status": "valid"})
    True
    >>> _pacing_eligible({"is_active": 0, "validation_status": "valid"})
    False
    >>> _pacing_eligible({"is_active": 1, "validation_status": "invalid"})
    False
    """
    if not acct or not acct.get("is_active", 1):
        return False
    return _eligible(acct) and acct.get("validation_status") != "invalid"


def compute_best_account_summary(
    accounts: Iterable[dict],
    now: Optional[datetime] = None,
    constrained_threshold: float = 90.0,
) -> dict:
    """Pacing summary: most-headroom eligible account + fleet pause target.

    Mirrors ``menubar_summary.compute_worst_account_summary`` in the
    opposite direction. ``pause_until`` is the earliest time some eligible
    account becomes WORKABLE: per account, that is the LATEST future reset
    among its CONSTRAINED windows (effective percent at or above
    ``constrained_threshold``) — one window resetting while the account's
    other window is still constrained does not make the account usable, so
    the earlier reset alone must not set the wake time. The fleet target is
    the min of those per-account times. An idle window's reset carries no
    information and never participates. An account with a constrained
    window whose wake time is unknowable (no parseable FUTURE ``resets_at``)
    contributes nothing — a partial answer would name a wake time at which
    the account is still walled. ``pause_until`` CAN therefore be null
    while the best percent is constrained; consumers must treat that as
    "pause time unknown" and fall back to error-driven pacing, never "no
    pause needed".

    >>> now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    >>> cached = int(now.timestamp()) - 60
    >>> s = compute_best_account_summary([
    ...     {"id": 1, "email": "dead@x.com", "is_active": 1,
    ...      "validation_status": "invalid", "cached_usage_5h": 0.0},
    ...     {"id": 2, "email": "live@x.com", "is_active": 1,
    ...      "validation_status": "valid", "cached_usage_5h": 8.0,
    ...      "cached_usage_7d": 96.0, "usage_cached_at": cached,
    ...      "cached_5h_resets_at": "2026-01-02T01:00:00Z",
    ...      "cached_7d_resets_at": "2026-01-05T00:00:00Z"},
    ... ], now=now)
    >>> s["best_account_email"], s["best_account_worst_window_pct"]
    ('live@x.com', 96.0)
    >>> s["pause_until"]        # the CONSTRAINED 7d window, not the idle 5h
    '2026-01-05T00:00:00+00:00'
    """
    now = now or datetime.now(timezone.utc)
    best: Optional[dict] = None
    best_pct: Optional[float] = None
    with_data = 0
    constrained_resets: list[datetime] = []

    for acct in accounts:
        if not _pacing_eligible(acct):
            continue
        cached_at = acct.get("usage_cached_at")
        windows = [
            (acct.get("cached_usage_5h"), acct.get("cached_5h_resets_at")),
            (acct.get("cached_usage_7d"), acct.get("cached_7d_resets_at")),
        ]
        effective = [
            (_effective_pct(pct, resets, cached_at, now), parse_reset_ts(resets))
            for pct, resets in windows
        ]
        known = [e for e, _ in effective if e is not None]
        if not known:
            continue
        with_data += 1
        worst = max(known)
        acct_constrained: list[datetime] = []
        acct_wake_unknown = False
        for e, reset in effective:
            if e is None or e < constrained_threshold:
                continue
            if reset and reset > now:
                acct_constrained.append(reset)
            else:
                acct_wake_unknown = True
        if acct_constrained and not acct_wake_unknown:
            # The account is workable only once its LAST constrained window
            # resets; any unknowable constrained window poisons the estimate.
            constrained_resets.append(max(acct_constrained))
        # Strict < keeps the first-seen account on ties (priority order).
        if best_pct is None or worst < best_pct:
            best_pct = worst
            best = acct

    return {
        "accounts_with_usage_data": with_data,
        "best_account_email": best.get("email") if best else None,
        "best_account_id": best.get("id") if best else None,
        "best_account_worst_window_pct": best_pct,
        "best_account_cache_age_seconds": (
            cache_age_seconds(best, now) if best else None
        ),
        "pause_until": min(constrained_resets).isoformat() if constrained_resets else None,
    }
