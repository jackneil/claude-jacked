"""Validation of an account whose provider CLI owns the login (Codex, Grok).

The stored token of such an account is a marker. The Anthropic profile API
would answer 401 and mark the row invalid, so ``jacked.web.auth`` sends these
rows here instead (moved out of that module by the module size ratchet).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from jacked.providers import CODEX, provider_of

logger = logging.getLogger("jacked.web.auth")


def validate_non_claude(account: dict, db) -> dict:
    """The validation verdict of a Codex or Grok ``account``.

    Codex: the signed-in state of the account's slot. Grok and any later
    provider: the status that the import or the last switch recorded. jacked
    has no Grok check that does not spend the rotating refresh token.
    """
    if provider_of(account) == CODEX:
        return _validate_codex_account(account["id"], db)
    return {"valid": account.get("validation_status") == "valid", "error": None}


def _validate_codex_account(account_id: int, db) -> dict:
    """Validate a Codex account by its signed-in state (its slot holds a real
    credential), NOT the Anthropic profile API — Codex tokens aren't Anthropic
    tokens, so the profile call would always 401 and falsely mark it invalid."""
    try:
        from jacked.codex.credentials import read_auth_json
        from jacked.codex.switching import codex_account_home

        auth = read_auth_json(codex_account_home(account_id))
        signed_in = bool(auth and (auth.get("tokens") or auth.get("OPENAI_API_KEY")))
    except Exception:
        logger.debug("codex validation read failed for %s", account_id, exc_info=True)
        signed_in = True  # never false-invalidate on a transient read error
    if signed_in:
        # A signed-in auth.json says nothing about whether usage is readable:
        # the usage fetch's error and failure counter stay until a successful
        # fetch clears them (fetch_codex_usage -> clear_account_errors).
        db.mark_account_validated(account_id)
        return {"valid": True, "error": None}
    db.update_account(
        account_id,
        validation_status="invalid",
        last_validated_at=int(time.time()),
        last_error="Codex account signed out. Run `codex login`.",
        last_error_at=datetime.now(timezone.utc).isoformat(),
    )
    return {"valid": False, "error": "Codex signed out"}
