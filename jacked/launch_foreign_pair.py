"""Remember beside a per-account file that its pair belongs to another account.

The launch token sync (``jacked.launch``) writes this marker after a final
ownership refusal, and reads it before it asks the profile endpoint again.
The marker holds digests, never a token.
"""

import json
import logging
import time as _time
from pathlib import Path

from jacked.atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

# A pair the profile endpoint proved to belong to another account is
# remembered beside the per-account file by its digests (never a token),
# so the next launch replaces it without a request, even when its access
# token expired meanwhile (an expired pair cannot be proven again).
_FOREIGN_PAIR_MARKER = ".jacked-foreign-pair.json"


def _pair_digests(oauth: dict, account: dict) -> dict:
    """The digests that identify a token pair and the row it was refused for.

    >>> d = _pair_digests({"accessToken": "a", "refreshToken": "r"}, {"id": 1, "email": "x@y"})
    >>> sorted(d) == ["access_sha256", "identity_sha256", "refresh_sha256"] and len(d["access_sha256"]) == 64
    True
    """
    import hashlib

    from jacked.api.credential_helpers import _identity_key

    def digest(value) -> str:
        return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()

    return {
        "access_sha256": digest(oauth.get("accessToken")),
        "refresh_sha256": digest(oauth.get("refreshToken")),
        "identity_sha256": digest("|".join(_identity_key(account))),
    }


def _remember_foreign_pair(config_dir: Path, oauth: dict, account: dict) -> None:
    """Record beside the per-account file that its pair belongs to another
    account (a final verdict), as digests. Best effort: a failed write only
    costs the next launch one profile request."""
    marker = config_dir / _FOREIGN_PAIR_MARKER
    try:
        record = {**_pair_digests(oauth, account), "recorded_at": int(_time.time())}
        atomic_write_text(marker, json.dumps(record), mode=0o600, mode_strict=False)
    except OSError as exc:
        logger.debug("Could not record the foreign pair for %s: %s", config_dir.name, exc)


def _known_foreign_pair(config_dir: Path, oauth: dict, account: dict) -> bool:
    """True when the marker names this exact pair, refused for this exact
    row identity. Any other marker, or none, is ignored (a re-authed row
    decides again through the profile request)."""
    marker = config_dir / _FOREIGN_PAIR_MARKER
    try:
        if not marker.exists() or marker.is_symlink():
            return False
        record = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(record, dict):
        return False
    digests = _pair_digests(oauth, account)
    return all(record.get(name) == value for name, value in digests.items())


def _forget_foreign_pair(config_dir: Path) -> None:
    """Drop the marker: the file now holds the row's own pair."""
    try:
        (config_dir / _FOREIGN_PAIR_MARKER).unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.debug("Could not drop the foreign-pair marker for %s: %s", config_dir.name, exc)
