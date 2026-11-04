"""Window keeper: confirmation of a ping that the provider accepted (#238i).

A ping can open its window before the usage endpoint shows it: the usage
read two seconds after the ping can still show the lapsed window. The
keeper then records the ping as pending (``last_status`` ``ping_pending``,
written by ``_keeper_record_success`` in :mod:`jacked.api.keeper.confirm`)
and confirms it later from a usage read. This module holds that
confirmation: the pending fields, the hold until the deadline, the usage
read at the deadline, and the transitions out of the pending state.

Every pending transition is a compare-and-swap on the ping that opened the
pending state (:func:`_pending_generation`). The sweep and a usage poll can
settle the same ping at the same time, and a write that is based on an old
read must never overwrite a newer state. A lost swap writes nothing and
logs one DEBUG line; the next sweep reads the new state.

The rest of the keeper lives in the :mod:`jacked.api.keeper` package, and
:mod:`jacked.api.usage_monitor` re-exports its names. Both import this module
when they load. The functions here import usage_monitor when they run, never
at load time: a load-time import back would be a cycle. They log under the usage monitor's logger name, so the keeper's log
lines keep the name that log filters already use.
"""

import asyncio
import logging
import math

from jacked.service.pill_text import finite_number
from jacked.web.database import Database

logger = logging.getLogger("jacked.api.usage_monitor")

# A ping the provider accepted whose window no usage read shows yet waits
# for a usage read to confirm it: two sweeps, and never less than this.
# Provider propagation lag is not a failure (#238i).
_KEEPER_CONFIRM_MIN_SECONDS = 900
# Why an accepted ping's window is not visible yet: code -> log text.
_PENDING_REASON_TEXT = {
    "no_header": "no reset header and no usage read-back",
    "read_not_landed": "usage read-back did not land",
    "read_no_window": "read-back landed with no window",
}


def _read_landed(read) -> bool:
    """True when a fetch_usage answer is a read that it stored.

    fetch_usage stores a read before it returns it. A cached or backed-off
    answer, None, or a timeout is a read that never landed.

    >>> _read_landed({}), _read_landed({"five_hour": None})
    (True, True)
    >>> _read_landed({"_cached": True}), _read_landed({"_backed_off": True}), _read_landed(None)
    (False, False, False)
    """
    return isinstance(read, dict) and not (read.get("_cached") or read.get("_backed_off"))


def _read_since_ping(row: dict, state: dict, window: str | None = None) -> bool:
    """True when *row* holds a usage read stored after the keeper's ping.

    Without *window*, any stored read counts: every usage write stamps
    ``usage_cached_at``. With *window* ("5h" or "7d"), the read must have
    ACCEPTED the reading of that window: only then does the window's own
    stamp move (``Database.WINDOW_READ_STAMPS``). A rejected reading moves
    the row stamp, never the window's. The ping time is the state's
    ``last_ping_at`` in whole seconds, so only a stamp from a later whole
    second proves a read after the ping. A ping time or a stamp that is not
    a finite number proves nothing.

    >>> _read_since_ping({"usage_cached_at": 101}, {"last_ping_at": 100})
    True
    >>> _read_since_ping({"usage_cached_at": 100}, {"last_ping_at": 100})
    False
    >>> _read_since_ping({"usage_cached_at": 101}, {"last_ping_at": None})
    False
    >>> _read_since_ping({"usage_cached_at": 101}, {"last_ping_at": 10**400})
    False
    >>> row = {"usage_cached_at": 130, "cached_5h_read_at": 130.2, "cached_7d_read_at": 90.5}
    >>> _read_since_ping(row, {"last_ping_at": 100}, "5h"), _read_since_ping(row, {"last_ping_at": 100}, "7d")
    (True, False)
    >>> _read_since_ping({"cached_7d_read_at": 100.9}, {"last_ping_at": 100}, "7d")  # the ping's own second
    False
    """
    ping = finite_number(state.get("last_ping_at"))
    column = Database.WINDOW_READ_STAMPS[window] if window else "usage_cached_at"
    stamp = finite_number(row.get(column))
    return ping is not None and stamp is not None and math.floor(stamp) > ping


def _window_read_after(row: dict, window: str, instant: float) -> bool:
    """True when a usage write after *instant* accepted the *window* reading.

    *instant* is an exact time in epoch seconds. The keeper's read-back runs
    within a second of its ping, so it compares the window's own stamp with
    the time after the ping, not with the whole-second ``last_ping_at``. A
    stamp EQUAL to *instant* counts: the read-back runs after the ping in
    program order, and a coarse clock (Windows ticks about every 15 ms) can
    give both the same reading.

    >>> _window_read_after({"cached_7d_read_at": 100.6}, "7d", 100.5)
    True
    >>> _window_read_after({"cached_7d_read_at": 100.5}, "7d", 100.5)
    True
    >>> _window_read_after({"cached_7d_read_at": 100.4, "usage_cached_at": 101}, "7d", 100.5)
    False
    >>> _window_read_after({}, "5h", 100.5)
    False
    """
    stamp = finite_number(row.get(Database.WINDOW_READ_STAMPS[window]))
    return stamp is not None and stamp >= instant


def _window_phrase(windows: list[str]) -> str:
    """The log name of one or more windows.

    >>> _window_phrase(["5h"]), _window_phrase(["5h", "7d"])
    ('5h window', '5h and 7d windows')
    """
    return " and ".join(windows) + (" windows" if len(windows) > 1 else " window")


def _pending_reason(missing: list[str], result, landed: bool) -> str:
    """Why the windows in *missing* are not visible after an accepted ping.

    ``read_no_window``: the read-back landed and the row still shows no open
    window (the usage endpoint lags the ping). ``read_not_landed``: the
    read-back did not land, and the reply carried a reset header for each
    missing window. ``no_header``: the read-back did not land, and the reply
    carried no reset header for a missing window. With two missing windows
    the weaker evidence names the reason, so the reason never claims a
    header that the reply did not carry.

    >>> from jacked.web.window_keeper import PingResult
    >>> bare = PingResult(ok=True)
    >>> h5 = PingResult(ok=True, rate_limit={"anthropic-ratelimit-unified-5h-reset": "1800003600"})
    >>> _pending_reason(["5h"], bare, True), _pending_reason(["5h"], h5, False)
    ('read_no_window', 'read_not_landed')
    >>> _pending_reason(["5h"], bare, False), _pending_reason(["5h", "7d"], h5, False)
    ('no_header', 'no_header')
    """
    if landed:
        return "read_no_window"
    headers = {"5h": result.reset_5h_iso(), "7d": result.reset_7d_iso()}
    if all(headers[name] for name in missing):
        return "read_not_landed"
    return "no_header"


def _pending_windows(state: dict) -> list[str]:
    """The windows a pending ping waits for; [] when the state is not pending.

    >>> _pending_windows({"last_status": "ping_pending", "pending_windows": ["7d", "5h"]})
    ['5h', '7d']
    >>> _pending_windows({"last_status": "ok", "pending_windows": ["5h"]})
    []
    >>> _pending_windows({"last_status": "ping_pending", "pending_windows": "5h"})
    []
    """
    raw = state.get("pending_windows")
    if state.get("last_status") != "ping_pending" or not isinstance(raw, list):
        return []
    return [name for name in ("5h", "7d") if name in raw]


def _pending_confirmed(row: dict, state: dict, windows: list[str]) -> bool:
    """True when *row* shows every pending window open (``_window_confirmed``).

    The evidence for a weekly window without a reset is a usage read stored
    after the ping that ACCEPTED the weekly reading
    (:func:`_read_since_ping` with the window). A read that rejected the
    weekly reading, or kept it, is no evidence for it, also when it stored
    the other window. The keeper's own read at the deadline counts the same
    way: the deadline is at least 15 minutes after the ping. A missing row
    confirms nothing.

    >>> st = {"last_ping_at": 100}
    >>> _pending_confirmed({"cached_7d_resets_at": None, "cached_7d_read_at": 90}, st, ["7d"])
    False
    >>> _pending_confirmed({"cached_7d_resets_at": None, "cached_7d_read_at": 130}, st, ["7d"])
    True
    >>> _pending_confirmed({"cached_7d_resets_at": None, "usage_cached_at": 130,
    ...                     "cached_7d_read_at": 90}, st, ["7d"])
    False
    >>> _pending_confirmed({}, st, ["7d"])
    False
    """
    from jacked.api import usage_monitor as um

    if not row:
        return False
    return all(
        um._window_confirmed(row, name, evidence=_read_since_ping(row, state, name))
        for name in windows
    )


def _pending_deadline(state: dict, now: float) -> float:
    """The confirmation deadline of a pending ping, in epoch seconds.

    A deadline that is not a finite number reads as *now*: an unreadable
    deadline never holds the account. A missing value, text, NaN, an
    infinity and an int too large for a float are all unreadable
    (:func:`jacked.service.pill_text.finite_number`).

    >>> _pending_deadline({"pending_until": 1800000900}, 5.0)
    1800000900.0
    >>> [_pending_deadline({"pending_until": v}, 5.0)
    ...  for v in (float("inf"), float("nan"), "1e999", 10**400, None, "x")]
    [5.0, 5.0, 5.0, 5.0, 5.0, 5.0]
    >>> _pending_deadline({}, 5.0)
    5.0
    """
    deadline = finite_number(state.get("pending_until"))
    return now if deadline is None else deadline


def _pending_generation(state: dict) -> dict:
    """The keeper-state keys that name the pending ping in *state*.

    The keys are the status and the ping time. Each pending transition is a
    compare-and-swap on them
    (:meth:`jacked.web.database.Database.set_keeper_state_if`): it applies
    only while the account still holds the ping that the writer read.

    >>> _pending_generation({"last_status": "ping_pending", "last_ping_at": 100,
    ...                      "pending_until": 1000})
    {'last_status': 'ping_pending', 'last_ping_at': 100}
    """
    return {"last_status": "ping_pending", "last_ping_at": state.get("last_ping_at")}


async def _keeper_usage_read(
    db, account_id: int, token: str | None, *, post_ping: bool
) -> bool:
    """One keeper usage read, bounded to 10 s. True when the read landed.

    A slow read does not fail the ping: the pending state names it
    (``read_not_landed``), and the verdict at the confirmation deadline
    names it once. So the timeout is INFO, not a second WARNING.
    """
    from jacked.web import auth as auth_mod

    try:
        read = await asyncio.wait_for(
            auth_mod.fetch_usage(
                account_id, db, access_token=token, post_ping=post_ping
            ),
            timeout=10.0,
        )
    except asyncio.TimeoutError:
        logger.info(
            "Window keeper: the usage read for account %d took more than 10s; "
            "moving on",
            account_id,
        )
        return False
    return _read_landed(read)


async def _finish_pending(db, account_id: int, state: dict, windows: list[str]) -> bool:
    """Record that a usage read shows every window of an accepted ping open.

    The verdict becomes ``ok``. The write clears the pending fields and the
    retry deadline, and so (``_write_keeper_state``) the persisted
    escalation count. It is a swap on the ping that *state* names, and only
    a write that lands drops the in-memory hold. Returns False when the
    write did not land: the sweep or a poll settled that ping first, a newer
    ping replaced it, or the database failed (logged).
    """
    from jacked.api import usage_monitor as um

    if not await asyncio.to_thread(
        um._write_keeper_state,
        db,
        account_id,
        expected=_pending_generation(state),
        last_status="ok",
        pending_windows=None,
        pending_until=None,
        pending_reason=None,
        next_attempt_at=None,
        skip_reason=None,
    ):
        return False
    um._ping_backoff.pop(account_id, None)
    logger.info(
        "Window keeper: the %s of account %d %s confirmed open by the usage poll",
        _window_phrase(windows),
        account_id,
        "are" if len(windows) > 1 else "is",
    )
    return True


async def _confirm_pending(
    db, acct: dict, state: dict, now: float, check_interval: float
) -> str | None:
    """Settle a pending ping (``last_status`` ``ping_pending``).

    Returns None when the account is not pending. Otherwise:

    a. Every pending window shows open on the row (:func:`_pending_confirmed`;
       a poll read that accepts a window stamps that window): ``ok``, returns
       "confirmed". A confirmation is not a ping.
    b. Before the deadline (:func:`_pending_deadline`): :func:`_hold`.
    c. At or after the deadline: :func:`_settle_at_deadline`.

    A transition that does not land returns "skipped". The hold does not
    depend on the token: a re-auth does not change whether the accepted
    ping opened its window. The count follows the generic rules
    (``_sync_backoff``): a restart resumes it, a new candidate set drops it.
    """
    from jacked.api import usage_monitor as um

    windows = _pending_windows(state)
    if not windows:
        return None
    account_id = acct["id"]
    if _pending_confirmed(acct, state, windows):
        landed = await _finish_pending(db, account_id, state, windows)
        return "confirmed" if landed else "skipped"
    # Resume the count after a restart (or drop it for a new token set)
    # before (b) persists it or (c) escalates it.
    had_entry = account_id in um._ping_backoff
    fp = um._candidate_fingerprint(*um._keeper_candidates(acct))
    um._sync_backoff(account_id, state, fp)
    restored = None if had_entry else um._ping_backoff.get(account_id)
    deadline = _pending_deadline(state, now)
    if now < deadline:
        outcome = await _hold(db, acct, state, deadline)
    else:
        outcome = await _settle_at_deadline(
            db, acct, state, windows, deadline, check_interval
        )
    if outcome != "lost":
        return outcome
    _forget_restored(account_id, restored)
    return "skipped"


def _forget_restored(account_id: int, restored: dict | None) -> None:
    """Drop the backoff entry that a lost transition restored from its read.

    After a lost swap the read is not the account's state, so a count
    restored from it must not outlive the swap. An entry that another
    writer put in its place stays.
    """
    from jacked.api import usage_monitor as um

    if restored is not None and um._ping_backoff.get(account_id) is restored:
        um._ping_backoff.pop(account_id, None)


async def _hold(db, acct: dict, state: dict, deadline: float) -> str:
    """Hold a pending ping until its *deadline*: no ping and no usage read.

    The skip reason is ``confirming``, with the deadline as the next
    attempt. The write is a swap on the pending ping. Returns "skipped", or
    "lost" when the write did not land.
    """
    from jacked.api import usage_monitor as um

    noted = await asyncio.to_thread(
        um._note_keeper_skip, db, acct, state, "confirming",
        next_attempt_at=deadline, expected=_pending_generation(state),
    )
    return "skipped" if noted else "lost"


async def _settle_at_deadline(
    db, acct: dict, state: dict, windows: list[str], deadline: float,
    check_interval: float,
) -> str:
    """Settle a pending ping whose confirmation deadline has passed.

    One normal-paced usage read with the keeper's first candidate token.
    When the row then shows every window open, the verdict is ``ok``
    ("confirmed"). Otherwise :func:`_record_unconfirmed` records the failure
    ("failed"). Returns "lost" when the transition did not land.
    """
    from jacked.api import usage_monitor as um

    account_id = acct["id"]
    cc_token, primary_token = um._keeper_candidates(acct)
    landed = await _keeper_usage_read(
        db, account_id, cc_token or primary_token, post_ping=False
    )
    fresh = await asyncio.to_thread(db.get_account, account_id) or {}
    if _pending_confirmed(fresh, state, windows):
        finished = await _finish_pending(db, account_id, state, windows)
        return "confirmed" if finished else "lost"
    return await _record_unconfirmed(
        db, account_id, state, windows, fresh, landed, deadline, check_interval
    )


async def _record_unconfirmed(
    db, account_id: int, state: dict, windows: list[str], fresh: dict,
    landed: bool, deadline: float, check_interval: float,
) -> str:
    """Record that the deadline passed with a pending window not visible.

    The verdict is ``ping_ok_no_window``, ONE WARNING names the windows that
    are still not visible and the reason that applied, and the backoff
    escalates one step. A pending window that *fresh* shows open is not
    named: per-window evidence can confirm one window of two. The retry
    delay counts from the deadline, not from this sweep: a machine that
    slept through the grace window and the window itself must not get a
    fresh delay in front of the ping it needs now. The write is a swap on
    the pending ping, so the step is computed for the write
    (``_backoff_step``), and the backoff is armed in memory, and the WARNING
    logged, only when the write lands. Returns "failed", or "lost" when the
    write did not land.
    """
    from jacked.api import usage_monitor as um

    fresh_fp = um._candidate_fingerprint(*um._keeper_candidates(fresh)) if fresh else None
    fails, retry_at = um._backoff_step(account_id, check_interval, deadline)
    if not await asyncio.to_thread(
        um._write_keeper_state,
        db,
        account_id,
        expected=_pending_generation(state),
        last_status="ping_ok_no_window",
        pending_windows=None,
        pending_until=None,
        pending_reason=None,
        next_attempt_at=retry_at,
        backoff_fails=fails,
        skip_reason=None,
        token_fp=fresh_fp,
    ):
        return "lost"
    # Not empty: _settle_at_deadline came here because a window is missing.
    still = [name for name in windows if not _pending_confirmed(fresh, state, [name])]
    logger.warning(
        "Window keeper: the %s of account %d %s still not visible after the "
        "confirmation wait (%s)",
        _window_phrase(still),
        account_id,
        "are" if len(still) > 1 else "is",
        "the usage read landed with no window"
        if landed or _read_since_ping(fresh, state)
        else "the usage read did not land",
    )
    um._arm_ping_backoff(account_id, fresh_fp, check_interval, deadline)
    return "failed"


async def _confirm_pending_from_poll(db, account_id: int) -> bool:
    """Confirm a pending keeper ping from a usage read that a poll stored.

    The active poll, its candidate reads and the all-accounts refresh call
    this after a read that landed. When the row's keeper state is
    ``ping_pending`` and every pending window shows open, the verdict
    becomes ``ok`` (:func:`_finish_pending`): the dashboard flips within one
    poll instead of one sweep. Returns True when it confirmed. Never raises:
    a database error is logged and the poll goes on.
    """
    from jacked.api import usage_monitor as um

    try:
        row = await asyncio.to_thread(db.get_account, account_id)
    except Exception:
        logger.warning(
            "Window keeper: could not confirm a pending ping for account %d",
            account_id,
            exc_info=True,
        )
        return False
    if not row:
        return False
    state = um._keeper_state_of(row)
    windows = _pending_windows(state)
    if not windows or not _pending_confirmed(row, state, windows):
        return False
    return await _finish_pending(db, account_id, state, windows)
