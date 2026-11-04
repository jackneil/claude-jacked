"""The validation verdicts that are not a profile 200 (moved from ``jacked.web.auth``).

``jacked.web.auth.validate_account`` calls these in a worker thread and
re-exports them. Each write is a compare-and-swap on the access token that
the refused request sent, so a re-auth that landed meanwhile keeps its own
status.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from jacked.web.database import Database
from jacked.web.refresh_verdict import standing_verdict_or as _standing_verdict_or

logger = logging.getLogger("jacked.auth")


def _record_auth_rejection(
    account_id: int, db: Database, status: int, token: Optional[str],
) -> dict:
    """Mark the row invalid after a 401/403 that no refresh fixed (worker thread).

    ``token`` is the access token that the refused request sent. The write
    is a compare-and-swap on it: a re-auth that stored a new token after
    the 401 keeps its own status. A standing terminal verdict keeps its
    text (``standing_verdict_or``): the sign-in text for a revoked refresh
    token (#152i), the foreign-token text (#154i). Any other failure names
    the HTTP status, as before.
    """
    row = db.get_account(account_id) or {}
    stored = _standing_verdict_or(row, f"Token invalid (HTTP {status}), refresh failed")
    shown = _standing_verdict_or(row, f"Token invalid (HTTP {status})")
    if not db.update_account_if_unchanged(
        account_id,
        {"access_token": token},
        validation_status="invalid",
        last_validated_at=int(time.time()),
        last_error=stored,
        last_error_at=datetime.now(timezone.utc).isoformat(),
    ):
        logger.info(
            "Account %d: validation verdict not recorded; the access token "
            "changed during the check",
            account_id,
        )
    return {"valid": False, "error": shown}


def _record_validation_http(
    account_id: int, db: Database, account: dict, status: int, token: Optional[str],
) -> dict:
    """Record a profile answer that is not 200, and not a renewed 401/403 (worker thread).

    Like ``_record_auth_rejection``, the write is a compare-and-swap on
    ``token``, the access token that the request sent (a re-auth that
    landed meanwhile keeps its own status), and a standing terminal verdict
    keeps its text (``standing_verdict_or``).
    """
    now = datetime.now(timezone.utc).isoformat()
    if status == 429:
        # Rate limited: keep the earlier status, only note the error.
        db.update_account_if_unchanged(
            account_id,
            {"access_token": token},
            validation_status=account.get("validation_status", "unknown"),
            last_error=_standing_verdict_or(account, "Rate limited during validation"),
            last_error_at=now,
        )
        return {"valid": False, "error": "Rate limited. Try again later."}
    db.update_account_if_unchanged(
        account_id,
        {"access_token": token},
        validation_status="unknown",
        last_error=_standing_verdict_or(account, f"Validation HTTP {status}"),
        last_error_at=now,
    )
    return {"valid": False, "error": f"Unexpected HTTP {status}"}
