"""Window keeper: the tokens that the keeper may send, and the 401/403 ladder.

An account has up to two candidate tokens, the Claude Code token and the
primary token (:func:`_keeper_candidates`). One fingerprint covers both, so
a change to either token releases a verdict for the old set
(:func:`_candidate_fingerprint`). After a rejected ping, the ladder gets a
new token, pings again, then tries the primary token (:func:`_keeper_auth_ladder`).
"""

import asyncio
import logging
from dataclasses import dataclass

logger = logging.getLogger("jacked.api.usage_monitor")


@dataclass
class _LadderOutcome:
    """What the 401/403 recovery ladder achieved for one account.

    ``good_token`` is the token whose ping succeeded (None when every attempt
    failed). ``last`` is the last PingResult. ``pending`` is set when a
    rotation that could still produce a usable token did not complete this
    sweep (lock held by another path, a transient exchange failure, or a
    rotation refused because the active account is unknown): such a failure
    is retried with a backoff, never recorded as needs_reauth.
    """

    good_token: str | None = None
    last: object | None = None
    pending: bool = False
    pending_reason: str | None = None


# Exchange errors that say the refresh token itself is dead or refused. Any
# other FAILED outcome (not_sent, reply_lost, http_5xx, circuit_breaker,
# lock_timeout, db_write_failed, ...) is transient.
_ROTATION_TERMINAL_ERRORS = frozenset(
    {
        "invalid_grant", "http_400", "http_401", "http_403", "account_not_found",
        "no_refresh_token",
    }
)
# Ceiling on a server-provided retry or 5h reset the keeper will honor; a
# 7d reset may lie up to _KEEPER_MAX_7D_RESET_SECONDS ahead.
_KEEPER_MAX_RETRY_SECONDS = 6 * 3600
_KEEPER_MAX_7D_RESET_SECONDS = 8 * 86400


def _keeper_candidates(acct: dict) -> tuple[str | None, str | None]:
    """(cc token the keeper may send or None, primary token or None).

    A dead cc chain (invalid_grant) or an API-refused cc token
    (auth_rejected), both recorded per chain by the usage path, count as no
    cc token: the keeper goes straight to the primary token.
    """
    from jacked.api.credential_helpers import CC_TOKEN_UNUSABLE_FAILURES

    cc = acct.get("cc_access_token")
    if acct.get("cc_refresh_failure_type") in CC_TOKEN_UNUSABLE_FAILURES:
        cc = None
    return (cc or None), _keeper_primary(acct)


def _keeper_primary(acct: dict) -> str | None:
    """The primary token the keeper may send, or None while a cross-account
    verdict binds it: a ping is a usage request, and it would open the other
    account's window and record its reset under this row.

    >>> from jacked.web.refresh_verdict import token_fingerprint
    >>> _keeper_primary({"access_token": "p", "refresh_token": "rt"})
    'p'
    >>> _keeper_primary({"access_token": "p", "refresh_token": "rt",
    ...                  "foreign_token_fp": token_fingerprint("rt")}) is None
    True
    """
    from jacked.web.refresh_verdict import primary_tokens_foreign

    if primary_tokens_foreign(acct):
        return None
    return acct.get("access_token") or None


def _candidate_fingerprint(cc: str | None, primary: str | None) -> str | None:
    """Fingerprint of the keeper's whole candidate set for one account.

    Keyed on BOTH chains so that a change to either one (a primary-only
    re-auth, a cc rotation) invalidates a persisted verdict or backoff that
    was recorded against the old set.

    >>> _candidate_fingerprint(None, None) is None
    True
    >>> _candidate_fingerprint("a", None) != _candidate_fingerprint("a", "b")
    True
    """
    from jacked.web import window_keeper as wk

    if not cc and not primary:
        return None
    return f"{wk.token_fingerprint(cc) or '-'}|{wk.token_fingerprint(primary) or '-'}"


def _rotation_is_transient(error: str | None) -> bool:
    """True when a failed exchange may succeed on a later attempt.

    >>> _rotation_is_transient("not_sent"), _rotation_is_transient("invalid_grant")
    (True, False)
    """
    if error is None:
        return True
    if error in _ROTATION_TERMINAL_ERRORS:
        return False
    return True


async def _keeper_auth_ladder(
    db, acct: dict, failed_token: str, failed_is_cc: bool, tried: set
) -> _LadderOutcome:
    """Recover from a 401/403 ping.

    1. Get a new token for the chain that owns the failed token. The cc
       chain is rotated only when ``cc_rotation_block`` allows it (invariant
       I2 fails closed): the active account reconciles from Claude Code's
       live store instead; an unknown active account or a row whose tokens a
       running session holds is never rotated. A failed PRIMARY token rotates
       the primary chain (jacked's own, never handed to Claude Code).
    2. Re-ping once with that token.
    3. Ping with the primary access token if it is present and untried.
    """
    from jacked.api.credential_helpers import cc_rotation_block
    from jacked.web import auth as auth_mod
    from jacked.web import window_keeper as wk

    account_id = acct["id"]
    out = _LadderOutcome()
    candidate: str | None = None

    if failed_is_cc:
        try:
            block = await asyncio.to_thread(cc_rotation_block, account_id, db)
        except Exception:
            logger.debug("Window keeper: rotation check failed", exc_info=True)
            block = "check_failed"
        if block == "active":
            logger.info(
                "Window keeper: skipping CC refresh for active account %d — "
                "reconciling instead",
                account_id,
            )
            try:
                from jacked.api.credential_helpers import (
                    reconcile_credentials_from_live_store,
                )

                await asyncio.to_thread(
                    reconcile_credentials_from_live_store, account_id, db
                )
                fresh = await asyncio.to_thread(db.get_account, account_id) or {}
                candidate = fresh.get("cc_access_token")
            except Exception:
                logger.exception(
                    "Window keeper reconcile failed for active account %d", account_id
                )
        elif block is not None:
            # Unknown active account, or a running session holds this row's
            # tokens: rotating would kill that session (I2). Retry later.
            logger.info(
                "Window keeper: not rotating the CC chain for account %d (%s)",
                account_id,
                block,
            )
            # The hold can end without this row's tokens changing (the
            # authority switches away; the resolver recovers), so none of
            # these is a verdict on the credential: retry with a backoff.
            out.pending = True
            out.pending_reason = block
        else:
            res = await auth_mod.refresh_cc_token_result(account_id, db, force=True)
            logger.info(
                "Window keeper: forced CC rotation for account %d: %s%s",
                account_id,
                res.outcome.value,
                f" ({res.error})" if res.error else "",
            )
            if res.new_token:
                candidate = res.access_token
            elif res.outcome is auth_mod.CCRefreshOutcome.LOCK_HELD:
                out.pending, out.pending_reason = True, "lock_held"
            elif res.outcome is auth_mod.CCRefreshOutcome.FAILED and _rotation_is_transient(
                res.error
            ):
                out.pending, out.pending_reason = True, res.error or "transient"
    else:
        try:
            flow = await auth_mod._refresh_token_flow(
                account_id, db, auth_mod.RefreshMode.PRIMARY_CIRCUIT_BREAKER
            )
        except Exception:
            logger.warning(
                "Window keeper: primary rotation raised for account %d",
                account_id,
                exc_info=True,
            )
            flow = None
        if flow is not None and flow.success and flow.access_token:
            candidate = flow.access_token
        elif flow is not None and _rotation_is_transient(flow.error):
            out.pending, out.pending_reason = True, flow.error or "transient"
        elif flow is None:
            out.pending, out.pending_reason = True, "exception"
        logger.info(
            "Window keeper: primary rotation for account %d: %s",
            account_id,
            "new token" if candidate and candidate not in tried else "no new token",
        )

    if candidate and candidate not in tried:
        tried.add(candidate)
        out.last = await wk.ping_account(candidate, account_id=account_id)
        if out.last.ok:
            out.good_token = candidate
            return out
        if not out.last.is_auth_failure:
            return out

    row = await asyncio.to_thread(db.get_account, account_id) or acct
    primary = _keeper_primary(row)
    if primary and primary not in tried:
        tried.add(primary)
        logger.info(
            "Window keeper: trying the primary token for account %d", account_id
        )
        out.last = await wk.ping_account(primary, account_id=account_id)
        if out.last.ok:
            out.good_token = primary
    return out
