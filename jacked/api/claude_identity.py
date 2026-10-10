"""Build and publish the Claude ``oauthAccount`` identity block.

Claude Code keeps the identity of the signed-in account in the
``oauthAccount`` block of ``~/.claude.json``. Claude Code 2.1.296 accepts
a profile merge into that block only when the account uuid agrees, and it
skips its own profile fetch while the block holds the full profile. A
partial block that jacked writes after a switch can therefore let a stale
process write the previous account back in.

This module is the one place that:

- builds the block for one account row (``build_identity_block``), with the
  same field mapping as the Claude Code profile writer;
- decides between a merge and a replace (``is_same_account``,
  ``apply_identity``);
- writes the block under a lock that is compatible with proper-lockfile,
  fails closed on a config it cannot read, and verifies the result
  (``write_identity``).

Logs from this module contain row ids, 8-character uuid prefixes and field
names. They never contain an email address or a token.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from jacked.api.credential_helpers import ClaudeConfigUnwritable, _safe_replace

logger = logging.getLogger(__name__)

CONFIG_FILENAME = ".claude.json"
LOCK_SUFFIX = ".lock"
# proper-lockfile (Claude Code) treats a lock directory as stale after 10 s.
STALE_LOCK_SECONDS = 10.0
_LOCK_POLL_SECONDS = 0.05
_RELEASE_ATTEMPTS = 5
_RELEASE_BACKOFF_SECONDS = 0.05

WRITTEN = "written"
UNCHANGED = "unchanged"
CAS_ABORTED = "cas_aborted"
LOCKED = "locked"
UNREADABLE = "unreadable"
SYMLINK = "symlink"
WRITE_FAILED = "write_failed"
POST_VERIFY_MISMATCH = "post_verify_mismatch"
WRITE_STATUSES = frozenset(
    {
        WRITTEN,
        UNCHANGED,
        CAS_ABORTED,
        LOCKED,
        UNREADABLE,
        SYMLINK,
        WRITE_FAILED,
        POST_VERIFY_MISMATCH,
    }
)
_SUCCESS_STATUSES = frozenset({WRITTEN, UNCHANGED})

MERGE = "merge"
REPLACE = "replace"


@dataclass(frozen=True)
class IdentityBlock:
    """The ``oauthAccount`` fields for one account, ready to write.

    ``fields`` holds the exact keys to put in the config. ``complete`` is
    True only when the block carries the stored profile. ``account_id`` is
    the jacked row id (0 when no row is known). ``email``,
    ``organization_uuid`` and ``account_uuid`` are the identity keys that
    the merge predicate and the post-write check use.
    """

    fields: dict
    complete: bool
    account_id: int
    email: str
    organization_uuid: str | None
    account_uuid: str | None


@dataclass(frozen=True)
class WriteResult:
    """The outcome of one ``write_identity`` call.

    ``status`` is one of ``WRITE_STATUSES``. ``mode`` is ``"merge"`` or
    ``"replace"`` when the block was applied, else None.
    """

    status: str
    mode: str | None = field(default=None)

    @property
    def ok(self) -> bool:
        """True when the config holds the block after the call.

        >>> WriteResult("written").ok, WriteResult("locked").ok
        (True, False)
        """
        return self.status in _SUCCESS_STATUSES


# ---------------------------------------------------------------------------
# Small normalizers
# ---------------------------------------------------------------------------


def _uuid_prefix(value: str | None) -> str:
    """Return a log-safe uuid prefix.

    >>> _uuid_prefix("5b0c7a1e-1111"), _uuid_prefix(None)
    ('5b0c7a1e', '-')
    """
    return value[:8] if value else "-"


def _norm_email(value: Any) -> str | None:
    """Normalize an email for identity comparison.

    >>> _norm_email("  Alice@Example.COM "), _norm_email(None), _norm_email(" ")
    ('alice@example.com', None, None)
    """
    if not isinstance(value, str):
        return None
    text = value.strip().casefold()
    return text or None


def _norm_org(value: Any) -> str | None:
    """Normalize an organization uuid: empty, None and missing are all None.

    >>> _norm_org(""), _norm_org(None), _norm_org(" org-1 ")
    (None, None, 'org-1')
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _text(value: Any) -> str | None:
    """Return a non-empty string, else None.

    >>> _text("abc"), _text(""), _text(5)
    ('abc', None, None)
    """
    if isinstance(value, str) and value:
        return value
    return None


def _coalesce(value: Any, default: Any) -> Any:
    """Return ``default`` only when ``value`` is None (JavaScript ``??``).

    >>> _coalesce(False, True), _coalesce(None, {})
    (False, {})
    """
    return default if value is None else value


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------


def _load_profile(row: Mapping, email: str, org_uuid: str | None):
    """Return ``(account, organization)`` from the stored profile, or None.

    The profile is used only when it belongs to the row: the profile email
    must casefold-equal the row email, and the profile org must equal the
    row org when both are known.
    """
    raw = row.get("profile_json")
    if not raw:
        return None
    account_id = row.get("id")
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        logger.warning("Account %s: stored profile is not valid JSON; ignored", account_id)
        return None
    if not isinstance(data, dict):
        return None
    account = data.get("account")
    organization = data.get("organization")
    if not isinstance(account, dict) or not isinstance(organization, dict):
        logger.warning(
            "Account %s: stored profile lacks the account or organization object; ignored",
            account_id,
        )
        return None
    if _norm_email(account.get("email")) != _norm_email(email):
        logger.warning(
            "Account %s: stored profile email does not match the row; ignored",
            account_id,
        )
        return None
    profile_org = _norm_org(organization.get("uuid"))
    if org_uuid and profile_org and profile_org != org_uuid:
        logger.warning(
            "Account %s: stored profile organization does not match the row; ignored",
            account_id,
        )
        return None
    return account, organization


def _uuid_is_shared(db, account_uuid: str, email: str, account_id: int) -> bool:
    """True when another Claude row with a different email has this uuid.

    One person can have rows in more than one organization; those rows
    share the uuid and the email, so they do not count. A failed lookup
    counts as shared: omitting the uuid is the safe direction.
    """
    try:
        rows = db.list_accounts(include_inactive=True, include_deleted=True)
    except Exception:  # noqa: BLE001 - any DB failure must fail closed
        logger.warning(
            "Account %s: could not check uuid %s for other owners; omitting it",
            account_id,
            _uuid_prefix(account_uuid),
            exc_info=True,
        )
        return True
    own_email = _norm_email(email)
    for other in rows:
        if (other.get("provider") or "claude") != "claude":
            continue
        if other.get("id") == account_id:
            continue
        if other.get("account_uuid") != account_uuid:
            continue
        if _norm_email(other.get("email")) != own_email:
            return True
    return False


def minimal_identity_block(
    account_id: int,
    email: str,
    organization_uuid: str | None = None,
    organization_name: str | None = None,
    *,
    account_uuid: str | None = None,
    display_name: str | None = None,
) -> IdentityBlock:
    """Build the identity block for an account without a stored profile.

    The block holds the uuid (when known), the email, the org uuid and the
    org name (when set). ``display_name`` is for the legacy callers that
    supply one explicitly.

    >>> minimal_identity_block(3, "a@b.c", "", None).fields
    {'emailAddress': 'a@b.c'}
    """
    org_uuid = _norm_org(organization_uuid)
    fields: dict = {}
    if account_uuid:
        fields["accountUuid"] = account_uuid
    fields["emailAddress"] = email
    if org_uuid:
        fields["organizationUuid"] = org_uuid
    if organization_name:
        fields["organizationName"] = organization_name
    if display_name:
        fields["displayName"] = display_name
    return IdentityBlock(
        fields=fields,
        complete=False,
        account_id=int(account_id or 0),
        email=email,
        organization_uuid=org_uuid,
        account_uuid=account_uuid or None,
    )


def build_identity_block(row: Mapping, *, db=None) -> IdentityBlock | None:
    """Build the ``oauthAccount`` block for one jacked account row.

    Return None for a row of another provider, or for a row without an
    email. With a stored profile, the block has the field set of the Claude
    Code profile writer (``complete=True``). Without one, the block has the
    minimal identity (``complete=False``). When ``db`` is given and the uuid
    is stored on a Claude row with a different email, the uuid is omitted.

    >>> build_identity_block({"provider": "codex", "email": "a@b.c"}) is None
    True
    >>> build_identity_block({"id": 1, "email": "a@b.c"}).fields
    {'emailAddress': 'a@b.c'}
    """
    if (row.get("provider") or "claude") != "claude":
        return None
    email = (row.get("email") or "").strip()
    if not email:
        return None
    account_id = int(row.get("id") or 0)
    org_uuid = _norm_org(row.get("organization_uuid"))
    org_name = _text(row.get("organization_name"))

    profile = _load_profile(row, email, org_uuid)
    if profile is not None:
        account_uuid = _text(profile[0].get("uuid")) or _text(row.get("account_uuid"))
    else:
        account_uuid = _text(row.get("account_uuid"))

    if account_uuid and db is not None and _uuid_is_shared(db, account_uuid, email, account_id):
        logger.warning(
            "Account %d: uuid %s is stored on another row with a different email; "
            "omitting it from the published identity",
            account_id,
            _uuid_prefix(account_uuid),
        )
        account_uuid = None

    if profile is None:
        return minimal_identity_block(
            account_id, email, org_uuid, org_name, account_uuid=account_uuid
        )

    account, organization = profile
    fields: dict = {}
    if account_uuid:
        fields["accountUuid"] = account_uuid
    fields["emailAddress"] = email
    fields["organizationUuid"] = org_uuid
    fields["hasExtraUsageEnabled"] = _coalesce(
        organization.get("has_extra_usage_enabled"), False
    )
    fields["billingType"] = organization.get("billing_type")
    fields["accountCreatedAt"] = account.get("created_at")
    subscription_created = organization.get("subscription_created_at")
    if subscription_created is not None:
        fields["subscriptionCreatedAt"] = subscription_created
    fields["ccOnboardingFlags"] = copy.deepcopy(
        _coalesce(organization.get("cc_onboarding_flags"), {})
    )
    fields["claudeCodeTrialEndsAt"] = organization.get("claude_code_trial_ends_at")
    fields["claudeCodeTrialDurationDays"] = organization.get(
        "claude_code_trial_duration_days"
    )
    fields["seatTier"] = organization.get("seat_tier")
    plan_name = organization.get("plan_display_name")
    if isinstance(plan_name, str) and plan_name.strip():
        fields["planDisplayName"] = plan_name
    if account.get("display_name"):
        fields["displayName"] = account["display_name"]
    if account.get("full_name"):
        fields["fullName"] = account["full_name"]
    fetched_at = row.get("profile_fetched_at")
    if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
        fields["profileFetchedAt"] = int(fetched_at * 1000)
    if org_name:
        fields["organizationName"] = org_name
    tier = _text(row.get("rate_limit_tier"))
    if tier:
        tier_key = "organizationRateLimitTier" if org_uuid else "userRateLimitTier"
        fields[tier_key] = tier
    return IdentityBlock(
        fields=fields,
        complete=True,
        account_id=account_id,
        email=email,
        organization_uuid=org_uuid,
        account_uuid=account_uuid,
    )


# ---------------------------------------------------------------------------
# Merge or replace
# ---------------------------------------------------------------------------


def is_same_account(existing: Any, block: IdentityBlock) -> bool:
    """True when ``existing`` is the same account as ``block``.

    The email (casefold, stripped) and the normalized org must be equal.
    The uuids must also be equal, unless the block has no uuid.

    >>> b = minimal_identity_block(1, "A@b.c", "", None)
    >>> is_same_account({"emailAddress": "a@b.c", "accountUuid": "u"}, b)
    True
    >>> is_same_account({"emailAddress": "z@b.c"}, b)
    False
    """
    if not isinstance(existing, dict):
        return False
    block_email = _norm_email(block.email)
    if block_email is None or _norm_email(existing.get("emailAddress")) != block_email:
        return False
    if _norm_org(existing.get("organizationUuid")) != _norm_org(block.organization_uuid):
        return False
    if block.account_uuid is None:
        return True
    return existing.get("accountUuid") == block.account_uuid


def _apply(config: dict, block: IdentityBlock) -> str:
    """Apply ``block`` to ``config`` and return the mode that was used."""
    existing = config.get("oauthAccount")
    if is_same_account(existing, block):
        merged = dict(existing)
        for key, value in block.fields.items():
            if key == "accountUuid" and not value:
                continue
            # A case-only difference is the same identity. Keep the stored
            # spelling so a no-op switch does not rewrite the file.
            if key == "emailAddress" and _norm_email(existing.get(key)) == _norm_email(value):
                continue
            merged[key] = copy.deepcopy(value)
        config["oauthAccount"] = merged
        return MERGE
    config["oauthAccount"] = copy.deepcopy(dict(block.fields))
    return REPLACE


def apply_identity(config: dict, block: IdentityBlock) -> None:
    """Merge or replace ``config["oauthAccount"]`` with ``block``.

    MERGE when ``is_same_account`` is True: the existing uuid and every
    field that the block does not carry stay, and the block fields are
    written over the rest. REPLACE otherwise. Other top-level keys do not
    change.

    >>> cfg = {"theme": "dark", "oauthAccount": {"emailAddress": "z@b.c", "x": 1}}
    >>> apply_identity(cfg, minimal_identity_block(1, "a@b.c"))
    >>> cfg
    {'theme': 'dark', 'oauthAccount': {'emailAddress': 'a@b.c'}}
    """
    _apply(config, block)


# ---------------------------------------------------------------------------
# Config lock (proper-lockfile compatible: an EMPTY <config>.lock directory)
# ---------------------------------------------------------------------------


def _lock_path_for(config_path: Path) -> Path:
    return config_path.with_name(config_path.name + LOCK_SUFFIX)


def _clear_stale_lock(lock_path: Path) -> bool:
    """Remove an empty lock directory that is older than the stale limit.

    Return True when the caller can try ``mkdir`` again at once. A lock
    directory that is not empty is not ours, so it is never removed.
    """
    try:
        age = time.time() - os.lstat(lock_path).st_mtime
    except FileNotFoundError:
        return True
    except OSError:
        return False
    if age <= STALE_LOCK_SECONDS:
        return False
    try:
        os.rmdir(lock_path)
    except FileNotFoundError:
        return True
    except OSError:
        logger.debug("Stale Claude config lock is not an empty directory; treated as held")
        return False
    logger.info("Removed a stale Claude config lock (age %.0f s)", age)
    return True


def _acquire_lock(lock_path: Path, timeout: float) -> bool:
    """Create the lock directory, retrying until ``timeout`` seconds pass.

    Raise OSError for a failure other than "the lock exists".
    """
    deadline = time.monotonic() + max(float(timeout), 0.0)
    retried_stale = False
    while True:
        try:
            os.mkdir(lock_path)
            return True
        except FileExistsError:
            # After a stale lock is cleared, always try once more, even when
            # the timeout is zero.
            if _clear_stale_lock(lock_path) and (
                not retried_stale or time.monotonic() < deadline
            ):
                retried_stale = True
                continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(_LOCK_POLL_SECONDS, remaining))


def _release_lock(lock_path: Path) -> None:
    """Remove the lock directory, with a bounded retry for Windows scanners."""
    for attempt in range(_RELEASE_ATTEMPTS):
        try:
            os.rmdir(lock_path)
            return
        except FileNotFoundError:
            logger.warning(
                "Claude config lock was removed by another process before release"
            )
            return
        except OSError:
            if attempt < _RELEASE_ATTEMPTS - 1:
                time.sleep(_RELEASE_BACKOFF_SECONDS * (attempt + 1))
    logger.warning("Could not release the Claude config lock")


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def _read_config_for_write(config_path: Path) -> dict | None:
    """Read the config under the lock.

    An absent file gives an empty dict. A file that cannot be read or that
    is not a JSON object gives None (fail closed: never start fresh over it).
    """
    try:
        text = config_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _atomic_write_json(config_path: Path, data: dict) -> None:
    """Write ``data`` to ``config_path`` through a private temp file."""
    fd, tmp = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=".claude_json_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(tmp, str(config_path))
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_locked(
    config_path: Path, block: IdentityBlock, expected: dict | None
) -> WriteResult:
    """Read, compare-and-swap and write. Call this only under the lock."""
    if config_path.is_symlink():
        return WriteResult(SYMLINK)
    config = _read_config_for_write(config_path)
    if config is None:
        return WriteResult(UNREADABLE)
    if expected is not None and config.get("oauthAccount") != expected:
        return WriteResult(CAS_ABORTED)
    had_block = "oauthAccount" in config
    before = copy.deepcopy(config.get("oauthAccount"))
    mode = _apply(config, block)
    if had_block and config["oauthAccount"] == before:
        return WriteResult(UNCHANGED, mode)
    try:
        _atomic_write_json(config_path, config)
    except Exception as exc:  # noqa: BLE001 - every write error is reported
        logger.warning(
            "Account %d: could not write the Claude config (%s)",
            block.account_id,
            type(exc).__name__,
        )
        return WriteResult(WRITE_FAILED, mode)
    return WriteResult(WRITTEN, mode)


def _diverged_fields(current: dict | None, block: IdentityBlock) -> list[str]:
    """Return the identity field names where ``current`` differs from ``block``."""
    if current is None:
        return ["oauthAccount"]
    diverged = []
    if block.account_uuid and current.get("accountUuid") != block.account_uuid:
        diverged.append("accountUuid")
    if _norm_email(current.get("emailAddress")) != _norm_email(block.email):
        diverged.append("emailAddress")
    if _norm_org(current.get("organizationUuid")) != _norm_org(block.organization_uuid):
        diverged.append("organizationUuid")
    return diverged


def _write(
    home: Path, block: IdentityBlock, expected: dict | None, lock_timeout: float
) -> WriteResult:
    config_path = home / CONFIG_FILENAME
    if config_path.is_symlink():
        return WriteResult(SYMLINK)
    lock_path = _lock_path_for(config_path)
    try:
        acquired = _acquire_lock(lock_path, lock_timeout)
    except OSError as exc:
        logger.warning(
            "Account %d: could not create the Claude config lock (%s)",
            block.account_id,
            type(exc).__name__,
        )
        return WriteResult(WRITE_FAILED)
    if not acquired:
        return WriteResult(LOCKED)
    try:
        result = _write_locked(config_path, block, expected)
    finally:
        _release_lock(lock_path)
    if result.status != WRITTEN:
        return result
    diverged = _diverged_fields(read_claude_config_identity(home), block)
    if diverged:
        logger.warning(
            "Account %d: post-verify-mismatch after the Claude config write "
            "(fields: %s); another writer changed the identity",
            block.account_id,
            ", ".join(diverged),
        )
        return WriteResult(POST_VERIFY_MISMATCH, result.mode)
    return result


def write_identity(
    home: Path | str,
    block: IdentityBlock,
    *,
    strict: bool = False,
    expected: dict | None = None,
    lock_timeout: float = 5.0,
) -> WriteResult:
    """Write ``block`` into ``<home>/.claude.json`` safely.

    The write takes the ``.claude.json.lock`` directory lock, reads the
    config, compares ``expected`` with the current ``oauthAccount`` when
    ``expected`` is given, applies the block and replaces the file
    atomically with mode 0600. After the lock is released, the identity is
    read again and verified. An existing config that is not a JSON object
    is never overwritten. With ``strict``, every status other than
    ``written`` and ``unchanged`` raises ``ClaudeConfigUnwritable``.
    """
    result = _write(Path(home), block, expected, lock_timeout)
    if result.status == WRITTEN:
        logger.info(
            "Account %d: published the Claude identity (%s, %s, uuid %s)",
            block.account_id,
            result.mode,
            "complete" if block.complete else "partial",
            _uuid_prefix(block.account_uuid),
        )
    elif result.status != UNCHANGED:
        logger.warning(
            "Account %d: Claude identity not published (%s)",
            block.account_id,
            result.status,
        )
    if strict and not result.ok:
        raise ClaudeConfigUnwritable(f"claude config identity {result.status}")
    return result


def read_claude_config_identity(home: Path | str) -> dict | None:
    """Return the ``oauthAccount`` block of ``<home>/.claude.json``, or None.

    None means a missing file, a symlink, a file that is not a JSON object,
    or no ``oauthAccount`` object. The read does not take the lock.
    """
    config_path = Path(home) / CONFIG_FILENAME
    if config_path.is_symlink():
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    block = data.get("oauthAccount")
    return block if isinstance(block, dict) else None
