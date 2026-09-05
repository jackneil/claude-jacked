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
3. reasserts the desired default account through the transaction engine.

An account jacked cannot identify is never overwritten: reasserting over it
would destroy a refresh lineage jacked does not hold.
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
from typing import Any, Callable

logger = logging.getLogger(__name__)

# One reassert per minute at most. The observer passes every 10 seconds, and a
# reassert is a full credential transaction against the live authority.
REASSERT_MIN_INTERVAL_SECONDS = 60.0
PROFILE_TIMEOUT_SECONDS = 10.0

ACTION_NONE = "none"
ACTION_ADOPTED = "adopted"
ACTION_REASSERTED = "reasserted"
ACTION_UNKNOWN_ACCOUNT = "unknown_account"
ACTION_SKIPPED = "skipped"


@dataclass(frozen=True)
class HealResult:
    """Outcome of one authority heal attempt. Never carries a secret."""

    action: str
    foreign_account_id: int | None = None
    desired_account_id: int | None = None
    reason: str = ""


_state_lock = threading.Lock()
# The payload jacked handled last, so the ten-second observer pass does not
# call the profile endpoint again for a payload it already identified.
# ``settled`` is False while a repair is still owed, for example when the
# reassert rate limit or the switch lease held it off. The identified account
# is kept, so the retry costs no network call.
_handled: dict[str, Any] = {"digest": None, "account_id": None, "settled": True}
_last_reassert_at: float = 0.0


def reset_authority_guard_state() -> None:
    """Forget the handled payload digest and the reassert rate limit."""
    global _last_reassert_at
    with _state_lock:
        _handled.update({"digest": None, "account_id": None, "settled": True})
        _last_reassert_at = 0.0


def _mark_handled(digest: str, account_id: int | None, *, settled: bool) -> None:
    with _state_lock:
        _handled.update(
            {"digest": digest, "account_id": account_id, "settled": settled}
        )


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


def _default_profile_lookup(access_token: str) -> dict | None:
    """Identify one access token through the OAuth profile endpoint."""
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
        logger.debug("Foreign authority profile lookup failed", exc_info=True)
        return None
    if response.status_code != 200:
        logger.debug(
            "Foreign authority profile lookup returned HTTP %d", response.status_code
        )
        return None
    try:
        return response.json()
    except ValueError:
        return None


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


def _expiry_seconds(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if raw <= 0:
        return None
    return int(raw / 1000) if raw > 1e12 else int(raw)


def _import_rotated_tokens(db, row: dict, payload: dict) -> int:
    """Adopt the rotated Claude Code tokens into one account row."""
    account_id = int(row["id"])
    oauth = _oauth_section(payload)
    updates: dict[str, Any] = {}

    access_token = oauth.get("accessToken")
    if access_token and access_token != row.get("cc_access_token"):
        updates["cc_access_token"] = access_token

    expires_at = _expiry_seconds(oauth.get("expiresAt"))
    if expires_at is not None and expires_at != row.get("cc_expires_at"):
        updates["cc_expires_at"] = expires_at

    refresh_token = oauth.get("refreshToken")
    if refresh_token:
        # The invalid_grant breaker means jacked and Claude Code are already
        # competing for one single-use refresh token. Importing it here would
        # let jacked exchange the token Claude Code still needs.
        if row.get("refresh_failure_type") == "invalid_grant":
            logger.debug(
                "Account %d: skipping cc_refresh_token import (invalid_grant active)",
                account_id,
            )
        elif refresh_token != row.get("cc_refresh_token"):
            updates["cc_refresh_token"] = refresh_token
            # A live rotation proves the lineage is healthy, so clear the same
            # breaker fields a successful refresh clears.
            if row.get("refresh_failure_type") or row.get("refresh_last_failed_at"):
                updates["refresh_failure_type"] = None
                updates["refresh_last_failed_at"] = None

    if updates:
        db.update_account(account_id, **updates)
        logger.info(
            "Adopted rotated Claude Code tokens for account %d from a foreign "
            "authority write",
            account_id,
        )
    return len(updates)


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
    global _last_reassert_at

    if db is None:
        return HealResult(ACTION_SKIPPED, reason="no database")

    payload = read_authority()
    if not isinstance(payload, dict) or not payload:
        return HealResult(ACTION_SKIPPED, reason="authority payload is unreadable")

    stamp = _stamped_account_id(payload)
    if stamp is not None:
        # A stamped payload is a jacked write. jacked itself may have chosen
        # another account (a scoped launch, for example), and the credential
        # repository already tracks that choice.
        return HealResult(ACTION_NONE, foreign_account_id=stamp)

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
        account_section = profile.get("account") or {}
        organization_section = profile.get("organization") or {}
        email = (
            account_section.get("email") if isinstance(account_section, dict) else None
        )
        organization_uuid = (
            organization_section.get("uuid")
            if isinstance(organization_section, dict)
            else None
        )
        if not isinstance(email, str) or not email:
            _mark_handled(digest, None, settled=True)
            return HealResult(ACTION_SKIPPED, reason="profile carries no email")

        row = _match_account(db, email, organization_uuid)
        if row is None:
            _mark_handled(digest, None, settled=True)
            logger.warning(
                "A foreign Claude Code write holds the credential authority for %s, "
                "which is not a known jacked account. jacked will not overwrite it.",
                email,
            )
            return HealResult(ACTION_UNKNOWN_ACCOUNT, reason=f"unknown account {email}")
    else:
        row = db.get_account(known_account_id)
        if row is None:
            _mark_handled(digest, None, settled=True)
            return HealResult(
                ACTION_SKIPPED, reason="identified account row disappeared"
            )

    foreign_account_id = int(row["id"])
    _import_rotated_tokens(db, row, payload)

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
    if refusal:
        _mark_handled(digest, foreign_account_id, settled=True)
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason=refusal,
        )

    with _state_lock:
        elapsed = now - _last_reassert_at
        if _last_reassert_at and elapsed < REASSERT_MIN_INTERVAL_SECONDS:
            # A repair is still owed. Keep the identification so the next pass
            # can retry without another profile call.
            _handled.update(
                {"digest": digest, "account_id": foreign_account_id, "settled": False}
            )
            return HealResult(
                ACTION_SKIPPED,
                foreign_account_id=foreign_account_id,
                desired_account_id=desired_id,
                reason="reassert rate limit is active",
            )
        _last_reassert_at = now

    from jacked.credentials.models import SwitchContext, SwitchOutcome
    from jacked.credentials.runtime import activate_account

    activator = activate or activate_account
    result = activator(
        db, desired_row, SwitchContext.REASSERT, f"reassert-{uuid.uuid4().hex}"
    )
    outcome = getattr(result, "outcome", None)
    if outcome is SwitchOutcome.INTERACTIVE_OPERATION_IN_PROGRESS:
        _mark_handled(digest, foreign_account_id, settled=False)
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason="another credential operation holds the switch lease",
        )
    if outcome not in {
        SwitchOutcome.COMMITTED,
        SwitchOutcome.COMMITTED_DEGRADED,
        SwitchOutcome.OBSERVED_TARGET_UNFENCED,
    }:
        _mark_handled(digest, foreign_account_id, settled=False)
        return HealResult(
            ACTION_SKIPPED,
            foreign_account_id=foreign_account_id,
            desired_account_id=desired_id,
            reason=f"reassert outcome {getattr(outcome, 'value', outcome)}",
        )

    _mark_handled(digest, foreign_account_id, settled=True)
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
