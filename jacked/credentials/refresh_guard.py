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

from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .canonical import CredentialPayload

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


def refuses_refresh_downgrade(
    target_oauth: dict, live_payload: dict | None, account: dict
) -> str | None:
    """The reason to refuse writing ``target_oauth`` over ``live_payload``.

    The function refuses only when all of these are true:

    1. ``target_oauth`` has no refresh token (None, empty or absent).
    2. The live payload holds a refresh token that is a non-empty string.
    3. The shared ownership rule proves that the live tokens belong to
       ``account`` (``credential_helpers.live_payload_ownership``, stamp not
       required): with no request when the live access or refresh token is
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
    """
    if _refresh_of(target_oauth) is not None:
        return None
    live_oauth = live_payload.get("claudeAiOauth") if isinstance(live_payload, dict) else None
    if _refresh_of(live_oauth) is None:
        return None
    from jacked.api.credential_helpers import live_payload_ownership

    owned = live_payload_ownership(account, live_payload, require_stamp=False)
    if owned is None:
        return REFRESH_DOWNGRADE_UNCHECKED_REASON
    return REFRESH_DOWNGRADE_REASON if owned else None


def downgrade_check_for(row: dict) -> Callable[[CredentialPayload | None], str | None]:
    """The engine's refresh downgrade check for a payload built from ``row``.

    ``SwitchRequest.refresh_downgrade_check`` takes it. The engine calls it
    with what the authority holds now, inside the switch lease and before
    any write. It returns the refusal reason when the row's payload
    (``build_oauth_data``) would put a null refresh token over a live one of
    this account, else None.

    >>> from jacked.credentials.canonical import CredentialPayload
    >>> row = {"id": 7, "cc_access_token": "at", "cc_refresh_token": None,
    ...        "cc_expires_at": 9999999999, "cc_refresh_failure_type": "invalid_grant"}
    >>> check = downgrade_check_for(row)
    >>> check(CredentialPayload.from_mapping(
    ...     {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}})) == REFRESH_DOWNGRADE_REASON
    True
    >>> check(None) is None
    True
    """
    from jacked.api.credential_helpers import build_oauth_data

    target_oauth = build_oauth_data(row)

    def check(live: CredentialPayload | None) -> str | None:
        live_mapping = live.to_mapping() if live is not None else None
        return refuses_refresh_downgrade(target_oauth, live_mapping, row)

    return check
