"""Window keeper: the full-sweep loop and its wall-clock schedule.

:func:`full_sweep_loop` runs one keeper pass (:func:`_run_window_keeper`) in
each ``usage_check_interval`` and waits on the wall clock between passes
(:func:`_sleep_until`). The settings readers stay in
:mod:`jacked.api.usage_monitor`, which imports this module when it loads, so
the functions here import those readers when they run. Each check of the wake
Event reads it through :func:`jacked.api.keeper.state.sweep_wake`.
"""

import asyncio
import logging
import time
from datetime import datetime

from jacked.api.keeper.process import _keeper_process_account
from jacked.api.keeper.state import (
    _KEEPER_PING_PACING_SECONDS, _PREWAKE_DONE_KEY, _SWEEP_SLEEP_SLICE_SECONDS,
    _keeper_state_of, _note_keeper_skip, sweep_wake,
)

logger = logging.getLogger("jacked.api.usage_monitor")


async def _sleep_until(deadline: float) -> None:
    """Wait until the WALL-CLOCK *deadline* or until ``_sweep_wake`` is set.

    The old wait counted nominal 5 s increments, so every slice that the
    process spent asleep (lid closed, dark wakes of a few seconds every
    ~15 min) still counted as 5 s and a 5-minute wait took hours of real
    time. ``time.time()`` is the wall clock: it keeps advancing across
    system sleep, where macOS monotonic time does not.
    """
    while not sweep_wake().is_set():
        remaining = deadline - time.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(_SWEEP_SLEEP_SLICE_SECONDS, remaining))


async def _run_window_keeper(db, check_interval: float) -> int:
    """One keeper pass over every enabled account. Returns windows pinged."""
    # A call-time import: usage_monitor imports this module when it loads.
    from jacked.api.usage_monitor import _Snapshot, _setting_str
    from jacked.web import window_keeper as wk

    def _read_pass():
        """One thread hop for the pass's reads (they ran back to back on the
        loop) and the plain time math between them."""
        accounts = db.list_accounts(include_inactive=False)
        snap = _Snapshot(db, (
            "window_keeper_active_start", "window_keeper_active_end",
            "window_keeper_prewake",
        ))
        wk_start = _setting_str(snap, "window_keeper_active_start", "06:00")
        wk_end = _setting_str(snap, "window_keeper_active_end", "23:00")
        wk_prewake = _setting_str(snap, "window_keeper_prewake", "04:00")

        # Local time intentional: users configure active hours in their local
        # timezone (e.g. "06:00" means local 6am).
        now_local = datetime.now()
        active = wk.is_active_hours(now_local, start=wk_start, end=wk_end)
        prewake = False
        if not active:
            prewake = wk.prewake_due(
                now_local,
                prewake=wk_prewake,
                active_start=wk_start,
                active_end=wk_end,
                done_date=db.get_setting(_PREWAKE_DONE_KEY),
            )
        return accounts, wk_start, now_local, active, prewake

    accounts, wk_start, now_local, active, prewake = await asyncio.to_thread(_read_pass)
    if prewake:
        logger.info(
            "Window keeper: pre-wake pass for %s", now_local.date().isoformat()
        )
    next_start = None if (active or prewake) else _next_active_start_epoch(now_local, wk_start)

    def _note_outside_hours(acct: dict) -> None:
        _note_keeper_skip(
            db,
            acct,
            _keeper_state_of(acct),
            "outside_active_hours",
            paused_until=next_start,
        )

    pinged = 0
    for acct in accounts:
        try:
            if not (active or prewake):
                await asyncio.to_thread(_note_outside_hours, acct)
                continue
            outcome = await _keeper_process_account(db, acct, check_interval)
            if outcome == "pinged":
                pinged += 1
            if outcome != "skipped":
                await asyncio.sleep(_KEEPER_PING_PACING_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception:
            # One bad row must never skip every later account.
            logger.exception(
                "Window keeper: account %s failed this sweep — continuing",
                acct.get("id") if isinstance(acct, dict) else "?",
            )
    if prewake:
        # Marked only after the whole pass: a pass cut short by a restart
        # runs again on the next sweep (accounts already opened are skipped
        # by their future reset).
        await asyncio.to_thread(
            db.set_setting, _PREWAKE_DONE_KEY, now_local.date().isoformat()
        )
    return pinged


def _next_active_start_epoch(now_local: datetime, start_hhmm: str) -> float | None:
    """Epoch seconds of the next keeper active start (today or tomorrow).

    >>> from datetime import datetime as _dt
    >>> n = _dt(2026, 10, 27, 23, 30)
    >>> _dt.fromtimestamp(_next_active_start_epoch(n, "06:00")).isoformat()
    '2026-10-28T06:00:00'
    >>> _dt.fromtimestamp(_next_active_start_epoch(n.replace(hour=3), "06:00")).isoformat()
    '2026-10-27T06:00:00'
    >>> _next_active_start_epoch(n, "garbage") is None
    True
    """
    from datetime import timedelta

    try:
        h, m = (int(x) for x in start_hhmm.split(":"))
        start = now_local.replace(hour=h, minute=m, second=0, microsecond=0)
    except (ValueError, TypeError):
        return None
    if start <= now_local:
        start += timedelta(days=1)
    return start.timestamp()


def _note_keeper_disabled(db) -> None:
    """Record 'keeper_disabled' on every account whose reason differs."""
    try:
        accounts = db.list_accounts(include_inactive=False)
    except Exception:
        logger.debug("Window keeper: could not list accounts", exc_info=True)
        return
    for acct in accounts:
        try:
            _note_keeper_skip(db, acct, _keeper_state_of(acct), "keeper_disabled")
        except Exception:
            logger.debug(
                "Window keeper: could not record keeper_disabled", exc_info=True
            )


async def full_sweep_loop(app):
    """Run the window keeper over all accounts on a wall-clock schedule.

    Runs at the user-configurable ``usage_check_interval`` (default 300s).
    Never crashes — all errors are caught and logged per tick.
    Emits a heartbeat INFO log at the TOP of every iteration (before
    any early-return shortcut), so operators see a heartbeat regardless
    of window-keeper state (0.41.23).
    """
    # A call-time import: usage_monitor imports this module when it loads.
    from jacked.api.usage_monitor import _Snapshot, _setting_bool, _setting_float

    _default_interval = 300
    iter_count = 0

    while True:
        iter_count += 1
        logger.info("Full-sweep heartbeat: iter=%d", iter_count)
        check_interval = _default_interval
        deadline = time.time() + check_interval
        try:
            db = getattr(app.state, "db", None)
            if db is None:
                await asyncio.sleep(60)
                continue

            def _read_sweep_settings():
                snap = _Snapshot(db, ("window_keeper_enabled", "usage_check_interval"))
                return (
                    _setting_bool(snap, "window_keeper_enabled", False),
                    _setting_float(snap, "usage_check_interval", 300),
                )

            window_keeper_enabled, check_interval = await asyncio.to_thread(
                _read_sweep_settings
            )
            deadline = time.time() + check_interval

            if not window_keeper_enabled:
                await asyncio.to_thread(_note_keeper_disabled, db)
            else:
                sweep_pinged = await _run_window_keeper(db, check_interval)
                logger.info(
                    "Full sweep complete: pinged %d windows",
                    sweep_pinged,
                )

        except asyncio.CancelledError:
            logger.info("Full sweep loop cancelled — shutting down")
            raise
        except Exception:
            logger.warning("Full sweep loop error", exc_info=True)

        # Wait on the WALL CLOCK, checking the wake signal between short
        # slices, so settings changes and new tokens trigger an immediate
        # sweep and a closed lid cannot stretch the interval.
        await _sleep_until(deadline)
        if sweep_wake().is_set():
            sweep_wake().clear()
            logger.info("Full sweep woken early")
