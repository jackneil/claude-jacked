"""Background usage monitor — two independent loops for active-account
polling and full-sweep (window keeper + bulk usage refresh).

Started from main.py lifespan as separate asyncio tasks.  Each loop has
its own ``while True`` with ``try/except`` — one loop crashing does NOT
affect the other.  Both read settings from DB each tick so changes take
effect without restart.
"""

import asyncio
import json
import logging
import random
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from jacked.web.auto_swap import BurnRate

logger = logging.getLogger(__name__)

# Module-level state — shared between loops.
# Only the active poll loop writes to _burn_rates.
_burn_rates: dict[int, "BurnRate"] = {}
_last_exhaustion_warning: float = 0.0
_EXHAUSTION_COOLDOWN_SECONDS = 1800  # 30 minutes
_last_swap_time: float = 0.0
_SWAP_COOLDOWN_SECONDS = 300  # 5 minutes between swaps to prevent ping-ponging

# Wall-clock time of the last COMMITTED swap (credential write succeeded)
# or manual switch (see note_external_swap). Feeds the min-residency gate
# and the residency_seconds audit column on swap_log rows.
_last_committed_swap_time: float = 0.0
# Proactive reasons (higher-tier / intra-tier / burn-rate) must let the
# active account settle this long before another departure. Forced
# departures (drained, 5h critical) are exempt.
_MIN_RESIDENCY_SECONDS = 900

# Exponential backoff for failed swap attempts (credential write
# failures; TOCTOU aborts bail before the attempt is recorded). Window:
# base * 2**(count-1), capped. Replaces the old retry-next-tick behavior;
# the regular swap cooldown only governs committed swaps.
_swap_failure_count: int = 0
_last_swap_failure_at: float = 0.0
_SWAP_FAILURE_BACKOFF_BASE_SECONDS = 60.0
_SWAP_FAILURE_BACKOFF_CAP_SECONDS = 600.0

# Drain advisor: per-account cooldown for 'expiring_with_stranded_capacity'
# broadcasts. Advisor only — no auto-burn worker; routing real work beats
# burning quota on no-ops.
_drain_advisor_last_sent: dict[int, float] = {}
_DRAIN_ADVISOR_COOLDOWN_SECONDS = 1800
_DRAIN_ADVISOR_STRANDING_THRESHOLD = 2.0

# Same-tier-deficit advisory (split out of stall pattern d): per spec,
# same-tier-never-overrides is intended behavior, so it must not page at
# ERROR — it gets its own INFO + WS advisory with an independent cooldown.
_same_tier_advisory_last: float = 0.0
_SAME_TIER_ADVISORY_COOLDOWN_SECONDS = 1800
_SAME_TIER_DEFICIT_THRESHOLD = 15.0

# Track consecutive unchanged ticks per account for burn-rate decay.
_burn_rate_unchanged_ticks: dict[int, int] = {}
_initial_fetch_done = False
_ticks_since_prune = 0

# Wake signal — settings PUT sets this to trigger an immediate sweep.
_sweep_wake: asyncio.Event = asyncio.Event()

# Per-account last-observed tier for hysteresis. Persists across ticks;
# cleared when an account is removed or a swap occurs to/from it.
_last_observed_tiers: dict[int, int] = {}

# Anti-jitter: require an emerged TIER (higher-tier or intra-tier reason)
# to persist across ``_EMERGENCE_PERSISTENCE_TICKS`` consecutive ticks
# before swapping. Keyed by tier rather than account id — two near-tied
# candidates in the same tier alternating as best must not reset each
# other's streak. Shape: {"tier": int | None, "count": int}.
_emerged_tier_streak: dict = {"tier": None, "count": 0}
_EMERGENCE_PERSISTENCE_TICKS = 2

# Silent-stall watchdog state.
_consecutive_no_best_ticks: int = 0
_last_stall_warning: float = 0.0
_STALL_TICK_THRESHOLD = 10
_STALL_USAGE_STALENESS_SECONDS = 1800
_STALL_WARNING_COOLDOWN_SECONDS = 1800

# Window-keeper ping backoff: account_id -> {"fails": int, "skip_until": float
# (monotonic)}. A consecutively-failing ping (e.g. an exhausted account
# answering 429) must not be retried every sweep forever — 2026-06 incident:
# one account pinged every 120s for hours, 429 every time. Exponential
# backoff capped at 1h; cleared on the first successful ping.
_ping_backoff: dict[int, dict] = {}
_PING_BACKOFF_CAP_SECONDS = 3600


def reset_locks() -> None:
    """Rebind module-level asyncio primitives + clear per-account state.

    See jacked.api.routes.auth.reset_locks for the full explanation of
    why in-process tray restarts require this. ``_sweep_wake`` is
    currently only read via sync ``is_set()`` / ``set()`` / ``clear()``
    calls which do not touch the loop, so today the stale binding is
    harmless — but the moment a caller switches to ``await wait()``
    (a natural efficiency refactor) the original bug returns. Rebind
    pre-emptively.

    Tier hysteresis and emergence streak counters depend on consecutive
    ticks; a lifespan restart resets the count so we observe fresh data
    from scratch instead of acting on remembered tiers from before the
    restart.
    """
    global _sweep_wake
    _sweep_wake = asyncio.Event()
    _last_observed_tiers.clear()
    _emerged_tier_streak["tier"] = None
    _emerged_tier_streak["count"] = 0
    global _consecutive_no_best_ticks, _last_stall_warning
    _consecutive_no_best_ticks = 0
    _last_stall_warning = 0.0
    global _swap_failure_count, _last_swap_failure_at
    global _last_committed_swap_time, _same_tier_advisory_last
    _swap_failure_count = 0
    _last_swap_failure_at = 0.0
    _last_committed_swap_time = 0.0
    _same_tier_advisory_last = 0.0
    _drain_advisor_last_sent.clear()
    _ping_backoff.clear()


def note_external_swap() -> None:
    """Record a credential switch performed OUTSIDE the auto-swap loop
    (manual switch in routes/auth.py).

    Arms the swap cooldown and the min-residency clock, and clears the
    emergence streak so the loop re-observes from scratch instead of
    immediately swapping away from the account the user just chose.
    """
    global _last_swap_time, _last_committed_swap_time
    now_ts = time.time()
    _last_swap_time = now_ts
    _last_committed_swap_time = now_ts
    _emerged_tier_streak["tier"] = None
    _emerged_tier_streak["count"] = 0


def _swap_backoff_remaining(now: float | None = None) -> float:
    """Seconds remaining in the exponential failure-backoff window.

    0.0 when no failures are recorded or the window has elapsed.
    Window: ``_SWAP_FAILURE_BACKOFF_BASE_SECONDS * 2**(count-1)``, capped
    at ``_SWAP_FAILURE_BACKOFF_CAP_SECONDS``.
    """
    if _swap_failure_count <= 0:
        return 0.0
    window = min(
        _SWAP_FAILURE_BACKOFF_CAP_SECONDS,
        _SWAP_FAILURE_BACKOFF_BASE_SECONDS * (2 ** (_swap_failure_count - 1)),
    )
    elapsed = (now if now is not None else time.time()) - _last_swap_failure_at
    return max(0.0, window - elapsed)


def _read_active_account_id() -> int | None:
    """Return only a consensus-resolved active credential identity."""
    try:
        from jacked.credentials.resolver import ResolverState
        from jacked.credentials.runtime import resolve_active_identity

        observation = resolve_active_identity()
        if observation.state is ResolverState.RESOLVED:
            return observation.identity.account_id
    except (OSError, RuntimeError):
        return None
    return None


def _setting_bool(db, key: str, default: bool = False) -> bool:
    """Read a boolean setting from DB (stored as 'true'/'false' strings)."""
    val = db.get_setting(key)
    if val is None:
        return default
    return val.lower() in ("true", "1", "yes")


def _setting_float(db, key: str, default: float) -> float:
    """Read a float setting from DB."""
    val = db.get_setting(key)
    if val is None:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _setting_str(db, key: str, default: str) -> str:
    """Read a string setting from DB."""
    val = db.get_setting(key)
    return val if val is not None else default


def _apply_emergence_persistence(
    reason: str | None,
    best_tier: int | None,
    streak: dict,
    persistence_ticks: int,
) -> str | None:
    """Gate emerge reasons (higher-tier or intra-tier) behind a multi-tick
    streak keyed by the BEST candidate's TIER, not its account id.

    Anti-jitter: the emerged tier must remain the best's tier
    ``persistence_ticks`` times in a row before the swap actually fires.
    Tier-keyed because two near-tied candidates in the same tier
    alternating as best would reset each other's id-keyed streak forever.
    Mutates ``streak`` ({"tier": int | None, "count": int}) in place.

    Streak handling rules:

    - ``reason`` starts with ``REASON_PREFIX_HIGHER_TIER`` or
      ``REASON_PREFIX_INTRA_TIER`` and ``best_tier`` is set: increment the
      count when ``best_tier`` equals the tracked tier (regardless of which
      account is best), else restart the streak at 1 for the new tier.
      Return reason if the count met the threshold, else None.
    - ``reason`` is None and ``best_tier`` is None (transient absence,
      e.g. _fetch_candidate_usage hiccup): PRESERVE the streak. A
      single-tick candidate fetch glitch should not reset the 2-minute
      clock. Next tick with the same emerged tier resumes incrementing.
      Return None.
    - ``reason`` is set but is NOT an emerge reason (drained / 5h critical
      / burn-rate), or is None with a classified candidate present: clear
      the streak (different decision path now). Return reason.
    - emerge ``reason`` with ``best_tier`` missing: shouldn't normally
      occur because `should_swap_now` only emits emerge reasons when best
      is non-None. Defensive clear + return None.

    Returns the original ``reason`` when persistence is met (let the
    swap fire), or None to suppress this tick.
    """
    from jacked.web.auto_swap import (
        REASON_PREFIX_HIGHER_TIER,
        REASON_PREFIX_INTRA_TIER,
    )

    if reason is None and best_tier is None:
        # Transient: no candidate, no swap reason. Preserve streak so a
        # single-tick fetch glitch doesn't restart the persistence clock.
        return None
    is_emerge = reason is not None and reason.startswith(
        (REASON_PREFIX_HIGHER_TIER, REASON_PREFIX_INTRA_TIER),
    )
    if not is_emerge:
        # Explicit non-emerge outcome — drained/critical/burn-rate fired
        # OR active was decisively classified as no-swap-needed. Clear.
        streak["tier"] = None
        streak["count"] = 0
        return reason
    if best_tier is None:
        # Defensive: should not happen (emerge reasons require best != None).
        streak["tier"] = None
        streak["count"] = 0
        return None
    if streak["tier"] == best_tier:
        streak["count"] += 1
    else:
        streak["tier"] = best_tier
        streak["count"] = 1
    if streak["count"] < persistence_ticks:
        return None
    return reason


def _evaluate_stall(
    *,
    decision_action: str,
    best: dict | None,
    usage_cached_at_age_seconds: int,
    has_other_accounts: bool,
    reason: str | None,
    staleness_threshold: int,
) -> bool:
    """Return True if this tick qualifies as a stall pattern.

    Three patterns trigger a bump (any one):
      (a) Multi-account stale: stay+no-best+stale active+has others
      (b) Single-account forced-out: only one account, departure reason fired
      (c) Drained-no-candidate: any reason fired but no eligible target

    Same-tier-stay-with-deficit (the old pattern d) is NOT a stall — per
    spec, same-tier-never-overrides is intended behavior, so flagging it
    at ERROR trains the operator to ignore the watchdog. It lives in the
    separate ``_same_tier_advisory_applies`` advisory path instead.

    Returns False otherwise (caller resets the counter).
    """
    if decision_action != "stay":
        return False
    stale = usage_cached_at_age_seconds > staleness_threshold
    forced_out = reason is not None
    pattern_a = best is None and stale and has_other_accounts
    pattern_b = not has_other_accounts and forced_out
    pattern_c = best is None and forced_out
    return pattern_a or pattern_b or pattern_c


def _same_tier_advisory_applies(
    *,
    decision_action: str,
    best: dict | None,
    reason: str | None,
    best_deficit: float | None,
    best_tier: int | None,
    threshold: float = _SAME_TIER_DEFICIT_THRESHOLD,
) -> bool:
    """Advisory condition (split from stall pattern d): a candidate exists
    but same-tier-never-overrides keeps the loop on stay even though the
    candidate is materially behind its tier target.

    Gated on the candidate tier being T0 or T1 — those are harvestable
    drain-to targets; T2/T3 targets are floors, so sitting behind them is
    not actionable. With intra-T0 preemption implemented, T0-vs-T0 cases
    may legitimately swap; this advisory covers the remaining
    same-tier-stay cases. Caller applies the 30-min cooldown.
    """
    from jacked.web.auto_swap import TIER_T0, TIER_T1

    return (
        decision_action == "stay"
        and best is not None
        and reason is None
        and best_deficit is not None
        and best_deficit >= threshold
        and best_tier in (TIER_T0, TIER_T1)
    )


def _trigger_for_reason(reason: str | None) -> str:
    """Map a should_swap_now reason-string to a decision-log trigger
    taxonomy value. Stable contract — see spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md.
    """
    from jacked.web.auto_swap import (
        REASON_PREFIX_HIGHER_TIER,
        REASON_PREFIX_INTRA_TIER,
        REASON_PREFIX_DRAINED,
        REASON_PREFIX_FIVE_H,
        REASON_PREFIX_BURN_RATE,
    )

    if reason is None:
        return "tick"
    if reason.startswith(REASON_PREFIX_HIGHER_TIER):
        return "higher_tier_emerged"
    if reason.startswith(REASON_PREFIX_INTRA_TIER):
        return "intra_tier_preempted"
    if reason.startswith(REASON_PREFIX_DRAINED):
        return "tier_drained"
    if reason.startswith(REASON_PREFIX_FIVE_H):
        return "forced_critical"
    if reason.startswith(REASON_PREFIX_BURN_RATE):
        return "burn_rate"
    return "tier_aware"


# -----------------------------------------------------------------------
# Loop 1 — Active account poll (60s)
# -----------------------------------------------------------------------


def _compute_poll_interval(
    active_id: int | None,
    db,
    burn_rates: dict,
) -> tuple[float, str]:
    """Compute the adaptive poll interval and urgency tier.

    Returns (interval_seconds, tier_name). Falls back to (60, "unknown")
    on any error.
    """
    if active_id is None or db is None:
        return 60.0, "unknown"
    try:
        from jacked.web.auth import (
            compute_urgency_tier,
            _get_usage_state,
            _TIER_INTERVALS,
        )

        acct = db.get_account(active_id)
        br = burn_rates.get(active_id)
        state = _get_usage_state(active_id)
        tier, base = compute_urgency_tier(
            usage_5h=acct.get("cached_usage_5h") if acct else None,
            usage_7d=acct.get("cached_usage_7d") if acct else None,
            burn_rate_5h=br.rate_5h_per_min if br else 0.0,
            critical_5h=_setting_float(db, "auto_swap_5h_critical", 90),
        )
        # Override: force idle if stuck in 429 cycle — stale data makes
        # urgency tiers unreliable, and polling faster is pointless.
        if state.get("consecutive_429s", 0) >= 3:
            tier = "idle"
            base = _TIER_INTERVALS["idle"]
        state["tier"] = tier
        state["interval"] = base
        jitter = base * 0.15
        interval = base + random.uniform(-jitter, jitter)
        return interval, tier
    except Exception:
        return 60.0, "unknown"


def _clamp_poll_interval(
    interval: float,
    accounts: list[dict],
    active_acct_id: int,
    now: datetime,
    active_start: str,
    active_end: str,
    prev_tiers: dict[int, int],
) -> float:
    """Clamp the adaptive poll interval when a T0 candidate is in play.

    - <= 90s whenever any otherwise-eligible NON-active account is TIER_T0
      with positive deficit — a draining deadline must not wait out a 5-min
      idle interval.
    - <= 60s when such a T0 candidate's 5h window is saturated
      (>= ``_FIVE_H_HEADROOM_LIMIT``) and its 5h reset lands within the
      next poll interval — event-driven re-entry approximation: be on time
      for the reset instead of discovering it up to a full interval late.
      (The literal "excluded by _has_5h_headroom" condition is
      unsatisfiable here: headroom already admits accounts within
      RESET_SUPPRESS_MINUTES of reset, which dwarfs any poll interval.)
    """
    from jacked.web.auto_swap import (
        TIER_T0,
        target_for_tier,
        tier_for,
    )
    from jacked.web.auto_swap.selection import _FIVE_H_HEADROOM_LIMIT

    clamped = interval
    saturated_5h: list[dict] = []
    for a in accounts:
        if a["id"] == active_acct_id:
            continue
        if a.get("is_active") == 0 or a.get("is_deleted") == 1:
            continue
        if (a.get("consecutive_failures") or 0) >= 3:
            continue
        if a.get("validation_status") == "invalid":
            continue
        if a.get("cc_access_token") is None:
            continue
        if a.get("auto_swap_enabled") == 0:
            continue
        tier = tier_for(a, now=now, prev_tier=prev_tiers.get(a["id"]))
        if tier != TIER_T0:
            continue
        target = target_for_tier(tier, a, now, active_start, active_end)
        usage_7d = a.get("cached_usage_7d")
        if target is None or usage_7d is None:
            continue
        if target - usage_7d <= 0:
            continue
        clamped = min(clamped, 90.0)
        if (a.get("cached_usage_5h") or 0) >= _FIVE_H_HEADROOM_LIMIT:
            saturated_5h.append(a)

    for a in saturated_5h:
        resets_at = a.get("cached_5h_resets_at")
        if not resets_at:
            continue
        try:
            reset_dt = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
            if reset_dt.tzinfo is None:
                reset_dt = reset_dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        remaining = (reset_dt - now).total_seconds()
        if 0 < remaining <= clamped:
            clamped = min(clamped, 60.0)
            break

    return clamped


_CANDIDATE_STALENESS_SECONDS = 600  # 10 minutes — non-active accounts rarely change
_CANDIDATE_FETCH_TIMEOUT_SECONDS = 30


def _candidate_staleness_override(acct: dict) -> bool:
    """True when a candidate's cached snapshot is provably stale and must
    be re-fetched regardless of ``_CANDIDATE_STALENESS_SECONDS``.

    Two conditions (either suffices):
    - cached 5h usage >= 90 but the 5h reset is already in the past —
      the saturation is stale; the account may now be the most usable
      candidate. Removes the 5-10 min blind spot right after a window
      reset, exactly when an account becomes most useful.
    - ``needs_7d_ping`` says the 7d window reset but the cache predates
      the reset (a fresh 7d window means a fresh tier classification).
    """
    from jacked.web.window_keeper import needs_7d_ping

    if (acct.get("cached_usage_5h") or 0) >= 90:
        resets_at = acct.get("cached_5h_resets_at")
        if resets_at:
            try:
                reset_dt = datetime.fromisoformat(
                    resets_at.replace("Z", "+00:00"),
                )
                if reset_dt.tzinfo is None:
                    reset_dt = reset_dt.replace(tzinfo=timezone.utc)
                if reset_dt <= datetime.now(timezone.utc):
                    return True
            except (ValueError, TypeError):
                pass
    return needs_7d_ping(
        acct.get("cached_7d_resets_at"),
        acct.get("usage_cached_at"),
    )


async def _fetch_candidate_usage(accounts: list, active_acct_id: int, db) -> list:
    """Fetch fresh usage for non-active candidate accounts with stale data.

    Only fetches accounts whose usage_cached_at is older than
    _CANDIDATE_STALENESS_SECONDS — unless ``_candidate_staleness_override``
    forces a refresh (past 5h reset with saturated cache, or 7d window
    reset). Non-active accounts rarely change, so there's no need to hit
    the API every tick. Each fetch is bounded by a 30s timeout and a
    catch-all so one bad candidate can't stall the pass.
    Returns the refreshed accounts list from DB.
    """
    from jacked.web.auth import fetch_usage

    now = int(time.time())
    fetched = 0
    for acct in accounts:
        if acct["id"] == active_acct_id:
            continue
        if acct.get("validation_status") == "invalid":
            continue  # Don't waste API calls on invalid accounts
        cached_at = acct.get("usage_cached_at")
        if (
            cached_at
            and (now - int(cached_at)) < _CANDIDATE_STALENESS_SECONDS
            and not _candidate_staleness_override(acct)
        ):
            continue  # data is fresh enough
        try:
            await asyncio.wait_for(
                fetch_usage(acct["id"], db),
                timeout=_CANDIDATE_FETCH_TIMEOUT_SECONDS,
            )
            fetched += 1
        except Exception:
            logger.warning(
                "Candidate usage: fetch for account %d failed or timed "
                "out — skipping this tick",
                acct["id"],
            )
        await asyncio.sleep(1)

    if fetched:
        logger.debug("Candidate usage: refreshed %d stale accounts", fetched)

    return db.list_accounts(include_inactive=False)


async def _drain_advisor_tick(
    accounts: list[dict],
    now_utc: datetime,
    active_start: str,
    active_end: str,
    ws_registry,
    prev_tiers: dict[int, int],
) -> None:
    """Broadcast 'expiring_with_stranded_capacity' for every T0 account
    (including the active one) projected to strand more than
    ``_DRAIN_ADVISOR_STRANDING_THRESHOLD`` percent of 7d capacity.

    Per-account 30-min cooldown via ``_drain_advisor_last_sent``.
    Advisor only by design — no auto-burn worker; routing real work beats
    burning quota on no-ops.
    """
    from jacked.web.auto_swap import (
        TIER_T0,
        achievable_burn,
        deficit_vs_target,
        format_account_label,
        stranding_estimate,
        tier_for,
    )

    now_ts = time.time()
    for acct in accounts:
        tier = tier_for(acct, now=now_utc, prev_tier=prev_tiers.get(acct["id"]))
        if tier != TIER_T0:
            continue
        stranding = stranding_estimate(
            acct,
            now_utc,
            active_start,
            active_end,
        )
        if stranding is None or stranding <= _DRAIN_ADVISOR_STRANDING_THRESHOLD:
            continue
        last_sent = _drain_advisor_last_sent.get(acct["id"], 0.0)
        if now_ts - last_sent < _DRAIN_ADVISOR_COOLDOWN_SECONDS:
            continue
        _drain_advisor_last_sent[acct["id"]] = now_ts
        deficit = deficit_vs_target(
            acct,
            now=now_utc,
            active_start=active_start,
            active_end=active_end,
        )
        achievable = achievable_burn(
            acct,
            now_utc,
            active_start,
            active_end,
        )
        logger.info(
            "Drain advisor: account %d (%s) expires with ~%.1f%% stranded "
            "capacity (deficit=%.1f%%, achievable=%.1f%%, resets_at=%s)",
            acct["id"],
            acct.get("email", "?"),
            stranding,
            deficit or 0.0,
            achievable or 0.0,
            acct.get("cached_7d_resets_at"),
        )
        if ws_registry:
            await ws_registry.broadcast(
                "expiring_with_stranded_capacity",
                {
                    "account_id": acct["id"],
                    "email": acct.get("email", ""),
                    "label": format_account_label(acct),
                    "deficit": round(deficit, 1) if deficit is not None else None,
                    "achievable": (
                        round(achievable, 1) if achievable is not None else None
                    ),
                    "stranding": round(stranding, 1),
                    "resets_at": acct.get("cached_7d_resets_at"),
                },
            )


def _build_tick_detail(
    active_acct: dict,
    usage_5h: float | None,
    usage_7d: float | None,
    want_swap: bool,
    suppression: dict | None,
    escape_override: bool,
    candidates: list[dict] | None,
    proactive_target_id: int | None,
    cooldown_active: bool,
    decision: str,
) -> dict:
    """Build the detail JSON for a decision log entry."""
    from jacked.web.auto_swap import format_account_label

    detail = {
        "active": {
            "id": active_acct.get("id"),
            "email": active_acct.get("email", ""),
            "label": format_account_label(active_acct),
            "5h": usage_5h,
            "7d": usage_7d,
        },
        "should_swap": want_swap,
        "escape_override": escape_override,
        "cooldown_active": cooldown_active,
        "decision": decision,
    }
    if suppression:
        detail["suppression"] = suppression
    if candidates is not None:
        detail["candidates"] = candidates
    if proactive_target_id is not None:
        detail["proactive_target_id"] = proactive_target_id
    return detail


def _certified_auto_swap_engine(db):
    """Return an explicitly installed transaction engine, never a guess.

    The production Database intentionally has no such attribute today. A
    capability bootstrap may install one only after exact-build capability
    resolution. ``CredentialTransactionEngine.activate`` then rechecks the
    writer fence before and after the write. Until that exists, auto-swap is
    recommendation-only.
    """
    from jacked.credentials.transaction import CredentialTransactionEngine

    engine = getattr(db, "_certified_auto_swap_engine", None)
    return engine if isinstance(engine, CredentialTransactionEngine) else None


def _auto_swap_request(target: dict):
    """Build a background transaction request from one database account."""
    import uuid

    from jacked.api.credential_helpers import build_oauth_data
    from jacked.credentials.canonical import CredentialPayload
    from jacked.credentials.models import InteractionMode, SwitchContext
    from jacked.credentials.transaction import SwitchRequest

    payload = CredentialPayload.from_mapping(
        {
            "_jackedAccountId": target["id"],
            "claudeAiOauth": build_oauth_data(target),
        }
    )
    return SwitchRequest(
        operation_id=f"auto-{uuid.uuid4().hex}",
        account_id=target["id"],
        email=target.get("email") or "",
        organization_id=target.get("organization_uuid") or None,
        payload=payload,
        context=SwitchContext.AUTO_SWAP,
        interaction=InteractionMode.BACKGROUND,
    )


async def _execute_swap(
    db,
    active_acct_id: int,
    active_acct: dict,
    target: dict,
    reason: str,
    trigger: str,
    usage_5h: float | None,
    usage_7d: float | None,
    ws_registry=None,
    transaction_engine=None,
) -> bool:
    """Execute a swap. Returns True if credential write succeeded.

    The transaction repository owns pending/final journal state, committed
    pointers, and swap audit publication. This wrapper owns only scheduling,
    recommendation-only audits, runtime backoff, cache cleanup, and broadcasts.
    """
    global _last_swap_time, _last_committed_swap_time
    global _swap_failure_count, _last_swap_failure_at

    from jacked.api.credential_helpers import invalidate_live_cred_cache
    from jacked.web.auto_swap import format_account_label

    # 1. TOCTOU guard
    current_active = await asyncio.to_thread(_read_active_account_id)
    if current_active != active_acct_id:
        logger.info(
            "Swap aborted: active account changed from %d to %s during evaluation",
            active_acct_id,
            current_active,
        )
        return False

    # No exact-build cooperative capability is registered in current
    # production builds. Persist the recommendation, but do not fall back to
    # the legacy unfenced writer. Global-uncooperative outcomes are likewise
    # not safe for a background mutation.
    transaction_engine = transaction_engine or _certified_auto_swap_engine(db)
    if transaction_engine is None:
        db.record_swap(
            from_account_id=active_acct_id,
            to_account_id=target["id"],
            reason=reason,
            trigger=trigger,
            from_5h=usage_5h,
            from_7d=usage_7d,
            to_5h=target.get("cached_usage_5h"),
            to_7d=target.get("cached_usage_7d"),
            status="recommendation_only",
        )
        logger.info(
            "Auto-swap recommendation only: account %d -> %d; no certified "
            "cooperative credential capability/writer fence is installed",
            active_acct_id,
            target["id"],
        )
        if ws_registry:
            await ws_registry.broadcast(
                "auto_swap_recommended",
                {
                    "from_account_id": active_acct_id,
                    "to_account_id": target["id"],
                    "reason": reason,
                },
            )
        return False

    # Arm the in-memory attempt cooldown before invoking the transactional
    # authority. The repository creates its durable pending row before writes.
    _last_swap_time = time.time()

    # Credential mutation runs off the event loop. The engine serializes
    # writers and atomically finalizes journal + active/desired pointers.
    from jacked.credentials.models import SwitchOutcome

    result = await asyncio.to_thread(
        transaction_engine.activate, _auto_swap_request(target)
    )
    credential_ok = result.outcome in {
        SwitchOutcome.COMMITTED,
        SwitchOutcome.COMMITTED_DEGRADED,
    }
    if credential_ok:
        try:
            db.mark_global_sessions_pending()
        except Exception:
            logger.exception("Failed to mark global sessions pending after auto-swap")

    # Broadcast and runtime cleanup only after the repository committed.
    if credential_ok:
        _last_committed_swap_time = time.time()
        _swap_failure_count = 0
        # Invalidate live credential cache (new account is active now) and
        # clean up burn-rate state — ONLY on success; a failed attempt
        # must not destroy the active account's burn-rate history.
        invalidate_live_cred_cache()
        _burn_rates.pop(active_acct_id, None)
        _burn_rate_unchanged_ticks.pop(active_acct_id, None)
        _burn_rates.pop(target["id"], None)
        _burn_rate_unchanged_ticks.pop(target["id"], None)
        if ws_registry:
            await ws_registry.broadcast(
                "auto_swap_triggered",
                {
                    "from_account_id": active_acct_id,
                    "to_account_id": target["id"],
                    "from_email": active_acct.get("email", ""),
                    "to_email": target.get("email", ""),
                    "from_label": format_account_label(active_acct),
                    "to_label": format_account_label(target),
                    "reason": reason,
                },
            )
    else:
        # Arm the exponential failure backoff; retry pacing for failed
        # attempts is governed by _swap_backoff_remaining, so un-arm the
        # committed-swap cooldown (it would mask the 60/120/240s windows).
        _swap_failure_count += 1
        _last_swap_failure_at = time.time()
        _last_swap_time = 0.0
        logger.warning(
            "Credential transaction failed — retry gated by "
            "failure backoff, attempt %d (account %d -> %d)",
            _swap_failure_count,
            active_acct_id,
            target["id"],
        )
        if ws_registry:
            await ws_registry.broadcast(
                "auto_swap_failed",
                {
                    "from_account_id": active_acct_id,
                    "to_account_id": target["id"],
                    "reason": reason,
                    "failure": "credential_write_failed",
                },
            )

    return credential_ok


async def active_account_poll_loop(app):
    """Poll the active account with adaptive interval for threshold detection.

    Interval adapts based on urgency tier. The tier thresholds + interval
    values are defined ONCE in jacked.web.auth (compute_urgency_tier +
    _TIER_INTERVALS) — that is the source of truth; keep this doc in sync:
    - Idle (5h ≤50%, no burn): 10 min
    - Normal (5h 50-70%, or low burn): 5 min
    - Warning (5h 70-85%, or projects critical within 15 min): 4 min
    - Critical (5h >85%, or projects critical within 5 min): 3 min
    A 7d window above 80% bumps the tier up one. ±15% jitter on each tick to
    prevent sync patterns; the interval can clamp tighter near a window reset.

    Handles auto-swap decisions, TOCTOU guard, burn-rate tracking with
    decay, and descriptive swap reason strings.  Never crashes — all
    errors are caught and logged per tick.
    """
    global _last_exhaustion_warning, _last_swap_time

    _poll_interval: float = 60.0
    _poll_tier: str = "unknown"
    _last_tick_at: float = 0.0

    while True:
        try:
            # External heartbeat — consumed by active_poll_watchdog_loop.
            # MUST be the first statement of the iteration: every early-exit
            # branch below ends in ``continue`` and skips the rest of the
            # body, so a tail-only write starves the watchdog whenever e.g.
            # auto-swap is disabled (2026-06 incident: 56 pointless respawns
            # at exactly 5-min intervals, ERROR-level log spam). Monotonic
            # clock is immune to wall-clock skew.
            app.state.active_poll_last_tick_at = time.monotonic()

            db = getattr(app.state, "db", None)
            if db is None:
                await asyncio.sleep(60)
                continue

            # -- Settings ------------------------------------------------
            auto_swap_enabled = _setting_bool(db, "auto_swap_enabled", False)
            if not auto_swap_enabled:
                await asyncio.sleep(60)
                continue

            # Check pause
            paused_until_str = _setting_str(db, "auto_swap_paused_until", "")
            if paused_until_str:
                try:
                    paused_until = datetime.fromisoformat(
                        paused_until_str.replace("Z", "+00:00"),
                    )
                    if paused_until > datetime.now(timezone.utc):
                        logger.info("Auto-swap paused until %s", paused_until_str)
                        await asyncio.sleep(60)
                        continue
                except (ValueError, TypeError):
                    logger.warning(
                        "Ignoring unparseable pause timestamp: %r",
                        paused_until_str,
                    )

            critical_5h = _setting_float(db, "auto_swap_5h_critical", 90)
            warning_5h = _setting_float(db, "auto_swap_5h_warning", 80)
            check_interval = _setting_float(db, "usage_check_interval", 300)
            active_start = _setting_str(db, "window_keeper_active_start", "06:00")
            active_end = _setting_str(db, "window_keeper_active_end", "23:00")

            _decision_action = "stay"
            _decision_trigger = "tick"
            _decision_target_id = None
            _decision_reason = None
            _candidate_summaries = None
            _suppression = None  # kept for log-schema compat (always None in new flow)

            # -- Late imports (avoid circular deps) ----------------------
            from jacked.web.auth import fetch_usage
            from jacked.api.credential_helpers import read_fresh_active_token
            from jacked.web.auto_swap import (
                should_swap_now,
                pick_best_target,
                update_burn_rate,
                tier_critical_threshold,
                tier_label as _tier_label,
                tier_for,
                target_for_tier,
                deficit_vs_target,
                stranding_estimate,
                format_account_label,
                REASON_PREFIX_HIGHER_TIER,
                REASON_PREFIX_INTRA_TIER,
                REASON_PREFIX_BURN_RATE,
                TIER_T0,
                TIER_T2,
                TIER_EXCLUDED,
            )

            # -- Active account ID ---------------------------------------
            active_acct_id = await asyncio.to_thread(_read_active_account_id)
            if active_acct_id is None:
                logger.debug("Active poll: no active account in credential file")
                await asyncio.sleep(60)
                continue

            global _initial_fetch_done
            global _ticks_since_prune
            if not _initial_fetch_done:
                from jacked.web.auth import fetch_usage as _prime_fetch

                logger.info("Auto-swap: priming usage data for all accounts")
                all_accts = db.list_accounts(include_inactive=False)
                primed = 0
                attempted = 0
                for a in all_accts:
                    if a["id"] != active_acct_id:
                        attempted += 1
                        try:
                            await asyncio.wait_for(
                                _prime_fetch(a["id"], db),
                                timeout=30,
                            )
                            primed += 1
                        except Exception:
                            logger.debug("Prime fetch failed for account %d", a["id"])
                        await asyncio.sleep(1)
                # Nothing to prime (single-account install) counts as done —
                # otherwise this block re-runs every tick forever.
                if attempted == 0 or primed > 0:
                    _initial_fetch_done = True
                    logger.info(
                        "Auto-swap: primed %d/%d accounts",
                        primed,
                        attempted,
                    )

            # -- Fetch usage (fresh token, bypasses cache) ---------------
            effective_token = read_fresh_active_token(active_acct_id)
            try:
                await asyncio.wait_for(
                    fetch_usage(
                        active_acct_id,
                        db,
                        access_token=effective_token,
                    ),
                    timeout=50,
                )
            except asyncio.TimeoutError:
                logger.warning(
                    "Active poll: fetch_usage for account %d exceeded 50s "
                    "— continuing tick with cached data",
                    active_acct_id,
                )

            # -- Read active account data from DB ------------------------
            accounts = db.list_accounts(include_inactive=False)
            active_acct = None
            for acct in accounts:
                if acct["id"] == active_acct_id:
                    active_acct = acct
                    break

            if active_acct is None:
                logger.debug(
                    "Active poll: account %d not in active account list",
                    active_acct_id,
                )
                await asyncio.sleep(60)
                continue

            # Compute adaptive poll interval BEFORE broadcast so the
            # frontend receives _poll_interval / _poll_tier / _last_poll_at
            # and can count down accurately instead of guessing.
            _poll_interval, _poll_tier = _compute_poll_interval(
                active_acct_id,
                db,
                _burn_rates,
            )
            _poll_interval = _clamp_poll_interval(
                _poll_interval,
                accounts,
                active_acct_id,
                datetime.now(timezone.utc),
                active_start,
                active_end,
                _last_observed_tiers,
            )

            # Publish the REAL next-poll schedule to app.state so REST callers
            # (the menu-bar panel's "next refresh in …") read the actual interval
            # the loop will wait — not a re-guessed tier. next_poll ≈ this tick's
            # wall-clock + the computed interval.
            app.state.active_poll_account_id = active_acct_id
            app.state.active_poll_at = int(time.time())
            app.state.active_poll_interval = int(_poll_interval)
            app.state.active_poll_tier = _poll_tier

            # Push fresh usage data to connected dashboards so the
            # countdown timer and usage bars update immediately.
            _ws = getattr(app.state, "ws_registry", None)
            if _ws and active_acct:
                # Whitelist safe fields — new DB columns won't leak by default.
                # Mirrors _account_to_response in routes/auth.py.
                _WS_SAFE_FIELDS = {
                    "id",
                    "provider",
                    "email",
                    "organization_uuid",
                    "organization_name",
                    "display_name",
                    "expires_at",
                    "scopes",
                    "subscription_type",
                    "rate_limit_tier",
                    "has_extra_usage",
                    "priority",
                    "is_active",
                    "is_deleted",
                    "last_used_at",
                    "cached_usage_5h",
                    "cached_usage_7d",
                    "cached_5h_resets_at",
                    "cached_7d_resets_at",
                    "usage_cached_at",
                    "last_error",
                    "last_error_at",
                    "consecutive_failures",
                    "last_validated_at",
                    "validation_status",
                    "created_at",
                    "updated_at",
                    "cc_expires_at",
                    "auto_swap_enabled",
                }
                safe_acct = {
                    k: v for k, v in active_acct.items() if k in _WS_SAFE_FIELDS
                }
                safe_acct["_poll_interval"] = int(_poll_interval)
                safe_acct["_poll_tier"] = _poll_tier
                safe_acct["_last_poll_at"] = int(time.time())
                # Compact binding-model cap so the dashboard can surgically
                # refresh the inline bar (this payload is whitelisted flat
                # fields, not the full AccountResponse the bulk path sends).
                try:
                    from jacked.service.menubar_summary import binding_model_compact

                    _raw = active_acct.get("cached_usage_raw")
                    safe_acct["_binding_model"] = binding_model_compact(
                        json.loads(_raw) if _raw else None
                    )
                except (
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                    KeyError,
                ) as _bm_err:
                    # WARNING, not DEBUG: the service runs at effective INFO, so a
                    # DEBUG line here would be invisible and a recurring parse
                    # failure of our own cached payload (schema drift / corruption)
                    # would silently drop the inline bar every tick. Poll interval
                    # is minutes, so this can't spam.
                    logger.warning(
                        "binding_model_compact failed for account %d: %s",
                        active_acct_id,
                        _bm_err,
                    )
                    safe_acct["_binding_model"] = None
                await _ws.broadcast(
                    "usage_poll_updated",
                    {
                        "account_id": active_acct_id,
                        "account_data": safe_acct,
                    },
                )

            usage_5h = active_acct.get("cached_usage_5h")
            usage_7d = active_acct.get("cached_usage_7d")

            # -- Burn rate (skip if usage unchanged) ---------------------
            prev = _burn_rates.get(active_acct_id)
            current_5h_val = usage_5h or 0

            if prev is not None and current_5h_val == prev.last_check_5h:
                # Usage unchanged — track consecutive ticks
                ticks = _burn_rate_unchanged_ticks.get(active_acct_id, 0) + 1
                _burn_rate_unchanged_ticks[active_acct_id] = ticks
                # Decay after 5+ unchanged ticks at ANY usage level —
                # unchanged usage is direct evidence burn stopped; a
                # frozen rate fires spurious burn-rate swaps after the
                # user goes idle at >=80%.
                if ticks >= 5:
                    prev.rate_5h_per_min *= 0.8
                    prev.rate_7d_per_min *= 0.8
                    if prev.rate_5h_per_min < 0.001:
                        prev.rate_5h_per_min = 0.0
                    if prev.rate_7d_per_min < 0.001:
                        prev.rate_7d_per_min = 0.0
                br = prev
            else:
                # Usage changed — update burn rate and reset tick counter
                _burn_rate_unchanged_ticks[active_acct_id] = 0
                br = update_burn_rate(
                    _burn_rates,
                    active_acct_id,
                    current_5h=current_5h_val,
                    current_7d=usage_7d or 0,
                )

            # -- Tier-aware threshold ------------------------------------
            tier_crit = tier_critical_threshold(active_acct)
            effective_critical = max(tier_crit, critical_5h)

            # -- Tier-aware unified decision ----------------------------
            # Single decision per tick: pick the best candidate across
            # the whole pool, then ask should_swap_now whether to leave
            # the active account. Replaces the prior defensive +
            # proactive split. See spec
            # docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md
            now_utc = datetime.now(timezone.utc)

            # Refresh candidate usage if stale (>10 min). Note: this is
            # called every tick now (was previously gated by want_swap);
            # _CANDIDATE_STALENESS_SECONDS keeps the API call-rate the
            # same for stable data — only candidates we haven't fetched
            # in 10+ minutes get re-fetched.
            accounts = await _fetch_candidate_usage(
                accounts,
                active_acct_id,
                db,
            )

            # Hysteresis: pass last-observed tier per non-active account
            # to suppress jitter-driven flips across tier boundaries.
            best = pick_best_target(
                accounts,
                current_id=active_acct_id,
                active_start=active_start,
                active_end=active_end,
                now=now_utc,
                prev_tiers=_last_observed_tiers,
            )

            reason = should_swap_now(
                active=active_acct,
                best=best,
                burn_rate=br,
                check_interval_min=check_interval / 60,
                critical_5h=effective_critical,
                warning_5h=warning_5h,
                now=now_utc,
                prev_tiers=_last_observed_tiers,
                active_start=active_start,
                active_end=active_end,
            )

            # ---- Anti-jitter persistence on emerged tiers ----
            # Hysteresis-damped tiers — must match what should_swap_now
            # evaluated (see TestActiveTierHysteresis in test_auto_swap).
            best_tier_damped = (
                tier_for(
                    best,
                    now=now_utc,
                    prev_tier=_last_observed_tiers.get(best["id"]),
                )
                if best is not None
                else None
            )
            active_tier_damped = tier_for(
                active_acct,
                now=now_utc,
                prev_tier=_last_observed_tiers.get(active_acct_id),
            )
            _is_emerge_reason = reason is not None and reason.startswith(
                (REASON_PREFIX_HIGHER_TIER, REASON_PREFIX_INTRA_TIER),
            )
            if (
                _is_emerge_reason
                and best_tier_damped == TIER_T0
                and active_tier_damped >= TIER_T2
            ):
                # Fast path: a >=2-tier gap (T0 best vs T2+/unclassified
                # active) cannot be boundary jitter — skip the persistence
                # requirement entirely.
                pass
            else:
                reason = _apply_emergence_persistence(
                    reason=reason,
                    best_tier=best_tier_damped,
                    streak=_emerged_tier_streak,
                    persistence_ticks=_EMERGENCE_PERSISTENCE_TICKS,
                )

            # Build candidate summaries for decision log. Target/deficit
            # computed against the DAMPED tier (target_for_tier) so the
            # log matches what selection actually evaluated.
            _candidate_summaries = []
            for cand in accounts:
                if cand["id"] == active_acct_id:
                    continue
                cand_tier = tier_for(
                    cand,
                    now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
                cand_target = target_for_tier(
                    cand_tier,
                    cand,
                    now_utc,
                    active_start,
                    active_end,
                )
                cand_usage_7d = cand.get("cached_usage_7d")
                cand_deficit = (
                    cand_target - cand_usage_7d
                    if cand_target is not None and cand_usage_7d is not None
                    else None
                )
                cand_stranding = stranding_estimate(
                    cand,
                    now_utc,
                    active_start,
                    active_end,
                )
                _candidate_summaries.append(
                    {
                        "id": cand["id"],
                        "email": cand.get("email", ""),
                        "label": format_account_label(cand),
                        "5h": cand.get("cached_usage_5h"),
                        "7d": cand.get("cached_usage_7d"),
                        "tier": cand_tier,
                        "target_7d": (
                            round(cand_target, 1) if cand_target is not None else None
                        ),
                        "deficit": (
                            round(cand_deficit, 1) if cand_deficit is not None else None
                        ),
                        "stranding": (
                            round(cand_stranding, 1)
                            if cand_stranding is not None
                            else None
                        ),
                        "is_best": (best is not None and cand["id"] == best["id"]),
                    }
                )

            # Refresh hysteresis state + prune dead account ids.
            # The ACTIVE account is included in _last_observed_tiers so
            # should_swap_now's tier_for(active) call picks up hysteresis
            # too — without this, Anthropic API timestamp jitter at the
            # 24h/48h boundary flickers active's tier each tick, causing
            # `best_tier < active_tier` to flip false → reason None →
            # emergence streak clears → 6+ minute swap delays. (Found
            # during DCR cycle on user-reported "6-min swap delay" bug.)
            live_ids = {a["id"] for a in accounts}
            for stale_id in list(_last_observed_tiers.keys()):
                if stale_id not in live_ids:
                    _last_observed_tiers.pop(stale_id, None)
            for stale_id in list(_drain_advisor_last_sent.keys()):
                if stale_id not in live_ids:
                    _drain_advisor_last_sent.pop(stale_id, None)
            for cand in accounts:
                cand_tier = tier_for(
                    cand,
                    now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
                if cand_tier == TIER_EXCLUDED:
                    _last_observed_tiers.pop(cand["id"], None)
                else:
                    _last_observed_tiers[cand["id"]] = cand_tier

            ws_registry = getattr(app.state, "ws_registry", None)

            global _last_exhaustion_warning, _consecutive_no_best_ticks
            global _last_stall_warning, _same_tier_advisory_last
            global _last_committed_swap_time

            # Drain advisor — purely informational; never affects the
            # decision below, so a failure must not abort the tick.
            try:
                await _drain_advisor_tick(
                    accounts,
                    now_utc,
                    active_start,
                    active_end,
                    ws_registry,
                    _last_observed_tiers,
                )
            except Exception:
                logger.debug("Drain advisor failed", exc_info=True)

            _residency_elapsed = time.time() - _last_committed_swap_time
            _residency_gated = reason is not None and reason.startswith(
                (
                    REASON_PREFIX_HIGHER_TIER,
                    REASON_PREFIX_INTRA_TIER,
                    REASON_PREFIX_BURN_RATE,
                )
            )

            if reason is None:
                _decision_action = "stay"
                if best is None:
                    _decision_trigger = "tick"
                    _decision_reason = (
                        f"stay: no candidate has deficit "
                        f"(tier {_tier_label(active_acct).strip() or 'unset'})"
                    )
                else:
                    _decision_target_id = best["id"]
                    # Tier-keyed streak: count applies to ANY account in
                    # the tracked tier. count > 0 here means persistence
                    # suppressed an emerge reason this very tick (the
                    # helper clears the streak on decisive non-emerge).
                    streak_count = (
                        _emerged_tier_streak["count"]
                        if _emerged_tier_streak["tier"] == best_tier_damped
                        else 0
                    )
                    if best_tier_damped != TIER_EXCLUDED and streak_count > 0:
                        _decision_trigger = "emergence_pending"
                        _decision_reason = (
                            f"stay: emergence streak "
                            f"{streak_count}/{_EMERGENCE_PERSISTENCE_TICKS}, "
                            f"awaiting confirmation "
                            f"(best id={best['id']} tier={best_tier_damped} "
                            f"vs active tier={active_tier_damped})"
                        )
                    else:
                        _decision_trigger = "tick"
                        _decision_reason = (
                            f"stay: best is same/lower tier "
                            f"(best id={best['id']} tier={best_tier_damped})"
                        )
            elif _residency_gated and _residency_elapsed < _MIN_RESIDENCY_SECONDS:
                # Min-residency gate: proactive departures must let the
                # active account settle. Drained / 5h-critical reasons are
                # forced departures and bypass this gate.
                _decision_action = "stay"
                _decision_trigger = "residency_blocked"
                _decision_target_id = best["id"] if best else None
                _decision_reason = (
                    f"swap warranted ({reason}) but minimum residency "
                    f"active "
                    f"({_MIN_RESIDENCY_SECONDS - _residency_elapsed:.0f}s remaining)"
                )
                logger.debug("Active poll: %s", _decision_reason)
            elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                _decision_action = "stay"
                _decision_trigger = "cooldown_blocked"
                _decision_target_id = best["id"] if best else None
                _decision_reason = (
                    f"swap warranted ({reason}) but cooldown active "
                    f"({_SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time):.0f}s remaining)"
                )
                logger.debug("Active poll: %s", _decision_reason)
            elif best is None:
                _decision_action = "stay"
                _decision_trigger = "no_target"
                _decision_reason = f"swap warranted ({reason}) but no eligible target"

                now_ts = time.time()
                if now_ts - _last_exhaustion_warning > _EXHAUSTION_COOLDOWN_SECONDS:
                    logger.warning(
                        "Auto-swap needed but no eligible target "
                        "(active account %d at 5h=%.1f%%)",
                        active_acct_id,
                        usage_5h or 0,
                    )
                    _last_exhaustion_warning = now_ts

                next_recovery_at = None
                for acct in accounts:
                    resets = acct.get("cached_5h_resets_at")
                    if not resets:
                        continue
                    try:
                        r = datetime.fromisoformat(resets.replace("Z", "+00:00"))
                        if r > now_utc and (
                            next_recovery_at is None or r < next_recovery_at
                        ):
                            next_recovery_at = r
                    except (ValueError, TypeError):
                        continue

                if ws_registry:
                    await ws_registry.broadcast(
                        "all_accounts_exhausted",
                        {
                            "active_account_id": active_acct_id,
                            "usage_5h": usage_5h,
                            "usage_7d": usage_7d,
                            "next_recovery_at": (
                                next_recovery_at.isoformat()
                                if next_recovery_at
                                else None
                            ),
                        },
                    )
            elif (_backoff_remaining := _swap_backoff_remaining()) > 0:
                _decision_action = "stay"
                _decision_trigger = "swap_aborted"
                _decision_target_id = best["id"]
                _decision_reason = (
                    f"swap warranted ({reason}) but failure backoff "
                    f"active ({_backoff_remaining:.0f}s remaining after "
                    f"{_swap_failure_count} failed attempt(s))"
                )
                logger.debug("Active poll: %s", _decision_reason)
            else:
                trigger = _trigger_for_reason(reason)
                logger.info(
                    "Auto-swap: switching from account %d (5h=%.1f%%) to "
                    "account %d (5h=%.1f%%) — %s [%s]",
                    active_acct_id,
                    usage_5h or 0,
                    best["id"],
                    best.get("cached_usage_5h") or 0,
                    reason,
                    trigger,
                )
                transaction_engine = _certified_auto_swap_engine(db)
                swap_committed = await _execute_swap(
                    db,
                    active_acct_id,
                    active_acct,
                    best,
                    reason=reason,
                    trigger=trigger,
                    usage_5h=usage_5h,
                    usage_7d=usage_7d,
                    ws_registry=ws_registry,
                    transaction_engine=transaction_engine,
                )
                if swap_committed:
                    _emerged_tier_streak["tier"] = None
                    _emerged_tier_streak["count"] = 0
                    # Clear hysteresis state for both swap participants —
                    # the new active needs a fresh tier observation, and
                    # the prior active will be re-observed as a candidate
                    # next tick. The next tick's hysteresis-refresh block
                    # repopulates both based on current data.
                    _last_observed_tiers.pop(best["id"], None)
                    _last_observed_tiers.pop(active_acct_id, None)
                    _decision_action = "swap"
                    _decision_trigger = trigger
                    _decision_target_id = best["id"]
                    _decision_reason = reason
                elif transaction_engine is None:
                    _decision_action = "recommend"
                    _decision_trigger = "recommendation_only"
                    _decision_target_id = best["id"]
                    _decision_reason = (
                        "swap recommended but automatic credential mutation "
                        f"is not certified: {reason}"
                    )
                else:
                    # TOCTOU mismatch or credential lock failure. Don't
                    # clear streak/hysteresis — retry pacing is governed
                    # by the failure backoff. Decision log records the
                    # attempt and the failure so operators see "we tried"
                    # rather than silent state-corruption.
                    _decision_action = "stay"
                    _decision_trigger = "swap_aborted"
                    _decision_target_id = best["id"]
                    _decision_reason = (
                        f"swap aborted (credential write or TOCTOU failed): {reason}"
                    )

            # ---- Silent-stall watchdog ------------------------------------
            # `reason` reflects should_swap_now's verdict AS POSSIBLY
            # MUTATED by emergence persistence (which may set it to
            # None to defer a higher-tier swap until streak met).
            # Cooldown does NOT mutate reason — only persistence does.
            cached_at = active_acct.get("usage_cached_at") or 0
            age_seconds = int(time.time()) - int(cached_at)
            has_other_accounts = (
                sum(1 for a in accounts if a["id"] != active_acct_id) > 0
            )
            best_deficit_val = None
            if best is not None:
                _bd = deficit_vs_target(
                    best,
                    now=now_utc,
                    active_start=active_start,
                    active_end=active_end,
                )
                if _bd is not None:
                    best_deficit_val = _bd
            stalled_this_tick = _evaluate_stall(
                decision_action=_decision_action,
                best=best,
                usage_cached_at_age_seconds=age_seconds,
                has_other_accounts=has_other_accounts,
                reason=reason,
                staleness_threshold=_STALL_USAGE_STALENESS_SECONDS,
            )

            # Same-tier-deficit advisory (split from stall pattern d):
            # intended-behavior stays must not page at ERROR. T0/T1
            # candidates only — those are harvestable drain-to targets.
            if _same_tier_advisory_applies(
                decision_action=_decision_action,
                best=best,
                reason=reason,
                best_deficit=best_deficit_val,
                best_tier=best_tier_damped,
            ):
                now_ts = time.time()
                if (
                    now_ts - _same_tier_advisory_last
                    > _SAME_TIER_ADVISORY_COOLDOWN_SECONDS
                ):
                    _same_tier_advisory_last = now_ts
                    logger.info(
                        "Same-tier deficit advisory: best id=%s "
                        "(tier=%s) is %.1f%% behind its tier target but "
                        "same-tier-never-overrides keeps the loop on "
                        "stay (active=%d)",
                        best["id"],
                        best_tier_damped,
                        best_deficit_val,
                        active_acct_id,
                    )
                    if ws_registry:
                        await ws_registry.broadcast(
                            "same_tier_deficit_advisory",
                            {
                                "active_account_id": active_acct_id,
                                "best_account_id": best["id"],
                                "best_tier": best_tier_damped,
                                "best_deficit": round(best_deficit_val, 1),
                            },
                        )
            was_stalled = _consecutive_no_best_ticks >= _STALL_TICK_THRESHOLD
            if stalled_this_tick:
                _consecutive_no_best_ticks += 1
            else:
                _consecutive_no_best_ticks = 0

            # Stall transition: was-stalled → now-cleared. Broadcast
            # so the dashboard banner can hide itself instead of
            # remaining stuck until manual dismiss + 30 min cooldown.
            now_stalled = _consecutive_no_best_ticks >= _STALL_TICK_THRESHOLD
            if was_stalled and not now_stalled:
                logger.info(
                    "Auto-swap stall cleared (active=%d)",
                    active_acct_id,
                )
                if ws_registry:
                    await ws_registry.broadcast(
                        "auto_swap_stall_clear",
                        {"active_account_id": active_acct_id},
                    )

            if now_stalled:
                now_ts = time.time()
                if now_ts - _last_stall_warning > _STALL_WARNING_COOLDOWN_SECONDS:
                    last_fetch_age = age_seconds
                    logger.error(
                        "Auto-swap stalled: %d consecutive ticks "
                        "(active=%d, last_fetch=%ss ago, best=%s, "
                        "best_deficit=%s)",
                        _consecutive_no_best_ticks,
                        active_acct_id,
                        last_fetch_age,
                        best["id"] if best else None,
                        f"{best_deficit_val:.1f}%" if best_deficit_val else "n/a",
                    )
                    _last_stall_warning = now_ts
                    if ws_registry:
                        await ws_registry.broadcast(
                            "auto_swap_stall",
                            {
                                "active_account_id": active_acct_id,
                                "consecutive_ticks": _consecutive_no_best_ticks,
                                "last_fetch_age_seconds": last_fetch_age,
                                "best_account_id": best["id"] if best else None,
                                "best_deficit": best_deficit_val,
                            },
                        )

            # Record decision in the log
            if active_acct is not None:
                try:
                    _tick_detail = _build_tick_detail(
                        active_acct=active_acct,
                        usage_5h=usage_5h,
                        usage_7d=usage_7d,
                        want_swap=(_decision_action == "swap"),
                        suppression=_suppression,
                        escape_override=False,
                        candidates=_candidate_summaries,
                        proactive_target_id=None,
                        cooldown_active=(time.time() - _last_swap_time)
                        < _SWAP_COOLDOWN_SECONDS,
                        decision=_decision_action,
                    )
                    decision_id = db.record_decision(
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger=_decision_trigger,
                        target_id=_decision_target_id,
                        reason=_decision_reason or "no trigger",
                        detail=_tick_detail,
                    )
                    if ws_registry and decision_id:
                        try:
                            await ws_registry.broadcast(
                                "decision_log_entry",
                                {
                                    "id": decision_id,
                                    "account_id": active_acct_id,
                                    "email": active_acct.get("email", ""),
                                    "label": format_account_label(active_acct),
                                    "action": _decision_action,
                                    "trigger": _decision_trigger,
                                    "reason": _decision_reason or "no trigger",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "detail": _tick_detail,
                                },
                            )
                        except Exception:
                            logger.debug(
                                "Decision log WS broadcast failed", exc_info=True
                            )
                except Exception:
                    logger.debug("Failed to record decision", exc_info=True)

            # Periodic prune — deterministic fallback every 500 ticks
            _ticks_since_prune += 1
            if _ticks_since_prune >= 500 or random.random() < 0.01:
                try:
                    db.prune_decision_log()
                    _ticks_since_prune = 0
                except Exception:
                    logger.warning("Failed to prune decision log", exc_info=True)

        except asyncio.CancelledError:
            logger.info("Active account poll loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Active account poll loop error", exc_info=True)

        # Watchdog: detect if the event loop was blocked or suspended
        now_tick = time.time()
        if (
            _last_tick_at > 0
            and _poll_interval > 0
            and (now_tick - _last_tick_at) > 2 * _poll_interval
        ):
            logger.warning(
                "Active poll loop delayed — last tick %ds ago, expected interval %ds",
                int(now_tick - _last_tick_at),
                int(_poll_interval),
            )
        _last_tick_at = now_tick

        # Refresh the heartbeat after the tick body too. The authoritative
        # write is at the TOP of the iteration (it covers all ``continue``
        # early exits); this tail write only resets the staleness clock so
        # it measures from the start of the upcoming sleep rather than
        # from tick start — a single tick can legitimately run long
        # (priming fetches, swap execution).
        try:
            app.state.active_poll_last_tick_at = time.monotonic()
        except Exception:
            # app.state vanishing means we're in lifespan teardown; let the
            # natural cancellation finish the loop on the next await.
            pass

        logger.debug("Active poll: tier=%s interval=%.0fs", _poll_tier, _poll_interval)
        await asyncio.sleep(_poll_interval)


# -----------------------------------------------------------------------
# Loop 1b — Watchdog for active_account_poll_loop
# -----------------------------------------------------------------------

# The active poll task can be silently dead-on-arrival under a specific
# race observed in production on 2026-05-10: tray force-exits uvicorn,
# lifespan creates a new task, but the task never gets its first slice
# of CPU. No exception fires, no log. Heartbeat-based detection is the
# only reliable signal.
_WATCHDOG_INTERVAL_SECONDS = 60
_HEARTBEAT_STALE_SECONDS = 300  # 5min — 5x default 60s interval, 1x worst-case 300s
_RESPAWN_COOLDOWN_SECONDS = 30  # prevent thrash if respawned task also dies fast


def _respawn_active_poll(app) -> bool:
    """Cancel any stale active_poll_task and start a fresh one.

    Returns True if a new task was created. Idempotent — safe to call
    when the existing task is healthy (it gets cancelled and replaced;
    the new task picks up the next tick).
    """
    old_task = getattr(app.state, "active_poll_task", None)
    if old_task is not None and not old_task.done():
        try:
            old_task.cancel()
        except Exception:
            logger.warning("Failed to cancel stale active_poll_task", exc_info=True)
    try:
        new_task = asyncio.create_task(active_account_poll_loop(app))
    except Exception:
        logger.error("Failed to respawn active_poll_task", exc_info=True)
        return False
    app.state.active_poll_task = new_task
    # Seed heartbeat so the watchdog grants the new task a full interval
    # to tick before declaring it stale again.
    app.state.active_poll_last_tick_at = time.monotonic()
    app.state.active_poll_last_respawn_at = time.monotonic()
    return True


async def active_poll_watchdog_loop(app):
    """Detect and recover from a silently-dead active_account_poll_loop.

    Checks ``app.state.active_poll_last_tick_at`` every minute. If the
    heartbeat is stale (no tick for > 5min) OR the task object is done()
    when it shouldn't be, respawn it via ``_respawn_active_poll``.

    Never crashes — exceptions are caught and logged per tick so the
    watchdog itself doesn't become the dead loop it was built to detect.
    """
    # Grace period: lifespan may still be wiring app.state. Skip first check.
    await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)

    while True:
        try:
            now = time.monotonic()
            last_tick = getattr(app.state, "active_poll_last_tick_at", None)
            last_respawn = getattr(app.state, "active_poll_last_respawn_at", 0.0)
            task = getattr(app.state, "active_poll_task", None)

            # Cooldown: if we just respawned, give the new task a full
            # cycle before considering another respawn.
            if last_respawn and (now - last_respawn) < _RESPAWN_COOLDOWN_SECONDS:
                await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)
                continue

            stale = last_tick is None or (now - last_tick) > _HEARTBEAT_STALE_SECONDS
            task_dead = task is not None and task.done()

            if stale or task_dead:
                logger.error(
                    "active_poll_watchdog: detected dead task — "
                    "heartbeat_age=%s done=%s — respawning",
                    f"{int(now - last_tick)}s" if last_tick else "never",
                    task_dead,
                )
                if _respawn_active_poll(app):
                    logger.info("active_poll_watchdog: respawn succeeded")

        except asyncio.CancelledError:
            logger.info("Active poll watchdog cancelled — shutting down")
            raise
        except Exception:
            logger.warning("active_poll_watchdog error", exc_info=True)

        await asyncio.sleep(_WATCHDOG_INTERVAL_SECONDS)


# -----------------------------------------------------------------------
# Loop 1b — Non-active account usage refresh (10 min)
# -----------------------------------------------------------------------

_ALL_ACCOUNTS_REFRESH_INTERVAL = 600  # 10 minutes


async def all_accounts_refresh_loop(app):
    """Refresh usage for NON-active accounts every 10 minutes.

    The active account is kept fresh by active_account_poll_loop (≤5 min); this
    loop ensures EVERY other account is also refreshed so quota burned on OTHER
    machines (shared accounts) shows up here — not just the one in use. This is
    unconditional: unlike full_sweep_loop it does NOT depend on the window
    keeper being enabled.

    Pacing/safety: fetch_usage enforces its own hard per-account rate-limit
    ceiling (no 429s), we skip the active account, skip Claude accounts without a
    CC token (Codex accounts have none but are polled via the app-server), and
    sleep between accounts. Never crashes — errors are logged per account/tick.
    """
    while True:
        # Sleep first — at startup the active loop + login already populate caches.
        await asyncio.sleep(_ALL_ACCOUNTS_REFRESH_INTERVAL)
        try:
            db = getattr(app.state, "db", None)
            if db is None:
                continue
            from jacked.web.auth import fetch_usage

            active_id = getattr(app.state, "active_poll_account_id", None)
            refreshed = 0
            for acct in db.list_accounts(include_inactive=False):
                if acct.get("id") == active_id:
                    continue  # covered (more often) by active_account_poll_loop
                is_codex = (acct.get("provider") or "claude") == "codex"
                cc_at = acct.get("cc_access_token")
                # Claude accounts fetch usage with their CC token; a Claude account
                # without one can't be polled. Codex usage comes from the
                # app-server (no token), so it must NOT be skipped here — that
                # CC-token skip is exactly why Codex usage stayed at 0%.
                if not is_codex and not cc_at:
                    continue
                try:
                    # fetch_usage dispatches on provider; for codex it ignores the
                    # token and only polls the live codex account (others no-op).
                    await fetch_usage(
                        acct["id"], db, access_token=None if is_codex else cc_at
                    )
                    refreshed += 1
                except Exception:
                    logger.warning(
                        "All-accounts refresh: fetch_usage failed for account %s",
                        acct.get("id"),
                        exc_info=True,
                    )
                await asyncio.sleep(2)  # pace between accounts
            if refreshed:
                logger.info(
                    "All-accounts refresh: updated %d non-active account(s)", refreshed
                )
        except asyncio.CancelledError:
            logger.info("All-accounts refresh loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("All-accounts refresh loop error", exc_info=True)


# -----------------------------------------------------------------------
# Loop 2 — Full sweep (configurable interval, default 5min)
# -----------------------------------------------------------------------


async def full_sweep_loop(app):
    """Fetch usage for all non-active accounts and run window keeper.

    Runs at the user-configurable ``usage_check_interval`` (default 300s).
    Never crashes — all errors are caught and logged per tick.
    Emits a heartbeat INFO log at the TOP of every iteration (before
    any early-return shortcut), so operators see a heartbeat regardless
    of window-keeper state (0.41.23).
    """
    _default_interval = 300
    iter_count = 0

    while True:
        iter_count += 1
        logger.info("Full-sweep heartbeat: iter=%d", iter_count)
        check_interval = _default_interval
        try:
            db = getattr(app.state, "db", None)
            if db is None:
                await asyncio.sleep(60)
                continue

            # -- Settings ------------------------------------------------
            window_keeper_enabled = _setting_bool(db, "window_keeper_enabled", False)
            check_interval = _setting_float(db, "usage_check_interval", 300)

            if not window_keeper_enabled:
                await asyncio.sleep(check_interval)
                continue

            # -- Late imports --------------------------------------------
            from jacked.web.auth import fetch_usage
            from jacked.web.window_keeper import (
                is_active_hours,
                is_prewake_time,
                needs_ping,
                needs_7d_ping,
                ping_account,
            )

            # -- Window keeper -------------------------------------------
            accounts = db.list_accounts(include_inactive=False)
            sweep_pinged = 0

            wk_start = _setting_str(db, "window_keeper_active_start", "06:00")
            wk_end = _setting_str(db, "window_keeper_active_end", "23:00")
            wk_prewake = _setting_str(db, "window_keeper_prewake", "04:00")

            # Local time intentional: users configure active hours in
            # their local timezone (e.g. "06:00" means local 6am).
            now = datetime.now()
            should_ping = is_active_hours(
                now, start=wk_start, end=wk_end
            ) or is_prewake_time(
                now,
                prewake=wk_prewake,
                check_interval_min=check_interval / 60,
            )

            if should_ping:
                for acct in accounts:
                    needs_5h = needs_ping(acct.get("cached_5h_resets_at"))
                    needs_7d = needs_7d_ping(
                        acct.get("cached_7d_resets_at"),
                        acct.get("usage_cached_at"),
                    )
                    if not needs_5h and not needs_7d:
                        continue
                    if not acct.get("auto_swap_enabled"):
                        continue
                    cc_at = acct.get("cc_access_token")
                    if not cc_at:
                        continue
                    _backoff = _ping_backoff.get(acct["id"])
                    if _backoff and _backoff["skip_until"] > time.monotonic():
                        logger.debug(
                            "Window keeper: account %d in ping backoff for "
                            "%.0fs more (%d consecutive failures) — skipping",
                            acct["id"],
                            _backoff["skip_until"] - time.monotonic(),
                            _backoff["fails"],
                        )
                        continue

                    logger.info(
                        "Window keeper: pinging account %d (%s)%s%s",
                        acct["id"],
                        acct.get("email", "?"),
                        " [5h expired]" if needs_5h else "",
                        " [7d reset]" if needs_7d else "",
                    )
                    success = await ping_account(cc_at)
                    if not success and acct.get("cc_refresh_token"):
                        # Never rotate the active account's CC refresh token —
                        # Claude Code still holds the pre-rotation value in its
                        # Keychain and will hit invalid_grant on next refresh.
                        # See architecture doc §7.3 and invariant I2.
                        # For the active account, reconcile from live creds
                        # instead (Claude Code keeps its own token fresh).
                        from jacked.api.credential_helpers import read_active_account_id

                        active_id_now = read_active_account_id()
                        if active_id_now == acct["id"]:
                            logger.info(
                                "Window keeper: skipping CC refresh for "
                                "active account %d — reconciling instead",
                                acct["id"],
                            )
                            try:
                                from jacked.api.credential_helpers import (
                                    reconcile_credentials_from_live_store,
                                )

                                reconcile_credentials_from_live_store(acct["id"], db)
                                fresh_acct = db.get_account(acct["id"])
                                fresh_cc = (
                                    fresh_acct.get("cc_access_token")
                                    if fresh_acct
                                    else None
                                )
                                if fresh_cc and fresh_cc != cc_at:
                                    success = await ping_account(fresh_cc)
                            except Exception:
                                logger.exception(
                                    "Window keeper reconcile failed for active account %d",
                                    acct["id"],
                                )
                        else:
                            from jacked.web.auth import refresh_cc_token

                            refreshed = await refresh_cc_token(acct["id"], db)
                            if refreshed:
                                fresh_acct = db.get_account(acct["id"])
                                fresh_cc = (
                                    fresh_acct.get("cc_access_token")
                                    if fresh_acct
                                    else None
                                )
                                if fresh_cc and fresh_cc != cc_at:
                                    success = await ping_account(fresh_cc)
                    if success:
                        _ping_backoff.pop(acct["id"], None)
                        sweep_pinged += 1
                        # Fetch fresh usage so cached_5h_resets_at updates
                        # and needs_ping returns False next sweep.
                        # Pass access_token to bypass the cache freshness guard.
                        try:
                            await asyncio.wait_for(
                                fetch_usage(acct["id"], db, access_token=cc_at),
                                timeout=10.0,
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "Full sweep: fetch_usage for account %d "
                                "exceeded 10s — moving on",
                                acct["id"],
                            )
                    else:
                        # Still failing after the refresh/reconcile fallbacks
                        # above — arm exponential backoff so the next sweeps
                        # skip this account instead of hammering it.
                        _backoff = _ping_backoff.setdefault(
                            acct["id"],
                            {"fails": 0, "skip_until": 0.0},
                        )
                        _backoff["fails"] += 1
                        _delay = min(
                            check_interval * 2 ** _backoff["fails"],
                            _PING_BACKOFF_CAP_SECONDS,
                        )
                        _backoff["skip_until"] = time.monotonic() + _delay
                        logger.info(
                            "Window keeper: ping failed for account %d — "
                            "backing off %.0fs (%d consecutive failures)",
                            acct["id"],
                            _delay,
                            _backoff["fails"],
                        )
                    await asyncio.sleep(2)  # pacing

            logger.info(
                "Full sweep complete: pinged %d windows",
                sweep_pinged,
            )

        except asyncio.CancelledError:
            logger.info("Full sweep loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Full sweep loop error", exc_info=True)

        # Sleep in short increments, checking wake signal between each.
        # This lets settings changes (e.g. toggling window keeper on)
        # trigger an immediate sweep instead of waiting the full interval.
        _slept = 0.0
        while _slept < check_interval and not _sweep_wake.is_set():
            await asyncio.sleep(min(5, check_interval - _slept))
            _slept += 5
        if _sweep_wake.is_set():
            _sweep_wake.clear()
            logger.info("Full sweep woken early by settings change")
