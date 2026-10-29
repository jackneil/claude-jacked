"""The ``claudeAiOauth`` fields jacked carries through without modelling them.

Claude Code writes more into its credential object than jacked's columns
hold. A switch rebuilds the object from the account row, so a field jacked
does not keep is dropped (logged by
``jacked.credentials.transaction._warn_on_schema_drift``). These fields are
kept per account in ``accounts.cc_oauth_extra`` (JSON) and written back on
the next switch.

What Claude Code does with them (2.1.312, read from the installed binary):

- ``refreshTokenExpiresAt`` (ms epoch): ``now + refresh_token_expires_in``
  from a token response; a sign-in without that field gets a 30-day default;
  a refresh without it keeps the previous value. It belongs to the refresh
  token beside it.
- ``clientId``: the OAuth client the tokens were issued to, written only
  when it is not the default client.
- ``subscriptionTypeSource``: where ``subscriptionType`` came from
  (``"sign_in"`` or ``"profile"``).

Only this allowlist passes, type-checked: an unknown future key keeps the
drift warning (and is not stored), and nothing that looks like a token or a
secret is ever kept here. jacked never invents a value: a field it did not
read from Claude Code or from a token response is absent.
"""
from __future__ import annotations

import json
from typing import Any

PASSTHROUGH_KEYS = ("refreshTokenExpiresAt", "clientId", "subscriptionTypeSource")
_MAX_TEXT = 200


def oauth_extra_from(oauth: Any) -> dict:
    """The pass-through fields of a ``claudeAiOauth`` object, type-checked.

    >>> oauth_extra_from({"accessToken": "x", "refreshTokenExpiresAt": 1.8e12,
    ...                   "clientId": "c", "subscriptionTypeSource": "sign_in"})
    {'refreshTokenExpiresAt': 1800000000000, 'clientId': 'c', 'subscriptionTypeSource': 'sign_in'}
    >>> oauth_extra_from({"refreshTokenExpiresAt": True, "clientId": 5, "other": "y"})
    {}
    >>> oauth_extra_from(None)
    {}
    """
    if not isinstance(oauth, dict):
        return {}
    out: dict = {}
    expires = oauth.get("refreshTokenExpiresAt")
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) and expires > 0:
        out["refreshTokenExpiresAt"] = int(expires)
    for key in ("clientId", "subscriptionTypeSource"):
        value = oauth.get(key)
        if isinstance(value, str) and value and len(value) <= _MAX_TEXT:
            out[key] = value
    return out


def parse_oauth_extra(raw: Any) -> dict:
    """Decode an ``accounts.cc_oauth_extra`` value; anything malformed is {}.

    >>> parse_oauth_extra('{"clientId": "c", "accessToken": "never"}')
    {'clientId': 'c'}
    >>> parse_oauth_extra("not json"), parse_oauth_extra(None)
    ({}, {})
    """
    if isinstance(raw, dict):
        return oauth_extra_from(raw)
    if not raw or not isinstance(raw, str):
        return {}
    try:
        return oauth_extra_from(json.loads(raw))
    except (TypeError, ValueError):
        return {}


def encode_oauth_extra(extra: dict) -> str | None:
    """The column value for ``extra`` (None when empty), stable key order.

    >>> encode_oauth_extra({"clientId": "c"}), encode_oauth_extra({})
    ('{"clientId": "c"}', None)
    """
    clean = oauth_extra_from(extra)
    return json.dumps(clean, sort_keys=True) if clean else None


def refresh_expiry_from_response(data: Any, now_ms: int) -> int | None:
    """``refreshTokenExpiresAt`` for a token response, or None when it says
    nothing (``refresh_token_expires_in`` missing or not a positive number).

    >>> refresh_expiry_from_response({"refresh_token_expires_in": 60}, 1000)
    61000
    >>> refresh_expiry_from_response({}, 1000) is None
    True
    """
    if not isinstance(data, dict):
        return None
    seconds = data.get("refresh_token_expires_in")
    if isinstance(seconds, str) and seconds.isdigit():
        seconds = int(seconds)
    if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds <= 0:
        return None
    return int(now_ms + seconds * 1000)
