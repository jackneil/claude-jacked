"""Refuse a credential write that puts a null refresh token over a live one.

A credential writer builds its payload from the account row, and the row can
be behind the live Claude Code store. After an ``invalid_grant`` the row holds
no refresh token, while a running Claude Code session can hold a working one
that it rotated itself. ``build_oauth_data`` then publishes
``refreshToken: null``. A write of that payload over the live store logs the
session out when its access token expires (#149i).

Every writer that publishes a row's payload over a live store asks
:func:`refuses_refresh_downgrade` first: the switch engine, through
``SwitchRequest.refresh_downgrade_check``, and the launch write of the
per-account credentials file.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .canonical import CredentialPayload

logger = logging.getLogger(__name__)

# Names the account that stores a refresh token (``Database.account_id_for_
# refresh_token``), or None: a refresh token is issued to one account, so the
# row that stores it names the owner of the live tokens without a request.
LineageFinder = Callable[[str], "int | None"]

# The stable machine-readable code of the refusal (``SwitchResult.code``).
CC_REAUTH_REQUIRED_CODE = "CC_REAUTH_REQUIRED"
# The refusal text for the log, the switch result and the dashboard. It names
# no token and no account.
REFRESH_DOWNGRADE_REASON = (
    "not publishing a null refresh token over a live one (cc re-auth required)"
)
# The live store holds a refresh token, the target has none, and nothing names
# the owner of the live tokens (the profile request failed, the access token
# expired, or the endpoint answered 401 or 403 with no stamp on the target): a
# transient refusal, retried by the caller. Fail closed: a refusal is
# retryable, a lost refresh lineage is not.
OWNERSHIP_UNCHECKED_CODE = "OWNERSHIP_UNCHECKED"
REFRESH_DOWNGRADE_UNCHECKED_REASON = (
    "not publishing a null refresh token over a live one whose owner could not "
    "be checked; try again later"
)


def _refresh_of(oauth: Any) -> str | None:
    """The refresh token of a ``claudeAiOauth`` object when it is a non-empty
    string, else None.

    >>> _refresh_of({"refreshToken": "rt"}), _refresh_of({"refreshToken": ""})
    ('rt', None)
    >>> _refresh_of({"refreshToken": 5}), _refresh_of(None)
    (None, None)
    """
    if not isinstance(oauth, dict):
        return None
    refresh = oauth.get("refreshToken")
    return refresh if isinstance(refresh, str) and refresh else None


def lineage_finder(db) -> LineageFinder | None:
    """``db.account_id_for_refresh_token`` when ``db`` has it, else None.

    A test double without the method gets the ownership rule alone.

    >>> class _Db:
    ...     def account_id_for_refresh_token(self, refresh_token):
    ...         return 4
    >>> lineage_finder(_Db())("rt")
    4
    >>> lineage_finder(object()) is None
    True
    """
    finder = getattr(db, "account_id_for_refresh_token", None)
    return finder if callable(finder) else None


def _known_owner(lineage_known: LineageFinder | None, refresh_token: str) -> int | None:
    """The account id the finder names for ``refresh_token``, or None.

    None when there is no finder, no row stores the token, or the lookup
    failed: the ownership rule then decides.

    >>> _known_owner(lambda rt: 3, "rt"), _known_owner(None, "rt")
    (3, None)
    >>> def boom(rt):
    ...     raise RuntimeError("database is locked")
    >>> _known_owner(boom, "rt") is None
    True
    """
    if lineage_known is None:
        return None
    try:
        owner = lineage_known(refresh_token)
    except Exception:  # noqa: BLE001 - the shortcut only saves a request
        logger.debug("The refresh lineage lookup failed", exc_info=True)
        return None
    return owner if isinstance(owner, int) and not isinstance(owner, bool) else None


def refuses_refresh_downgrade(
    target_oauth: dict,
    live_payload: dict | None,
    account: dict,
    *,
    lineage_known: LineageFinder | None = None,
    directory_names_target: bool = False,
) -> str | None:
    """The reason to refuse writing ``target_oauth`` over ``live_payload``.

    A write of a null refresh token over a live one goes ahead only on
    positive evidence that the live tokens belong to another account. In
    order:

    1. ``target_oauth`` holds a refresh token, or the live payload holds no
       refresh token that is a non-empty string: nothing to protect.
    2. ``lineage_known`` names the row that stores the live refresh token.
       The target row: refused. Another row: the write goes ahead.
    3. The shared ownership rule (``credential_helpers.live_payload_ownership``,
       stamp not required) proves the owner. It costs no request when the
       live access or refresh token is the row's, else it uses the cached
       profile verdict. The target: refused. A 200 that names another owner:
       the write goes ahead. A failed request: refused for now.
    4. A payload whose expiry is not a real time is not a Claude Code rotation:
       the write goes ahead.
    5. Nothing proves the owner (an expired access token, a 401 or a 403). A
       stamp that names the target, or ``directory_names_target`` (the
       per-account directory of the target holds the payload), keeps the pair
       with the final refusal. Any other payload gets the transient refusal.

    The transient refusal is ``REFRESH_DOWNGRADE_UNCHECKED_REASON``, which the
    caller retries. Returns None when the write may go ahead. The function
    never logs: each caller logs the reason once.

    >>> row = {"id": 7, "cc_access_token": "at", "cc_refresh_token": None}
    >>> live = {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}}
    >>> refuses_refresh_downgrade({"accessToken": "at", "refreshToken": None}, live, row)
    'not publishing a null refresh token over a live one (cc re-auth required)'
    >>> refuses_refresh_downgrade({"accessToken": "at", "refreshToken": "rt"}, live, row) is None
    True
    >>> refuses_refresh_downgrade({"accessToken": "at"}, None, row) is None
    True
    >>> refuses_refresh_downgrade({"accessToken": "at"}, live, row, lineage_known=lambda rt: 9) is None
    True
    >>> refuses_refresh_downgrade({"accessToken": "at"}, live, row, lineage_known=lambda rt: 7)
    'not publishing a null refresh token over a live one (cc re-auth required)'
    >>> expired = {"accessToken": "old", "refreshToken": "rt", "expiresAt": 1000}
    >>> refuses_refresh_downgrade({"accessToken": "at"}, {"claudeAiOauth": expired}, row) == REFRESH_DOWNGRADE_UNCHECKED_REASON
    True
    >>> refuses_refresh_downgrade({"accessToken": "at"}, {"_jackedAccountId": 7, "claudeAiOauth": expired}, row)
    'not publishing a null refresh token over a live one (cc re-auth required)'
    >>> refuses_refresh_downgrade({"accessToken": "at"}, {"claudeAiOauth": expired}, row, directory_names_target=True)
    'not publishing a null refresh token over a live one (cc re-auth required)'
    """
    if _refresh_of(target_oauth) is not None:
        return None
    live_oauth = live_payload.get("claudeAiOauth") if isinstance(live_payload, dict) else None
    live_refresh = _refresh_of(live_oauth)
    if live_refresh is None:
        return None
    owner = _known_owner(lineage_known, live_refresh)
    if owner is not None:
        return REFRESH_DOWNGRADE_REASON if owner == _account_id(account) else None
    from jacked.api.credential_helpers import (
        _malformed_expiry,
        _stamp_of,
        live_payload_ownership,
        ownership_refusal_is_final,
    )

    owned = live_payload_ownership(account, live_payload, require_stamp=False)
    if owned is True:
        return REFRESH_DOWNGRADE_REASON
    if owned is None:
        return REFRESH_DOWNGRADE_UNCHECKED_REASON
    if ownership_refusal_is_final(account, live_oauth.get("accessToken")):
        return None
    if _malformed_expiry(live_oauth.get("expiresAt")):
        return None
    if directory_names_target or _stamp_of(live_payload) == _account_id(account):
        return REFRESH_DOWNGRADE_REASON
    return REFRESH_DOWNGRADE_UNCHECKED_REASON


def _account_id(account: dict) -> int | None:
    """The row's id as an int, or None.

    >>> _account_id({"id": 7}), _account_id({"id": "7"}), _account_id({})
    (7, 7, None)
    """
    try:
        return int(account.get("id"))
    except (TypeError, ValueError):
        return None


def downgrade_check_for(
    row: dict,
    target_oauth: dict | None = None,
    *,
    lineage_known: LineageFinder | None = None,
) -> Callable[[CredentialPayload | None], str | None]:
    """The engine's refresh downgrade check for the payload a request publishes.

    ``SwitchRequest.refresh_downgrade_check`` takes it. The engine calls it
    with what the authority holds now, inside the switch lease and before
    any write. It returns the refusal reason when the published payload
    (``target_oauth``; by default the row's ``build_oauth_data``) would put
    a null refresh token over a live one of this account, else None. A
    re-stamp republishes the authority's own object, so its check sees that
    object, never the row's. ``lineage_known`` is the database's refresh
    lineage finder (``lineage_finder``), which spares the profile request.
    Building the row's payload raises ``ForeignPrimaryTokens`` for a row
    whose primary tokens belong to another account: the caller refuses.

    >>> from jacked.credentials.canonical import CredentialPayload
    >>> row = {"id": 7, "cc_access_token": "at", "cc_refresh_token": None,
    ...        "cc_expires_at": 9999999999, "cc_refresh_failure_type": "invalid_grant"}
    >>> held = CredentialPayload.from_mapping({"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}})
    >>> downgrade_check_for(row)(held) == REFRESH_DOWNGRADE_REASON
    True
    >>> downgrade_check_for(row, {"accessToken": "at", "refreshToken": "rt"})(held) is None
    True
    >>> downgrade_check_for(row)(None) is None
    True
    """
    if target_oauth is None:
        from jacked.api.credential_helpers import build_oauth_data

        target_oauth = build_oauth_data(row)
    published = target_oauth

    def check(live: CredentialPayload | None) -> str | None:
        live_mapping = live.to_mapping() if live is not None else None
        return refuses_refresh_downgrade(
            published, live_mapping, row, lineage_known=lineage_known
        )

    return check
