"""Background usage monitor — orchestrates auto-swap and window keeper.

Runs as a single asyncio task started from main.py lifespan.  Reads
settings from the DB each tick so changes take effect without restart.
Window keeper runs BEFORE auto-swap so freshly pinged accounts get
correct evaluation ordering.
"""

import asyncio
import json
import logging
import time
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Module-level state for burn-rate tracking and exhaustion cooldown.
_burn_rates: dict[int, object] = {}
_last_exhaustion_warning: float = 0.0
_EXHAUSTION_COOLDOWN_SECONDS = 1800  # 30 minutes


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


async def usage_monitor_loop(app):
    """Background loop that checks usage and triggers auto-swap / window keeper.

    Never crashes — all errors are caught and logged per tick.
    """
    global _last_exhaustion_warning

    _default_interval = 300  # fallback if settings read fails

    while True:
        check_interval = _default_interval
        try:
            db = getattr(app.state, "db", None)
            if db is None:
                await asyncio.sleep(60)
                continue

            # ----------------------------------------------------------
            # 1. Read settings
            # ----------------------------------------------------------
            auto_swap_enabled = _setting_bool(db, "auto_swap_enabled", False)
            window_keeper_enabled = _setting_bool(db, "window_keeper_enabled", False)
            check_interval = _setting_float(db, "usage_check_interval", 300)
            critical_5h = _setting_float(db, "auto_swap_5h_critical", 90)
            warning_5h = _setting_float(db, "auto_swap_5h_warning", 80)
            threshold_7d = _setting_float(db, "auto_swap_7d_threshold", 85)
            wk_start = _setting_str(db, "window_keeper_active_start", "06:00")
            wk_end = _setting_str(db, "window_keeper_active_end", "23:00")
            wk_prewake = _setting_str(db, "window_keeper_prewake", "04:00")

            # ----------------------------------------------------------
            # 2. Skip if nothing enabled
            # ----------------------------------------------------------
            if not auto_swap_enabled and not window_keeper_enabled:
                await asyncio.sleep(check_interval)
                continue

            # ----------------------------------------------------------
            # 3. Fetch usage for all active accounts
            # ----------------------------------------------------------
            # Late imports to avoid circular deps
            from jacked.web.auth import fetch_usage
            from jacked.api.credential_helpers import (
                read_fresh_active_token,
                sync_credential_to_all_stores,
            )
            from jacked.web.auto_swap import (
                should_swap,
                pick_best_target,
                update_burn_rate,
            )
            from jacked.web.window_keeper import (
                is_active_hours,
                is_prewake_time,
                needs_ping,
                ping_account,
            )

            accounts = db.list_accounts(include_inactive=False)

            # Determine which account is active for fresh token reads
            active_acct_id = _read_active_account_id()

            for acct in accounts:
                acct_id = acct["id"]
                effective_token = None
                if acct_id == active_acct_id:
                    effective_token = read_fresh_active_token(acct_id)

                result = await fetch_usage(
                    acct_id, db, access_token=effective_token,
                )
                # _cached means data is fresh — not an error
                # None means API error — DB still has cached values
                if result and not result.get("_cached"):
                    logger.debug(
                        "Usage fetched for account %d in monitor loop", acct_id,
                    )

                # Pace between accounts
                await asyncio.sleep(1)

            # ----------------------------------------------------------
            # 4. Re-read accounts with fresh usage data
            # ----------------------------------------------------------
            accounts = db.list_accounts(include_inactive=False)

            # ----------------------------------------------------------
            # 5. Window keeper FIRST
            # ----------------------------------------------------------
            if window_keeper_enabled:
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
                        cc_rt = acct.get("cc_refresh_token")
                        if not cc_rt:
                            continue

                        scopes = acct.get("scopes") or ""
                        logger.info(
                            "Window keeper: pinging account %d (%s)",
                            acct["id"], acct.get("email", "?"),
                        )
                        await ping_account(cc_rt, scopes)
                        await asyncio.sleep(2)  # Pace between pings

            # ----------------------------------------------------------
            # 6. Auto-swap SECOND
            # ----------------------------------------------------------
            if auto_swap_enabled:
                active_acct_id = _read_active_account_id()
                if active_acct_id is None:
                    logger.debug("Auto-swap: no active account in credential file")
                    await asyncio.sleep(check_interval)
                    continue

                # Find the active account's usage from DB
                active_acct = None
                for acct in accounts:
                    if acct["id"] == active_acct_id:
                        active_acct = acct
                        break

                if active_acct is None:
                    logger.debug(
                        "Auto-swap: active account %d not in active account list",
                        active_acct_id,
                    )
                    await asyncio.sleep(check_interval)
                    continue

                usage_5h = active_acct.get("cached_usage_5h")
                usage_7d = active_acct.get("cached_usage_7d")

                # Track velocity
                br = update_burn_rate(
                    _burn_rates, active_acct_id,
                    current_5h=usage_5h or 0,
                    current_7d=usage_7d or 0,
                )

                if should_swap(
                    usage_5h=usage_5h,
                    usage_7d=usage_7d,
                    critical_5h=critical_5h,
                    warning_5h=warning_5h,
                    threshold_7d=threshold_7d,
                    burn_rate=br,
                    check_interval_min=check_interval / 60,
                ):
                    target = pick_best_target(
                        accounts, current_id=active_acct_id,
                        threshold_7d=threshold_7d,
                    )

                    ws_registry = getattr(app.state, "ws_registry", None)

                    if target is not None:
                        logger.info(
                            "Auto-swap: switching from account %d (5h=%.1f%%) "
                            "to account %d (5h=%.1f%%)",
                            active_acct_id, usage_5h or 0,
                            target["id"],
                            target.get("cached_usage_5h") or 0,
                        )
                        sync_credential_to_all_stores(
                            target["id"], target,
                            email=target.get("email"),
                        )
                        reason = (
                            f"5h={usage_5h:.1f}% "
                            f"(critical={critical_5h}%)"
                        )
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
                        if ws_registry:
                            await ws_registry.broadcast(
                                "auto_swap_triggered",
                                {
                                    "from_account_id": active_acct_id,
                                    "to_account_id": target["id"],
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
                        if ws_registry:
                            await ws_registry.broadcast(
                                "all_accounts_exhausted",
                                {
                                    "active_account_id": active_acct_id,
                                    "usage_5h": usage_5h,
                                    "usage_7d": usage_7d,
                                },
                            )

        except asyncio.CancelledError:
            logger.info("Usage monitor loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Usage monitor loop error", exc_info=True)

        await asyncio.sleep(check_interval)
