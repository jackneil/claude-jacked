"""A short fingerprint of a credential payload's token pair.

The engine reports it for every committed activation
(``SwitchResult.published_fingerprint``), and the authority guard keeps the
last one it published (the ``_last_authority_write_fp`` setting), so a later
pass recognizes jacked's own write without a token value anywhere.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping

_TOKEN_KEYS = ("refreshToken", "accessToken")


def payload_fingerprint(oauth: Any) -> str:
    """Return a 16-character fingerprint of a ``claudeAiOauth`` object.

    The fingerprint is the start of a sha256 digest over the refresh and the
    access token. It holds no token value, and a missing token counts as
    empty.

    >>> one = payload_fingerprint({"accessToken": "a", "refreshToken": "r"})
    >>> len(one), one == payload_fingerprint({"refreshToken": "r", "accessToken": "a"})
    (16, True)
    >>> one == payload_fingerprint({"accessToken": "r", "refreshToken": "a"})
    False
    >>> payload_fingerprint(None) == payload_fingerprint({})
    True
    """
    oauth = oauth if isinstance(oauth, Mapping) else {}
    raw = "\x1f".join(str(oauth.get(key) or "") for key in _TOKEN_KEYS)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
