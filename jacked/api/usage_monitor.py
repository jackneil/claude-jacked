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

            # -- Late imports (avoid circular deps) ----------------------
            from jacked.web.auth import fetch_usage
            from jacked.api.credential_helpers import (
                read_fresh_active_token,
                sync_credential_to_all_stores,
            )
            from jacked.web.auto_swap import (
                should_swap,
                pick_best_target,
                update_burn_rate,
                tier_critical_threshold,
                tier_label as _tier_label,
                score_candidate,
                _resets_within,
                RESET_SUPPRESS_MINUTES,
                SUPPRESS_OVERRIDE_SCORE,
            )

            # -- Active account ID ---------------------------------------
            active_acct_id = _read_active_account_id()
            if active_acct_id is None:
                logger.debug("Active poll: no active account in credential file")
                await asyncio.sleep(60)
                continue

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

            # Push fresh usage data to connected dashboards so the
            # countdown timer and usage bars update immediately.
            _ws = getattr(app.state, "ws_registry", None)
            if _ws and active_acct:
                await _ws.broadcast(
                    "usage_poll_updated",
                    {
                        "account_id": active_acct_id,
                        "account_data": active_acct,
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
            )

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
                    # No reset params → no suppression
                )
                if would_swap_without_suppress:
                    escape_override = True

            if want_swap or escape_override:
                target = pick_best_target(
                    accounts, current_id=active_acct_id,
                    threshold_7d=threshold_7d,
                )

                # For escape hatch, verify candidate is good enough
                if escape_override and not want_swap and target:
                    target_score = score_candidate(target)
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
                        logger.debug(
                            "Active poll: swap cooldown active (%.0fs remaining)",
                            _SWAP_COOLDOWN_SECONDS - (time.time() - _last_swap_time),
                        )
                        await asyncio.sleep(60)
                        continue

                    # -- TOCTOU guard: re-read active ID before swap ------
                    current_active = _read_active_account_id()
                    if current_active != active_acct_id:
                        logger.info(
                            "Active poll: active account changed from %d to %s "
                            "during swap evaluation — skipping swap",
                            active_acct_id, current_active,
                        )
                        # continue, NOT return — loop keeps running
                        await asyncio.sleep(60)
                        continue

                    # -- Build descriptive reason -------------------------
                    if escape_override and not want_swap:
                        reason = (
                            f"escape hatch: suppressed swap overridden — "
                            f"target scores {score_candidate(target):.0f}"
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

                    # Record swap and set cooldown BEFORE credential
                    # write — if sync_credential_to_all_stores raises,
                    # we still have the audit trail and cooldown set.
                    _last_swap_time = time.time()
                    db.record_swap(
                        from_account_id=active_acct_id,
                        to_account_id=target["id"],
                        reason=reason,
                        trigger="auto_swap",
                        from_5h=usage_5h,
                        from_7d=usage_7d,
                        to_5h=target.get("cached_usage_5h"),
                        to_7d=target.get("cached_usage_7d"),
                    )
                    # Reconcile outgoing account's credentials before writing
                    # new ones — captures any token rotation by Claude Code.
                    from jacked.api.credential_helpers import reconcile_outgoing_credentials
                    reconcile_outgoing_credentials(active_acct_id, db)

                    sync_credential_to_all_stores(
                        target["id"], target,
                        email=target.get("email"),
                    )

                    # Clean up burn rate for both old and new account
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
                                "to_email": target.get("email", ""),
                                "reason": reason,
                            },
                        )
                else:
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

        except asyncio.CancelledError:
            logger.info("Active account poll loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Active account poll loop error", exc_info=True)

        _poll_interval, _poll_tier = _compute_poll_interval(
            _read_active_account_id(), db, _burn_rates,
        )
        logger.debug("Active poll: tier=%s interval=%.0fs", _poll_tier, _poll_interval)
        await asyncio.sleep(_poll_interval)


# -----------------------------------------------------------------------
# Loop 2 — Full sweep (configurable interval, default 5min)
# -----------------------------------------------------------------------

async def full_sweep_loop(app):
    """Fetch usage for all non-active accounts and run window keeper.

    Runs at the user-configurable ``usage_check_interval`` (default 300s).
    Never crashes — all errors are caught and logged per tick.
    """
    _default_interval = 300

    while True:
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
                ping_account,
            )

            # -- Fetch usage for ALL non-active accounts -----------------
            active_acct_id = _read_active_account_id()
            accounts = db.list_accounts(include_inactive=False)
            sweep_checked = 0
            sweep_pinged = 0

            for acct in accounts:
                acct_id = acct["id"]
                if acct_id == active_acct_id:
                    continue  # active account handled by poll loop
                result = await fetch_usage(acct_id, db)
                if result and not result.get("_cached"):
                    logger.debug(
                        "Usage fetched for account %d in full sweep", acct_id,
                    )
                await asyncio.sleep(1)  # pacing
                sweep_checked += 1

            # -- Window keeper -------------------------------------------
            wk_start = _setting_str(db, "window_keeper_active_start", "06:00")
            wk_end = _setting_str(db, "window_keeper_active_end", "23:00")
            wk_prewake = _setting_str(db, "window_keeper_prewake", "04:00")

            # Re-read accounts after usage fetch for fresh data
            accounts = db.list_accounts(include_inactive=False)

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
                    if not needs_ping(acct.get("cached_5h_resets_at")):
                        continue
                    if not acct.get("auto_swap_enabled"):
                        continue
                    cc_at = acct.get("cc_access_token")
                    if not cc_at:
                        continue

                    logger.info(
                        "Window keeper: pinging account %d (%s)",
                        acct["id"], acct.get("email", "?"),
                    )
                    success = await ping_account(cc_at)
                    if success:
                        sweep_pinged += 1
                        # Fetch fresh usage so cached_5h_resets_at updates
                        # and needs_ping returns False next sweep.
                        # Pass access_token to bypass the cache freshness guard.
                        await fetch_usage(acct["id"], db, access_token=cc_at)
                    await asyncio.sleep(2)  # pacing

            logger.info(
                "Full sweep complete: checked %d accounts, pinged %d windows",
                sweep_checked, sweep_pinged,
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
