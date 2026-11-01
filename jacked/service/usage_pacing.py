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


def account_windows(
    acct: dict, now: datetime
) -> list[tuple[str, Optional[float], Optional[datetime]]]:
    """``(kind, effective percent, reset)`` for the windows the account has.

    ``kind`` is ``"5h"`` or ``"7d"``. The 5-hour window is listed only when the
    account's plan has one (:func:`jacked.usage_plans.session_window`): a
    verified Ultra or a ChatGPT Pro account has none, and a 5-hour value that
    lingers in its cache is stale, so it never counts. Percents carry the
    staleness rule of :func:`_effective_pct`; a window with no reading has
    percent None.

    >>> now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    >>> ultra = {"subscription_type": "max", "rate_limit_tier": "default_claude_max_20x",
    ...          "cached_usage_raw": '{"five_hour": null, "limits": [{"kind": "weekly_all"}]}',
    ...          "cached_usage_5h": 99.0, "cached_usage_7d": 40.0}
    >>> [(k, p) for k, p, _ in account_windows(ultra, now)]
    [('7d', 40.0)]
    >>> [(k, p) for k, p, _ in account_windows({"cached_usage_5h": 12.0, "cached_usage_7d": 3.0}, now)]
    [('5h', 12.0), ('7d', 3.0)]
    """
    from jacked.usage_plans import session_window

    cached_at = acct.get("usage_cached_at")
    sources = [("7d", acct.get("cached_usage_7d"), acct.get("cached_7d_resets_at"))]
    try:
        has_5h = session_window(acct).state != "absent"
    except Exception:  # noqa: BLE001 - one unreadable row must not stop pacing
        has_5h = True  # never rule out a window jacked cannot read
    if has_5h:
        sources.insert(0, ("5h", acct.get("cached_usage_5h"), acct.get("cached_5h_resets_at")))
    return [
        (kind, _effective_pct(pct, resets, cached_at, now), parse_reset_ts(resets))
        for kind, pct, resets in sources
    ]


def worst_window(
    acct: dict, now: datetime
) -> Optional[tuple[str, float, Optional[datetime]]]:
    """The account's most constrained window ``(kind, percent, reset)``, or None.

    Only windows with a reading count. On a tie the weekly window wins: it
    takes longer to come back.

    >>> now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    >>> worst_window({"cached_usage_5h": 50.0, "cached_usage_7d": 50.0}, now)[0]
    '7d'
    >>> worst_window({"cached_usage_5h": 80.0, "cached_usage_7d": 50.0}, now)[:2]
    ('5h', 80.0)
    >>> worst_window({}, now) is None
    True
    """
    known = [w for w in account_windows(acct, now) if w[1] is not None]
    if not known:
        return None
    # max() keeps the first maximum: put 7d first so it wins ties.
    ordered = sorted(known, key=lambda w: 0 if w[0] == "7d" else 1)
    return max(ordered, key=lambda w: w[1])


def account_recovery(
    acct: dict, now: datetime, constrained_threshold: float = 90.0
) -> Optional[tuple[datetime, str]]:
    """When the account becomes workable again, and the window that decides it.

    Returns ``(reset, kind)``: the LATEST future reset among its constrained
    windows (effective percent at or above ``constrained_threshold``), and
    that window's kind ("5h" or "7d"). One window resetting while another is
    still constrained does not make the account usable. None when no window
    is constrained, or when a constrained window has no knowable future reset
    (a partial answer would name a time at which the account is still
    walled).

    >>> now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    >>> cached = int(now.timestamp()) - 60
    >>> acct = {"cached_usage_5h": 95.0, "cached_5h_resets_at": "2026-01-02T03:00:00Z",
    ...         "cached_usage_7d": 97.0, "cached_7d_resets_at": "2026-01-05T00:00:00Z",
    ...         "usage_cached_at": cached}
    >>> reset, kind = account_recovery(acct, now)
    >>> reset.isoformat(), kind
    ('2026-01-05T00:00:00+00:00', '7d')
    >>> account_recovery({"cached_usage_7d": 10.0}, now) is None
    True
    """
    constrained: list[tuple[datetime, str]] = []
    for kind, pct, reset in account_windows(acct, now):
        if pct is None or pct < constrained_threshold:
            continue
        if reset is None or reset <= now:
            return None
        constrained.append((reset, kind))
    return max(constrained) if constrained else None


def account_recovery_at(
    acct: dict, now: datetime, constrained_threshold: float = 90.0
) -> Optional[datetime]:
    """:func:`account_recovery` without the window kind.

    >>> now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    >>> account_recovery_at({"cached_usage_7d": 10.0}, now) is None
    True
    """
    found = account_recovery(acct, now, constrained_threshold)
    return found[0] if found else None


def compute_best_account_summary(
    accounts: Iterable[dict],
    now: Optional[datetime] = None,
    constrained_threshold: float = 90.0,
) -> dict:
    """Pacing summary: most-headroom eligible account + fleet pause target.

    Mirrors ``menubar_summary.compute_worst_account_summary`` in the
    opposite direction. ``pause_until`` is the earliest time some eligible
    account becomes WORKABLE (:func:`account_recovery_at`): per account, the
    LATEST future reset among its CONSTRAINED windows (effective percent at
    or above ``constrained_threshold``) — one window resetting while the
    account's other window is still constrained does not make the account
    usable, so the earlier reset alone must not set the wake time. The fleet
    target is the min of those per-account times. An idle window's reset
    carries no information and never participates. An account with a
    constrained window whose wake time is unknowable (no parseable FUTURE
    ``resets_at``) contributes nothing — a partial answer would name a wake
    time at which the account is still walled. ``pause_until`` CAN therefore
    be null while the best percent is constrained; consumers must treat that
    as "pause time unknown" and fall back to error-driven pacing, never "no
    pause needed".

    Windows are plan-aware (:func:`account_windows`): an account whose plan
    has no 5-hour window is judged on its weekly window alone.
    ``best_account_worst_window`` names the window behind
    ``best_account_worst_window_pct`` ("5h" or "7d"), and
    ``best_account_worst_window_resets_at`` is that window's reset.
    ``plan_catalog`` reports the release's plan catalog date and every
    eligible account whose payload disagrees with it (catalog drift).

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
    >>> s["best_account_email"], s["best_account_worst_window_pct"], s["best_account_worst_window"]
    ('live@x.com', 96.0, '7d')
    >>> s["pause_until"]        # the CONSTRAINED 7d window, not the idle 5h
    '2026-01-05T00:00:00+00:00'
    """
    from jacked.usage_plans import account_view, catalog

    now = now or datetime.now(timezone.utc)
    best: Optional[dict] = None
    best_window: Optional[tuple[str, float, Optional[datetime]]] = None
    with_data = 0
    recoveries: list[datetime] = []
    drift: list[dict] = []

    for acct in accounts:
        if not _pacing_eligible(acct):
            continue
        try:
            codes = account_view(acct).plan_payload()["drift"]
        except Exception:  # noqa: BLE001 - one unreadable row must not stop pacing
            codes = []
        if codes:
            drift.append({"account_id": acct.get("id"), "codes": codes})
        worst = worst_window(acct, now)
        if worst is None:
            continue
        with_data += 1
        recovery = account_recovery_at(acct, now, constrained_threshold)
        if recovery is not None:
            recoveries.append(recovery)
        # Strict < keeps the first-seen account on ties (priority order).
        if best_window is None or worst[1] < best_window[1]:
            best_window = worst
            best = acct

    return {
        "accounts_with_usage_data": with_data,
        "best_account_email": best.get("email") if best else None,
        "best_account_id": best.get("id") if best else None,
        "best_account_worst_window_pct": best_window[1] if best_window else None,
        "best_account_worst_window": best_window[0] if best_window else None,
        "best_account_worst_window_resets_at": (
            best_window[2].isoformat() if best_window and best_window[2] else None
        ),
        "best_account_cache_age_seconds": (
            cache_age_seconds(best, now) if best else None
        ),
        "pause_until": min(recoveries).isoformat() if recoveries else None,
        "plan_catalog": {"verified_at": catalog().verified_at, "drift": drift},
    }
