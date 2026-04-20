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

    # 7. Broadcast via WebSocket
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

    if not credential_ok:
        _last_swap_time = 0.0  # Reset cooldown so next tick retries
        logger.warning(
            "Swap recorded but credential write failed — will retry next tick"
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
            threshold_7d = _setting_float(db, "auto_swap_7d_threshold", 85)
            check_interval = _setting_float(db, "usage_check_interval", 300)
            active_start = _setting_str(db, "window_keeper_active_start", "06:00")
            active_end = _setting_str(db, "window_keeper_active_end", "23:00")

            _decision_action = "stay"
            _decision_target_id = None
            _decision_reason = None
            _candidate_summaries = None
            _proactive_target_id = None
            _suppression = None

            # -- Late imports (avoid circular deps) ----------------------
            from jacked.web.auth import fetch_usage
            from jacked.api.credential_helpers import read_fresh_active_token
            from jacked.web.auto_swap import (
                should_swap,
                pick_best_target,
                update_burn_rate,
                tier_critical_threshold,
                tier_label as _tier_label,
                score_candidate,
                _resets_within,
                format_account_label,
                RESET_SUPPRESS_MINUTES,
                SUPPRESS_OVERRIDE_SCORE,
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

            # -- Should swap? --------------------------------------------
            want_swap = should_swap(
                usage_5h=usage_5h,
                usage_7d=usage_7d,
                critical_5h=effective_critical,
                warning_5h=warning_5h,
                threshold_7d=threshold_7d,
                burn_rate=br,
                check_interval_min=check_interval / 60,
                resets_5h_at=active_acct.get("cached_5h_resets_at"),
                resets_7d_at=active_acct.get("cached_7d_resets_at"),
                usage_cached_at=active_acct.get("usage_cached_at"),
                account=active_acct,
                active_start=active_start,
                active_end=active_end,
            )

            if not want_swap:
                if usage_5h is not None and usage_5h >= effective_critical:
                    _suppression = {"type": "5h_reset_imminent"}
                elif usage_7d is not None and usage_7d >= threshold_7d:
                    _suppression = {"type": "deficit", "usage_7d": usage_7d}

            # -- Escape hatch: override reset suppression if a clearly
            # better candidate exists.  Don't keep the user on a degraded
            # account just to save a window reset when a much better
            # option is available.
            escape_override = False
            if not want_swap and _resets_within(
                active_acct.get("cached_5h_resets_at"),
                RESET_SUPPRESS_MINUTES,
            ):
                # Verify suppression was actually the reason: would a swap
                # have triggered WITHOUT the reset suppression?
                would_swap_without_suppress = should_swap(
                    usage_5h=usage_5h,
                    usage_7d=usage_7d,
                    critical_5h=effective_critical,
                    warning_5h=warning_5h,
                    threshold_7d=threshold_7d,
                    burn_rate=br,
                    check_interval_min=check_interval / 60,
                    # No reset params → no reset suppression
                    # But keep account + active hours for deficit suppression
                    account=active_acct,
                    active_start=active_start,
                    active_end=active_end,
                )
                if would_swap_without_suppress:
                    escape_override = True

            if want_swap or escape_override:
                # Fetch fresh usage for candidates before scoring
                accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)

                # Build candidate summaries for decision log
                from jacked.web.auto_swap import compute_7d_deficit
                _candidate_summaries = []
                for _cand in accounts:
                    if _cand["id"] == active_acct_id:
                        continue
                    _cand_score = score_candidate(_cand, active_start, active_end)
                    _cand_deficit = compute_7d_deficit(_cand, active_start, active_end)
                    _candidate_summaries.append({
                        "id": _cand["id"],
                        "email": _cand.get("email", ""),
                        "label": format_account_label(_cand),
                        "5h": _cand.get("cached_usage_5h"),
                        "7d": _cand.get("cached_usage_7d"),
                        "score": round(_cand_score, 1),
                        "deficit": round(_cand_deficit["deficit"], 1) if _cand_deficit else None,
                    })

                target = pick_best_target(
                    accounts, current_id=active_acct_id,
                    threshold_7d=threshold_7d,
                    active_start=active_start,
                    active_end=active_end,
                )

                # For escape hatch, verify candidate is good enough
                if escape_override and not want_swap and target:
                    target_score = score_candidate(target, active_start, active_end)
                    if target_score <= SUPPRESS_OVERRIDE_SCORE:
                        logger.debug(
                            "Escape hatch: candidate %d scores %.0f "
                            "(<= %d), staying put",
                            target["id"],
                            target_score,
                            SUPPRESS_OVERRIDE_SCORE,
                        )
                        target = None  # not good enough, stay put

                ws_registry = getattr(app.state, "ws_registry", None)

                if target is not None:
                    # -- Swap cooldown: prevent ping-ponging ------
                    if (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                        _decision_reason = (
                            f"swap needed but cooldown active "
                            f"({_SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time):.0f}s remaining)"
                        )
                        logger.debug("Active poll: %s", _decision_reason)
                        try:
                            _tick_detail = _build_tick_detail(
                                active_acct=active_acct,
                                usage_5h=usage_5h, usage_7d=usage_7d,
                                want_swap=want_swap, suppression=_suppression,
                                escape_override=escape_override,
                                candidates=_candidate_summaries,
                                proactive_target_id=None,
                                cooldown_active=True, decision="stay",
                            )
                            _cooldown_decision_id = db.record_decision(
                                account_id=active_acct_id, action="stay",
                                trigger="tick", target_id=target["id"],
                                reason=_decision_reason, detail=_tick_detail,
                            )
                            if ws_registry and _cooldown_decision_id:
                                await ws_registry.broadcast(
                                    "decision_log_entry",
                                    {
                                        "id": _cooldown_decision_id,
                                        "account_id": active_acct_id,
                                        "email": active_acct.get("email", ""),
                                        "label": format_account_label(active_acct),
                                        "action": "stay",
                                        "trigger": "tick",
                                        "reason": _decision_reason,
                                        "timestamp": datetime.now(timezone.utc).isoformat(),
                                        "detail": _tick_detail,
                                    },
                                )
                        except Exception:
                            pass
                        await asyncio.sleep(60)
                        continue

                    # -- Build descriptive reason -------------------------
                    if escape_override and not want_swap:
                        reason = (
                            f"escape hatch: suppressed swap overridden — "
                            f"target scores {score_candidate(target, active_start, active_end):.0f}"
                        )
                    elif usage_5h is not None and usage_5h >= effective_critical:
                        tier_lbl = _tier_label(active_acct)
                        reason = (
                            f"5h critical: {usage_5h:.1f}% >= "
                            f"{effective_critical:.0f}%{tier_lbl}"
                        )
                    elif usage_7d is not None and usage_7d >= threshold_7d:
                        reason = (
                            f"7d threshold: {usage_7d:.1f}% >= "
                            f"{threshold_7d:.0f}%"
                        )
                    else:
                        projected = usage_5h or 0
                        if br and br.rate_5h_per_min > 0:
                            mins = (check_interval / 60) * 2
                            projected = (usage_5h or 0) + br.rate_5h_per_min * mins
                        reason = (
                            f"burn-rate projection: {usage_5h:.1f}% -> "
                            f"{projected:.1f}% in {int((check_interval / 60) * 2)}min"
                        )

                    logger.info(
                        "Auto-swap: switching from account %d (5h=%.1f%%) "
                        "to account %d (5h=%.1f%%) — %s",
                        active_acct_id, usage_5h or 0,
                        target["id"],
                        target.get("cached_usage_5h") or 0,
                        reason,
                    )

                    ws_registry = getattr(app.state, "ws_registry", None)
                    await _execute_swap(
                        db, active_acct_id, active_acct, target,
                        reason=reason, trigger="auto_swap",
                        usage_5h=usage_5h, usage_7d=usage_7d,
                        active_start=active_start, active_end=active_end,
                        ws_registry=ws_registry,
                    )
                    _decision_action = "swap"
                    _decision_target_id = target["id"]
                    _decision_reason = reason
                else:
                    # accounts already fetched before pick_best_target — reuse

                    # No eligible target — cooldown to avoid log spam
                    now_ts = time.time()
                    if now_ts - _last_exhaustion_warning > _EXHAUSTION_COOLDOWN_SECONDS:
                        logger.warning(
                            "Auto-swap needed but no eligible target "
                            "(active account %d at 5h=%.1f%%)",
                            active_acct_id, usage_5h or 0,
                        )
                        _last_exhaustion_warning = now_ts

                    # Compute next_recovery_at from earliest cached_5h_resets_at
                    next_recovery_at = None
                    now_utc = datetime.now(timezone.utc)
                    for acct in accounts:
                        resets = acct.get("cached_5h_resets_at")
                        if not resets:
                            continue
                        try:
                            r = datetime.fromisoformat(
                                resets.replace("Z", "+00:00"),
                            )
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

            # -- Proactive 7d capacity scheduler ---------------------------
            # Scan for accounts with EXPIRING capacity that must be burned.
            # Uses remaining 5h windows to determine urgency — the closer
            # to expiry, the lower the threshold for triggering a swap.
            if not want_swap and not escape_override:
                from jacked.web.auto_swap import (
                    compute_7d_deficit,
                    compute_urgency_threshold,
                    compute_burn_per_window,
                )

                if usage_5h is not None and usage_5h < warning_5h:
                    # Don't proactively swap near end of active hours —
                    # not worth opening a 5h window for a few minutes of use
                    from jacked.web.auto_swap import MIN_PROACTIVE_MINUTES
                    _now_local = datetime.now()
                    _end_h, _end_m = map(int, active_end.split(":"))
                    _active_end_today = _now_local.replace(
                        hour=_end_h, minute=_end_m, second=0, microsecond=0,
                    )
                    _minutes_left_today = (
                        _active_end_today - _now_local
                    ).total_seconds() / 60.0
                    if 0 < _minutes_left_today < MIN_PROACTIVE_MINUTES:
                        logger.debug(
                            "Proactive: skipping — only %.0f min until active hours end",
                            _minutes_left_today,
                        )
                    else:
                        # Fetch fresh candidate data
                        accounts = await _fetch_candidate_usage(accounts, active_acct_id, db)

                        # Scan ALL candidates for urgency — not pick_best_target,
                        # because the most-urgent account (expiring capacity) may
                        # not be the highest-scored account overall.
                        best_urgent = None
                        best_urgency = 0.0
                        best_deficit_result = None
                        _candidate_summaries = []

                        for acct in accounts:
                            if acct["id"] == active_acct_id:
                                continue
                            if not acct.get("cc_access_token"):
                                continue
                            if acct.get("auto_swap_enabled") == 0:
                                continue
                            if acct.get("is_active") == 0 or acct.get("is_deleted") == 1:
                                continue
                            if (acct.get("consecutive_failures") or 0) >= 3:
                                continue

                            # Skip accounts without viable headroom —
                            # swapping to an account that'll exhaust in
                            # minutes is worse than not swapping at all.
                            from jacked.web.auto_swap import has_viable_headroom
                            if not has_viable_headroom(acct, active_start, active_end):
                                _candidate_summaries.append({
                                    "id": acct["id"],
                                    "email": acct.get("email", ""),
                                    "7d": acct.get("cached_usage_7d"),
                                    "passes": False,
                                    "skip_reason": "near_exhaustion",
                                })
                                continue

                            dr = compute_7d_deficit(acct, active_start, active_end)
                            if not dr:
                                continue
                            if dr["deficit"] <= 0:
                                _candidate_summaries.append({
                                    "id": acct["id"],
                                    "email": acct.get("email", ""),
                                    "7d": acct.get("cached_usage_7d"),
                                    "deficit": round(dr["deficit"], 1),
                                    "windows_remaining": round(dr["effective_windows_remaining"], 1),
                                    "passes": False,
                                    "skip_reason": "ahead_of_schedule",
                                })
                                continue

                            # Urgency threshold scales with remaining windows
                            threshold = compute_urgency_threshold(
                                dr["effective_windows_remaining"],
                                active_start, active_end,
                            )
                            if dr["deficit"] <= threshold:
                                _candidate_summaries.append({
                                    "id": acct["id"],
                                    "email": acct.get("email", ""),
                                    "7d": acct.get("cached_usage_7d"),
                                    "deficit": round(dr["deficit"], 1),
                                    "windows_remaining": round(dr["effective_windows_remaining"], 1),
                                    "threshold": round(threshold, 1),
                                    "passes": False,
                                    "skip_reason": "below_threshold",
                                })
                                continue

                            # Urgency = recoverable capacity per hour of inaction
                            burn = compute_burn_per_window(active_start, active_end)
                            recoverable = min(
                                dr["unused_7d"],
                                dr["effective_windows_remaining"] * burn,
                            )

                            # Skip if recoverable < one window's burn —
                            # not worth the disruption of swapping for scraps.
                            if recoverable < burn:
                                _candidate_summaries.append({
                                    "id": acct["id"],
                                    "email": acct.get("email", ""),
                                    "7d": acct.get("cached_usage_7d"),
                                    "deficit": round(dr["deficit"], 1),
                                    "recoverable": round(recoverable, 1),
                                    "passes": False,
                                    "skip_reason": "recoverable_too_low",
                                })
                                continue

                            urgency = recoverable / max(dr["effective_hours_remaining"], 0.5)

                            _candidate_summaries.append({
                                "id": acct["id"],
                                "email": acct.get("email", ""),
                                "label": format_account_label(acct),
                                "5h": acct.get("cached_usage_5h"),
                                "7d": acct.get("cached_usage_7d"),
                                "deficit": round(dr["deficit"], 1),
                                "windows_remaining": round(dr["effective_windows_remaining"], 1),
                                "urgency_tier": (
                                    "CRITICAL" if dr["effective_windows_remaining"] < 1 else
                                    "HIGH" if dr["effective_windows_remaining"] < 3 else
                                    "MEDIUM" if dr["effective_windows_remaining"] < 5 else
                                    "NORMAL"
                                ),
                                "threshold": round(threshold, 1),
                                "passes": dr["deficit"] > threshold,
                                "urgency_score": round(urgency, 2),
                            })

                            if urgency > best_urgency:
                                best_urgency = urgency
                                best_urgent = acct
                                best_deficit_result = dr

                        if not best_urgent:
                            logger.debug("Proactive: no urgent candidate found")
                        elif (time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS:
                            logger.debug(
                                "Proactive: urgent target %d found but cooldown active",
                                best_urgent["id"],
                            )
                        else:
                            # Re-fetch fresh data for the target
                            await fetch_usage(best_urgent["id"], db)
                            target = db.get_account(best_urgent["id"])

                            if target:
                                deficit_result = compute_7d_deficit(target, active_start, active_end)
                                if not deficit_result or deficit_result["deficit"] <= 0:
                                    logger.debug(
                                        "Proactive: target %d deficit gone after re-fetch",
                                        target["id"],
                                    )
                                else:
                                    # Re-check threshold with fresh data
                                    threshold = compute_urgency_threshold(
                                        deficit_result["effective_windows_remaining"],
                                        active_start, active_end,
                                    )
                                    if deficit_result["deficit"] <= threshold:
                                        logger.debug(
                                            "Proactive: target %d deficit %.1f%% below "
                                            "threshold %.1f%% after re-fetch",
                                            target["id"], deficit_result["deficit"], threshold,
                                        )
                                    else:
                                        reason = (
                                            f"proactive: burning {deficit_result['unused_7d']:.0f}% "
                                            f"unused 7d on {format_account_label(target)} — "
                                            f"{deficit_result['effective_hours_remaining']:.0f}h left "
                                            f"({deficit_result['effective_windows_remaining']:.1f} windows), "
                                            f"deficit={deficit_result['deficit']:.0f}%"
                                        )
                                        logger.info(
                                            "Proactive swap: account %d has %.0f%% deficit, "
                                            "%.1f windows remaining, urgency=%.2f",
                                            target["id"], deficit_result["deficit"],
                                            deficit_result["effective_windows_remaining"],
                                            best_urgency,
                                        )

                                        ws_registry = getattr(app.state, "ws_registry", None)
                                        await _execute_swap(
                                            db, active_acct_id, active_acct, target,
                                            reason=reason, trigger="proactive_7d",
                                            usage_5h=usage_5h, usage_7d=usage_7d,
                                            active_start=active_start, active_end=active_end,
                                            ws_registry=ws_registry,
                                        )
                                        _decision_action = "swap"
                                        _decision_target_id = target["id"]
                                        _decision_reason = reason
                                        _proactive_target_id = target["id"]

            # Record decision in the log
            if active_acct is not None:
                try:
                    _tick_detail = _build_tick_detail(
                        active_acct=active_acct,
                        usage_5h=usage_5h,
                        usage_7d=usage_7d,
                        want_swap=want_swap,
                        suppression=_suppression,
                        escape_override=escape_override if 'escape_override' in dir() else False,
                        candidates=_candidate_summaries,
                        proactive_target_id=_proactive_target_id,
                        cooldown_active=(time.time() - _last_swap_time) < _SWAP_COOLDOWN_SECONDS,
                        decision=_decision_action,
                    )
                    decision_id = db.record_decision(
                        account_id=active_acct_id,
                        action=_decision_action,
                        trigger=(
                            ("proactive_7d" if _proactive_target_id else "auto_swap")
                            if _decision_action == "swap"
                            else "tick"
                        ),
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
                                    "trigger": (
                                        ("proactive_7d" if _proactive_target_id else "auto_swap")
                                        if _decision_action == "swap"
                                        else "tick"
                                    ),
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
            active_acct_id = _read_active_account_id()
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
