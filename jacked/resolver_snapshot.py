"""Strict reader for the canonical secret-free resolver snapshot."""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Callable

SNAPSHOT_FILENAME = "jacked-resolver-snapshot.json"
SNAPSHOT_SCHEMA_VERSION = 1
SNAPSHOT_MAX_BYTES = 64 * 1024
_SECRET_KEY_PARTS = ("token", "secret", "password", "hmac", "digest", "locator")
_SNAPSHOT_KEYS = {
    "schema_version",
    "published_at",
    "fresh_until",
    "scope",
    "state",
    "evidence",
    "credential_revision",
    "desired",
    "observed",
}


def _contains_secret_key(value) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            lowered = str(key).casefold()
            if any(part in lowered for part in _SECRET_KEY_PARTS):
                return True
            if _contains_secret_key(child):
                return True
    elif isinstance(value, list):
        return any(_contains_secret_key(child) for child in value)
    return False


def read_resolver_snapshot(
    path: str | Path,
    *,
    now: float | None = None,
    require_fresh: bool = False,
    json_load: Callable = json.load,
) -> dict | None:
    """Read one owner-private, unlinked, fixed-schema snapshot."""
    path = Path(path)
    descriptor = None
    try:
        if path.is_symlink():
            return None
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_size > SNAPSHOT_MAX_BYTES
            or getattr(info, "st_file_attributes", 0) & 0x400
            or info.st_nlink != 1
        ):
            return None
        if os.name != "nt":
            getuid = getattr(os, "getuid", None)
            if stat.S_IMODE(info.st_mode) & 0o077:
                return None
            if getuid is not None and info.st_uid != getuid():
                return None
        with os.fdopen(descriptor, encoding="utf-8", errors="strict") as source:
            descriptor = None
            data = json_load(source)
    except (AttributeError, json.JSONDecodeError, OSError, TypeError, ValueError):
        return None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if (
        not isinstance(data, dict)
        or set(data) != _SNAPSHOT_KEYS
        or data.get("schema_version") != SNAPSHOT_SCHEMA_VERSION
        or _contains_secret_key(data)
        or data.get("scope") not in {"global", "scoped", "unknown", None}
        or data.get("state")
        not in {"resolved", "conflict", "missing", "unusable", "stale"}
    ):
        return None
    evidence = data.get("evidence")
    if not isinstance(evidence, list) or not all(
        isinstance(item, str) and item for item in evidence
    ):
        return None
    revision = data.get("credential_revision")
    if revision is not None and not isinstance(revision, str):
        return None
    if require_fresh:
        now = time.time() if now is None else now
        published_at = data.get("published_at")
        fresh_until = data.get("fresh_until")
        if (
            isinstance(published_at, bool)
            or not isinstance(published_at, (int, float))
            or isinstance(fresh_until, bool)
            or not isinstance(fresh_until, (int, float))
            or published_at > now + 300
            or fresh_until < now
        ):
            return None
    return data


def snapshot_identity(value) -> dict | None:
    """Validate one nullable schema-v1 identity object."""
    if not isinstance(value, dict):
        return None
    if set(value) - {"account_id", "email", "organization_id"}:
        return None
    account_id = value.get("account_id")
    email = value.get("email")
    organization_id = value.get("organization_id")
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        return None
    if not isinstance(email, str) or not email:
        return None
    if organization_id is not None and not isinstance(organization_id, str):
        return None
    return {
        "account_id": account_id,
        "email": email,
        "organization_id": organization_id or None,
    }
