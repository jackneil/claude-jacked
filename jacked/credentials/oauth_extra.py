"""The ``claudeAiOauth`` fields jacked carries through without modelling them.

Claude Code writes more into its credential object than jacked's columns
hold. A switch rebuilds the object from the account row, so a field jacked
does not keep is dropped (logged by
``jacked.credentials.transaction._warn_on_schema_drift``). These fields are
kept per account in ``accounts.cc_oauth_extra`` (JSON) and written back on
the next switch.

What Claude Code does with them (2.1.316, read from the installed binary):

- ``refreshTokenExpiresAt`` (ms epoch): ``now + refresh_token_expires_in``
  from a token response; a sign-in without that field gets a 30-day default;
  a refresh without it keeps the previous value. It belongs to the refresh
  token beside it.
- ``clientId``: the OAuth client the tokens were issued to, written only
  when it is not the default client.
- ``subscriptionTypeSource``: where ``subscriptionType`` came from
  (``"sign_in"`` or ``"profile"``).
- ``subscriptionTypeVerifiedAt`` (ms epoch): when a profile fetch verified
  the tier. Claude Code keeps it across a refresh only while the tier is
  unchanged; a ``"profile"`` source without it is an unverified tier that
  Claude Code re-checks on its next profile fetch.

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

``subscriptionTypeVerifiedAt`` is bound a second way: ``_tier`` records the
``subscriptionType`` it verified. jacked publishes the tier from its own
profile poll, so the timestamp is written back only while the row's tier
still equals the captured one (Claude Code's own merge rule); otherwise the
source stays and the timestamp is dropped, the shape Claude Code itself
emits and self-heals.
"""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

PASSTHROUGH_KEYS = (
    "refreshTokenExpiresAt",
    "clientId",
    "subscriptionTypeSource",
    "subscriptionTypeVerifiedAt",
)
_MAX_TEXT = 200
# The latest epoch Python's datetime can represent (9999-12-31T23:59:59Z), in
# ms: a larger "expiry" is not a date, and would overflow on the way to one.
_MAX_EPOCH_MS = 253_402_300_799_000
_LINEAGE_KEY = "_lineage"
# The subscriptionType that subscriptionTypeVerifiedAt verified, at capture.
_TIER_KEY = "_tier"
_VERIFIED_AT = "subscriptionTypeVerifiedAt"


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
    >>> oauth_extra_from({"subscriptionTypeVerifiedAt": 1798000000000})
    {'subscriptionTypeVerifiedAt': 1798000000000}
    >>> [oauth_extra_from({"subscriptionTypeVerifiedAt": v})
    ...  for v in (True, float("inf"), 10**309, -1, "soon", 2**60)]
    [{}, {}, {}, {}, {}, {}]
    """
    if not isinstance(oauth, dict):
        return {}
    out: dict = {}
    for key in PASSTHROUGH_KEYS:  # allowlist order, so the record reads stably
        value = oauth.get(key)
        if key in ("refreshTokenExpiresAt", _VERIFIED_AT):
            number = _positive_number(value)
            if number is not None and number <= _MAX_EPOCH_MS:
                out[key] = int(number)
        elif isinstance(value, str) and value and len(value) <= _MAX_TEXT:
            out[key] = value
    return out


def _tier_of(value: Any) -> str | None:
    """``value`` as a tier string, else None (never invented).

    >>> _tier_of("max"), _tier_of(""), _tier_of(5), _tier_of("x" * 201)
    ('max', None, None, None)
    """
    if isinstance(value, str) and value and len(value) <= _MAX_TEXT:
        return value
    return None


def _stored_record(raw: Any, refresh_token: str | None) -> dict | None:
    """The stored record for the row's CURRENT refresh token, else None."""
    if not raw or not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(value, dict) or value.get(_LINEAGE_KEY) != lineage_of(refresh_token):
        return None
    return value


def parse_oauth_extra(
    raw: Any, refresh_token: str | None, *, current_tier: Any = None
) -> dict:
    """Decode an ``accounts.cc_oauth_extra`` value for the row's CURRENT
    refresh token: {} when malformed or captured with another lineage.

    ``subscriptionTypeVerifiedAt`` is returned only while ``current_tier``
    (the tier the row publishes now) equals the tier it verified at capture;
    otherwise the source is kept and the timestamp dropped, exactly as Claude
    Code's own refresh merge does. A caller that does not pass the tier never
    gets the timestamp: a verification is never claimed for an unknown tier.

    >>> stored = encode_oauth_extra({"clientId": "c", "accessToken": "never"}, "rt")
    >>> parse_oauth_extra(stored, "rt")
    {'clientId': 'c'}
    >>> parse_oauth_extra(stored, "a-newer-rt")
    {}
    >>> parse_oauth_extra("not json", "rt"), parse_oauth_extra(None, "rt")
    ({}, {})
    >>> verified = encode_oauth_extra({"subscriptionTypeSource": "profile",
    ...     "subscriptionTypeVerifiedAt": 1798000000000}, "rt", tier="max")
    >>> parse_oauth_extra(verified, "rt", current_tier="max")
    {'subscriptionTypeSource': 'profile', 'subscriptionTypeVerifiedAt': 1798000000000}
    >>> parse_oauth_extra(verified, "rt", current_tier="pro")
    {'subscriptionTypeSource': 'profile'}
    >>> parse_oauth_extra(verified, "rt")
    {'subscriptionTypeSource': 'profile'}
    >>> untiered = encode_oauth_extra({"subscriptionTypeSource": "profile",
    ...     "subscriptionTypeVerifiedAt": 1798000000000}, "rt")
    >>> parse_oauth_extra(untiered, "rt", current_tier="max")
    {'subscriptionTypeSource': 'profile'}
    """
    value = _stored_record(raw, refresh_token)
    if value is None:
        return {}
    fields = oauth_extra_from(value)
    if _VERIFIED_AT in fields:
        captured = _tier_of(value.get(_TIER_KEY))
        if captured is None or captured != _tier_of(current_tier):
            fields.pop(_VERIFIED_AT)
    return fields


def captured_tier(raw: Any, refresh_token: str | None) -> str | None:
    """The tier recorded with the stored fields for this lineage, or None.

    A refresh rotates the lineage but not the tier, so the re-encoded record
    carries the same tier forward.

    >>> stored = encode_oauth_extra(
    ...     {"subscriptionTypeVerifiedAt": 1798000000000}, "rt", tier="max")
    >>> captured_tier(stored, "rt"), captured_tier(stored, "other")
    ('max', None)
    >>> captured_tier(encode_oauth_extra({"clientId": "c"}, "rt", tier="max"), "rt") is None
    True
    """
    value = _stored_record(raw, refresh_token)
    return _tier_of(value.get(_TIER_KEY)) if value is not None else None


def encode_oauth_extra(
    extra: dict, refresh_token: str | None, *, tier: Any = None
) -> str | None:
    """The column value for ``extra``, bound to the refresh lineage it
    describes (None when there is nothing to keep).

    ``tier`` is the ``subscriptionType`` beside the fields at capture; it is
    stored only when there is a verification timestamp to bind it to.

    >>> encode_oauth_extra({}, "rt") is None
    True
    >>> sorted(json.loads(encode_oauth_extra({"clientId": "c"}, "rt")))
    ['_lineage', 'clientId']
    >>> sorted(json.loads(encode_oauth_extra({"clientId": "c"}, "rt", tier="max")))
    ['_lineage', 'clientId']
    >>> sorted(json.loads(encode_oauth_extra(
    ...     {"subscriptionTypeVerifiedAt": 1798000000000}, "rt", tier="max")))
    ['_lineage', '_tier', 'subscriptionTypeVerifiedAt']
    """
    clean = oauth_extra_from(extra)
    if not clean:
        return None
    record = {**clean, _LINEAGE_KEY: lineage_of(refresh_token)}
    captured = _tier_of(tier)
    if _VERIFIED_AT in clean and captured is not None:
        record[_TIER_KEY] = captured
    return json.dumps(record, sort_keys=True)


def capture_oauth_extra(oauth: Any, refresh_token: str | None) -> str | None:
    """The column value for a ``claudeAiOauth`` object Claude Code wrote,
    bound to ``refresh_token`` and to the tier the object carries.

    >>> capture_oauth_extra({"accessToken": "x"}, "rt") is None
    True
    >>> stored = capture_oauth_extra({"subscriptionType": "max",
    ...     "subscriptionTypeSource": "profile",
    ...     "subscriptionTypeVerifiedAt": 1798000000000}, "rt")
    >>> captured_tier(stored, "rt")
    'max'
    """
    if not isinstance(oauth, dict):
        return None
    return encode_oauth_extra(
        oauth_extra_from(oauth), refresh_token, tier=oauth.get("subscriptionType")
    )


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
    >>> refresh_expiry_from_response({"refresh_token_expires_in": "60"}, 1000)
    61000
    >>> [refresh_expiry_from_response({"refresh_token_expires_in": v}, 1000) for v in ("\u00b2", "\u0661\u0662", "9" * 5000)]
    [None, None, None]
    """
    if not isinstance(data, dict):
        return None
    seconds = data.get("refresh_token_expires_in")
    # ASCII digits only, and short: str.isdigit() also accepts digits such as
    # "\u00b2" that int() refuses, and int() refuses strings over 4300 digits.
    # Either raise would also lose the new pair.
    if isinstance(seconds, str) and seconds.isascii() and seconds.isdigit() and len(seconds) <= 20:
        seconds = int(seconds)
    # A non-finite or absurd value (Infinity, 1e308, 10**309) would raise on
    # the way to an int after the server already consumed the refresh token,
    # losing the new pair; such a value says nothing usable, so it is ignored.
    seconds = _positive_number(seconds)
    if seconds is None or seconds > _MAX_EPOCH_MS:
        return None
    expires = now_ms + seconds * 1000
    return int(expires) if expires <= _MAX_EPOCH_MS else None
