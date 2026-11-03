"""The revoked refresh token verdict (#152i).

An ``invalid_grant`` from the token endpoint means that one refresh token is
dead. jacked binds that verdict to the token it was about: the account row
stores the fingerprint of the failed token beside the chain's breaker
(``refresh_failed_token_fp`` for the primary chain,
``cc_refresh_failed_token_fp`` for the Claude Code chain). A verdict that is
not bound to its token decays into "retry later" on a timer, and every retry
sends a dead token to the token endpoint again.

The verdict is the fingerprint alone. The breaker columns beside it
(``refresh_failure_type``, ``cc_refresh_failure_type``) do not take part, so
a writer that clears a breaker cannot release a condemned token.

- While the row holds a primary refresh token with the recorded fingerprint,
  no path sends that token (``primary_refresh_revoked``). Only a new stored
  refresh token releases the verdict: a database trigger clears the
  fingerprint.
- The Claude Code clean-up removes the failed token from the row, so the
  fingerprints are the only record of which tokens died. The column keeps
  the fingerprint of each condemned token, newest first, separated by
  spaces (``condemned_after``). No importer of live tokens adopts a token
  with one of these fingerprints, and no path sends one
  (``cc_refresh_condemned``).

The cross-account verdict (#154i) is bound the same way: a profile 200 that
names another account stores the fingerprint of the row's primary refresh
token in ``foreign_token_fp``. While the row holds that token, no path sends
it and no usage request sends the access token beside it
(``primary_tokens_foreign``). No error text or status releases it; a new
stored refresh token (a database trigger) or a validation that proves the
row's own account does.

This module has no jacked imports, so every importer of tokens can use it
without an import cycle. It never logs and never returns a token value.
"""
from __future__ import annotations

import hashlib
from typing import Any, Optional

# The terminal verdict for a revoked primary refresh token. The dashboard
# shows it as ``last_error`` beside its sign-in action.
SIGN_IN_REQUIRED_ERROR = "Sign-in required: the refresh token was revoked"

# The validation verdict for a row whose access token belongs to another
# account (#154i). The dashboard shows it as ``last_error``.
FOREIGN_TOKEN_ERROR = "Token belongs to a different account"


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
    """True when an invalid_grant condemned the row's primary refresh token.

    The verdict is the fingerprint binding alone: ``refresh_failed_token_fp``
    must equal the fingerprint of the stored refresh token. The breaker
    columns do not take part, so a cleared breaker does not release the
    token.

    >>> row = {"refresh_token": "rt", "refresh_failure_type": "invalid_grant",
    ...        "refresh_failed_token_fp": token_fingerprint("rt")}
    >>> primary_refresh_revoked(row)
    True
    >>> primary_refresh_revoked({**row, "refresh_failure_type": None})
    True
    >>> primary_refresh_revoked({**row, "refresh_token": "rt-new"})
    False
    >>> primary_refresh_revoked({**row, "refresh_failed_token_fp": None})
    False
    >>> primary_refresh_revoked({"refresh_token": None, "refresh_failed_token_fp": None})
    False
    """
    fp = row.get("refresh_failed_token_fp")
    return fp is not None and fp == token_fingerprint(row.get("refresh_token"))


def primary_tokens_foreign(row: dict) -> bool:
    """True while a cross-account verdict covers the row's primary tokens.

    A profile 200 that names another account marks the row invalid with
    ``FOREIGN_TOKEN_ERROR`` and binds the verdict to the stored primary
    refresh token: ``foreign_token_fp`` holds its fingerprint (#154i). While
    the binding holds, no path exchanges that refresh token (the exchange
    spends a single-use token of the other account under this row), and no
    usage request sends the access token beside it. The verdict is the
    binding alone, like ``primary_refresh_revoked``: the error text and the
    status do not take part.

    A mark that predates the binding column (the text with no fingerprint)
    is bound once when the schema opens (``Database._bind_legacy_foreign_marks``),
    so the predicate never has to read the text.

    >>> row = {"refresh_token": "rt", "foreign_token_fp": token_fingerprint("rt")}
    >>> primary_tokens_foreign(row)
    True
    >>> primary_tokens_foreign({**row, "last_error": None, "validation_status": "valid"})
    True
    >>> primary_tokens_foreign({**row, "refresh_token": "rt-new"})
    False
    >>> primary_tokens_foreign({"last_error": FOREIGN_TOKEN_ERROR}), primary_tokens_foreign({})
    (False, False)
    """
    fp = row.get("foreign_token_fp")
    return fp is not None and fp == token_fingerprint(row.get("refresh_token"))


def standing_verdict_or(row: dict, text: str) -> str:
    """Return the row's standing terminal verdict, else ``text``.

    Every path that writes an error over a failed refresh or validation uses
    this, so the dashboard keeps the terminal verdict instead of the last
    generic failure: the sign-in text while the primary refresh token is
    revoked, and the foreign-token text while the cross-account binding
    holds. The bindings are the verdicts; this keeps only their text.

    >>> standing_verdict_or({}, "Token invalid (HTTP 401)")
    'Token invalid (HTTP 401)'
    >>> foreign = {"refresh_token": "rt", "foreign_token_fp": token_fingerprint("rt")}
    >>> standing_verdict_or(foreign, "Token invalid (HTTP 401)") == FOREIGN_TOKEN_ERROR
    True
    """
    if primary_refresh_revoked(row):
        return SIGN_IN_REQUIRED_ERROR
    if primary_tokens_foreign(row):
        return FOREIGN_TOKEN_ERROR
    return text


def condemned_fingerprints(value: Any) -> tuple[str, ...]:
    """Return the fingerprints in a ``cc_refresh_failed_token_fp`` value.

    The newest fingerprint comes first. A value from before the list form
    holds one fingerprint.

    >>> condemned_fingerprints("aa bb"), condemned_fingerprints("aa")
    (('aa', 'bb'), ('aa',))
    >>> condemned_fingerprints(None), condemned_fingerprints(""), condemned_fingerprints(5)
    ((), (), ())
    """
    if not isinstance(value, str):
        return ()
    return tuple(value.split())


def condemned_after(value: Any, failed_refresh_token: Optional[str]) -> Optional[str]:
    """Return the ``cc_refresh_failed_token_fp`` value after a new condemnation.

    ``value`` is the stored value. The fingerprint of
    ``failed_refresh_token`` comes first. The earlier fingerprints stay,
    because a dead token stays dead and a stale credential store can offer
    it again. A fingerprint is never kept twice.

    >>> one = condemned_after(None, "r1")
    >>> one == token_fingerprint("r1")
    True
    >>> two = condemned_after(one, "r2")
    >>> condemned_fingerprints(two) == (token_fingerprint("r2"), token_fingerprint("r1"))
    True
    >>> condemned_fingerprints(condemned_after(two, "r1")) == (
    ...     token_fingerprint("r1"), token_fingerprint("r2"))
    True
    >>> condemned_after(None, None) is None
    True
    """
    new = token_fingerprint(failed_refresh_token)
    kept = [fp for fp in condemned_fingerprints(value) if fp != new]
    return " ".join([new, *kept] if new else kept) or None


def cc_refresh_condemned(row: dict, refresh_token: Any) -> bool:
    """True when an invalid_grant condemned the Claude Code ``refresh_token``.

    The verdict is the fingerprint alone, whatever ``cc_refresh_failure_type``
    says: a chain that adopted a new lineage still refuses the tokens that
    died before it. Use it before an import adopts a live refresh token as
    the row's cc lineage, and before an exchange sends one. The token that
    got the invalid_grant is no longer in the row, so only its fingerprint
    can refuse it.

    >>> row = {"cc_refresh_failed_token_fp": token_fingerprint("cc-dead")}
    >>> cc_refresh_condemned(row, "cc-dead"), cc_refresh_condemned(row, "cc-new")
    (True, False)
    >>> row = {"cc_refresh_failed_token_fp": condemned_after(
    ...     condemned_after(None, "cc-old"), "cc-dead")}
    >>> [cc_refresh_condemned(row, t) for t in ("cc-dead", "cc-old", "cc-new")]
    [True, True, False]
    >>> cc_refresh_condemned({}, "cc-dead"), cc_refresh_condemned(row, None)
    (False, False)
    """
    if not isinstance(refresh_token, str):
        return False
    fp = token_fingerprint(refresh_token)
    return fp is not None and fp in condemned_fingerprints(row.get("cc_refresh_failed_token_fp"))
