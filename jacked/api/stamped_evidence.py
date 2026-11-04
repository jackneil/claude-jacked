"""The evidence that classifies a stamped rotation (#154i).

The stamped path of the authority guard (``jacked.api.stamped_rotation``)
uses two kinds of evidence. This module holds both:

- The ``oauthAccount`` block of ``~/.claude.json``. Claude Code 2.1.316
  writes the identity keys of the block at ``/login``, before it saves the
  new tokens. A token refresh merges only display fields into the block.
  Thus the block names the account that the user chose, and a stale session
  that refreshes another account does not change it. The block never proves
  who owns a token: the profile endpoint and the shared ownership rule do
  that.
- A fingerprint of the tokens that the last committed repair of the guard
  published (the ``_last_authority_write_fp`` setting). A payload with these
  tokens is jacked's own write, so the guard does not examine it again. The
  leading underscore makes it an internal marker that ``list_settings`` and
  the settings API do not show: it is derived from token values.

Logs contain row ids and field names only. They never contain a token or an
email address.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Mapping

from jacked.api.claude_identity import read_claude_config_identity

logger = logging.getLogger(__name__)

OWNER = "owner"
STAMPED = "stamped"
AMBIGUOUS = "ambiguous"
LAST_WRITE_FP_SETTING = "_last_authority_write_fp"


# --- the write fingerprint ---------------------------------------------------


def token_fingerprint(oauth: Any) -> str:
    """Return a 16-character fingerprint of a refresh and an access token.

    The fingerprint is the start of a sha256 digest. It holds no token value.

    >>> one = token_fingerprint({"accessToken": "a", "refreshToken": "r"})
    >>> len(one), one == token_fingerprint({"refreshToken": "r", "accessToken": "a"})
    (16, True)
    >>> one == token_fingerprint({"accessToken": "r", "refreshToken": "a"})
    False
    """
    from jacked.credentials.fingerprint import payload_fingerprint

    return payload_fingerprint(oauth)


def last_published_fingerprint(db) -> str | None:
    """Return the fingerprint of the last committed repair, or None (unknown)."""
    try:
        value = db.get_setting(LAST_WRITE_FP_SETTING)
    except Exception as exc:  # noqa: BLE001 - an unknown value never blocks a pass
        logger.debug(
            "The setting %s is not readable (%s)",
            LAST_WRITE_FP_SETTING,
            type(exc).__name__,
        )
        return None
    return value if isinstance(value, str) and value else None


def is_own_publication(db, payload: Mapping) -> bool:
    """True when ``payload`` holds the tokens of the last committed repair."""
    known = last_published_fingerprint(db)
    return known is not None and known == token_fingerprint(payload.get("claudeAiOauth"))


def record_publication(db, fingerprint: str) -> None:
    """Keep the fingerprint of the pair that a committed repair published.

    The engine reports the fingerprint of the exact payload it wrote
    (``SwitchResult.published_fingerprint``); a row read after the commit
    could already hold a newer pair. A failure is logged and ignored:
    without the value, the next pass only examines the payload again.
    """
    if not fingerprint:
        return
    try:
        db.set_setting(LAST_WRITE_FP_SETTING, fingerprint)
    except Exception as exc:  # noqa: BLE001 - the fingerprint only saves work
        logger.debug(
            "The fingerprint of the published tokens (%s) was not kept (%s)",
            fingerprint[:4],
            type(exc).__name__,
        )


# --- which account the Claude config identity names --------------------------


def _text(value: Any) -> str | None:
    """Return a stripped string that is not empty, else None.

    >>> _text("  u1 "), _text(""), _text(None)
    ('u1', None, None)
    """
    text = value.strip() if isinstance(value, str) else ""
    return text or None


def config_names_row(config: Any, row: Mapping | None) -> bool:
    """True when the ``oauthAccount`` block ``config`` names the account ``row``.

    When the row knows its account uuid, the uuid decides. Otherwise the
    email decides, and upper and lower case are the same. When both sides
    know the organization uuid, it must also be the same: one person can
    have rows in more than one organization, and these rows share the
    account uuid.

    >>> row = {"email": "a@b.c", "organization_uuid": "o1", "account_uuid": "u1"}
    >>> config_names_row({"accountUuid": "u1", "organizationUuid": "o1"}, row)
    True
    >>> config_names_row({"accountUuid": "u1", "organizationUuid": "o2"}, row)
    False
    >>> config_names_row({"accountUuid": "u2", "emailAddress": "a@b.c"}, row)
    False
    >>> config_names_row({"emailAddress": "A@B.C"}, {"email": "a@b.c"})
    True
    """
    if not isinstance(config, Mapping) or not row:
        return False
    row_org = _text(row.get("organization_uuid"))
    config_org = _text(config.get("organizationUuid"))
    if row_org and config_org and row_org != config_org:
        return False
    row_uuid = _text(row.get("account_uuid"))
    if row_uuid:
        return _text(config.get("accountUuid")) == row_uuid
    email = _text(row.get("email"))
    named = _text(config.get("emailAddress"))
    return bool(email and named and email.casefold() == named.casefold())


def classify(config: Any, owner: Mapping, stamped: Mapping | None) -> tuple[str, str]:
    """Return the account that ``oauthAccount`` names, and a reason for a log.

    ``owner`` is the row that owns the tokens. ``stamped`` is the row that the
    stamp names. The verdict is ``OWNER``, ``STAMPED`` or ``AMBIGUOUS``. The
    reason contains no value.

    >>> owner, stamped = {"account_uuid": "ub"}, {"account_uuid": "ua"}
    >>> classify({"accountUuid": "ub"}, owner, stamped)[0]
    'owner'
    >>> classify({"accountUuid": "ua"}, owner, stamped)[0]
    'stamped'
    >>> classify(None, owner, stamped)
    ('ambiguous', 'the Claude config identity is missing or unreadable')
    """
    if not isinstance(config, Mapping):
        return AMBIGUOUS, "the Claude config identity is missing or unreadable"
    names_owner = config_names_row(config, owner)
    names_stamped = config_names_row(config, stamped)
    if names_owner and names_stamped:
        return AMBIGUOUS, "the Claude config identity names both accounts"
    if names_owner:
        return OWNER, "the Claude config identity names the owner of the tokens"
    if names_stamped:
        return STAMPED, "the Claude config identity names the stamped account"
    return AMBIGUOUS, "the Claude config identity names neither account"


def read_config_identity() -> dict | None:
    """Return the ``oauthAccount`` block of ``~/.claude.json``, or None."""
    try:
        return read_claude_config_identity(Path.home())
    except Exception as exc:  # noqa: BLE001 - an unreadable block cannot decide
        logger.debug(
            "The Claude config identity is not readable (%s)", type(exc).__name__
        )
        return None
