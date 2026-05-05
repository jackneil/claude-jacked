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
from pathlib import Path
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

# Track consecutive unchanged ticks per account for burn-rate decay.
_burn_rate_unchanged_ticks: dict[int, int] = {}
_initial_fetch_done = False
_ticks_since_prune = 0

# Wake signal — settings PUT sets this to trigger an immediate sweep.
_sweep_wake: asyncio.Event = asyncio.Event()

# Per-account last-observed tier for hysteresis. Persists across ticks;
# cleared when an account is removed or a swap occurs to/from it.
_last_observed_tiers: dict[int, int] = {}

# Anti-jitter: require the same higher-tier target to persist across
# ``_EMERGENCE_PERSISTENCE_TICKS`` consecutive ticks before swapping.
# Maps target account id -> consecutive-tick count.
_emerged_target_streak: dict[int, int] = {}
_EMERGENCE_PERSISTENCE_TICKS = 2

# Silent-stall watchdog state.
_consecutive_no_best_ticks: int = 0
_last_stall_warning: float = 0.0
_STALL_TICK_THRESHOLD = 10
_STALL_USAGE_STALENESS_SECONDS = 1800
_STALL_WARNING_COOLDOWN_SECONDS = 1800


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
    _emerged_target_streak.clear()
    global _consecutive_no_best_ticks, _last_stall_warning
    _consecutive_no_best_ticks = 0
    _last_stall_warning = 0.0


def _read_active_account_id() -> int | None:
    """Read the active account ID from the credential file stamp.

    Returns the _jackedAccountId integer, or None if unreadable.
    """
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if not cred_path.exists() or cred_path.is_symlink():
        return None
    try:
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        return data.get("_jackedAccountId")
    except (json.JSONDecodeError, OSError):
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
    best_id: int | None,
    streak: dict[int, int],
    persistence_ticks: int,
) -> str | None:
    """Gate "higher tier emerged" reasons behind a multi-tick streak.

    Anti-jitter: a candidate must remain the best ``persistence_ticks``
    times in a row before the swap actually fires. Mutates ``streak``
    in place — increments the count for ``best_id``, prunes any other
    keys, preserves on transient absence, or clears entirely on
    explicit non-emerge reason.

    Streak handling rules:

    - ``reason`` starts with ``REASON_PREFIX_HIGHER_TIER`` and ``best_id``
      is set: increment streak for ``best_id``, prune other keys.
      Return reason if streak met threshold, else None.
    - ``reason`` is None and ``best_id`` is None (transient absence,
      e.g. _fetch_candidate_usage hiccup): PRESERVE the streak. A
      single-tick candidate fetch glitch should not reset the 2-minute
      clock. Next tick with the same best will resume incrementing.
      Return None.
    - ``reason`` is set but does NOT start with the higher-tier prefix
      (drained / 5h critical / burn-rate): clear streak (different
      decision path now). Return reason.
    - ``reason`` is None but emergence-prefix detection happened with
      best_id missing: shouldn't normally occur because `should_swap_now`
      only emits the higher-tier reason when best is non-None. Defensive
      clear + return None.

    Returns the original ``reason`` when persistence is met (let the
    swap fire), or None to suppress this tick.
    """
    from jacked.web.auto_swap import REASON_PREFIX_HIGHER_TIER

    if reason is None and best_id is None:
        # Transient: no candidate, no swap reason. Preserve streak so a
        # single-tick fetch glitch doesn't restart the persistence clock.
        return None
    if reason is None or not reason.startswith(REASON_PREFIX_HIGHER_TIER):
        # Explicit non-emerge outcome — drained/critical/burn-rate fired
        # OR active was decisively classified as no-swap-needed. Clear.
        streak.clear()
        return reason
    if best_id is None:
        # Defensive: should not happen (rule 1 requires best != None).
        streak.clear()
        return None
    streak[best_id] = streak.get(best_id, 0) + 1
    for stale_id in list(streak.keys()):
        if stale_id != best_id:
            del streak[stale_id]
    if streak[best_id] < persistence_ticks:
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
    best_deficit: float | None = None,
    same_tier_stay_threshold: float = 15.0,
) -> bool:
    """Return True if this tick qualifies as a stall pattern.

    Four patterns trigger a bump (any one):
      (a) Multi-account stale: stay+no-best+stale active+has others
      (b) Single-account forced-out: only one account, departure reason fired
      (c) Drained-no-candidate: any reason fired but no eligible target
      (d) Same-tier-stay-with-meaningful-deficit: a candidate exists every
          tick but the spec's "same-tier-never-overrides" rule keeps the
          loop on stay forever even though the candidate is materially
          behind tier target. Without this pattern, a T2 active sitting on
          the same tier as a T2 candidate with 25% deficit looks identical
          to "healthy steady state" — invisible to operators.
          Threshold: candidate must be at least
          ``same_tier_stay_threshold`` percent behind its tier target.
          Caller is responsible for the time-window — the watchdog
          `_STALL_TICK_THRESHOLD` already gates how long this can fire
          before the user-visible alert.

    Returns False otherwise (caller resets the counter).
    """
    if decision_action != "stay":
        return False
    stale = usage_cached_at_age_seconds > staleness_threshold
    forced_out = reason is not None
    pattern_a = best is None and stale and has_other_accounts
    pattern_b = not has_other_accounts and forced_out
    pattern_c = best is None and forced_out
    pattern_d = (
        best is not None
        and reason is None
        and best_deficit is not None
        and best_deficit >= same_tier_stay_threshold
    )
    return pattern_a or pattern_b or pattern_c or pattern_d


def _trigger_for_reason(reason: str | None) -> str:
    """Map a should_swap_now reason-string to a decision-log trigger
    taxonomy value. Stable contract — see spec
    docs/superpowers/specs/2026-05-04-auto-swap-utilization-redesign-design.md.
    """
    from jacked.web.auto_swap import (
        REASON_PREFIX_HIGHER_TIER,
        REASON_PREFIX_DRAINED,
        REASON_PREFIX_FIVE_H,
        REASON_PREFIX_BURN_RATE,
    )
    if reason is None:
        return "tick"
    if reason.startswith(REASON_PREFIX_HIGHER_TIER):
        return "higher_tier_emerged"
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
        from jacked.web.auth import compute_urgency_tier, _get_usage_state, _TIER_INTERVALS
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


_CANDIDATE_STALENESS_SECONDS = 600  # 10 minutes — non-active accounts rarely change


async def _fetch_candidate_usage(accounts: list, active_acct_id: int, db) -> list:
    """Fetch fresh usage for non-active candidate accounts with stale data.

    Only fetches accounts whose usage_cached_at is older than
    _CANDIDATE_STALENESS_SECONDS. Non-active accounts rarely change,
    so there's no need to hit the API every tick.
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
        if cached_at and (now - int(cached_at)) < _CANDIDATE_STALENESS_SECONDS:
            continue  # data is fresh enough
        await fetch_usage(acct["id"], db)
        fetched += 1
        await asyncio.sleep(1)

    if fetched:
        logger.debug("Candidate usage: refreshed %d stale accounts", fetched)

    return db.list_accounts(include_inactive=False)


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


async def _execute_swap(
    db,
    active_acct_id: int,
    active_acct: dict,
    target: dict,
    reason: str,
    trigger: str,
    usage_5h: float | None,
    usage_7d: float | None,
    active_start: str,
    active_end: str,
    ws_registry=None,
) -> bool:
    """Execute a swap. Returns True if credential write succeeded.

    Canonical ordering:
    1. TOCTOU guard
    2. Record swap + arm cooldown (audit trail survives credential failure)
    3. Reconcile outgoing credentials
    4. Write incoming credentials under cross-process lock
    5. Clean up burn-rate state
    6. Broadcast via WebSocket
    """
    global _last_swap_time

    from jacked.api.credential_helpers import (
        acquire_claude_lock,
        invalidate_live_cred_cache,
        reconcile_credentials_from_live_store,
        sync_credential_to_all_stores,
    )
    from jacked.web.auto_swap import format_account_label

    # 1. TOCTOU guard
    current_active = _read_active_account_id()
    if current_active != active_acct_id:
        logger.info(
            "Swap aborted: active account changed from %d to %s during evaluation",
            active_acct_id, current_active,
        )
        return False

    # 2. Record swap + arm cooldown BEFORE credential write
    _last_swap_time = time.time()
    db.record_swap(
        from_account_id=active_acct_id,
        to_account_id=target["id"],
        reason=reason,
        trigger=trigger,
        from_5h=usage_5h,
        from_7d=usage_7d,
        to_5h=target.get("cached_usage_5h"),
        to_7d=target.get("cached_usage_7d"),
    )

    # 3. Reconcile outgoing credentials
    reconcile_credentials_from_live_store(active_acct_id, db)

    # 4. Write incoming credentials under cross-process lock
    credential_ok = False
    with acquire_claude_lock() as locked:
        if locked:
            sync_credential_to_all_stores(
                target["id"], target,
                email=target.get("email"),
            )
            credential_ok = True
        else:
            logger.warning(
                "Swap: could not acquire lock for credential write "
                "(account %d -> %d)", active_acct_id, target["id"],
            )

    # 5. Invalidate live credential cache (new account is active now)
    invalidate_live_cred_cache()

    # 6. Clean up burn-rate state
    _burn_rates.pop(active_acct_id, None)
    _burn_rate_unchanged_ticks.pop(active_acct_id, None)
    _burn_rates.pop(target["id"], None)
    _burn_rate_unchanged_ticks.pop(target["id"], None)

    # 7. Sync DB active_account_id setting + broadcast WS event ONLY when
    # credentials actually committed. The DB setting is the launch-time
    # Layer-2 fallback (see jacked/launch.py); leaving it stale across
    # auto-swaps means a credential-file recovery uses an out-of-date
    # account. Without gating the broadcast on credential_ok, dashboards
    # would display "swap completed" even when the filesystem still
    # belongs to the old account — a misleading audit signal.
    if credential_ok:
        try:
            db.set_setting("active_account_id", target["id"])
        except Exception:
            logger.exception(
                "Failed to sync active_account_id setting after swap "
                "(target=%d) — credential file is authoritative; setting "
                "may stay stale until next manual switch",
                target["id"],
            )
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
        # Step 2 already wrote a record_swap row; without an audit-trail
        # success column on the table, the row falsely implies success.
        # Surface the failure to operators via WS + log so the dashboard
        # can show a different state and the on-call can grep for it.
        _last_swap_time = 0.0  # reset cooldown so next tick retries
        logger.warning(
            "Swap recorded but credential write failed — will retry next "
            "tick (account %d -> %d)",
            active_acct_id, target["id"],
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

    Interval adapts based on urgency tier:
    - Idle (<50% usage, no burn): 5 min
    - Normal (<70% or low burn): 2.5 min
    - Warning (70-85% or projects critical in 15min): 90s
    - Critical (>85% or projects critical in 5min): 65s
    ±15% jitter on each tick to prevent sync patterns.

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
                target_7d,
                deficit_vs_target,
                format_account_label,
                TIER_EXCLUDED,
            )

            # -- Active account ID ---------------------------------------
            active_acct_id = _read_active_account_id()
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
                for a in all_accts:
                    if a["id"] != active_acct_id:
                        try:
                            await _prime_fetch(a["id"], db)
                            primed += 1
                        except Exception:
                            logger.debug("Prime fetch failed for account %d", a["id"])
                        await asyncio.sleep(1)
                if primed > 0:
                    _initial_fetch_done = True
                    logger.info("Auto-swap: primed %d/%d accounts", primed, len(all_accts) - 1)

            # -- Fetch usage (fresh token, bypasses cache) ---------------
            effective_token = read_fresh_active_token(active_acct_id)
            await fetch_usage(
                active_acct_id, db, access_token=effective_token,
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
                active_acct_id, db, _burn_rates,
            )

            # Push fresh usage data to connected dashboards so the
            # countdown timer and usage bars update immediately.
            _ws = getattr(app.state, "ws_registry", None)
            if _ws and active_acct:
                # Whitelist safe fields — new DB columns won't leak by default.
                # Mirrors _account_to_response in routes/auth.py.
                _WS_SAFE_FIELDS = {
                    "id", "email", "organization_uuid", "organization_name",
                    "display_name", "expires_at", "scopes",
                    "subscription_type", "rate_limit_tier", "has_extra_usage",
                    "priority", "is_active", "is_deleted",
                    "last_used_at", "cached_usage_5h", "cached_usage_7d",
                    "cached_5h_resets_at", "cached_7d_resets_at",
                    "usage_cached_at", "last_error", "last_error_at",
                    "consecutive_failures", "last_validated_at",
                    "validation_status", "created_at", "updated_at",
                    "cc_expires_at", "auto_swap_enabled",
                }
                safe_acct = {
                    k: v for k, v in active_acct.items()
                    if k in _WS_SAFE_FIELDS
                }
                safe_acct["_poll_interval"] = int(_poll_interval)
                safe_acct["_poll_tier"] = _poll_tier
                safe_acct["_last_poll_at"] = int(time.time())
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
                # Decay after 5+ unchanged ticks, but only when below warning
                if ticks >= 5 and current_5h_val < warning_5h:
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
                    _burn_rates, active_acct_id,
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
                accounts, active_acct_id, db,
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
            )

            # ---- Anti-jitter persistence on higher-tier emergence ----
            reason = _apply_emergence_persistence(
                reason=reason,
                best_id=best["id"] if best else None,
                streak=_emerged_target_streak,
                persistence_ticks=_EMERGENCE_PERSISTENCE_TICKS,
            )

            # Build candidate summaries for decision log
            _candidate_summaries = []
            for cand in accounts:
                if cand["id"] == active_acct_id:
                    continue
                cand_tier = tier_for(
                    cand, now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
                cand_target = target_7d(cand, now=now_utc)
                cand_deficit = deficit_vs_target(cand, now=now_utc)
                _candidate_summaries.append({
                    "id": cand["id"],
                    "email": cand.get("email", ""),
                    "label": format_account_label(cand),
                    "5h": cand.get("cached_usage_5h"),
                    "7d": cand.get("cached_usage_7d"),
                    "tier": cand_tier,
                    "target_7d": (
                        round(cand_target, 1)
                        if cand_target is not None else None
                    ),
                    "deficit": (
                        round(cand_deficit, 1)
                        if cand_deficit is not None else None
                    ),
                    "is_best": (best is not None and cand["id"] == best["id"]),
                })

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
            for stale_id in list(_emerged_target_streak.keys()):
                if stale_id not in live_ids:
                    _emerged_target_streak.pop(stale_id, None)
            for cand in accounts:
                cand_tier = tier_for(
                    cand, now=now_utc,
                    prev_tier=_last_observed_tiers.get(cand["id"]),
                )
                if cand_tier == TIER_EXCLUDED:
                    _last_observed_tiers.pop(cand["id"], None)
                else:
                    _last_observed_tiers[cand["id"]] = cand_tier

            ws_registry = getattr(app.state, "ws_registry", None)

            global _last_exhaustion_warning, _consecutive_no_best_ticks
            global _last_stall_warning

            if reason is None:
                _decision_action = "stay"
                if best is None:
                    _decision_reason = (
                        f"stay: no candidate has deficit "
                        f"(tier {_tier_label(active_acct).strip() or 'unset'})"
                    )
                else:
                    _decision_target_id = best["id"]
                    active_tier_now = tier_for(active_acct, now=now_utc)
                    best_tier = tier_for(
                        best, now=now_utc,
                        prev_tier=_last_observed_tiers.get(best["id"]),
                    )
                    streak = _emerged_target_streak.get(best["id"], 0)
                    if (best_tier < active_tier_now
                            and best_tier != TIER_EXCLUDED
                            and streak > 0):
                        _decision_reason = (
                            f"stay: emergence streak "
                            f"{streak}/{_EMERGENCE_PERSISTENCE_TICKS}, "
                            f"awaiting confirmation "
                            f"(best id={best['id']} tier={best_tier})"
                        )
                    else:
                        _decision_reason = (
                            f"stay: best is same/lower tier "
                            f"(best id={best['id']} tier={best_tier})"
                        )
            elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                _decision_action = "stay"
                _decision_target_id = best["id"] if best else None
                _decision_reason = (
                    f"swap warranted ({reason}) but cooldown active "
                    f"({_SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time):.0f}s remaining)"
                )
                logger.debug("Active poll: %s", _decision_reason)
            elif best is None:
                _decision_action = "stay"
                _decision_reason = (
                    f"swap warranted ({reason}) but no eligible target"
                )

                now_ts = time.time()
                if now_ts - _last_exhaustion_warning > _EXHAUSTION_COOLDOWN_SECONDS:
                    logger.warning(
                        "Auto-swap needed but no eligible target "
                        "(active account %d at 5h=%.1f%%)",
                        active_acct_id, usage_5h or 0,
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
                                if next_recovery_at else None
                            ),
                        },
                    )
            else:
                trigger = _trigger_for_reason(reason)
                logger.info(
                    "Auto-swap: switching from account %d (5h=%.1f%%) to "
                    "account %d (5h=%.1f%%) — %s [%s]",
                    active_acct_id, usage_5h or 0,
                    best["id"], best.get("cached_usage_5h") or 0,
                    reason, trigger,
                )
                swap_committed = await _execute_swap(
                    db, active_acct_id, active_acct, best,
                    reason=reason, trigger=trigger,
                    usage_5h=usage_5h, usage_7d=usage_7d,
                    active_start=active_start, active_end=active_end,
                    ws_registry=ws_registry,
                )
                if swap_committed:
                    _emerged_target_streak.clear()
                    # Clear hysteresis state for both swap participants —
                    # the new active needs a fresh tier observation, and
                    # the prior active will be re-observed as a candidate
                    # next tick. The next tick's hysteresis-refresh block
                    # repopulates both based on current data.
                    _last_observed_tiers.pop(best["id"], None)
                    _last_observed_tiers.pop(active_acct_id, None)
                    _decision_action = "swap"
                    _decision_target_id = best["id"]
                    _decision_reason = reason
                else:
                    # TOCTOU mismatch or credential lock failure. Don't
                    # clear streak/hysteresis — next tick will retry.
                    # Decision log records the attempt and the failure
                    # so operators see "we tried" rather than silent
                    # state-corruption.
                    _decision_action = "stay"
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
            has_other_accounts = sum(
                1 for a in accounts if a["id"] != active_acct_id
            ) > 0
            # Pattern (d): same-tier-stay with meaningful deficit.
            # Surface the candidate's deficit so the watchdog can detect
            # "best exists every tick but never qualifies for departure"
            # — the silent-failure mode the user reported during DCR.
            best_deficit_val = None
            if best is not None:
                _bd = deficit_vs_target(best, now=now_utc)
                if _bd is not None:
                    best_deficit_val = _bd
            stalled_this_tick = _evaluate_stall(
                decision_action=_decision_action, best=best,
                usage_cached_at_age_seconds=age_seconds,
                has_other_accounts=has_other_accounts,
                reason=reason,
                staleness_threshold=_STALL_USAGE_STALENESS_SECONDS,
                best_deficit=best_deficit_val,
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
                    "Auto-swap stall cleared (active=%d)", active_acct_id,
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
                        _consecutive_no_best_ticks, active_acct_id,
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
                        cooldown_active=(time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS,
                        decision=_decision_action,
                    )
                    decision_id = db.record_decision(
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger=_trigger_for_reason(_decision_reason),
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
                                    "trigger": _trigger_for_reason(_decision_reason),
                                    "reason": _decision_reason or "no trigger",
                                    "timestamp": datetime.now(timezone.utc).isoformat(),
                                    "detail": _tick_detail,
                                },
                            )
                        except Exception:
                            logger.debug("Decision log WS broadcast failed", exc_info=True)
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
        if _last_tick_at > 0 and _poll_interval > 0 and (now_tick - _last_tick_at) > 2 * _poll_interval:
            logger.warning(
                "Active poll loop delayed — last tick %ds ago, expected interval %ds",
                int(now_tick - _last_tick_at), int(_poll_interval),
            )
        _last_tick_at = now_tick

        logger.debug("Active poll: tier=%s interval=%.0fs", _poll_tier, _poll_interval)
        await asyncio.sleep(_poll_interval)


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
            should_ping = (
                is_active_hours(now, start=wk_start, end=wk_end)
                or is_prewake_time(
                    now, prewake=wk_prewake,
                    check_interval_min=check_interval / 60,
                )
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

                    logger.info(
                        "Window keeper: pinging account %d (%s)%s%s",
                        acct["id"], acct.get("email", "?"),
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
                                from jacked.api.credential_helpers import reconcile_credentials_from_live_store
                                reconcile_credentials_from_live_store(acct["id"], db)
                                fresh_acct = db.get_account(acct["id"])
                                fresh_cc = fresh_acct.get("cc_access_token") if fresh_acct else None
                                if fresh_cc and fresh_cc != cc_at:
                                    success = await ping_account(fresh_cc)
                            except Exception:
                                logger.exception("Window keeper reconcile failed for active account %d", acct["id"])
                        else:
                            from jacked.web.auth import refresh_cc_token
                            refreshed = await refresh_cc_token(acct["id"], db)
                            if refreshed:
                                fresh_acct = db.get_account(acct["id"])
                                fresh_cc = fresh_acct.get("cc_access_token") if fresh_acct else None
                                if fresh_cc and fresh_cc != cc_at:
                                    success = await ping_account(fresh_cc)
                    if success:
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
