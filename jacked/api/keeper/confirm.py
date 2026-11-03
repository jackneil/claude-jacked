"""Window keeper: what a successful ping opened, and when a window counts as open.

A ping that the provider accepted writes the window that it reports to the
usage cache (:func:`_ping_window_cache_fields`). A usage read then confirms
each window that the keeper pinged for (:func:`_window_confirmed`). The
verdict is ``ok`` or ``ping_pending`` (:func:`_keeper_record_success`).
:mod:`jacked.api.keeper_confirmation` settles a pending ping later.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

from jacked.api.keeper.auth_ladder import (
    _KEEPER_MAX_7D_RESET_SECONDS, _KEEPER_MAX_RETRY_SECONDS, _candidate_fingerprint,
    _keeper_candidates,
)
from jacked.api.keeper.state import _ping_backoff, _write_keeper_state
from jacked.api.keeper_confirmation import (
    _KEEPER_CONFIRM_MIN_SECONDS, _PENDING_REASON_TEXT, _keeper_usage_read, _pending_reason,
    _window_phrase, _window_read_after,
)
from jacked.service.pill_text import finite_number

logger = logging.getLogger("jacked.api.usage_monitor")


def _bounded_reset(iso: str | None, now: float, max_ahead: float) -> str | None:
    """An ISO reset that lies in (now, now + max_ahead], else None.

    >>> _bounded_reset("2000-01-01T00:00:00+00:00", 1.7e9, 3600) is None
    True
    >>> _bounded_reset(None, 1.7e9, 3600) is None
    True
    """
    if not iso:
        return None
    try:
        ts = datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None
    if now < ts <= now + max_ahead:
        return iso
    return None


def _ping_window_cache_fields(
    result, now: float | None = None, has_session: bool = True
) -> dict:
    """update_account_usage_cache kwargs from a successful ping's headers.

    A window is written only when the ping reports BOTH its reset and its
    utilization: writing a new reset over the previous window's utilization
    would stamp that stale reading as a fresh measurement of the new window
    (the dashboard would show the old 100% against a window that just
    opened). A reset-only reply leaves the cache to the usage read-back.
    Resets outside a sane horizon (5h: 6h ahead; 7d: 8d ahead) are ignored.
    Utilization headers are a 0..1 fraction; the cache columns use percent.

    The write stamps usage_cached_at for the whole row. On an account that
    has a 5-hour window (*has_session*, the default) the 5h pair must
    therefore be complete: a 7d-only measurement would stamp a stale 5h
    utilization as fresh. An account whose plan has no 5-hour window (a
    verified Ultra, ChatGPT Pro) has no 5h reading to go stale: its 5h
    headers are ignored completely, and a complete 7d pair is written alone.

    >>> from jacked.web.window_keeper import PingResult
    >>> now = 1_800_000_000
    >>> reply = PingResult(ok=True, rate_limit={
    ...     "anthropic-ratelimit-unified-5h-reset": str(now + 3600),
    ...     "anthropic-ratelimit-unified-7d-reset": str(now + 86400),
    ...     "anthropic-ratelimit-unified-7d-utilization": "0.4"})
    >>> _ping_window_cache_fields(reply, now)  # the 5h pair is incomplete
    {}
    >>> sorted(_ping_window_cache_fields(reply, now, has_session=False).items())
    [('seven_day', 40.0), ('seven_day_resets_at', '2027-01-16T08:00:00+00:00')]
    """
    now = time.time() if now is None else now
    fields: dict = {}
    windows = [
        ("seven_day", result.reset_7d_iso(), _KEEPER_MAX_7D_RESET_SECONDS,
         "anthropic-ratelimit-unified-7d-utilization"),
    ]
    if has_session:
        windows.insert(
            0,
            ("five_hour", result.reset_5h_iso(), _KEEPER_MAX_RETRY_SECONDS,
             "anthropic-ratelimit-unified-5h-utilization"),
        )
    for key, reset_iso, horizon, util_header in windows:
        reset = _bounded_reset(reset_iso, now, horizon)
        raw = result.rate_limit.get(util_header)
        try:
            value = float(raw) if raw is not None else None
        except (TypeError, ValueError):
            value = None
        if reset and value is not None and 0.0 <= value <= 1.0:
            fields[f"{key}_resets_at"] = reset
            fields[key] = round(value * 100.0, 2)
    # The write stamps usage_cached_at for the whole row, so on an account
    # with a 5h window that window must be complete; a 7d-only measurement
    # would stamp a stale 5h utilization as fresh.
    if has_session and "five_hour" not in fields:
        return {}
    return fields


def _keeper_window_needed(acct: dict, state: dict) -> tuple[bool, bool]:
    """(5h window lapsed, 7d window lapsed) for one account.

    An account whose plan has no 5-hour window (session state ``absent``: a
    verified Ultra, ChatGPT Pro) never needs a 5h ping, whatever reset lingers
    in its cache; a ping cannot open a window the plan does not have. An
    ``unknown`` state keeps the check: jacked never skips a window it cannot
    rule out (:func:`jacked.usage_plans.has_session`).

    *state* feeds the recent-success rule for a missing 7d reset below (a
    recent ``ok`` ping). The post-ping confirmation does not use this
    function: it reads :func:`_window_confirmed`.
    """
    from jacked.usage_plans import has_session
    from jacked.web import window_keeper as wk

    needs_5h = has_session(acct) and wk.needs_ping(acct.get("cached_5h_resets_at"))
    needs_7d = wk.needs_7d_ping(
        acct.get("cached_7d_resets_at"), acct.get("usage_cached_at")
    )
    if (
        needs_7d
        and not needs_5h
        and acct.get("cached_7d_resets_at") is None
        and state.get("last_status") == "ok"
        # A ping time that is not a finite number is no recent ping.
        and time.time() - (finite_number(state.get("last_ping_at")) or 0.0) < 5 * 3600
    ):
        # A successful ping inside the open 5h window already started the
        # 7d window; a usage read that reports no 7d reset is not a reason
        # to ping again every sweep.
        needs_7d = False
    return needs_5h, needs_7d


def _window_confirmed(fresh: dict, kind: str, *, evidence: bool) -> bool:
    """True when the fresh row shows *kind* ("5h" or "7d") open after a ping.

    1. A future reset confirms either window.
    2. A 7d window with NO reset at all (``None``) is confirmed only by
       *evidence* from after the ping: a usage read that ACCEPTED the weekly
       reading (the API omits the weekly reset for days at a time; the
       window's own stamp, ``cached_7d_read_at``, shows the accepted read),
       or this ping's own window headers (a counted request starts the
       weekly window). A row that nothing new wrote proves nothing, and a
       read that rejected the weekly reading proves nothing about it.
    3. A lapsed or unreadable reset never confirms a window, an empty string
       included: scheduling may read a fresh row as "running", confirmation
       may not.

    >>> import time
    >>> _window_confirmed({"cached_7d_resets_at": None}, "7d", evidence=True)
    True
    >>> _window_confirmed({"cached_7d_resets_at": None}, "7d", evidence=False)
    False
    >>> _window_confirmed({"cached_7d_resets_at": ""}, "7d", evidence=True)
    False
    >>> _window_confirmed({"cached_5h_resets_at": None}, "5h", evidence=True)
    False
    >>> _window_confirmed({"cached_7d_resets_at": "2000-01-01T00:00:00+00:00"}, "7d", evidence=True)
    False
    """
    from jacked.web import window_keeper as wk

    reset = fresh.get("cached_5h_resets_at" if kind == "5h" else "cached_7d_resets_at")
    if not wk.needs_ping(reset):
        return True  # a future reset
    return kind == "7d" and reset is None and evidence


async def _keeper_record_success(
    db,
    acct: dict,
    good_token: str,
    result,
    check_interval: float,
    now: float,
    *,
    needed: tuple[bool, bool],
) -> None:
    """Persist what a successful ping opened, then the keeper verdict.

    *needed* is ``(needs_5h, needs_7d)``: the windows that made the keeper
    ping. After the header write and the usage read-back, each of them is
    confirmed against the fresh row by :func:`_window_confirmed` (a future
    reset; for a weekly window with no reset at all, evidence from after this
    ping; never a lapsed or unreadable reset). The API omits the weekly reset for days at a
    time (one account here read ``resets_at: null`` for 9 days), so a weekly
    window with no reset is confirmed by a read-back that landed and
    ACCEPTED the weekly reading after the ping (the window's own stamp,
    :func:`jacked.api.keeper_confirmation._window_read_after`), or by this
    ping's own window headers (a counted request starts the weekly window).
    A 200 with no window headers and a read that never lands confirms
    nothing; nor does a read-back that rejected the weekly reading.

    The verdict is ``ok`` when every needed window is confirmed. Otherwise
    the provider accepted the ping and the window is not visible yet: the
    read-back runs about two seconds after the ping, before the usage
    endpoint shows the new window (#238i). That is not a failure. The verdict
    is ``ping_pending`` with the missing windows, the reason that applied
    (:func:`_pending_reason`) and a confirmation deadline: two sweeps, and
    never less than ``_KEEPER_CONFIRM_MIN_SECONDS``. The escalation count
    stays as it is, and an in-memory hold keeps the generic backoff check
    from pinging again before the deadline even if the state write fails. A
    later usage read confirms the windows (:func:`_confirm_pending`,
    :func:`_confirm_pending_from_poll`); only a deadline that passes
    unconfirmed escalates the backoff (600 s, 1200 s, 2400 s, then the
    one-hour cap at the default interval). The count is dropped ONLY on
    success: a ping that never opens its window must not restart at the
    first step on every cycle.
    """
    from jacked.usage_plans import has_session

    account_id = acct["id"]
    needs_5h, needs_7d = needed
    cache_fields = _ping_window_cache_fields(result, has_session=has_session(acct))
    if cache_fields:
        try:
            await asyncio.to_thread(
                db.update_account_usage_cache, account_id, **cache_fields
            )
        except Exception:
            logger.warning(
                "Window keeper: could not store the ping's window for account %d",
                account_id,
                exc_info=True,
            )
    landed = await _keeper_usage_read(db, account_id, good_token, post_ping=True)
    fresh = await asyncio.to_thread(db.get_account, account_id) or {}
    # A deleted row (gone mid-sweep) confirms nothing. The evidence is per
    # window: the ping's own headers, or a read-back that landed and
    # accepted that window's reading after the ping (*now*).
    missing = [
        name
        for name, needed_before in (("5h", needs_5h), ("7d", needs_7d))
        if needed_before and not (
            fresh and _window_confirmed(
                fresh,
                name,
                evidence=bool(cache_fields)
                or (landed and _window_read_after(fresh, name, now)),
            )
        )
    ]
    # The row may hold a rotated token now: key the state on the CURRENT
    # candidate set so a persisted backoff survives the next sweep's check.
    fresh_fp = _candidate_fingerprint(*_keeper_candidates(fresh)) if fresh else None
    fields = {
        "last_ping_at": int(now),
        "last_status": "ok",
        "error_type": None,
        "request_id": result.request_id,
        "consecutive_auth_failures": 0,
        "next_attempt_at": None,
        "skip_reason": None,
        "paused_until": None,
        "token_fp": fresh_fp,
    }
    if not missing:
        _ping_backoff.pop(account_id, None)
        await asyncio.to_thread(_write_keeper_state, db, account_id, **fields)
        return
    reason = _pending_reason(missing, result, landed)
    pending_until = now + max(2 * check_interval, _KEEPER_CONFIRM_MIN_SECONDS)
    entry = _ping_backoff.setdefault(
        account_id, {"fails": 0, "skip_until": 0.0, "token_fp": fresh_fp}
    )
    entry["token_fp"] = fresh_fp
    entry["skip_until"] = pending_until
    fields.update(
        last_status="ping_pending",
        pending_windows=missing,
        pending_until=pending_until,
        pending_reason=reason,
        # Persist the count as it is: with next_attempt_at None,
        # _write_keeper_state would otherwise write 0.
        backoff_fails=int(entry["fails"]),
    )
    logger.info(
        "Window keeper: ping for account %d accepted; %s not visible yet (%s); "
        "confirming by %s",
        account_id,
        _window_phrase(missing),
        _PENDING_REASON_TEXT[reason],
        datetime.fromtimestamp(pending_until, timezone.utc).isoformat(timespec="seconds"),
    )
    await asyncio.to_thread(_write_keeper_state, db, account_id, **fields)
