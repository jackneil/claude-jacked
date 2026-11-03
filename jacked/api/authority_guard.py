"""Repair foreign writes to the Claude credential authority.

Claude Code refreshes an expiring OAuth token from the account it holds in
memory and writes the refreshed payload straight into the shared authority
(the macOS Keychain item, or the global credential file elsewhere). That
payload carries no ``_jackedAccountId`` stamp, so a long-lived session on
another account silently replaces the account the user chose.

This module runs on the service observer pass. It reads the authority, and
when the payload is unstamped it:

1. identifies the payload through the OAuth profile endpoint,
2. adopts the rotated tokens into that account's row, so no refresh lineage
   is lost (losing one is what kills an account with ``invalid_grant``),
3. reasserts the desired default account through the transaction engine,
   or, when the desired account cannot be reasserted, re-stamps the
   authority for the account that holds it.

An account jacked cannot identify is never overwritten: reasserting over it
would destroy a refresh lineage jacked does not hold.

Claude Code keeps the stamp when it rewrites the authority. When the payload
is stamped but the stamped row does not hold its tokens (a ``/login`` to
another account, or a refresh by a stale session), the guard:

1. identifies the payload one time through the profile endpoint, unless the
   payload holds the tokens of jacked's last committed repair,
2. adopts the tokens into the row of the proven owner, which can differ from
   the stamp,
3. when the owner differs from the stamp, follows the account that the
   ``oauthAccount`` block of ``~/.claude.json`` names. It makes the owner
   the active account and re-stamps the authority for it, or it reasserts
   the stamped account. When the block cannot decide, the guard only adopts
   the tokens.

``jacked.api.stamped_rotation`` holds this stamped path, and
``jacked.api.stamped_evidence`` holds the evidence that it uses. The guard
state, the repair floor and the settle rule stay in this module.

A re-stamp publishes the row's own tokens. It runs only when these are the
tokens that the authority holds, so it adds only the stamp.

All of these repairs obey one retry rule. Only the outcome of an attempt
settles the payload. The attempts share a floor of one a minute. A held
switch lease is retried, and any other failure is tried at most
``RESTAMP_MAX_ATTEMPTS`` times for one payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from jacked.credentials.models import SwitchOutcome

logger = logging.getLogger(__name__)

# One repair per minute at most. The observer passes every 10 seconds, and a
# repair (a reassert of the desired account, or a re-stamp of the account that
# holds the authority) is a full credential transaction against the live
# authority. Both repairs share this floor.
REASSERT_MIN_INTERVAL_SECONDS = 60.0
# A repair that ends in a durable failure (any outcome that is not committed
# and is not a held switch lease) is tried this many times for one payload.
# The last failure settles the payload with one WARNING, so a repair that
# cannot succeed does not run once a minute forever.
RESTAMP_MAX_ATTEMPTS = 3
# Consecutive transient ("not now") answers from the engine for one payload
# before the guard logs one WARNING about the run. The run itself is not
# capped: the floor bounds it to one attempt a minute.
TRANSIENT_WARN_AFTER = 10
PROFILE_TIMEOUT_SECONDS = 10.0

ACTION_NONE = "none"
ACTION_ADOPTED = "adopted"
ACTION_REASSERTED = "reasserted"
ACTION_UNKNOWN_ACCOUNT = "unknown_account"
ACTION_SKIPPED = "skipped"
# A stamped payload whose tokens jacked did not hold: a running Claude Code
# session rotated its lineage. The tokens went into the proven owner's row.
ACTION_ROTATION_ADOPTED = "rotation_adopted"


@dataclass(frozen=True)
class HealResult:
    """Outcome of one authority heal attempt. Never carries a secret."""

    action: str
    foreign_account_id: int | None = None
    desired_account_id: int | None = None
    reason: str = ""


_state_lock = threading.Lock()


def _initial_handled() -> dict[str, Any]:
    return {
        "digest": None,
        "account_id": None,
        "settled": True,
        "attempts": 0,
        "transient": 0,
        "refusal_logged": False,
    }


# The payload jacked handled last, so the ten-second observer pass does not
# call the profile endpoint again for a payload it already identified.
# ``settled`` is False while a repair is still owed, for example when the
# repair floor or the switch lease held it off, or an attempt failed. The
# identified account is kept, so the retry costs no network call.
# ``attempts`` counts this payload's repair attempts that ended in a durable
# failure (see RESTAMP_MAX_ATTEMPTS). ``refusal_logged`` is True after the
# "cannot be reasserted" WARNING for this payload. Both start again when the
# payload changes.
_handled: dict[str, Any] = _initial_handled()
# The stamped payload whose rotation jacked identified last (settled), or
# whose transient lookup failure is waiting out its retry cooldown.
_stamped_handled: dict[str, Any] = {"digest": None, "retry_at": None}
# The time of the last repair attempt; the floor for the next one.
_last_reassert_at: float = 0.0


def reset_authority_guard_state() -> None:
    """Forget the handled payloads, their retry state and the repair floor."""
    global _last_reassert_at
    with _state_lock:
        _handled.update(_initial_handled())
        _stamped_handled.update({"digest": None, "retry_at": None})
        _last_reassert_at = 0.0


def _store_handled_locked(
    digest: str, account_id: int | None, *, settled: bool
) -> None:
    """Record the payload jacked handled last. The caller holds ``_state_lock``.

    A different payload starts its own retry state: the attempt count, the
    transient run and the refusal WARNING start again with the digest.
    """
    if _handled["digest"] != digest:
        _handled.update({"attempts": 0, "transient": 0, "refusal_logged": False})
    _handled.update({"digest": digest, "account_id": account_id, "settled": settled})


def _mark_handled(digest: str, account_id: int | None, *, settled: bool) -> None:
    with _state_lock:
        _store_handled_locked(digest, account_id, settled=settled)


def _payload_digest(payload: dict) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _default_read_authority() -> dict | None:
    """Read the authority payload through the certified store topology."""
    from jacked.credentials.models import StoreStatus
    from jacked.credentials.runtime import (
        SHIPPED_REGISTRY,
        build_stores,
        detect_claude_identity,
    )

    home = Path.home()
    resolution = SHIPPED_REGISTRY.resolve(detect_claude_identity(home))
    if not resolution.can_mutate or resolution.capability is None:
        raise RuntimeError(resolution.reason or "authority is not certified")
    stores = build_stores(resolution.capability, home)
    authority = stores[resolution.capability.authority.locator]
    result = authority.read()
    if result.status is not StoreStatus.OK or result.payload is None:
        raise RuntimeError(f"authority read {result.status.value}: {result.reason}")
    return result.payload.to_mapping()


@dataclass(frozen=True)
class ProfileLookup:
    """Outcome of one profile request. It never carries the access token.

    ``status_code`` is None when the request did not complete (a network
    failure or a timeout). ``profile`` is the response object only for an
    HTTP 200 with a JSON object body.
    """

    status_code: int | None
    profile: dict | None = None


def lookup_profile(access_token: str) -> ProfileLookup:
    """Identify one access token through the OAuth profile endpoint.

    This function sends one GET request. It never refreshes a token and
    never writes the database, so a caller can use it to prove who owns a
    token without changing any refresh lineage.
    """
    import httpx

    from jacked.web.oauth import OAUTH_BETA_HEADER, PROFILE_URL

    try:
        with httpx.Client(timeout=PROFILE_TIMEOUT_SECONDS) as client:
            response = client.get(
                PROFILE_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
            )
    except Exception:
        logger.debug("Authority profile lookup failed", exc_info=True)
        return ProfileLookup(None)
    if response.status_code != 200:
        logger.debug(
            "Authority profile lookup returned HTTP %d", response.status_code
        )
        return ProfileLookup(response.status_code)
    try:
        body = response.json()
    except ValueError:
        return ProfileLookup(200)
    return ProfileLookup(200, body if isinstance(body, dict) else None)


# Seconds before a stamped payload whose profile lookup failed TRANSIENTLY
# (no reply, 5xx) is identified again. Bounds the request rate while the
# failure lasts; a terminal refusal (4xx) settles the payload instead.
_STAMPED_LOOKUP_RETRY_SECONDS = 600.0


def _default_profile_lookup(access_token: str) -> dict | None:
    """Identify one access token through the OAuth profile endpoint.

    Returns the profile object on 200; an EMPTY dict when the endpoint
    refused the token (400/401/403/404: a revoked or foreign token, a
    terminal verdict); ``None`` when the request did not complete, the
    server failed, or the endpoint throttled it (408/429: a transient
    failure worth a later retry).
    """
    outcome = lookup_profile(access_token)
    if outcome.profile:
        return outcome.profile
    if outcome.status_code in _PROFILE_REFUSED_STATUSES:
        return {}
    return None


# Profile statuses that are a verdict on the token, not a hiccup.
_PROFILE_REFUSED_STATUSES = frozenset({400, 401, 403, 404})


def _uuid_prefix(value: Any) -> str:
    """Return the first 8 characters of a uuid for a values-free log."""
    return value[:8] if isinstance(value, str) and value else "unknown"


def _stamped_account_id(payload: dict) -> int | None:
    stamp = payload.get("_jackedAccountId")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, str)):
        return None
    try:
        account_id = int(stamp)
    except (TypeError, ValueError):
        return None
    return account_id if account_id > 0 else None


def _oauth_section(payload: dict) -> dict:
    section = payload.get("claudeAiOauth")
    return section if isinstance(section, dict) else {}


def _match_account(db, email: str, organization_uuid: str | None) -> dict | None:
    """Find the account row the foreign payload belongs to.

    The email plus the organization uuid identify a row. The organization uuid
    is used alone as a tie-break: a row that carries no organization uuid can
    still match on the email, but only when it is the sole candidate.
    """
    rows = db.list_accounts(include_inactive=True)
    wanted = email.strip().lower()
    candidates = [
        row
        for row in rows
        if (row.get("email") or "").strip().lower() == wanted
        and (row.get("provider") or "claude") == "claude"
    ]
    if not candidates:
        return None
    if organization_uuid:
        exact = [
            row for row in candidates if row.get("organization_uuid") == organization_uuid
        ]
        if len(exact) == 1:
            return exact[0]
        if exact:
            return None
        unstamped = [row for row in candidates if not row.get("organization_uuid")]
        return unstamped[0] if len(unstamped) == 1 else None
    return candidates[0] if len(candidates) == 1 else None


# _import_rotated_tokens result: the row kept changing under every attempt.
ADOPTION_RACE_LOST = -1
# _import_rotated_tokens result: the shared ownership rule refused the
# payload. jacked holds no copy of that refresh lineage, so nothing may
# overwrite the authority on the strength of this match.
ADOPTION_REFUSED = -2
# _import_rotated_tokens result: the ownership lookup did not complete (a
# transient failure), so nothing is proven either way. The caller must not
# settle the payload or overwrite the authority; the next pass tries again.
ADOPTION_DEFERRED = -3
_ADOPTION_ATTEMPTS = 3


def _import_rotated_tokens(
    db, row: dict, payload: dict, profile: dict | None = None
) -> int:
    """Adopt the rotated Claude Code tokens into one account row.

    The shared ownership rule (``live_payload_belongs_to``) gates the
    import: the identifying ``profile`` must name the row, or the row must
    already hold one of the tokens, or the profile endpoint must prove it.
    The payload stamp plays no part here, because a stamp survives Claude
    Code's writes for other accounts.

    The write is compare-and-set against the columns the decision read; when
    the row changed meanwhile (a re-auth, a reconcile) the decision is made
    again from the new row. Returns the number of fields written, 0 when there
    was nothing to adopt, ``ADOPTION_REFUSED`` when the ownership rule refused
    the payload (the caller must not overwrite the authority: jacked does not
    hold that lineage), or ``ADOPTION_RACE_LOST`` when the row kept changing
    (the caller must not settle the payload or overwrite the authority).
    """
    from jacked.api.credential_helpers import CC_IMPORT_GUARD_COLS

    account_id = int(row["id"])
    for _attempt in range(_ADOPTION_ATTEMPTS):
        updates = _adoption_updates(row, payload, profile)
        if updates is None:
            return ADOPTION_REFUSED
        if updates == ADOPTION_DEFERRED:
            return ADOPTION_DEFERRED
        if not updates:
            return 0
        expected = {col: row.get(col) for col in CC_IMPORT_GUARD_COLS}
        if db.update_account_if_unchanged(account_id, expected, **updates):
            logger.info(
                "Adopted rotated Claude Code tokens for account %d from a foreign "
                "authority write (%d field(s))",
                account_id,
                len(updates),
            )
            try:
                from jacked.api.usage_monitor import clear_ping_backoff

                clear_ping_backoff(account_id)
            except Exception:  # noqa: BLE001 - never fail an import over this
                logger.debug("Could not clear the keeper backoff", exc_info=True)
            return len(updates)
        # The ownership verdict for this payload is cached per token, so the
        # next decision sends no second profile request.
        fresh = db.get_account(account_id)
        if fresh is None:
            return 0
        row = fresh
        logger.info(
            "Account %d: the row changed while adopting rotated Claude Code "
            "tokens; deciding again",
            account_id,
        )
    logger.warning(
        "Account %d: the row kept changing during %d adoption attempts; the "
        "rotated Claude Code tokens were not adopted yet",
        account_id,
        _ADOPTION_ATTEMPTS,
    )
    return ADOPTION_RACE_LOST


def _adoption_updates(row: dict, payload: dict, profile: dict | None) -> dict | None:
    """The fields that adopt ``payload`` into ``row``.

    Returns None only when the ownership rule refuses the payload. jacked's
    own primary publication returns an empty dict: jacked holds that lineage
    in the primary columns, so there is nothing to adopt and nothing to lose.
    """
    from jacked.api.credential_helpers import (
        cc_chain_dead,
        dead_chain_adoption,
        expiry_seconds,
        is_primary_publication,
        live_payload_belongs_to,
        live_payload_ownership,
        refresh_token_importable,
    )

    account_id = int(row["id"])
    if is_primary_publication(row, _oauth_section(payload).get("accessToken")):
        return {}
    if profile is not None:
        owned = live_payload_belongs_to(row, payload, require_stamp=False, profile=profile)
    else:
        # No profile in hand: the shared rule proves offline or asks once.
        # A lookup that did not complete proves nothing either way.
        owned = live_payload_ownership(row, payload, require_stamp=False)
        if owned is None:
            logger.info(
                "Account %d: the ownership lookup for the rotated Claude Code "
                "tokens did not complete; adoption is retried next pass",
                account_id,
            )
            return ADOPTION_DEFERRED
    if not owned:
        logger.warning(
            "Account %d: rotated Claude Code tokens are not proven to belong to "
            "this account; not adopting them",
            account_id,
        )
        return None
    oauth = _oauth_section(payload)
    updates: dict[str, Any] = {}

    access_token = oauth.get("accessToken")
    if access_token and access_token != row.get("cc_access_token"):
        updates["cc_access_token"] = access_token

    expires_at = expiry_seconds(oauth.get("expiresAt"))
    if expires_at is not None and expires_at != row.get("cc_expires_at"):
        updates["cc_expires_at"] = expires_at

    refresh_token = oauth.get("refreshToken")
    adoption = dead_chain_adoption(row, oauth)
    if adoption is not None:
        # The settled #149i rule, shared with reconcile and the launch sync:
        # a proven new lineage over a dead chain is adopted, with its
        # pass-through fields bound to it, and the cc breaker clears.
        updates.update(adoption)
    elif refresh_token and cc_chain_dead(row):
        # The failed token, a condemned one, or the primary token: nothing
        # beyond the access token and its expiry is taken.
        logger.debug(
            "Account %d: not adopting the Claude Code refresh token over a dead chain",
            account_id,
        )
    elif (
        refresh_token
        and refresh_token != row.get("cc_refresh_token")
        and refresh_token_importable(row, refresh_token)
    ):
        updates["cc_refresh_token"] = refresh_token
        # A live rotation proves the lineage is healthy, so clear the same
        # breaker fields a successful cc refresh clears.
        if row.get("cc_refresh_failure_type") or row.get("cc_refresh_last_failed_at"):
            updates["cc_refresh_failure_type"] = None
            updates["cc_refresh_last_failed_at"] = None
    if (
        "cc_access_token" in updates
        and row.get("cc_refresh_failure_type") == "auth_rejected"
    ):
        # A newer access token replaces the one the API refused.
        updates["cc_refresh_failure_type"] = None
        updates["cc_refresh_last_failed_at"] = None
    # Claude Code's pass-through fields (#174i). An adoption carries them,
    # bound to the adopted token; a dead chain that adopts nothing takes
    # none, because they describe the lineage jacked does not take.
    if adoption is None and not cc_chain_dead(row):
        from jacked.credentials.oauth_extra import capture_oauth_extra

        row_refresh = updates.get("cc_refresh_token", row.get("cc_refresh_token"))
        extra = capture_oauth_extra(oauth, row_refresh)
        if extra != row.get("cc_oauth_extra"):
            updates["cc_oauth_extra"] = extra
    return updates


def _profile_identity(profile: dict) -> tuple[str | None, Any]:
    """Return the email and the organization uuid that a profile names.

    >>> _profile_identity({"account": {"email": "e"}, "organization": {"uuid": "o"}})
    ('e', 'o')
    >>> _profile_identity({"account": "bad", "organization": None})
    (None, None)
    """
    account = profile.get("account") or {}
    organization = profile.get("organization") or {}
    email = account.get("email") if isinstance(account, dict) else None
    org = organization.get("uuid") if isinstance(organization, dict) else None
    return (email if isinstance(email, str) and email else None), org


def _desired_account_id(db) -> int | None:
    for key in ("desired_account_id", "active_account_id"):
        raw = db.get_setting(key)
        if raw is None or raw == "":
            continue
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            continue
        if account_id > 0:
            return account_id
    return None


def _repair_committed(outcome: Any) -> bool:
    """True when a repair outcome leaves the authority holding its target.

    >>> from jacked.credentials.models import SwitchOutcome
    >>> _repair_committed(SwitchOutcome.OBSERVED_TARGET_UNFENCED)
    True
    >>> _repair_committed(SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS)
    False
    >>> _repair_committed(None)
    False
    """
    from jacked.credentials.models import SwitchOutcome

    return isinstance(outcome, SwitchOutcome) and outcome in {
        SwitchOutcome.COMMITTED,
        SwitchOutcome.COMMITTED_DEGRADED,
        SwitchOutcome.OBSERVED_TARGET_UNFENCED,
    }


def _outcome_label(outcome: Any) -> str:
    """Name a repair outcome for a log or a reason. The name holds no value.

    >>> from jacked.credentials.models import SwitchOutcome
    >>> _outcome_label(SwitchOutcome.FAILED_PRESERVED)
    'failed_preserved'
    >>> _outcome_label(None)
    'none'
    """
    from jacked.credentials.models import SwitchOutcome

    if isinstance(outcome, SwitchOutcome):
        return outcome.value
    return "none" if outcome is None else "unrecognized"


def _attempt_repair(
    db,
    row: dict,
    activate: Callable[..., Any] | None,
    kind: str,
    *,
    republish: dict | None = None,
    expected_digest: str | None = None,
) -> SwitchOutcome | None:
    """Run one repair transaction; return its outcome.

    A reassert publishes ``row``. A re-stamp (``republish``) publishes the
    authority's own payload with the row's stamp, and names the digest the
    authority must still hold (``expected_digest``). Returns None when the
    activator raised: the observer pass never breaks over a repair, and the
    settle rule counts None as a failed attempt.
    """
    from jacked.credentials.models import SwitchContext
    from jacked.credentials.runtime import activate_account

    activator = activate or activate_account
    operation_id = f"{kind}-{uuid.uuid4().hex}"
    try:
        if republish is None:
            result = activator(db, row, SwitchContext.REASSERT, operation_id)
        else:
            result = activator(
                db, row, SwitchContext.REASSERT, operation_id,
                republish=republish, expected_digest=expected_digest,
            )
    except Exception:  # noqa: BLE001 - never break the observer pass
        logger.debug(
            "The authority %s of account %d failed", kind, int(row["id"]),
            exc_info=True,
        )
        return None
    outcome = getattr(result, "outcome", None)
    if _repair_committed(outcome):
        from jacked.api.stamped_evidence import record_publication

        # The next pass then knows this payload as jacked's own write. The
        # engine reports the fingerprint of the pair it published; an
        # activator that reports none (a test double) gets the pair this
        # repair asked it to publish.
        record_publication(
            db,
            getattr(result, "published_fingerprint", "")
            or _requested_fingerprint(row, republish),
        )
    return outcome


def _requested_fingerprint(row: dict, republish: dict | None) -> str:
    """The fingerprint of the pair a repair asked the engine to publish."""
    from jacked.api.credential_helpers import build_oauth_data
    from jacked.credentials.fingerprint import payload_fingerprint

    oauth = republish.get("claudeAiOauth") if republish is not None else build_oauth_data(row)
    return payload_fingerprint(oauth)


def _restamp_blocker(row: dict | None, payload: dict | None) -> str:
    """Say why a re-stamp of ``row`` cannot run, or return an empty string.

    With the authority's ``payload`` in hand, a re-stamp republishes exactly
    that payload with the row's stamp: it adds the stamp and the config
    identity, nothing else, so nothing blocks it but a missing row. Without
    a payload, the re-stamp publishes the row's own tokens, which needs a
    Claude Code access token in the row. The reason names fields only.

    >>> _restamp_blocker({"cc_access_token": None}, None)
    'holds no Claude Code token to re-stamp with'
    >>> _restamp_blocker({"cc_access_token": None}, {"claudeAiOauth": {"accessToken": "a"}})
    ''
    >>> _restamp_blocker(None, {"claudeAiOauth": {"accessToken": "a"}})
    'row is missing'
    """
    if not row:
        return "row is missing"
    if payload is not None:
        return ""
    if not row.get("cc_access_token"):
        return "holds no Claude Code token to re-stamp with"
    return ""


def _restamp_foreign_holder(
    db, row: dict, activate: Callable[..., Any] | None, payload: dict | None = None
) -> SwitchOutcome | None:
    """Re-stamp the authority for the account that holds it.

    With the authority's ``payload`` the write republishes exactly that
    payload with the row's stamp (the engine refuses when the authority
    changed meanwhile, and the guard adopts the new payload on its next
    pass). Without a payload it publishes the row's own tokens, which are
    the ones Claude Code wrote. Either way running sessions are unaffected:
    the write only adds the stamp and the config identity. Returns the
    engine's outcome, or None when the re-stamp cannot run or the activator
    raised.
    """
    fresh = db.get_account(int(row["id"])) or row
    if _restamp_blocker(fresh, payload):
        return None
    if payload is None:
        return _attempt_repair(db, fresh, activate, "restamp")
    from jacked.credentials.canonical import CredentialPayload

    return _attempt_repair(
        db, fresh, activate, "restamp",
        republish=payload,
        expected_digest=CredentialPayload.from_mapping(payload).digest,
    )


def _claim_repair_slot(digest: str, account_id: int, now: float) -> bool:
    """Take the repair slot for this pass, or record the repair as owed.

    The reassert of the desired account and the re-stamp of the holder share
    one floor of REASSERT_MIN_INTERVAL_SECONDS. The payload is recorded as
    owed, not settled: only the outcome of an attempt settles it. The slot
    is stamped BEFORE the attempt, so a failed attempt never shortens the
    floor. While the floor holds, the identification stays cached, so the
    next pass retries with no profile call. Returns True when the caller
    may attempt the repair now.
    """
    global _last_reassert_at
    with _state_lock:
        _store_handled_locked(digest, account_id, settled=False)
        elapsed = now - _last_reassert_at
        if _last_reassert_at and elapsed < REASSERT_MIN_INTERVAL_SECONDS:
            return False
        _last_reassert_at = now
        return True


def _claim_refusal_warning(digest: str) -> bool:
    """True once per payload, so its refusal WARNING is logged one time."""
    with _state_lock:
        if _handled["digest"] != digest or _handled["refusal_logged"]:
            return False
        _handled["refusal_logged"] = True
        return True


def _transient_outcomes() -> frozenset:
    from jacked.credentials.models import SwitchOutcome

    return frozenset({
        SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS,
        SwitchOutcome.BUSY,
        SwitchOutcome.CONCURRENT_WRITE,
        SwitchOutcome.INDETERMINATE,
    })


_TRANSIENT_OUTCOMES = _transient_outcomes()


def _settle_after(
    digest: str, account_id: int, outcome: Any, *, row_id: int, repair: str
) -> bool:
    """Record the outcome of one repair attempt. Return True when settled.

    This is the one settle rule for both repairs. A committed outcome
    settles the payload. A transient outcome (a held switch lease, a busy
    outgoing row, a torn or unreadable authority) keeps the payload owed,
    and the repair floor limits the retries to one a minute; a run of
    TRANSIENT_WARN_AFTER of them in a row is reported once. Any other
    outcome, or None, counts toward RESTAMP_MAX_ATTEMPTS. The last of those
    settles the payload with one WARNING. The WARNINGs name the row id and
    the outcome, never a token or an email.
    """
    with _state_lock:
        _store_handled_locked(digest, account_id, settled=False)
        if _repair_committed(outcome):
            _handled["settled"] = True
            return True
        if outcome in _TRANSIENT_OUTCOMES:
            # The engine says "not now": a held lease, a busy outgoing row,
            # an unreadable or torn authority. The floor bounds the retry.
            # A long run of them is reported once, so a durable "not now"
            # (an unusable recovery key) is visible without capping the run.
            _handled["transient"] += 1
            if _handled["transient"] != TRANSIENT_WARN_AFTER:
                return False
            transient = _handled["transient"]
        else:
            transient = 0
            _handled["transient"] = 0
            _handled["attempts"] += 1
            attempts = _handled["attempts"]
            if attempts < RESTAMP_MAX_ATTEMPTS:
                return False
            _handled["settled"] = True
    if transient:
        logger.warning(
            "Account %d: the authority %s has answered \"not now\" %d times in a "
            "row (last outcome: %s); jacked keeps retrying it once a minute",
            row_id,
            repair,
            transient,
            _outcome_label(outcome),
        )
        return False
    logger.warning(
        "Account %d: the authority %s did not commit after %d attempts (last "
        "outcome: %s); jacked stops retrying it until the authority payload "
        "changes",
        row_id,
        repair,
        attempts,
        _outcome_label(outcome),
    )
    return True


def _desired_row_refusal(row: dict | None) -> str:
    """Say why a desired account cannot be reasserted, or an empty string."""
    if row is None:
        return "desired account row is missing"
    if row.get("is_deleted"):
        return "desired account is deleted"
    if not row.get("is_active"):
        return "desired account is disabled"
    if row.get("validation_status") == "invalid":
        return "desired account is invalid"
    if not row.get("cc_access_token") or not row.get("cc_refresh_token"):
        return "desired account has no Claude Code tokens"
    return ""


def heal_foreign_authority(
    db,
    *,
    now: float | None = None,
    profile_lookup: Callable[[str], dict | None] | None = None,
    activate: Callable[..., Any] | None = None,
    read_authority: Callable[[], dict | None] | None = None,
) -> HealResult:
    """Adopt a foreign authority write and reassert the desired account.

    This function never raises. Every failure is logged and reported as a
    skipped heal with a reason.
    """
    try:
        return _heal(
            db,
            now=time.monotonic() if now is None else now,
            profile_lookup=profile_lookup or _default_profile_lookup,
            activate=activate,
            read_authority=read_authority or _default_read_authority,
        )
    except Exception as exc:  # never break the observer pass
        logger.debug("Authority heal failed", exc_info=True)
        return HealResult(ACTION_SKIPPED, reason=f"heal failed: {exc}")


def _heal(
    db,
    *,
    now: float,
    profile_lookup: Callable[[str], dict | None],
    activate: Callable[..., Any] | None,
    read_authority: Callable[[], dict | None],
) -> HealResult:
    if db is None:
        return HealResult(ACTION_SKIPPED, reason="no database")

    payload = read_authority()
    if not isinstance(payload, dict) or not payload:
        return HealResult(ACTION_SKIPPED, reason="authority payload is unreadable")

    stamp = _stamped_account_id(payload)
    if stamp is not None:
        # The stamp survives Claude Code's own rotations, so adopt any
        # rotated tokens before anything overwrites the authority. A stamp
        # is not the user's choice: when the tokens belong to another
        # account, the stamped path follows ``oauthAccount`` (#154i).
        from jacked.api.stamped_rotation import adopt_stamped_rotation

        return adopt_stamped_rotation(
            db, payload, stamp, profile_lookup, now=now, activate=activate
        )

    digest = _payload_digest(payload)
    with _state_lock:
        repeat = digest == _handled["digest"]
        if repeat and _handled["settled"]:
            return HealResult(ACTION_SKIPPED, reason="foreign payload already handled")
        known_account_id = _handled["account_id"] if repeat else None

    if known_account_id is None:
        access_token = _oauth_section(payload).get("accessToken")
        if not isinstance(access_token, str) or not access_token:
            _mark_handled(digest, None, settled=True)
            return HealResult(
                ACTION_SKIPPED, reason="foreign payload has no access token"
            )

        # Identify the payload before anything else writes: a failure here is
        # settled, so the observer cannot hit the profile endpoint every pass.
        profile = profile_lookup(access_token)
        if not isinstance(profile, dict) or not profile:
            _mark_handled(digest, None, settled=True)
            return HealResult(ACTION_SKIPPED, reason="profile lookup failed")
        email, organization_uuid = _profile_identity(profile)
        if email is None:
            _mark_handled(digest, None, settled=True)
            return HealResult(ACTION_SKIPPED, reason="profile carries no email")

        row = _match_account(db, email, organization_uuid)
        if row is None:
            _mark_handled(digest, None, settled=True)
            # No row matched, so there is no row id to name. The log names the
            # organization uuid prefix and the payload digest prefix only:
            # an email is personal data and never goes to the log.
            logger.warning(
                "A foreign Claude Code write holds the credential authority for an "
                "account that is not a known jacked account (organization %s, "
                "payload %s). jacked will not overwrite it.",
                _uuid_prefix(organization_uuid),
                digest[:8],
            )
            return HealResult(ACTION_UNKNOWN_ACCOUNT, reason="unknown account")
    else:
        row = db.get_account(known_account_id)
        if row is None:
            _mark_handled(digest, None, settled=True)
            return HealResult(
                ACTION_SKIPPED, reason="identified account row disappeared"
            )

    foreign_account_id = int(row["id"])
    adopted = _import_rotated_tokens(
        db, row, payload, profile=profile if known_account_id is None else None
    )
    if adopted == ADOPTION_RACE_LOST:
        # Do not settle, and do not reassert over the authority: it still holds
        # the only copy of the rotated refresh token. The next pass retries.
        _mark_handled(digest, foreign_account_id, settled=False)
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            reason="the row kept changing; adoption retried next pass",
        )
    if adopted == ADOPTION_DEFERRED:
        # Not settled, and the account stays identified: the next pass
        # retries the lookup with no second profile call for the identity.
        _mark_handled(digest, foreign_account_id, settled=False)
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            reason="ownership lookup failed; adoption retried next pass",
        )
    if adopted == ADOPTION_REFUSED:
        # The email matched a row, but the ownership rule refused the tokens,
        # so jacked holds no copy of this lineage. A reassert, or a re-stamp
        # (which publishes the row's own stored tokens), would overwrite the
        # only copy outside Claude Code's memory. Treat it as an unknown
        # account: settle it and never overwrite it.
        _mark_handled(digest, None, settled=True)
        logger.info(
            "A foreign Claude Code write holds tokens that are not proven to "
            "belong to account %d (payload %s); jacked will not overwrite it",
            foreign_account_id,
            digest[:8],
        )
        return HealResult(
            ACTION_UNKNOWN_ACCOUNT,
            reason="the tokens are not proven to belong to the matched account",
        )

    desired_id = _desired_account_id(db)
    if desired_id is None:
        _mark_handled(digest, foreign_account_id, settled=True)
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            reason="no desired account is set",
        )
    desired_row = db.get_account(desired_id)
    refusal = _desired_row_refusal(desired_row)
    blocker = (
        _restamp_blocker(db.get_account(foreign_account_id), payload) if refusal else ""
    )
    if blocker:
        # Nothing safe to publish: the holder's row has no Claude Code access
        # token, or its publication would replace a token that Claude Code
        # just wrote. Settle it as such, without spending the shared floor or
        # an attempt on a repair that never ran.
        _mark_handled(digest, foreign_account_id, settled=True)
        logger.warning(
            "Desired account %d cannot be reasserted (%s) and account %d %s; the "
            "authority stays unstamped",
            desired_id,
            refusal,
            foreign_account_id,
            blocker,
        )
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason=refusal,
        )
    # Both repairs below share the floor and the settle rule: the payload
    # stays owed until the outcome of an attempt settles it (#157i).
    if not _claim_repair_slot(digest, foreign_account_id, now):
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason="reassert rate limit is active",
        )
    if refusal:
        # The chosen account cannot be reasserted (typically its refresh
        # lineage is already dead and it needs a re-auth). Do not leave the
        # authority unstamped: re-stamp it for the account that actually
        # holds it so jacked's observed identity is usable and the user sees
        # the truth instead of "runtime unknown".
        if _claim_refusal_warning(digest):
            logger.warning(
                "Desired account %d cannot be reasserted (%s); keeping account "
                "%d, which now holds the authority",
                desired_id,
                refusal,
                foreign_account_id,
            )
        outcome = _restamp_foreign_holder(db, row, activate, payload)
        _settle_after(
            digest,
            foreign_account_id,
            outcome,
            row_id=foreign_account_id,
            repair="re-stamp",
        )
        return HealResult(
            ACTION_ADOPTED if _repair_committed(outcome) else ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason=refusal,
        )

    from jacked.credentials.models import SwitchOutcome

    outcome = _attempt_repair(db, desired_row, activate, "reassert")
    _settle_after(
        digest, foreign_account_id, outcome, row_id=desired_id, repair="reassert"
    )
    if outcome is SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS:
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason="another credential operation holds the switch lease",
        )
    if not _repair_committed(outcome):
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason=f"reassert outcome {_outcome_label(outcome)}",
        )

    if foreign_account_id == desired_id:
        # The tokens are identical, so no session changes account. The write
        # restores the jacked stamp, which makes the observed identity usable.
        logger.info(
            "Re-stamped the credential authority for account %d after a foreign "
            "write",
            desired_id,
        )
        return HealResult(
            ACTION_ADOPTED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
        )
    logger.info(
        "Reasserted desired account %d over a foreign authority write from "
        "account %d",
        desired_id,
        foreign_account_id,
    )
    return HealResult(
        ACTION_REASSERTED,
        foreign_account_id=foreign_account_id,
        desired_account_id=desired_id,
    )
