"""Build and publish the Claude ``oauthAccount`` identity block.

Claude Code keeps the identity of the signed-in account in the
``oauthAccount`` block of ``~/.claude.json``. In Claude Code 2.1.296 only
the bootstrap merge checks the account uuid. The profile writers at start
and at token refresh merge with no uuid check, and they run only while one
of the gate fields (``COMPLETENESS_GATE_FIELDS``) is missing. A partial
block that jacked writes after a switch can therefore let a stale process
write the previous account back in.

This module is the one place that:

- builds the block for one account row (``build_identity_block``), with the
  same field mapping as the Claude Code profile writer;
- decides between a merge and a replace (``is_same_account``,
  ``apply_identity``);
- writes the block through ``claude_config_store`` (lock, fail-closed
  read, whole-file compare-and-swap, fsync, post-write verify) with
  ``write_identity``.

Logs from this module contain row ids, 8-character uuid prefixes and field
names. They never contain an email address or a token.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from jacked.api import claude_config_store as store
from jacked.api.claude_config_store import (  # noqa: F401 - public re-exports
    CAS_ABORTED,
    LOCKED,
    POST_VERIFY_MISMATCH,
    SYMLINK,
    UNCHANGED,
    UNREADABLE,
    UNSUPPORTED,
    WRITE_FAILED,
    WRITE_STATUSES,
    WRITTEN,
    WriteResult,
)
from jacked.api.credential_helpers import ClaudeConfigUnwritable

logger = logging.getLogger(__name__)

# Lock patience. A strict caller cannot fall back on a later retry, so it
# waits longer than a background writer. An explicit ``lock_timeout`` wins.
DEFAULT_LOCK_TIMEOUT_SECONDS = 5.0
STRICT_LOCK_TIMEOUT_SECONDS = 10.0

# Claude Code 2.1.296 skips its unguarded profile merges only while every
# one of these fields is present. A block without one of them is partial.
COMPLETENESS_GATE_FIELDS = (
    "billingType",
    "accountCreatedAt",
    "subscriptionCreatedAt",
    "ccOnboardingFlags",
)
# Fields that name the account. A merge over a block that Claude Code
# fetched more recently than jacked's stored profile writes only these.
IDENTITY_FIELDS = frozenset(
    {
        "accountUuid",
        "emailAddress",
        "organizationUuid",
        "organizationName",
        "organizationRateLimitTier",
        "userRateLimitTier",
    }
)

MERGE = "merge"
REPLACE = "replace"


@dataclass(frozen=True)
class IdentityBlock:
    """The ``oauthAccount`` fields for one account, ready to write.

    ``fields`` holds the exact keys to put in the config. ``complete`` is
    True only when the block carries every ``COMPLETENESS_GATE_FIELDS`` key,
    so Claude Code will not run its unguarded profile merges over it. ``account_id`` is
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


def profile_owner_mismatch(
    row_email: Any,
    row_org: Any,
    row_uuid: Any,
    profile: Any,
    *,
    require_uuid: bool = True,
) -> str | None:
    """Return the first profile field that shows another owner, or None.

    The one owner rule for a profile response or a stored profile: the
    ``account.email`` must casefold-equal the row email; the
    ``organization.uuid`` must equal the row org when the row has one; and,
    with ``require_uuid``, the ``account.uuid`` must equal the row uuid when
    both are known. The result is a field NAME, safe to log.

    >>> p = {"account": {"email": "A@b.c", "uuid": "u1"}, "organization": {"uuid": "o1"}}
    >>> profile_owner_mismatch("a@b.c", "o1", "u1", p) is None
    True
    >>> profile_owner_mismatch("a@b.c", "o2", None, p)
    'organization.uuid'
    >>> profile_owner_mismatch("z@b.c", "", None, p)
    'account.email'
    """
    if not isinstance(profile, Mapping):
        return "profile"
    account = profile.get("account")
    if not isinstance(account, Mapping):
        return "account"
    if _norm_email(account.get("email")) is None or _norm_email(
        account.get("email")
    ) != _norm_email(row_email):
        return "account.email"
    org = _norm_org(row_org)
    if org is not None:
        organization = profile.get("organization")
        profile_org = (
            _norm_org(organization.get("uuid"))
            if isinstance(organization, Mapping)
            else None
        )
        if profile_org != org:
            return "organization.uuid"
    if require_uuid:
        own_uuid = _text(row_uuid)
        profile_uuid = _text(account.get("uuid"))
        if own_uuid and profile_uuid and own_uuid != profile_uuid:
            return "account.uuid"
    return None


def _load_profile(row: Mapping, email: str, org_uuid: str | None):
    """Return ``(account, organization)`` from the stored profile, or None.

    The profile is used only when ``profile_owner_mismatch`` accepts it for
    the row, and only when it has both objects.
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
    mismatch = profile_owner_mismatch(
        email, org_uuid, row.get("account_uuid"), data, require_uuid=False
    )
    if mismatch is not None:
        logger.warning(
            "Account %s: stored profile does not belong to the row (%s); ignored",
            account_id,
            mismatch,
        )
        return None
    return account, organization


def stored_profile(row: Mapping) -> tuple[dict, dict] | None:
    """Return the stored ``(account, organization)`` the builder would use.

    >>> stored_profile({"email": "a@b.c"}) is None
    True
    """
    email = (row.get("email") or "").strip()
    if not email:
        return None
    return _load_profile(row, email, _norm_org(row.get("organization_uuid")))


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
    Code profile writer. Without one, the block has the minimal identity.
    ``complete`` is True only when every ``COMPLETENESS_GATE_FIELDS`` key is
    present. When ``db`` is given and the uuid is stored on a Claude row
    with a different email, the uuid is omitted.

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
    # The row may not know its org yet. The profile passed the owner guard,
    # so its org is this account's org. Every org-dependent choice below
    # (the org field, the tier key, the merge predicate) uses this value.
    if org_uuid is None:
        org_uuid = _norm_org(organization.get("uuid"))
    fields: dict = {}
    if account_uuid:
        fields["accountUuid"] = account_uuid
    fields["emailAddress"] = email
    fields["organizationUuid"] = org_uuid
    fields["hasExtraUsageEnabled"] = _coalesce(
        organization.get("has_extra_usage_enabled"), False
    )
    # Claude Code writes these with ``?? void 0``: a missing value leaves
    # the key out, and a missing key re-arms its profile fetch.
    for key, value in (
        ("billingType", organization.get("billing_type")),
        ("accountCreatedAt", account.get("created_at")),
        ("subscriptionCreatedAt", organization.get("subscription_created_at")),
    ):
        if value is not None:
            fields[key] = value
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
        complete=all(key in fields for key in COMPLETENESS_GATE_FIELDS),
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


def _fetched_ms(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _apply(config: dict, block: IdentityBlock) -> str:
    """Apply ``block`` to ``config`` and return the mode that was used."""
    existing = config.get("oauthAccount")
    if is_same_account(existing, block):
        merged = dict(existing)
        # Claude Code fetched this same account's profile after jacked did.
        # Its profile fields are fresher, so only the identity keys change.
        theirs = _fetched_ms(existing.get("profileFetchedAt"))
        ours = _fetched_ms(block.fields.get("profileFetchedAt"))
        identity_only = theirs is not None and (ours is None or theirs > ours)
        for key, value in block.fields.items():
            if identity_only and key not in IDENTITY_FIELDS:
                continue
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
    written over the rest (only the identity keys when the existing block
    was fetched after jacked's stored profile). REPLACE otherwise. Other
    top-level keys do not change.

    >>> cfg = {"theme": "dark", "oauthAccount": {"emailAddress": "z@b.c", "x": 1}}
    >>> apply_identity(cfg, minimal_identity_block(1, "a@b.c"))
    >>> cfg
    {'theme': 'dark', 'oauthAccount': {'emailAddress': 'a@b.c'}}
    """
    _apply(config, block)


def diverged_identity_fields(current: dict | None, block: IdentityBlock) -> list[str]:
    """Return the identity field names where ``current`` differs from ``block``.

    >>> b = minimal_identity_block(1, "a@b.c", "o1")
    >>> diverged_identity_fields({"emailAddress": "A@b.c", "organizationUuid": "o2"}, b)
    ['organizationUuid']
    """
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


# ---------------------------------------------------------------------------
# Write and read
# ---------------------------------------------------------------------------


def write_identity(
    home: Path | str,
    block: IdentityBlock,
    *,
    strict: bool = False,
    expected: dict | None = None,
    lock_timeout: float | None = None,
    scoped: bool = False,
) -> WriteResult:
    """Write ``block`` into the Claude config of ``home`` safely.

    ``scoped`` marks ``home`` as a ``CLAUDE_CONFIG_DIR`` directory (a
    per-account dir) instead of the user's home.

    See ``claude_config_store.write_config_identity`` for the lock, the
    fail-closed read, the whole-file compare-and-swap and the post-write
    verify. ``expected`` adds a compare on the ``oauthAccount`` that the
    caller read. With ``strict``, every status other than ``written`` and
    ``unchanged`` raises ``ClaudeConfigUnwritable``.

    ``lock_timeout`` is the lock wait in seconds. When it is None, a strict
    call waits ``STRICT_LOCK_TIMEOUT_SECONDS`` and other calls wait
    ``DEFAULT_LOCK_TIMEOUT_SECONDS``.
    """
    if lock_timeout is None:
        lock_timeout = (
            STRICT_LOCK_TIMEOUT_SECONDS if strict else DEFAULT_LOCK_TIMEOUT_SECONDS
        )
    result = store.write_config_identity(
        home,
        lambda config: _apply(config, block),
        lambda current: diverged_identity_fields(current, block),
        account_id=block.account_id,
        expected=expected,
        lock_timeout=lock_timeout,
        scoped=scoped,
    )
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
    """Return the ``oauthAccount`` block of the Claude config, or None.

    None means no supported config, a missing file, a symlink, a file that
    is not a JSON object, or no ``oauthAccount`` object. The read does not
    take the lock.
    """
    return store.read_config_identity(home)
