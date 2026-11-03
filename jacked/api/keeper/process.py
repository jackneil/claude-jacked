"""Window keeper: one account in a pass, and the verdict of a failed ping.

:func:`_keeper_process_account` decides if an account needs a ping, sends
the ping and records the result. :func:`_keeper_record_failure` sorts a
failed ping by its last attempt: an auth failure, a rate limit, a transport
error or a different error. Each verdict goes into the keeper state.
"""

import asyncio
import logging
import time
from datetime import datetime

from jacked.api.keeper.auth_ladder import (
    _KEEPER_MAX_RETRY_SECONDS, _LadderOutcome, _candidate_fingerprint, _keeper_auth_ladder,
    _keeper_candidates,
)
from jacked.api.keeper.confirm import _keeper_record_success, _keeper_window_needed
from jacked.api.keeper.state import (
    _arm_ping_backoff, _keeper_state_of, _note_keeper_skip, _ping_backoff, _sync_backoff,
    _write_keeper_state,
)
from jacked.api.keeper_confirmation import _confirm_pending
from jacked.service.pill_text import finite_number

logger = logging.getLogger("jacked.api.usage_monitor")


async def _keeper_record_failure(
    db,
    acct: dict,
    state: dict,
    result,
    ladder: _LadderOutcome | None,
    fp: str | None,
    check_interval: float,
    now: float,
    tried: set | None = None,
) -> None:
    """Classify a failed sweep by its LAST attempt and persist the verdict.

    ``tried`` is every token the sweep pinged (the ladder's rotations
    included). A verdict is persisted against the CURRENT candidate set, and
    only when every current candidate was tried: a token that landed during
    the ladder (a dashboard re-auth, a reconcile) is untried and gets an
    immediate retry instead of a latch, while a token the keeper itself
    rotated and then saw rejected is tried and does latch (otherwise every
    sweep would rotate once more: credential churn).

    Async so the DB read and write run off the event loop while every
    ``_ping_backoff`` change stays on it.
    """
    account_id = acct["id"]
    tried = tried or set()
    base = {
        "last_ping_at": int(now),
        "error_type": result.error_type,
        "request_id": result.request_id,
        "skip_reason": None,
        "paused_until": None,
        "token_fp": fp,
    }
    # A persisted count that is not a finite number reads as 0: int() on it
    # raised, so no verdict was ever written.
    auth_failures = int(finite_number(state.get("consecutive_auth_failures")) or 0)
    if result.is_auth_failure:
        auth_failures += 1
        fresh = await asyncio.to_thread(db.get_account, account_id) or acct
        fresh_cc, fresh_primary = _keeper_candidates(fresh)
        fresh_fp = _candidate_fingerprint(fresh_cc, fresh_primary)
        base["token_fp"] = fresh_fp
        untried = [t for t in (fresh_cc, fresh_primary) if t and t not in tried]
        if untried:
            # A token landed while the ladder ran (a dashboard re-auth, a
            # reconcile): it was never pinged, so no verdict is recorded
            # against it. Retry at once.
            logger.info(
                "Window keeper: account %d got new tokens during recovery — "
                "retrying next sweep",
                account_id,
            )
            _ping_backoff.pop(account_id, None)
            await asyncio.to_thread(
                _write_keeper_state,
                db,
                account_id,
                last_status="auth_failed",
                consecutive_auth_failures=auth_failures,
                next_attempt_at=now,
                **base,
            )
            return
        if ladder is not None and ladder.pending:
            # A rotation may still produce a usable token (lock held, a
            # transient exchange failure, an unknown active account): back
            # off, never give up on the credential.
            skip_until = _arm_ping_backoff(account_id, fp, check_interval, now)
            logger.info(
                "Window keeper: account %d rejected every token but the %s "
                "rotation is retryable — backing off",
                account_id,
                ladder.pending_reason or "pending",
            )
            await asyncio.to_thread(
                _write_keeper_state,
                db,
                account_id,
                last_status="auth_failed",
                consecutive_auth_failures=auth_failures,
                next_attempt_at=skip_until,
                **base,
            )
            return
        _ping_backoff.pop(account_id, None)
        logger.warning(
            "Window keeper: every token for account %d was rejected "
            "(status=%s error_type=%s) — re-auth needed; pings stop until "
            "the account gets a new token",
            account_id,
            result.status,
            result.error_type,
        )
        await asyncio.to_thread(
            _write_keeper_state,
            db,
            account_id,
            last_status="needs_reauth",
            consecutive_auth_failures=auth_failures,
            next_attempt_at=None,
            **base,
        )
        return

    if result.is_rate_limited:
        retry_at = None
        if result.retry_after is not None:
            retry_at = now + min(result.retry_after, _KEEPER_MAX_RETRY_SECONDS)
        else:
            reset_iso = result.reset_5h_iso()
            if reset_iso:
                try:
                    reset_ts = datetime.fromisoformat(reset_iso).timestamp()
                except ValueError:
                    reset_ts = None
                if reset_ts and now < reset_ts <= now + _KEEPER_MAX_RETRY_SECONDS:
                    retry_at = reset_ts
        if retry_at is None:
            retry_at = _arm_ping_backoff(account_id, fp, check_interval, now)
        else:
            entry = _ping_backoff.setdefault(
                account_id, {"fails": 0, "skip_until": 0.0, "token_fp": fp}
            )
            entry["token_fp"] = fp
            entry["skip_until"] = retry_at
        base["error_type"] = result.error_type or "rate_limited"
        await asyncio.to_thread(
            _write_keeper_state,
            db,
            account_id,
            last_status="rate_limited",
            next_attempt_at=retry_at,
            **base,
        )
        return

    if result.is_transient:
        # Network or server trouble says nothing about the account: retry on
        # the next sweep and leave the escalation count alone.
        entry = _ping_backoff.get(account_id)
        if entry is not None:
            entry["skip_until"] = 0.0
        await asyncio.to_thread(
            _write_keeper_state,
            db,
            account_id,
            last_status="transport",
            next_attempt_at=now + check_interval,
            **base,
        )
        return

    skip_until = _arm_ping_backoff(account_id, fp, check_interval, now)
    await asyncio.to_thread(
        _write_keeper_state,
        db,
        account_id,
        last_status="error",
        next_attempt_at=skip_until,
        **base,
    )


async def _keeper_process_account(db, acct: dict, check_interval: float) -> str:
    """Run the keeper for one account. Returns 'pinged', 'failed' or 'skipped'.

    A pending ping (#238i) holds the account until its confirmation
    deadline. Once it settles (:func:`_confirm_pending`), the account goes
    on through the needs and backoff rules in the same pass: a window that
    lapsed while the machine slept gets its ping now, and a pre-wake pass
    does not skip it.
    """
    from jacked.usage_plans import has_session
    from jacked.web import window_keeper as wk

    account_id = acct["id"]
    state = _keeper_state_of(acct)

    if (acct.get("provider") or "claude") == "codex":
        await asyncio.to_thread(_note_keeper_skip, db, acct, state, "codex_no_keeper")
        return "skipped"

    pending = await _confirm_pending(db, acct, state, time.time(), check_interval)
    if pending == "skipped":
        return pending
    if pending is not None:
        # Settled ("confirmed" or "failed"): read the row and the state it
        # wrote, then let the rules below decide about a new ping.
        fresh = await asyncio.to_thread(db.get_account, account_id)
        if not fresh:
            return pending
        acct, state = fresh, _keeper_state_of(fresh)

    needs_5h, needs_7d = _keeper_window_needed(acct, state)
    if not needs_5h and not needs_7d:
        if not has_session(acct):
            # The plan has no 5-hour window (a verified Ultra): no ping can
            # open one, so say why the keeper stays idle on this account.
            await asyncio.to_thread(
                _note_keeper_skip, db, acct, state, "no_session_window"
            )
        elif state.get("skip_reason"):
            await asyncio.to_thread(_note_keeper_skip, db, acct, state, None)
        return "skipped"
    if not acct.get("auto_swap_enabled"):
        await asyncio.to_thread(
            _note_keeper_skip, db, acct, state, "auto_swap_disabled"
        )
        return "skipped"

    cc_token, primary_token = _keeper_candidates(acct)
    first_token = cc_token or primary_token
    if not first_token:
        await asyncio.to_thread(_note_keeper_skip, db, acct, state, "no_token")
        return "skipped"
    first_is_cc = bool(cc_token)
    # One fingerprint for the whole candidate set: a change to EITHER chain
    # (a primary-only re-auth included) releases a persisted verdict.
    fp = _candidate_fingerprint(cc_token, primary_token)
    now = time.time()

    if state.get("last_status") == "needs_reauth" and state.get("token_fp") == fp:
        logger.debug(
            "Window keeper: account %d needs re-auth — waiting for a new token",
            account_id,
        )
        if state.get("skip_reason"):
            await asyncio.to_thread(_note_keeper_skip, db, acct, state, None)
        return "skipped"

    skip_until = _sync_backoff(account_id, state, fp)
    if skip_until and skip_until > now:
        logger.debug(
            "Window keeper: account %d in ping backoff for %.0fs more — skipping",
            account_id,
            skip_until - now,
        )
        await asyncio.to_thread(
            _note_keeper_skip, db, acct, state, "backoff", next_attempt_at=skip_until
        )
        return "skipped"

    logger.info(
        "Window keeper: pinging account %d (%s)%s%s",
        account_id,
        acct.get("email", "?"),
        " [5h expired]" if needs_5h else "",
        " [7d reset]" if needs_7d else "",
    )
    tried = {first_token}
    result = await wk.ping_account(first_token, account_id=account_id)
    good_token = first_token if result.ok else None
    ladder: _LadderOutcome | None = None
    if not result.ok and result.is_auth_failure:
        ladder = await _keeper_auth_ladder(db, acct, first_token, first_is_cc, tried)
        good_token = ladder.good_token
        if ladder.last is not None:
            result = ladder.last

    now = time.time()
    if good_token is not None:
        await _keeper_record_success(
            db, acct, good_token, result, check_interval, now,
            needed=(needs_5h, needs_7d),
        )
        return "pinged"
    await _keeper_record_failure(
        db, acct, state, result, ladder, fp, check_interval, now, tried=tried
    )
    return "failed"
