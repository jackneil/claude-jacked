"""The revoked refresh token verdict (#152i).

An ``invalid_grant`` from the token endpoint means that one refresh token is
dead. jacked binds that verdict to the token it was about: the account row
stores the fingerprint of the failed token beside the chain's breaker
(``refresh_failed_token_fp`` for the primary chain,
``cc_refresh_failed_token_fp`` for the Claude Code chain). A verdict that is
not bound to its token decays into "retry later" on a timer, and every retry
sends a dead token to the token endpoint again.

- While the row holds a primary refresh token with the recorded fingerprint,
  no path sends that token (``primary_refresh_revoked``). A new stored
  refresh token clears the fingerprint (a database trigger).
- The Claude Code clean-up removes the failed token from the row, so the
  fingerprint is the only record of which token died. An importer of live
  tokens must not adopt a token with that fingerprint
  (``cc_refresh_condemned``).

This module has no jacked imports, so every importer of tokens can use it
without an import cycle. It never logs and never returns a token value.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

# The terminal verdict for a revoked primary refresh token. The dashboard
# shows it as ``last_error`` beside its sign-in action.
SIGN_IN_REQUIRED_ERROR = "Sign-in required: the refresh token was revoked"


def token_fingerprint(token: Optional[str]) -> Optional[str]:
    """Return a short, non-reversible fingerprint of a token for state keys.

    >>> token_fingerprint(None) is None
    True
    >>> len(token_fingerprint("abc"))
    16
    """
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]


def primary_refresh_revoked(row: dict) -> bool:
    """True when the row's primary refresh token got an invalid_grant.

    The verdict counts only while the row still holds the token it was
    about: ``refresh_failed_token_fp`` must equal the fingerprint of the
    stored refresh token.

    >>> row = {"refresh_token": "rt", "refresh_failure_type": "invalid_grant",
    ...        "refresh_failed_token_fp": token_fingerprint("rt")}
    >>> primary_refresh_revoked(row)
    True
    >>> primary_refresh_revoked({**row, "refresh_token": "rt-new"})
    False
    >>> primary_refresh_revoked({**row, "refresh_failed_token_fp": None})
    False
    """
    fp = row.get("refresh_failed_token_fp")
    return (
        row.get("refresh_failure_type") == "invalid_grant"
        and fp is not None
        and fp == token_fingerprint(row.get("refresh_token"))
    )


def revoked_or(row: dict, text: str) -> str:
    """Return the sign-in text when the primary refresh token is revoked.

    Every path that marks a row invalid after a failed refresh uses this,
    so the dashboard keeps the terminal verdict instead of the last generic
    HTTP failure.

    >>> revoked_or({}, "Token invalid (HTTP 401)")
    'Token invalid (HTTP 401)'
    """
    return SIGN_IN_REQUIRED_ERROR if primary_refresh_revoked(row) else text


def cc_refresh_condemned(row: dict, refresh_token: Any) -> bool:
    """True when ``refresh_token`` is a Claude Code refresh token that died.

    Use it before an import adopts a live refresh token as the row's cc
    lineage: the token that got the invalid_grant is no longer in the row,
    so only its fingerprint can refuse it.

    >>> row = {"cc_refresh_failed_token_fp": token_fingerprint("cc-dead")}
    >>> cc_refresh_condemned(row, "cc-dead"), cc_refresh_condemned(row, "cc-new")
    (True, False)
    >>> cc_refresh_condemned({}, "cc-dead"), cc_refresh_condemned(row, None)
    (False, False)
    """
    fp = row.get("cc_refresh_failed_token_fp")
    if fp is None or not isinstance(refresh_token, str):
        return False
    return fp == token_fingerprint(refresh_token)
