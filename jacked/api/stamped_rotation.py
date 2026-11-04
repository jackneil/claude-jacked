"""The stamped path of the authority guard (#154i).

Claude Code keeps the ``_jackedAccountId`` stamp when it rewrites the
credential authority. Thus a ``/login`` to account B in one session, or a
refresh by a stale session that holds B, leaves an authority that is stamped
for account A but holds the tokens of B. The stamp records only the last
jacked write. It does not record the choice of the user.

On each observer pass, for a stamped payload whose tokens the stamped row
does not hold, this module:

1. identifies the payload one time through the profile endpoint, unless the
   payload holds the tokens of the last committed repair of the guard,
2. adopts the tokens into the row of the proven owner (B), so no refresh
   lineage is lost,
3. when B is not the stamped account A, reads the ``oauthAccount`` block of
   ``~/.claude.json`` and follows the account that it names (the rule that
   Jack settled on 2026-11-02):

   - The block names B: the user chose B. jacked makes B the desired and
     the active account. Then it re-stamps the authority for B with the
     same tokens.
   - The block still names A: a stale session refreshed the tokens of B.
     jacked reasserts A.
   - The block is missing or unreadable, or it names neither account or
     both accounts: jacked only adopts the tokens.

``jacked.api.stamped_evidence`` holds the evidence: the reading of the block
and the fingerprint of jacked's own writes. The guard state, the shared
repair floor and the settle rule stay in ``jacked.api.authority_guard``. A
repair that is owed is tried again on a later pass, with no second profile
request. Logs contain row ids and field names only. They never contain a
token or an email address.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable

from jacked.api import authority_guard as guard
from jacked.api.stamped_evidence import (
    OWNER,
    STAMPED,
    classify,
    is_own_publication,
    read_config_identity,
)

logger = logging.getLogger(__name__)

_PHASE_NEW = "new"
_PHASE_OWED = "owed"
_PHASE_DONE = "done"


def adopt_stamped_rotation(
    db,
    payload: dict,
    stamp: int,
    profile_lookup: Callable[[str], dict | None],
    *,
    now: float,
    activate: Callable[..., Any] | None,
) -> guard.HealResult:
    """Keep jacked's copy of a lineage in sync after a stamped rotation.

    The stamped row holding the tokens is the common case and costs no
    network call. Otherwise the payload is identified one time through the
    profile endpoint, the matching row is proven with the shared ownership
    rule, and the tokens go into THAT row, which can differ from the stamp.
    When it differs, the pass follows the account that ``oauthAccount``
    names (``follow_config``).
    """
    if _holds_payload_tokens(db.get_account(stamp), payload):
        return guard.HealResult(guard.ACTION_NONE, foreign_account_id=stamp)
    access_token = guard._oauth_section(payload).get("accessToken")
    if not isinstance(access_token, str) or not access_token:
        return guard.HealResult(guard.ACTION_NONE, foreign_account_id=stamp)
    digest = guard._payload_digest(payload)
    phase, owner_id = _stamped_phase(digest)
    if phase == _PHASE_DONE:
        return guard.HealResult(guard.ACTION_NONE, foreign_account_id=stamp)
    if phase == _PHASE_OWED:
        return _resume(db, payload, stamp, digest, owner_id, now=now, activate=activate)
    if is_own_publication(db, payload):
        # jacked's own committed repair. The row can hold newer tokens by
        # now, and adopting these again would move it back.
        _settle_stamped(digest)
        return guard.HealResult(guard.ACTION_NONE, foreign_account_id=stamp)
    found = _identify_owner(db, stamp, digest, access_token, profile_lookup)
    if isinstance(found, guard.HealResult):
        return found
    owner, profile = found
    return _adopt_into_owner(
        db, payload, stamp, digest, owner, profile, now=now, activate=activate
    )


def _resume(
    db, payload: dict, stamp: int, digest: str, owner_id: int, *, now: float, activate
) -> guard.HealResult:
    """Try an owed repair again for the owner that an earlier pass identified."""
    owner = db.get_account(owner_id)
    if owner is None:
        guard._mark_handled(digest, None, settled=True)
        return _skip(stamp, "identified account row disappeared")
    # Adopt again first: the row can have changed since the last pass.
    return _adopt_into_owner(
        db, payload, stamp, digest, owner, None, now=now, activate=activate
    )


def _holds_payload_tokens(row: dict | None, payload: dict) -> bool:
    """True when ``row`` already holds the payload's access and refresh tokens."""
    if not row:
        return False
    oauth = guard._oauth_section(payload)
    access_token = oauth.get("accessToken")
    refresh_token = oauth.get("refreshToken")
    if access_token and access_token == row.get("access_token"):
        # jacked's own primary-fallback publication (build_oauth_data). It
        # belongs to the primary lineage and must never enter the cc_* columns.
        return True
    if not access_token or access_token != row.get("cc_access_token"):
        return False
    return not refresh_token or refresh_token == row.get("cc_refresh_token")


def _stamped_phase(digest: str) -> tuple[str, int | None]:
    """Say what the stamped path owes the payload ``digest``.

    ``new``: identify the payload (it is new, or its lookup retry is due).
    ``owed``: the payload is identified and a repair is still owed, so the
    pass tries it again for the returned owner id. ``done``: nothing.
    """
    with guard._state_lock:
        if guard._stamped_handled["digest"] != digest:
            return _PHASE_NEW, None
        retry_at = guard._stamped_handled.get("retry_at")
        if retry_at is not None:
            return (_PHASE_NEW if retry_at <= time.time() else _PHASE_DONE), None
        handled = guard._handled
        owed = handled["digest"] == digest and not handled["settled"]
        if owed and handled["account_id"] is not None:
            return _PHASE_OWED, int(handled["account_id"])
        return _PHASE_DONE, None


def _settle_stamped(digest: str, retry_after: float | None = None) -> None:
    """Record the stamped payload as identified, or park it for a retry.

    A settled verdict retires the identification for good. A transient
    profile failure parks it for a retry delay instead. A failed database
    write (which raises past the caller) leaves it unset: a rotated
    single-use refresh token that is never adopted dies with the next
    authority overwrite, so the retry matters.
    """
    with guard._state_lock:
        guard._stamped_handled["digest"] = digest
        guard._stamped_handled["retry_at"] = (
            time.time() + retry_after if retry_after is not None else None
        )


def _identify_owner(
    db, stamp: int, digest: str, access_token: str, profile_lookup
) -> tuple[dict, dict] | guard.HealResult:
    """Return ``(owner_row, profile)``, or the result that ends this pass."""
    profile = profile_lookup(access_token)
    ended = _unusable_profile(profile, stamp, digest)
    if ended is not None:
        return ended
    email, organization_uuid = guard._profile_identity(profile)
    if email is None:
        _settle_stamped(digest)
        return _skip(stamp, "profile carries no email")
    owner = guard._match_account(db, email, organization_uuid)
    if owner is None:
        _settle_stamped(digest)
        logger.warning(
            "Stamped authority payload for account %d holds tokens of an account "
            "that is not a known jacked account (organization %s, payload %s); "
            "nothing was adopted",
            stamp,
            guard._uuid_prefix(organization_uuid),
            digest[:8],
        )
        return guard.HealResult(
            guard.ACTION_UNKNOWN_ACCOUNT, foreign_account_id=stamp, reason="unknown account"
        )
    if int(owner["id"]) != stamp:
        logger.warning(
            "The credential authority carries account %d's tokens under account "
            "%d's stamp; adopting them into account %d",
            int(owner["id"]),
            stamp,
            int(owner["id"]),
        )
    return owner, profile


def _unusable_profile(profile: Any, stamp: int, digest: str) -> guard.HealResult | None:
    """Return the result for a lookup that identified no one, or None."""
    if profile is None:
        _settle_stamped(digest, retry_after=guard._STAMPED_LOOKUP_RETRY_SECONDS)
        logger.info(
            "Stamped authority payload for account %d carries tokens jacked does "
            "not hold; the profile lookup did not complete, so nothing was "
            "adopted yet (payload %s); it is retried in %d s",
            stamp,
            digest[:8],
            int(guard._STAMPED_LOOKUP_RETRY_SECONDS),
        )
        return _skip(stamp, "profile lookup failed")
    if not isinstance(profile, dict) or not profile:
        # The endpoint refused the token: a verdict, not a hiccup.
        _settle_stamped(digest)
        logger.info(
            "Stamped authority payload for account %d carries tokens the profile "
            "endpoint refused; nothing adopted (payload %s)",
            stamp,
            digest[:8],
        )
        return _skip(stamp, "profile refused the token")
    return None


def _adopt_into_owner(
    db,
    payload: dict,
    stamp: int,
    digest: str,
    owner: dict,
    profile: dict | None,
    *,
    now: float,
    activate: Callable[..., Any] | None,
) -> guard.HealResult:
    """Adopt the tokens into ``owner``, then follow ``oauthAccount`` if needed."""
    owner_id = int(owner["id"])
    adopted = guard._import_rotated_tokens(db, owner, payload, profile=profile)
    if adopted == guard.ADOPTION_RACE_LOST:
        # Not settled: the next pass adopts the rotation once the row is quiet.
        return guard.HealResult(
            guard.ACTION_NONE,
            foreign_account_id=owner_id,
            reason="the row kept changing; adoption retried next pass",
        )
    if adopted == guard.ADOPTION_DEFERRED:
        _settle_stamped(digest, retry_after=guard._STAMPED_LOOKUP_RETRY_SECONDS)
        return _skip(owner_id, "ownership lookup failed; adoption retried next pass")
    _settle_stamped(digest)
    if adopted == guard.ADOPTION_REFUSED:
        guard._mark_handled(digest, None, settled=True)
        return guard.HealResult(
            guard.ACTION_NONE,
            foreign_account_id=stamp,
            reason="the tokens are not proven to belong to the matched account",
        )
    if owner_id != stamp:
        try:
            followed = follow_config(
                db, payload, digest, stamp, owner, now=now, activate=activate
            )
        except Exception as exc:  # noqa: BLE001 - a failed read must not lose the follow
            # The tokens are safe in the row. Keep the repair owed, so that
            # the next pass follows the block with no second profile request.
            guard._mark_handled(digest, owner_id, settled=False)
            logger.info(
                "Following the Claude config identity for account %d raised %s; "
                "it is tried again on the next pass",
                owner_id,
                type(exc).__name__,
            )
            return _skip(owner_id, "the follow failed; tried again next pass")
        if followed is not None:
            return followed
    return guard.HealResult(
        guard.ACTION_ROTATION_ADOPTED if adopted else guard.ACTION_NONE,
        foreign_account_id=owner_id,
        reason="" if adopted else "nothing new to adopt",
    )


# --- follow the account that oauthAccount names --------------------------------


def follow_config(
    db,
    payload: dict,
    digest: str,
    stamp: int,
    owner: dict,
    *,
    now: float,
    activate: Callable[..., Any] | None,
) -> guard.HealResult | None:
    """Repair a stamped mismatch toward the account that ``oauthAccount`` names.

    The tokens are already in the row of ``owner``. Return None when the
    guard only adopts them: the block cannot decide, or the stamped account
    cannot be reasserted. That payload is then settled, so this runs one time
    for it. A repair that is owed keeps the payload open for the next pass.
    """
    owner_id = int(owner["id"])
    verdict, why = classify(read_config_identity(), owner, db.get_account(stamp))
    if verdict == OWNER and not _followable(owner):
        # The user signed in to an account jacked has disabled or deleted:
        # the tokens are saved, the choice is not followed (fail safe).
        guard._mark_handled(digest, owner_id, settled=True)
        logger.warning(
            "oauthAccount names account %d, which is disabled or deleted in jacked; "
            "jacked only adopts the tokens and does not make it the active account",
            owner_id,
        )
        return None
    if verdict == OWNER:
        guard._mark_handled(digest, owner_id, settled=False)
        return _follow_owner(db, payload, digest, owner_id, now=now, activate=activate)
    if verdict == STAMPED:
        target, refusal = _stale_target(db, stamp)
        if target is not None:
            guard._mark_handled(digest, owner_id, settled=False)
            return _restore_stamped(db, digest, owner_id, target, now=now, activate=activate)
        guard._mark_handled(digest, owner_id, settled=True)
        logger.info(
            "jacked cannot restore account %d after a stale-session rotation "
            "(tokens of account %d): %s. jacked only adopts the tokens.",
            stamp,
            owner_id,
            refusal,
        )
        return None
    guard._mark_handled(digest, owner_id, settled=True)
    logger.info(
        "jacked cannot classify the stamped rotation (tokens of account %d, stamp "
        "of account %d): %s. jacked only adopts the tokens.",
        owner_id,
        stamp,
        why,
    )
    return None


def _followable(owner: dict) -> bool:
    """True when jacked may make ``owner`` the active account.

    >>> _followable({"is_active": 1, "is_deleted": 0})
    True
    >>> _followable({"is_active": 0}), _followable({"is_active": 1, "is_deleted": 1})
    (False, False)
    """
    return bool(owner.get("is_active")) and not owner.get("is_deleted")


def _stale_target(db, stamp: int) -> tuple[dict | None, str]:
    """Return the row to reassert after a stale-session rotation, or why none."""
    from jacked.web.database import selected_account_id

    desired_id = selected_account_id(db)
    if desired_id != stamp:
        return None, "the desired account is not the stamped account"
    row = db.get_account(desired_id)
    refusal = guard._desired_row_refusal(row)
    return (None, refusal) if refusal else (row, "")


def _follow_owner(
    db, payload: dict, digest: str, owner_id: int, *, now: float, activate
) -> guard.HealResult:
    """Make the owner the active account, then re-stamp the authority for it."""
    _point_at(db, owner_id)
    row = db.get_account(owner_id)
    blocker = guard._restamp_blocker(row, payload)
    if blocker:
        guard._mark_handled(digest, owner_id, settled=True)
        logger.warning(
            "Account %d is the active account, but jacked cannot re-stamp the "
            "authority for it: account %d %s. The authority keeps its stamp.",
            owner_id,
            owner_id,
            blocker,
        )
        return _result(guard.ACTION_SKIPPED, owner_id, owner_id, f"account {owner_id} {blocker}")
    if not guard._claim_repair_slot(digest, owner_id, now):
        return _result(guard.ACTION_SKIPPED, owner_id, owner_id, "reassert rate limit is active")
    outcome = guard._restamp_foreign_holder(db, row, activate, payload)
    guard._settle_after(digest, owner_id, outcome, row_id=owner_id, repair="re-stamp")
    if guard._repair_committed(outcome):
        return _result(guard.ACTION_ADOPTED, owner_id, owner_id)
    return _result(guard.ACTION_SKIPPED, owner_id, owner_id, _not_committed(outcome, "re-stamp"))


def _restore_stamped(
    db, digest: str, owner_id: int, target: dict, *, now: float, activate
) -> guard.HealResult:
    """Reassert the stamped account after a stale session wrote the payload."""
    desired_id = int(target["id"])
    if not guard._claim_repair_slot(digest, owner_id, now):
        return _result(guard.ACTION_SKIPPED, owner_id, desired_id, "reassert rate limit is active")
    outcome = guard._attempt_repair(db, target, activate, "reassert")
    guard._settle_after(digest, owner_id, outcome, row_id=desired_id, repair="reassert")
    if not guard._repair_committed(outcome):
        reason = _not_committed(outcome, "reassert")
        return _result(guard.ACTION_SKIPPED, owner_id, desired_id, reason)
    logger.info(
        "Account %d restored after a stale-session rotation stamped for it "
        "(oauthAccount still names it)",
        desired_id,
    )
    return _result(guard.ACTION_REASSERTED, owner_id, desired_id)


def _point_at(db, account_id: int) -> None:
    """Make ``account_id`` the desired and the active account in one write."""
    from jacked.web.database import SELECTED_ACCOUNT_KEYS

    wanted = str(account_id)
    if all(db.get_setting(key) == wanted for key in SELECTED_ACCOUNT_KEYS):
        return
    try:
        db.set_settings({key: wanted for key in SELECTED_ACCOUNT_KEYS})
    except Exception as exc:
        # The guard never raises past the observer; say why the user's
        # choice is not the active account yet, then let the pass end.
        logger.warning(
            "Account %d: could not record it as the active account after a "
            "stamped rotation (%s); the next pass tries again",
            account_id,
            type(exc).__name__,
        )
        raise
    logger.info(
        "Account %d adopted as the active account after a stamped rotation "
        "(oauthAccount names it)",
        account_id,
    )
    # The user chose this account: the same follow-up as a manual switch
    # (routes/auth.py), so the auto-swap sweep does not move off it within
    # minutes and open sessions learn the account changed under them.
    db.mark_global_sessions_pending()
    from jacked import usage_events
    from jacked.api import usage_monitor  # local import: avoids a cycle

    usage_monitor.note_external_swap()
    # The menu-bar pill follows the active account (see credential_repository).
    usage_events.bump()


def _not_committed(outcome: Any, repair: str) -> str:
    """Say why a repair did not commit. The text contains no value.

    >>> from jacked.credentials.models import SwitchOutcome
    >>> _not_committed(SwitchOutcome.BUSY, "reassert")
    'reassert outcome busy'
    """
    from jacked.credentials.models import SwitchOutcome

    if outcome is SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS:
        return "another credential operation holds the switch lease"
    return f"{repair} outcome {guard._outcome_label(outcome)}"


def _skip(account_id: int, reason: str) -> guard.HealResult:
    return guard.HealResult(guard.ACTION_SKIPPED, foreign_account_id=account_id, reason=reason)


def _result(action: str, foreign: int, desired: int, reason: str = "") -> guard.HealResult:
    return guard.HealResult(
        action, foreign_account_id=foreign, desired_account_id=desired, reason=reason
    )
