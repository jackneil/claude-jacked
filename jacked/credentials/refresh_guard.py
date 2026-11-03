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
# The live store holds a refresh token, the target has none, and the owner
# of the live tokens could not be checked (the profile request failed): a
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
) -> str | None:
    """The reason to refuse writing ``target_oauth`` over ``live_payload``.

    The function refuses only when all of these are true:

    1. ``target_oauth`` has no refresh token (None, empty or absent).
    2. The live payload holds a refresh token that is a non-empty string.
    3. The live tokens belong to ``account``: ``lineage_known`` names the
       row that stores the live refresh token (the target row: refused;
       another row: the write goes ahead; no row: the next step decides),
       else the shared ownership rule
       (``credential_helpers.live_payload_ownership``, stamp not required)
       proves it with no request when the live access or refresh token is
       the row's, else through the cached profile verdict.

    A live store with no refresh token and a payload of another owner are not
    protected: the write goes ahead, as before. A payload whose owner could
    not be checked (the profile request failed) is refused with
    ``REFRESH_DOWNGRADE_UNCHECKED_REASON``, a transient refusal the caller
    retries. The two local checks run first, so only a real downgrade can
    cost a profile request. Returns None when the write may go ahead. The
    function never logs: each caller logs the reason once.

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
    from jacked.api.credential_helpers import live_payload_ownership

    owned = live_payload_ownership(account, live_payload, require_stamp=False)
    if owned is None:
        return REFRESH_DOWNGRADE_UNCHECKED_REASON
    return REFRESH_DOWNGRADE_REASON if owned else None


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
