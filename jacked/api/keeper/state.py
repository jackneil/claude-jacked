"""Window keeper state: the ping backoff, the keeper state row and the sweep wake.

This module owns the in-memory ping backoff, the keeper constants and the
``asyncio.Event`` that wakes the full sweep early. The lifespan calls this
module's :func:`reset_locks`, which rebinds that Event (:func:`reset_sweep_wake`),
so a reader gets it through :func:`sweep_wake` when the reader runs. A copy
that another module keeps goes stale at the next ``reset_locks``.
"""

import asyncio
import logging

from jacked.service.pill_text import finite_number

logger = logging.getLogger("jacked.api.usage_monitor")

# Wake signal — settings PUT sets this to trigger an immediate sweep.
_sweep_wake: asyncio.Event = asyncio.Event()

# Window-keeper ping backoff: account_id -> {"fails": int, "skip_until": float
# (wall-clock epoch), "token_fp": str | None}. A consecutively-failing ping
# (e.g. an exhausted account answering 429) must not be retried every sweep
# forever — 2026-06 incident: one account pinged every 120s for hours, 429
# every time. Exponential backoff capped at 1h; cleared on the first ping
# whose windows are confirmed open, and dropped as soon as the account's
# token fingerprint changes (a re-auth must never wait out the old token's
# backoff). A 200 whose window no usage read shows yet is pending, not a
# failure: the entry holds the account until the confirmation deadline with
# ``fails`` unchanged, and only a deadline that passes unconfirmed escalates
# it (verdict ping_ok_no_window, #238i). Wall clock, not monotonic: macOS
# monotonic time stops while the lid is closed, which would stretch a 1h
# backoff across a whole night of dark wakes.
# Transport errors and 5xx never increment ``fails``.
_ping_backoff: dict[int, dict] = {}
_PING_BACKOFF_CAP_SECONDS = 3600
# Past this many doublings every delay is at the cap already, and a larger
# exponent overflows the float conversion (2 ** 1024 is not a float).
_PING_BACKOFF_MAX_DOUBLINGS = 16
# A persisted count above this is corrupt (10,000 failures at the one-hour
# cap is more than a year).
_PING_BACKOFF_MAX_FAILS = 10_000
# Statuses whose persisted retry deadline a restart honors. A transport
# error is not one: its retry is the next sweep (its next_attempt_at is
# only the displayed estimate). Nor is ping_pending: its deadline is
# pending_until, which the pending branch reads itself
# (jacked.api.keeper_confirmation).
_RESTORED_SCHEDULE_STATUSES = ("rate_limited", "auth_failed", "error", "ping_ok_no_window")
# Seconds between a keeper pass's per-ping pacing sleeps.
_KEEPER_PING_PACING_SECONDS = 2
# Upper bound on a single wall-clock wait slice. Short slices let a dark
# wake (a few seconds every ~15 min with the lid closed) notice that the
# deadline has passed.
_SWEEP_SLEEP_SLICE_SECONDS = 5.0
# Settings key holding the local ISO date of the last pre-wake pass.
_PREWAKE_DONE_KEY = "window_keeper_prewake_done"


def sweep_wake() -> asyncio.Event:
    """The current sweep wake Event. Read it here when the code runs; never keep a copy."""
    return _sweep_wake


def reset_sweep_wake() -> asyncio.Event:
    """Bind a new sweep wake Event and return it (``reset_locks`` calls this)."""
    global _sweep_wake
    _sweep_wake = asyncio.Event()
    return _sweep_wake


def reset_locks() -> None:
    """Rebind the sweep wake Event and clear the in-memory ping backoff.

    The lifespan calls this at each start (``_module_specs`` in
    jacked/api/main.py), because the tray restarts uvicorn on a new event
    loop. The Event's readers call only the sync ``is_set()``, ``set()`` and
    ``clear()``. These calls do not touch the loop, so a stale Event is
    harmless today. A caller that changes to ``await wait()`` would bring
    back the "bound to a different event loop" bug, so rebind in advance.
    The backoff starts empty, as it does on a cold start.
    """
    reset_sweep_wake()
    _ping_backoff.clear()


def clear_ping_backoff(account_id: int) -> None:
    """Forget an account's keeper ping backoff and wake the sweep.

    Called by every path that stores new tokens for an account (OAuth
    add/re-auth, CC token store, live-store reconcile, authority-guard
    import) so the keeper re-pings at once instead of waiting out a backoff
    armed against the old token. Safe from a worker thread: the sweep only
    polls ``_sweep_wake.is_set()``, so ``set()`` has no waiters to wake.
    """
    try:
        _ping_backoff.pop(int(account_id), None)
    except (TypeError, ValueError):
        return
    _sweep_wake.set()


def _keeper_state_of(acct: dict) -> dict:
    from jacked.web.database import Database

    return Database.parse_keeper_state(acct.get("keeper_state"))


def _write_keeper_state(db, account_id: int, *, expected: dict | None = None, **fields) -> bool:
    """Persist keeper state; a DB hiccup must never break the sweep.

    A write that sets a retry deadline (``next_attempt_at``) also persists
    the in-memory escalation count (``backoff_fails``), and a write that
    clears the deadline clears the count: a restart then resumes the
    escalation where it stopped instead of falling back to the first step.

    With *expected* the write is a compare-and-swap
    (:meth:`jacked.web.database.Database.set_keeper_state_if`): it applies
    only while every expected key still holds its value. A lost swap writes
    nothing and logs one DEBUG line; the next sweep reads the new state.
    Returns True when the state was written. A lost swap and a DB error
    (logged as a WARNING) return False.
    """
    if "next_attempt_at" in fields and "backoff_fails" not in fields:
        entry = _ping_backoff.get(account_id)
        fields["backoff_fails"] = (
            int(entry["fails"])
            if entry is not None and fields["next_attempt_at"] is not None
            else 0
        )
    try:
        if expected is None:
            db.set_keeper_state(account_id, **fields)
            return True
        if db.set_keeper_state_if(account_id, expected, **fields):
            return True
    except Exception:
        logger.warning(
            "Window keeper: could not persist state for account %s",
            account_id,
            exc_info=True,
        )
        return False
    logger.debug(
        "Window keeper: the state of account %d changed before this write; "
        "nothing written (the next sweep reads the new state)",
        account_id,
    )
    return False


def _note_keeper_skip(
    db, acct: dict, state: dict, reason: str | None, next_attempt_at=None,
    paused_until=None, *, expected: dict | None = None,
) -> bool:
    """Record why the keeper did not ping an account — only on change.

    ``paused_until`` (the next active start) is display state kept apart
    from ``next_attempt_at``: the latter is a retry DEADLINE that the
    restart recovery honors, and a pause must never overwrite it. With
    *expected* the write is a compare-and-swap (:func:`_write_keeper_state`)
    that always runs, also when *state* already holds the note: *state* can
    be an old read, and a lost swap is how the caller learns that the
    account moved on. Returns False when a needed write did not land;
    *state* then stays as it was.
    """
    if expected is None and state.get("skip_reason") == reason and (
        (reason != "backoff" and next_attempt_at is None and paused_until is None)
        or (next_attempt_at is not None and state.get("next_attempt_at") == next_attempt_at)
        or (paused_until is not None and state.get("paused_until") == paused_until)
    ):
        return True
    fields: dict = {"skip_reason": reason}
    if reason == "backoff" or next_attempt_at is not None:
        fields["next_attempt_at"] = next_attempt_at
    if reason == "outside_active_hours" or paused_until is not None:
        fields["paused_until"] = paused_until
    if reason is not None and not state.get("last_status"):
        fields["last_status"] = "skipped"
    if not _write_keeper_state(db, acct["id"], expected=expected, **fields):
        return False
    state.update(fields)
    return True


def _backoff_step(account_id: int, check_interval: float, now: float) -> tuple[int, float]:
    """(failure count, retry deadline) one failure past the account's entry.

    Changes nothing: :func:`_arm_ping_backoff` applies the step. A caller
    whose write must land first (a compare-and-swap) computes the step for
    the write, and arms the backoff only after the write landed.

    >>> _ping_backoff[1] = {"fails": 2, "skip_until": 0.0, "token_fp": "a"}
    >>> _backoff_step(1, 300.0, 1000.0), _ping_backoff.pop(1)["fails"]
    ((3, 3400.0), 2)
    >>> _backoff_step(1, 300.0, 1000.0)
    (1, 1600.0)
    """
    entry = _ping_backoff.get(account_id)
    fails = min((entry["fails"] if entry is not None else 0) + 1, _PING_BACKOFF_MAX_FAILS)
    doublings = min(fails, _PING_BACKOFF_MAX_DOUBLINGS)
    return fails, now + min(check_interval * 2 ** doublings, _PING_BACKOFF_CAP_SECONDS)


def _arm_ping_backoff(
    account_id: int, token_fp: str | None, check_interval: float, now: float
) -> float:
    """Escalate the account's backoff by one failure (:func:`_backoff_step`);
    return skip_until."""
    fails, skip_until = _backoff_step(account_id, check_interval, now)
    entry = _ping_backoff.setdefault(
        account_id, {"fails": 0, "skip_until": 0.0, "token_fp": token_fp}
    )
    entry["token_fp"] = token_fp
    entry["fails"] = fails
    entry["skip_until"] = skip_until
    logger.info(
        "Window keeper: ping failed for account %d — backing off %.0fs "
        "(%d consecutive failures)",
        account_id,
        skip_until - now,
        fails,
    )
    return skip_until


def _restore_ping_backoff(account_id: int, state: dict, fp: str | None) -> float | None:
    """Rebuild an account's backoff from its persisted state after a restart.

    For the same token only. The escalation count resumes whatever the last
    status was: a transport error keeps the count, so a restart after one
    must not drop it. The retry deadline is honored only for the statuses
    that set a backoff deadline, and only when it is a finite number: an
    unreadable deadline never holds the account. Returns that deadline, or
    None. A pending ping is not a retry schedule: its count resumes, and the
    pending branch reads its own deadline (``pending_until``).

    >>> _restore_ping_backoff(1, {"token_fp": "a", "last_status": "ping_ok_no_window",
    ...     "next_attempt_at": 50.0, "backoff_fails": 4}, "a"), _ping_backoff.pop(1)["fails"]
    (50.0, 4)
    >>> [_restore_ping_backoff(1, {"token_fp": "a", "last_status": "error",
    ...     "next_attempt_at": v, "backoff_fails": 2}, "a") for v in (float("inf"), 10**400)]
    [None, None]
    >>> _ping_backoff.pop(1)["fails"]
    2
    >>> _restore_ping_backoff(1, {"token_fp": "a", "last_status": "transport",
    ...     "next_attempt_at": 50.0, "backoff_fails": 4}, "a"), _ping_backoff.pop(1)["fails"]
    (None, 4)
    >>> _restore_ping_backoff(1, {"token_fp": "a", "last_status": "ping_pending",
    ...     "next_attempt_at": 50.0, "pending_until": 50.0, "backoff_fails": 3}, "a"), _ping_backoff.pop(1)["fails"]
    (None, 3)
    >>> _restore_ping_backoff(1, {"token_fp": "a", "backoff_fails": 10**9}, "a"), _ping_backoff.pop(1)["fails"]
    (None, 10000)
    >>> _restore_ping_backoff(1, {"token_fp": "a", "backoff_fails": "x"}, "b"), 1 in _ping_backoff
    (None, False)
    """
    if state.get("token_fp") != fp:
        return None
    skip_until = None
    if state.get("last_status") in _RESTORED_SCHEDULE_STATUSES:
        skip_until = finite_number(state.get("next_attempt_at"))
    try:
        fails = int(state.get("backoff_fails") or 0)
    except (TypeError, ValueError, OverflowError):
        fails = 0
    fails = min(max(0, fails), _PING_BACKOFF_MAX_FAILS)
    if fails:
        _ping_backoff[account_id] = {
            "fails": fails, "skip_until": skip_until or 0.0, "token_fp": fp,
        }
    return skip_until


def _sync_backoff(account_id: int, state: dict, fp: str | None) -> float | None:
    """Align the in-memory backoff with candidate set *fp*; return its deadline.

    A new candidate set drops the entry: a re-auth never waits out the old
    token's backoff. With no entry (a restart), the persisted schedule and
    count resume through :func:`_restore_ping_backoff`. Returns the backoff
    deadline (epoch seconds), or None.

    >>> _ping_backoff[1] = {"fails": 2, "skip_until": 9.0, "token_fp": "old"}
    >>> _sync_backoff(1, {}, "new"), 1 in _ping_backoff
    (None, False)
    >>> _sync_backoff(1, {"token_fp": "new", "backoff_fails": 2}, "new"), _ping_backoff.pop(1)["fails"]
    (None, 2)
    """
    entry = _ping_backoff.get(account_id)
    if entry is not None and entry.get("token_fp") != fp:
        logger.info(
            "Window keeper: account %d has a new token — dropping its ping backoff",
            account_id,
        )
        _ping_backoff.pop(account_id, None)
        entry = None
    if entry is not None:
        return entry["skip_until"]
    return _restore_ping_backoff(account_id, state, fp)
