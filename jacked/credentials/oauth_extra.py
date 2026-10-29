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

The fields describe ONE refresh lineage, so the stored value is bound to it:
``_lineage`` is a short hash of the refresh token they were captured with, and
they are read back only while the row still holds that refresh token. A
writer that replaces the tokens without touching this column (an older jacked
after a downgrade, a path added later) therefore can never publish another
lineage's expiry.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

PASSTHROUGH_KEYS = ("refreshTokenExpiresAt", "clientId", "subscriptionTypeSource")
_MAX_TEXT = 200
# The latest epoch Python's datetime can represent (9999-12-31T23:59:59Z), in
# ms: a larger "expiry" is not a date, and would overflow on the way to one.
_MAX_EPOCH_MS = 253_402_300_799_000
_LINEAGE_KEY = "_lineage"


def _positive_number(value: Any) -> float | None:
    """``value`` as a positive finite float, else None. An int too large for a
    float (10**309) is None, never an OverflowError.

    >>> _positive_number(5), _positive_number(10**309), _positive_number(True)
    (5.0, None, None)
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) and number > 0 else None


def lineage_of(refresh_token: str | None) -> str:
    """A short, one-way fingerprint of the refresh lineage (never the token).

    >>> lineage_of("rt") == lineage_of("rt") != lineage_of("other")
    True
    >>> len(lineage_of(None))
    16
    """
    return hashlib.sha256((refresh_token or "").encode("utf-8")).hexdigest()[:16]


def oauth_extra_from(oauth: Any) -> dict:
    """The pass-through fields of a ``claudeAiOauth`` object, type-checked.

    >>> oauth_extra_from({"accessToken": "x", "refreshTokenExpiresAt": 1.8e12,
    ...                   "clientId": "c", "subscriptionTypeSource": "sign_in"})
    {'refreshTokenExpiresAt': 1800000000000, 'clientId': 'c', 'subscriptionTypeSource': 'sign_in'}
    >>> oauth_extra_from({"refreshTokenExpiresAt": True, "clientId": 5, "other": "y"})
    {}
    >>> oauth_extra_from(None), oauth_extra_from({"refreshTokenExpiresAt": float("inf")})
    ({}, {})
    """
    if not isinstance(oauth, dict):
        return {}
    out: dict = {}
    expires = _positive_number(oauth.get("refreshTokenExpiresAt"))
    if expires is not None and expires <= _MAX_EPOCH_MS:
        out["refreshTokenExpiresAt"] = int(expires)
    for key in ("clientId", "subscriptionTypeSource"):
        value = oauth.get(key)
        if isinstance(value, str) and value and len(value) <= _MAX_TEXT:
            out[key] = value
    return out


def parse_oauth_extra(raw: Any, refresh_token: str | None) -> dict:
    """Decode an ``accounts.cc_oauth_extra`` value for the row's CURRENT
    refresh token: {} when malformed or captured with another lineage.

    >>> stored = encode_oauth_extra({"clientId": "c", "accessToken": "never"}, "rt")
    >>> parse_oauth_extra(stored, "rt")
    {'clientId': 'c'}
    >>> parse_oauth_extra(stored, "a-newer-rt")
    {}
    >>> parse_oauth_extra("not json", "rt"), parse_oauth_extra(None, "rt")
    ({}, {})
    """
    if not raw or not isinstance(raw, str):
        return {}
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(value, dict) or value.get(_LINEAGE_KEY) != lineage_of(refresh_token):
        return {}
    return oauth_extra_from(value)


def encode_oauth_extra(extra: dict, refresh_token: str | None) -> str | None:
    """The column value for ``extra``, bound to the refresh lineage it
    describes (None when there is nothing to keep).

    >>> encode_oauth_extra({}, "rt") is None
    True
    >>> sorted(json.loads(encode_oauth_extra({"clientId": "c"}, "rt")))
    ['_lineage', 'clientId']
    """
    clean = oauth_extra_from(extra)
    if not clean:
        return None
    return json.dumps({**clean, _LINEAGE_KEY: lineage_of(refresh_token)}, sort_keys=True)


def refresh_expiry_from_response(data: Any, now_ms: int) -> int | None:
    """``refreshTokenExpiresAt`` for a token response, or None when it says
    nothing (``refresh_token_expires_in`` missing or not a positive number).

    >>> refresh_expiry_from_response({"refresh_token_expires_in": 60}, 1000)
    61000
    >>> refresh_expiry_from_response({}, 1000) is None
    True
    >>> refresh_expiry_from_response({"refresh_token_expires_in": float("inf")}, 1000) is None
    True
    >>> refresh_expiry_from_response({"refresh_token_expires_in": 1e308}, 1000) is None
    True
    >>> refresh_expiry_from_response({"refresh_token_expires_in": 10**309}, 1000) is None
    True
    >>> refresh_expiry_from_response({"refresh_token_expires_in": "1" + "0" * 309}, 1000) is None
    True
    """
    if not isinstance(data, dict):
        return None
    seconds = data.get("refresh_token_expires_in")
    if isinstance(seconds, str) and seconds.isdigit():
        seconds = int(seconds)
    # A non-finite or absurd value (Infinity, 1e308, 10**309) would raise on
    # the way to an int after the server already consumed the refresh token,
    # losing the new pair; such a value says nothing usable, so it is ignored.
    seconds = _positive_number(seconds)
    if seconds is None or seconds > _MAX_EPOCH_MS:
        return None
    expires = now_ms + seconds * 1000
    return int(expires) if expires <= _MAX_EPOCH_MS else None
