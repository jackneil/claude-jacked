"""Credential helpers: atomic file I/O, keychain access, credential builders.

Account switches write the credential stores through
``jacked.credentials.transaction.CredentialTransactionEngine``; the
``sync_credential_to_all_stores()`` wrapper here is a legacy compatibility
path. The ``oauthAccount`` identity in ``~/.claude.json`` is written only by
``jacked.api.claude_identity``.
"""

import contextlib
import json
import logging
import os
import random
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_RATE_LIMIT_TIER_UNSET = object()


class ClaudeConfigUnwritable(RuntimeError):
    """The Claude config identity could not be written.

    Raised only by the strict callers. Claude Code re-reads its credential
    store when the ``oauthAccount`` identity in the config changes, so a
    caller that reports "running sessions will follow" must know whether the
    identity actually landed. Non-strict callers keep logging and continuing.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class _ClaudeConfigAccount:
    """Account metadata mirrored into Claude's non-secret config."""

    email: str
    display_name: str | None = None
    organization_uuid: str | None = None
    organization_name: str | None = None
    rate_limit_tier: object = _RATE_LIMIT_TIER_UNSET


# ---------------------------------------------------------------------------
# Cross-process lock (compatible with Claude Code's proper-lockfile)
# ---------------------------------------------------------------------------

_LOCK_DIR = str(Path.home() / ".claude.lock")
_LOCK_MAX_RETRIES = 5
_LOCK_RETRY_BASE_MS = 1000


@contextlib.contextmanager
def acquire_claude_lock(timeout_retries: int = _LOCK_MAX_RETRIES):
    """Acquire a cross-process lock compatible with Claude Code's proper-lockfile.

    proper-lockfile creates a {path}.lock directory as its lock. We use
    os.mkdir (atomic on POSIX) to implement the same protocol.

    Includes stale lock detection: if the lock directory exists but the
    PID file inside is from a dead process, the lock is removed and retried.

    Usage:
        with acquire_claude_lock() as locked:
            if locked:
                # ... exchange tokens, write credentials ...
    """
    lock_path = _LOCK_DIR
    pid_file = os.path.join(lock_path, "pid")
    acquired = False

    for attempt in range(timeout_retries):
        try:
            os.mkdir(lock_path)
            try:
                with open(pid_file, "w") as f:
                    f.write(str(os.getpid()))
            except OSError:
                pass
            acquired = True
            break
        except FileExistsError:
            # Lock exists — check if it's stale. Uses the cross-platform
            # is_process_alive (ctypes WaitForSingleObject on Windows)
            # because os.kill(pid, 0) is not a valid probe on Windows and
            # can surface CPython SystemError for exited PIDs.
            from jacked.service.process import is_process_alive
            holder_pid = None
            try:
                with open(pid_file, "r") as f:
                    holder_pid = int(f.read().strip())
            except (FileNotFoundError, ValueError):
                pass

            if holder_pid is None or not is_process_alive(holder_pid):
                try:
                    shutil.rmtree(lock_path, ignore_errors=True)
                    logger.info("Removed stale Claude lock (holder PID gone)")
                    continue
                except OSError:
                    pass
            # Process exists — lock is valid, fall through to retry/timeout

            if attempt < timeout_retries - 1:
                wait = (_LOCK_RETRY_BASE_MS + random.randint(0, 1000)) / 1000.0
                logger.debug("Claude lock held, retrying in %.1fs (attempt %d)", wait, attempt + 1)
                time.sleep(wait)

    if not acquired:
        logger.warning("Failed to acquire Claude lock after %d retries", timeout_retries)

    try:
        yield acquired
    finally:
        if acquired:
            shutil.rmtree(lock_path, ignore_errors=True)


# ---------------------------------------------------------------------------
# Keychain helpers
# ---------------------------------------------------------------------------


def _get_keychain_username() -> str:
    """Get the Keychain account name from the operating-system identity."""
    from jacked.credentials.macos_store import system_account_name

    return system_account_name()


def _safe_replace(src: str, dst: str, *, retries: int = 3, delay: float = 0.1):
    """os.replace() with retry for Windows PermissionError.

    On Windows, os.replace() can fail if the target file is held open
    by another process.  Retries with exponential backoff.
    On macOS/Linux, this is equivalent to a single os.replace() call.
    """
    for attempt in range(retries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if sys.platform != "win32" or attempt == retries - 1:
                raise
            time.sleep(delay * (2 ** attempt))


def _write_credential_file(cred_path: Path, data: dict) -> float:
    """Write credential data to file atomically with secure permissions.

    Returns mtime of written file. Raises on failure.
    """
    if cred_path.is_symlink():
        raise OSError("Refusing to write through symlink")
    cred_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        dir=str(cred_path.parent),
        prefix=".credentials_tmp_",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(tmp, str(cred_path))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return cred_path.stat().st_mtime


def _legacy_identity_block(metadata: _ClaudeConfigAccount):
    """Build an identity block from caller-supplied fields (no account row).

    The caller knows no account uuid and no stored profile, so the block is
    the minimal identity. A tier that the caller supplies is kept.
    """
    from dataclasses import replace

    from jacked.api.claude_identity import minimal_identity_block

    block = minimal_identity_block(
        0,
        metadata.email,
        metadata.organization_uuid,
        metadata.organization_name,
        display_name=metadata.display_name,
    )
    tier = metadata.rate_limit_tier
    if tier is not _RATE_LIMIT_TIER_UNSET and tier:
        tier_key = (
            "organizationRateLimitTier"
            if block.organization_uuid
            else "userRateLimitTier"
        )
        block = replace(block, fields={**block.fields, tier_key: tier})
    return block


def _update_claude_config_account(
    metadata: _ClaudeConfigAccount,
    *,
    home: Path | None = None,
    strict: bool = False,
) -> None:
    """Write one account's identity to ~/.claude.json through the safe writer.

    The write merges into the block of the same account and replaces the
    block of a different account. An existing config that is not a JSON
    object is never overwritten. ``strict`` makes the outcome explicit: a
    strict caller gets ``ClaudeConfigUnwritable`` when the identity did not
    land. Non-strict callers get a logged warning.
    """
    from jacked.api.claude_identity import write_identity

    write_identity(
        home or Path.home(), _legacy_identity_block(metadata), strict=strict
    )


def update_claude_config_email(
    email: str,
    display_name: str = None,
    organization_uuid: str = None,
    organization_name: str = None,
    *,
    home: Path | None = None,
    strict: bool = False,
):
    """Write a minimal Claude identity from caller-supplied fields.

    Callers that have the account row use ``claude_identity`` directly, so
    the block carries the stored profile. ``home`` selects the config
    directory; it defaults to the user's home. ``strict`` raises
    ``ClaudeConfigUnwritable`` instead of logging when the identity cannot
    be written.

    >>> import tempfile
    >>> with tempfile.TemporaryDirectory() as tmp:
    ...     update_claude_config_email("test@example.com", home=Path(tmp))
    ...     json.loads((Path(tmp) / ".claude.json").read_text())["oauthAccount"]
    {'emailAddress': 'test@example.com'}
    """
    _update_claude_config_account(
        _ClaudeConfigAccount(
            email=email,
            display_name=display_name,
            organization_uuid=organization_uuid,
            organization_name=organization_name,
        ),
        home=home,
        strict=strict,
    )


# ---------------------------------------------------------------------------
# Platform credential store (macOS Keychain)
# ---------------------------------------------------------------------------


def read_platform_credentials() -> dict | None:
    """Read credentials from the platform's native credential store.

    macOS: Keychain entry with service "Claude Code-credentials" and
    account name matching the system username (same as Claude Code).
    Linux/Windows: not yet needed (still use .credentials.json)

    Returns parsed dict (same shape as .credentials.json) or None.
    """
    if sys.platform != "darwin":
        return None
    from jacked.credentials.macos_store import MacOSCredentialStore
    from jacked.credentials.models import StoreStatus

    try:
        result = MacOSCredentialStore(_get_keychain_username()).read()
    except (OSError, RuntimeError) as exc:
        logger.debug("Keychain read error: %s", exc)
        return None
    if result.status is not StoreStatus.OK or result.payload is None:
        logger.debug("Keychain read unavailable: %s", result.reason)
        return None
    return result.payload.to_mapping()


def read_fresh_active_token(account_id: int) -> str | None:
    """Read the current access token from credential stores for an account.

    After activation, Claude Code refreshes the access token on its own
    schedule, making the DB copy stale.  This reads the live token from
    the same stores Claude Code uses (Keychain first, then file).

    Returns the access token string, or None if the stores don't belong
    to this account or are unreadable.
    """
    # The authority store decides the identity; a required mirror that drifts
    # is reported as evidence, not as a conflict. Gate on that observation
    # first so a stale file can never make another account's token "active".
    if read_active_account_id() != account_id:
        return None

    # The observation does NOT promise the file mirror holds the same account
    # as the authority, so every raw read below re-checks _jackedAccountId.
    kc_data = read_platform_credentials()
    if kc_data and kc_data.get("_jackedAccountId") == account_id:
        token = kc_data.get("claudeAiOauth", {}).get("accessToken")
        if token:
            return token

    # Fall back to global .credentials.json. The _jackedAccountId equality
    # check below is what keeps this fallback same-account: on macOS the file
    # mirror can legitimately name a different account while the authority
    # observation above resolved. Do not remove it as redundant.
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
            if data.get("_jackedAccountId") == account_id:
                token = data.get("claudeAiOauth", {}).get("accessToken")
                if token:
                    return token
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("Credential file read failed for account %d: %s", account_id, exc)

    return None


def read_active_account_id() -> int | None:
    """Return the currently-active jacked account id, or None.

    Consulted by all paths that must NOT rotate the active account's CC
    refresh token (architecture doc §7.1, §7.2, §7.3 + invariant I2).

    Returns an ID only when the canonical resolver RESOLVES an identity from
    the authority store; required-mirror drift is carried as evidence rather
    than suppressing the identity. Never raises; unsupported, missing, and
    conflicting states all resolve to None.
    """
    try:
        from jacked.credentials.resolver import ResolverState
        from jacked.credentials.runtime import resolve_active_identity

        observation = resolve_active_identity()
        if observation.state is ResolverState.RESOLVED:
            return observation.identity.account_id
    except Exception:
        logger.debug("read_active_account_id failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Cache for live credential reads (30s TTL)
# ---------------------------------------------------------------------------

_live_cred_cache: dict = {"account_id": None, "data": None, "expires_at": 0.0}


def _get_cached_live_credentials(account_id: int):
    """Return cached live credentials if fresh, else None."""
    if (_live_cred_cache["account_id"] == account_id
            and time.time() < _live_cred_cache["expires_at"]
            and _live_cred_cache["data"] is not None):
        return _live_cred_cache["data"]
    return None


def _cache_live_credentials(account_id: int, data: dict | None):
    """Cache live credential read for 30 seconds."""
    _live_cred_cache["account_id"] = account_id
    _live_cred_cache["data"] = data
    _live_cred_cache["expires_at"] = time.time() + 30.0


def invalidate_live_cred_cache():
    """Invalidate the cache (call on swap)."""
    _live_cred_cache["account_id"] = None
    _live_cred_cache["data"] = None
    _live_cred_cache["expires_at"] = 0.0


def _ownership_lookup(access_token: str):
    """Identify an access token for the reconcile ownership check (test seam)."""
    from jacked.api.authority_guard import lookup_profile

    return lookup_profile(access_token)


def _owner_mismatch(account: dict, profile) -> str | None:
    from jacked.api.claude_identity import profile_owner_mismatch

    return profile_owner_mismatch(
        account.get("email"),
        account.get("organization_uuid"),
        account.get("account_uuid"),
        profile,
    )


# Profile verdicts by (account id, sha256 of the access token). A settled
# verdict (proven, or another owner) is kept for the token's life; a failed
# request is retried after _OWNERSHIP_RETRY_SECONDS. Without the cache every
# dashboard poll would send a profile request for a rejected foreign token.
_OWNERSHIP_RETRY_SECONDS = 300.0
_OWNERSHIP_CACHE_LIMIT = 64
_ownership_cache: dict[tuple, tuple[bool | None, float]] = {}


def clear_ownership_cache() -> None:
    """Forget every cached ownership verdict (tests and account changes)."""
    _ownership_cache.clear()


def _live_tokens_belong_to(
    account: dict, live_access: str, live_refresh: str | None, live_expires
) -> bool:
    """True when the live tokens provably belong to ``account``.

    A token the row already holds proves ownership with no network call.
    Any other token is identified through the OAuth profile endpoint with the
    shared owner rule. Nothing else counts as proof: the stamp survives Claude
    Code's own writes, and jacked itself rewrites ~/.claude.json on every
    switch, so neither one names the token's owner.

    An expired token, a failed request, or a call on an event-loop thread
    proves nothing, and the result is False, so the caller imports nothing.

    >>> row = {"cc_access_token": "a1", "cc_refresh_token": "r1"}
    >>> _live_tokens_belong_to(row, "a1", "r9", None)
    True
    >>> _live_tokens_belong_to(row, "a9", "r1", None)
    True
    >>> _live_tokens_belong_to(row, "a9", "r9", 1)  # expired, cannot prove
    False
    """
    if live_access == account.get("cc_access_token"):
        return True
    if live_refresh and live_refresh == account.get("cc_refresh_token"):
        return True
    if live_expires:
        expires_s = live_expires / 1000 if live_expires > 1e12 else live_expires
        if expires_s <= time.time():
            return False
    try:
        import asyncio

        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        logger.warning(
            "Account %s: credential ownership check skipped on the event loop",
            account.get("id"),
        )
        return False

    import hashlib

    key = (account.get("id"), hashlib.sha256(live_access.encode("utf-8")).hexdigest())
    now = time.time()
    cached = _ownership_cache.get(key)
    if cached is not None:
        verdict, at = cached
        if verdict is not None or now - at < _OWNERSHIP_RETRY_SECONDS:
            return bool(verdict)

    result = _ownership_lookup(live_access)
    if result.status_code == 200 and isinstance(result.profile, dict):
        field = _owner_mismatch(account, result.profile)
        verdict = field is None
        if not verdict:
            logger.warning(
                "Account %s: live token profile names another owner (%s)",
                account.get("id"),
                field,
            )
    else:
        verdict = None
        logger.info(
            "Account %s: credential ownership lookup returned %s",
            account.get("id"),
            result.status_code,
        )
    _ownership_cache[key] = (verdict, now)
    while len(_ownership_cache) > _OWNERSHIP_CACHE_LIMIT:
        _ownership_cache.pop(next(iter(_ownership_cache)))
    return bool(verdict)


# Claude Code chain failure types that mean "do not use this chain". An
# ``invalid_grant`` is a dead refresh lineage. ``auth_rejected`` is a cc
# access token the API refused (HTTP 401/403) that jacked could not replace.
CC_CHAIN_DEAD_FAILURES = frozenset({"invalid_grant"})
CC_TOKEN_UNUSABLE_FAILURES = frozenset({"invalid_grant", "auth_rejected"})


def cc_chain_dead(account: dict) -> bool:
    """True when the Claude Code refresh lineage of ``account`` is dead.

    >>> cc_chain_dead({"cc_refresh_failure_type": "invalid_grant"})
    True
    >>> cc_chain_dead({"refresh_failure_type": "invalid_grant"})
    False
    """
    return account.get("cc_refresh_failure_type") in CC_CHAIN_DEAD_FAILURES


def cc_token_known_dead(account: dict) -> bool:
    """True when the row's Claude Code access token is known not to work.

    Two recorded verdicts count, both tied to the token the row holds now:
    ``auth_rejected`` (the API refused this access token; every writer that
    replaces the access token clears it), and the window keeper's
    ``needs_reauth`` recorded against the row's CURRENT candidate tokens.
    ``invalid_grant`` does not count: it marks the refresh LINEAGE dead, and
    the access token beside it may be a fresh, working one (one the authority
    guard just adopted, or one recovered from the live store). Expiry order
    is never such evidence (#179i).

    >>> cc_token_known_dead({"cc_refresh_failure_type": "auth_rejected"})
    True
    >>> cc_token_known_dead({"cc_refresh_failure_type": "invalid_grant"})
    False
    >>> cc_token_known_dead({"cc_refresh_failure_type": None, "keeper_state": None})
    False
    """
    if account.get("cc_refresh_failure_type") == "auth_rejected":
        return True
    from jacked.web.database import Database

    state = Database.parse_keeper_state(account.get("keeper_state"))
    if state.get("last_status") != "needs_reauth":
        return False
    from jacked.api.usage_monitor import _candidate_fingerprint, _keeper_candidates

    cc, primary = _keeper_candidates(account)
    if not cc:
        # The keeper did not try the cc token (its chain carries a verdict),
        # so a needs_reauth says nothing about it.
        return False
    return state.get("token_fp") == _candidate_fingerprint(cc, primary)


def is_primary_publication(account: dict, live_access) -> bool:
    """True when ``live_access`` is the row's PRIMARY access token.

    ``build_oauth_data`` publishes the primary access token (with a null
    refresh token) when the row has no usable Claude Code token. That token
    belongs to the primary lineage: importing it into the cc_* columns
    would merge the two chains.

    >>> is_primary_publication({"access_token": "p1"}, "p1")
    True
    >>> is_primary_publication({"access_token": "p1"}, "c1")
    False
    """
    return bool(live_access) and live_access == account.get("access_token")


def _stamp_of(payload: dict):
    stamp = payload.get("_jackedAccountId")
    if isinstance(stamp, bool) or not isinstance(stamp, (int, str)):
        return None
    try:
        return int(stamp)
    except (TypeError, ValueError):
        return None


def live_payload_belongs_to(
    account: dict,
    payload: dict | None,
    *,
    require_stamp: bool = True,
    profile: dict | None = None,
) -> bool:
    """True when the Claude Code tokens in ``payload`` provably belong to ``account``.

    This is the ONE stamp-and-ownership rule for every path that imports
    live tokens into a row: reconcile, the authority guard, and the
    invalid_grant live recovery.

    1. When ``require_stamp`` is true, the payload stamp must name the row.
       The stamp is necessary but never sufficient: Claude Code keeps it
       when it writes another account's tokens.
    2. A token the row already holds proves the owner with no network call.
    3. A ``profile`` that the caller already fetched for this access token
       proves the owner when the shared owner rule accepts it.
    4. Otherwise ``_live_tokens_belong_to`` asks the profile endpoint. It
       refuses on an event-loop thread, so an async caller must use
       ``asyncio.to_thread``.

    The function never logs a token value.

    >>> row = {"id": 1, "email": "a@x.com", "cc_access_token": "a1"}
    >>> live_payload_belongs_to(row, {"_jackedAccountId": 1,
    ...                               "claudeAiOauth": {"accessToken": "a1"}})
    True
    >>> live_payload_belongs_to(row, {"_jackedAccountId": 5,
    ...                               "claudeAiOauth": {"accessToken": "a1"}})
    False
    >>> live_payload_belongs_to(row, {"claudeAiOauth": {"accessToken": "z9"}},
    ...                         require_stamp=False,
    ...                         profile={"account": {"email": "a@x.com"}})
    True
    """
    if not isinstance(payload, dict):
        return False
    if require_stamp and _stamp_of(payload) != account.get("id"):
        return False
    oauth = payload.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    live_access = oauth.get("accessToken")
    if not isinstance(live_access, str) or not live_access:
        return False
    live_refresh = oauth.get("refreshToken")
    if live_access == account.get("cc_access_token"):
        return True
    if live_refresh and live_refresh == account.get("cc_refresh_token"):
        return True
    if profile is not None:
        field = _owner_mismatch(account, profile)
        if field is not None:
            logger.warning(
                "Account %s: live token profile names another owner (%s)",
                account.get("id"),
                field,
            )
            return False
        return True
    return _live_tokens_belong_to(
        account, live_access, live_refresh, oauth.get("expiresAt")
    )


def _row_holds_live_tokens(row: dict, live: dict | None) -> bool:
    """True when ``row``'s Claude Code tokens are the ones in the live payload.

    >>> live = {"claudeAiOauth": {"accessToken": "a1", "refreshToken": "r1"}}
    >>> _row_holds_live_tokens({"cc_access_token": "a1"}, live)
    True
    >>> _row_holds_live_tokens({"cc_refresh_token": "r1"}, live)
    True
    >>> _row_holds_live_tokens({"cc_access_token": "zz"}, live)
    False
    >>> _row_holds_live_tokens({"cc_access_token": "a1"}, None)
    False
    """
    if not isinstance(live, dict):
        return False
    oauth = live.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return False
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    if access and access == row.get("cc_access_token"):
        return True
    return bool(refresh) and refresh == row.get("cc_refresh_token")


def live_holder_account_ids(db, live: dict | None = None) -> set[int] | None:
    """Ids of the rows whose Claude Code tokens the live authority holds.

    A running Claude Code session refreshes from the tokens in the authority,
    so every row holding those tokens shares a single-use refresh lineage
    with that session, whatever the payload's stamp says (the stamp survives
    Claude Code's writes for other accounts). Reads the live store once when
    ``live`` is not given.

    The session tracker's accounts join the set: a session started on X
    keeps X's refresh token in memory even after the authority moved to Y,
    so rotating X would send that session to /login (2026-09-05).

    Returns ``None`` when the holders are UNKNOWN (the live store could not
    be read, the account list or the session tracker failed): a caller must
    treat that as "every chain may be held", never as an empty set.
    """
    if live is None:
        try:
            live = read_live_credentials()
        except Exception:
            logger.debug("live_holder_account_ids: live store read failed", exc_info=True)
            return None
    if not isinstance(live, dict):
        return None
    held: set[int] = set()
    try:
        rows = db.list_accounts(include_inactive=True)
        for row in rows:
            if _row_holds_live_tokens(row, live):
                held.add(int(row["id"]))
        for session in db.get_active_sessions():
            account_id = session.get("account_id") if isinstance(session, dict) else None
            if isinstance(account_id, int) and not isinstance(account_id, bool):
                held.add(account_id)
    except Exception:
        logger.debug("live_holder_account_ids: holder discovery failed", exc_info=True)
        return None
    return held


def cc_rotation_block(account_id: int, db) -> str | None:
    """Why jacked must NOT exchange this row's Claude Code refresh token now.

    Invariant I2 fails closed: a Claude Code refresh token is single-use and
    shared with the running sessions, so the rotation is refused unless the
    active account is known AND it is not this row AND the live authority
    does not hold this row's tokens. Returns ``None`` when the rotation is
    allowed, else one of ``"active"``, ``"active_unknown"``,
    ``"live_session_holds"``. Reads the Keychain/file store; call it off
    the event loop.
    """
    active = read_active_account_id()
    if active is None:
        return "active_unknown"
    if active == account_id:
        return "active"
    held = live_holder_account_ids(db)
    if held is None:
        return "live_store_unreadable"
    if account_id in held:
        return "live_session_holds"
    return None


def read_live_credentials() -> dict | None:
    """Read the live Claude credential payload: Keychain first, then the file.

    Returns None when neither store holds a readable JSON object.
    """
    live = read_platform_credentials()
    if live:
        return live
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if cred_path.exists() and not cred_path.is_symlink():
        try:
            data = json.loads(cred_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None
    return None


def _platform_store_holds(live: dict) -> bool:
    """Whether ``live`` holds the tokens the platform store itself holds.

    On macOS the credentials file is a mirror that Claude Code never updates,
    so ``read_live_credentials`` returns a stale copy after a failed Keychain
    read. A decision that admits an OLDER payload (the dead-token override)
    must not act on that copy. Other platforms have no separate store: the
    file is the store.
    """
    if sys.platform != "darwin":
        return True
    store = read_platform_credentials()
    if not isinstance(store, dict):
        return False
    held = store.get("claudeAiOauth") or {}
    seen = live.get("claudeAiOauth") or {}
    return bool(held.get("accessToken")) and (
        held.get("accessToken"), held.get("refreshToken")
    ) == (seen.get("accessToken"), seen.get("refreshToken"))


# The row columns every Claude Code token import decision reads. An import
# that read the row, then did slow work (the ownership lookup), writes only
# while these still hold what it read (Database.update_account_if_unchanged),
# and otherwise decides again from the new row. Columns the decision did not
# read stay out: a keeper bookkeeping write must not veto an import, or the
# rotated refresh token is lost when the switch overwrites the live store.
CC_IMPORT_GUARD_COLS = (
    "cc_access_token",
    "cc_refresh_token",
    "cc_expires_at",
    "cc_refresh_failure_type",
    # Read to decide whether the pass-through fields changed (#174i).
    "cc_oauth_extra",
)
# Added when a dead-token verdict (cc_token_known_dead) decided the import:
# the keeper verdict and the primary token its fingerprint includes.
CC_VERDICT_GUARD_COLS = ("access_token", "keeper_state")
# Read-decide-write attempts before an import gives up on a busy row.
_IMPORT_ATTEMPTS = 3


def reconcile_credentials_from_live_store(account_id: int, db) -> bool:
    """Read live credentials for the active account and reconcile with DB.

    Before swapping away from an account, check if Claude Code rotated
    the refresh token during its session. If so, import the fresh token
    into our DB so we don't lose it.

    Called before an account switch moves the credential stores to another
    account.

    Returns False only when the row kept changing during every attempt: an
    owned rotation may then still be waiting in the live store, and a caller
    about to overwrite that store (an account switch, a launch) must not.
    Returns True in every other case, imported or not.

    SAFETY: Never imports cc_refresh_token when circuit breaker shows
    invalid_grant — the live token is Claude Code's active token and
    importing+exchanging it would destroy CC's session.
    """
    # Check cache first to avoid Keychain subprocess calls
    live = _get_cached_live_credentials(account_id)
    from_cache = bool(live)

    if not live:
        # Read live credentials (Keychain first, file fallback)
        live = read_live_credentials()
        # Cache the result (even if None would be returned early below)
        _cache_live_credentials(account_id, live)

    if not live:
        return True

    # Only reconcile if the credentials belong to the outgoing account
    if _stamp_of(live) != account_id:
        return True

    if not live.get("claudeAiOauth", {}).get("accessToken"):
        return True

    for attempt in range(_IMPORT_ATTEMPTS):
        # Read the row this decision is made from (again, after a lost
        # compare-and-set: the ownership verdict for this payload is cached
        # per token, so a retry sends no second profile request).
        account = db.get_account(account_id)
        if not account:
            return True
        decision = _reconcile_decision(account_id, account, live, from_cache)
        if decision is _RECONCILE_RETRY_FRESH:
            # The verdict is what admits this older payload, so act on a fresh
            # read, never on one cached up to 30 s ago.
            invalidate_live_cred_cache()
            return reconcile_credentials_from_live_store(account_id, db)
        if not decision:
            return True
        updates, guard_cols = decision
        # Compare-and-set against the columns this decision read: a
        # re-authentication or adoption that landed during the ownership
        # lookup wins, and the decision is made again from the new row.
        expected = {col: account.get(col) for col in guard_cols}
        if db.update_account_if_unchanged(account_id, expected, **updates):
            logger.info(
                "Account %d: reconciled %d credential field(s) from live store",
                account_id, len(updates),
            )
            _clear_keeper_backoff(account_id)
            return True
        logger.info(
            "Account %d: the row changed while reconciling; deciding again",
            account_id,
        )
    logger.warning(
        "Account %d: the row kept changing during %d reconcile attempts; the "
        "live credentials were not imported",
        account_id, _IMPORT_ATTEMPTS,
    )
    return False


# Sentinel: the decision needs a fresh (uncached) read of the live store.
_RECONCILE_RETRY_FRESH = object()


def _reconcile_decision(account_id: int, account: dict, live: dict, cached: bool):
    """``(updates, guard columns)`` to import ``live`` into ``account``, or a
    falsy value (nothing to import), or ``_RECONCILE_RETRY_FRESH``.

    Pure apart from the ownership lookup, which is cached per token.
    """
    oauth = live.get("claudeAiOauth", {})
    live_access = oauth.get("accessToken")
    live_refresh = oauth.get("refreshToken")
    live_expires = oauth.get("expiresAt")  # milliseconds

    # jacked's own primary-fallback publication is not a Claude Code token.
    if is_primary_publication(account, live_access):
        return None

    # The stamp alone does not prove ownership: Claude Code keeps jacked's
    # _jackedAccountId on its own writes (a /login to another account, or a
    # refresh by a session that holds another account), so a stamped payload
    # can carry a different account's tokens. Importing those would put one
    # account's tokens in another account's row.
    if not live_payload_belongs_to(account, live):
        logger.warning(
            "Account %d: live credentials carry its stamp but ownership is not "
            "proven; not importing them%s",
            account_id,
            (
                " (their refresh token is not one jacked holds, and a switch "
                "would overwrite it)"
                if live_refresh and live_refresh != account.get("cc_refresh_token")
                else ""
            ),
        )
        return None

    guard_cols = CC_IMPORT_GUARD_COLS
    # Never move a row backward: a stale payload (for example the file mirror
    # read after a failed Keychain read) must not replace newer tokens. Unless
    # the row's token is KNOWN dead: then an owned live token that merely
    # expires sooner is the one that works (#179i).
    row_expires = account.get("cc_expires_at") or 0
    if live_expires and row_expires:
        live_expires_s = int(live_expires / 1000) if live_expires > 1e12 else int(live_expires)
        if live_expires_s < row_expires:
            if not cc_token_known_dead(account):
                logger.info(
                    "Account %d: live credentials are older than the stored ones; "
                    "not importing them",
                    account_id,
                )
                return None
            if cached:
                return _RECONCILE_RETRY_FRESH
            if not _platform_store_holds(live):
                logger.info(
                    "Account %d: the older live credentials are not the "
                    "Keychain's own copy; not importing them",
                    account_id,
                )
                return None
            # The verdict decided this import, so it is guarded too.
            guard_cols = CC_IMPORT_GUARD_COLS + CC_VERDICT_GUARD_COLS

    db_cc_refresh = account.get("cc_refresh_token")

    updates = {}

    # Always import the latest access token
    if live_access != account.get("cc_access_token"):
        updates["cc_access_token"] = live_access

    # Import expiry (convert ms -> seconds)
    if live_expires:
        live_expires_s = int(live_expires / 1000) if live_expires > 1e12 else int(live_expires)
        if live_expires_s != account.get("cc_expires_at"):
            updates["cc_expires_at"] = live_expires_s

    # Reconcile refresh token — BUT skip if circuit breaker shows invalid_grant
    # Importing a live CC refresh token during invalid_grant would compete with
    # Claude Code for the single-use token, destroying CC's session.
    if live_refresh:
        # The Claude Code chain breaker guards this import. The primary
        # chain's breaker says nothing about the Claude Code lineage.
        if cc_chain_dead(account):
            logger.debug(
                "Account %d: skipping cc_refresh_token import (invalid_grant active)",
                account_id,
            )
        elif db_cc_refresh is None:
            # Recovery: our token was cleared but Claude Code has one
            logger.info(
                "Account %d: recovering cc_refresh_token from live credentials",
                account_id,
            )
            updates["cc_refresh_token"] = live_refresh
        elif live_refresh != db_cc_refresh:
            # Rotation: Claude Code got a new token
            logger.info(
                "Account %d: importing rotated cc_refresh_token from live credentials",
                account_id,
            )
            updates["cc_refresh_token"] = live_refresh

    # A newer access token replaces one the API refused, so that refusal no
    # longer applies. A dead lineage (invalid_grant) stays recorded.
    if (
        "cc_access_token" in updates
        and account.get("cc_refresh_failure_type") == "auth_rejected"
    ):
        updates["cc_refresh_failure_type"] = None
        updates["cc_refresh_last_failed_at"] = None

    # The pass-through fields (#174i) as Claude Code holds them now; it has
    # already applied its own keep-the-previous-value rule. Skipped when the
    # refresh lineage is dead, because they describe a refresh token jacked
    # does not take.
    if not cc_chain_dead(account):
        from jacked.credentials.oauth_extra import encode_oauth_extra, oauth_extra_from

        # Bound to the refresh token the row holds after this write.
        row_refresh = updates.get("cc_refresh_token", account.get("cc_refresh_token"))
        extra = encode_oauth_extra(oauth_extra_from(oauth), row_refresh)
        if extra != account.get("cc_oauth_extra"):
            updates["cc_oauth_extra"] = extra

    return (updates, guard_cols) if updates else None


def _clear_keeper_backoff(account_id: int) -> None:
    """Drop the window keeper's ping backoff after a token import."""
    try:
        from jacked.api.usage_monitor import clear_ping_backoff

        clear_ping_backoff(account_id)
    except Exception:  # noqa: BLE001 - never fail a reconcile over this
        logger.debug("Could not clear the keeper backoff", exc_info=True)


# Backward-compatible alias
reconcile_outgoing_credentials = reconcile_credentials_from_live_store

OUTGOING_ACCOUNT_BUSY_MESSAGE = (
    "The current account is being updated. Try again in a moment."
)


def reconcile_outgoing_before_activation(
    db, target_id: int, outgoing_id: int | None = None
) -> bool:
    """Save the active account's live tokens before another account's are
    written over them.

    Claude Code rotates refresh tokens and spends the old one, so the live
    store can hold the only working copy of the active account's chain.
    ``activate_account`` calls this on every path (switch, launch, OAuth).
    Returns False when the outgoing row kept changing: the caller must not
    overwrite the store yet. ``outgoing_id`` is the active account a caller
    already resolved; None resolves it here.
    """
    if outgoing_id is None:
        from jacked.api.usage_monitor import _read_active_account_id

        outgoing_id = _read_active_account_id()
    if not outgoing_id or outgoing_id == int(target_id):
        return True
    return reconcile_credentials_from_live_store(outgoing_id, db)


def write_platform_credentials(data: dict) -> bool:
    """Write credentials to the platform's native credential store.

    macOS: a Keychain generic-password entry written through the signed
    /usr/bin/security tool, addressed by the full service/account locator.
    Bytes are handed to the tool on stdin. A payload too large for the tool's
    stdin line limit goes in a subprocess argument instead, the same way
    Claude Code writes this item, so the bytes are briefly visible to other
    local processes. JACKED_KEYCHAIN_ARGV_FALLBACK=0 refuses that write.

    Linux/Windows: no-op (they use .credentials.json)

    Returns True if written successfully, False otherwise.

    >>> write_platform_credentials({}) if sys.platform != "darwin" else True
    True
    """
    if sys.platform != "darwin":
        return True  # no-op on non-macOS (file write is sufficient)
    from jacked.credentials.canonical import CredentialFormatError, CredentialPayload
    from jacked.credentials.macos_store import MacOSCredentialStore
    from jacked.credentials.models import InteractionMode, StoreStatus

    try:
        payload = CredentialPayload.from_mapping(data)
        result = MacOSCredentialStore(_get_keychain_username()).write(
            payload, InteractionMode.BACKGROUND
        )
        if result.status is StoreStatus.OK:
            return True
        logger.warning("Keychain write did not commit: %s", result.reason)
        return False
    except (CredentialFormatError, OSError, RuntimeError) as exc:
        logger.warning("Keychain write error: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Credential data builders
# ---------------------------------------------------------------------------


def build_oauth_data(account: dict) -> dict:
    """Build Claude Code credential format from account dict.

    Prefers CC (Claude Code) tokens when present and usable.
    Falls back to primary tokens when CC tokens don't exist yet.
    CRITICAL: The fallback path sets refreshToken to None to prevent
    Claude Code from consuming the primary refresh token.

    >>> build_oauth_data({"access_token": "at", "refresh_token": "rt", "expires_at": 100, "scopes": None, "subscription_type": "max", "rate_limit_tier": "t1"})["accessToken"]
    'at'
    >>> build_oauth_data({"access_token": "at", "refresh_token": "rt", "expires_at": 100, "scopes": None, "subscription_type": "max", "rate_limit_tier": "t1"})["refreshToken"] is None
    True
    >>> build_oauth_data({"access_token": "at", "refresh_token": "rt", "expires_at": 100, "scopes": None, "subscription_type": "max", "rate_limit_tier": "t1", "cc_access_token": "cc_at", "cc_refresh_token": "cc_rt", "cc_expires_at": 200})["accessToken"]
    'cc_at'
    >>> build_oauth_data({"access_token": "at", "expires_at": 100, "cc_access_token": "cc_at", "cc_refresh_token": "spent", "cc_expires_at": 200, "cc_refresh_failure_type": "invalid_grant"})["accessToken"]
    'at'
    """
    scopes = None
    raw_scopes = account.get("scopes")
    if raw_scopes:
        try:
            parsed = json.loads(raw_scopes)
            if isinstance(parsed, list):
                scopes = parsed
        except (json.JSONDecodeError, TypeError):
            pass

    # Prefer CC tokens when present AND usable
    cc_at = account.get("cc_access_token")
    cc_rt = account.get("cc_refresh_token")
    cc_exp = account.get("cc_expires_at")
    if cc_chain_dead(account):
        # A dead lineage never goes to Claude Code: its refresh token is
        # spent, and handing it out forces a re-login in every session.
        # A live access token recovered for the dead chain may still serve
        # until it expires, with no refresh token beside it.
        cc_rt = None

    if cc_at:
        # If CC token is expired AND no refresh token, fall through to primary
        if cc_exp is not None and cc_exp < time.time() and not cc_rt:
            pass  # Fall through — expired CC with no refresh is unusable
        else:
            from jacked.credentials.oauth_extra import parse_oauth_extra

            # The fields Claude Code wrote that jacked carries through (#174i),
            # only when known: never invented. refreshTokenExpiresAt describes
            # the refresh token beside it, so it goes only with one.
            # Read for the row's own refresh token: fields captured with an
            # earlier lineage are not this one's.
            extra = parse_oauth_extra(
                account.get("cc_oauth_extra"), account.get("cc_refresh_token")
            )
            if not cc_rt:
                extra.pop("refreshTokenExpiresAt", None)
            return {
                "accessToken": cc_at,
                "refreshToken": cc_rt,  # may be None pre-CC-auth
                "expiresAt": (cc_exp or 0) * 1000,
                "scopes": scopes,
                "subscriptionType": account.get("subscription_type"),
                "rateLimitTier": account.get("rate_limit_tier"),
                "organizationUuid": account.get("organization_uuid") or None,
                **extra,
            }

    # Fallback: primary tokens, but NEVER expose primary refresh_token.
    # If Claude Code consumed this refresh token, it would break jacked's
    # background refresh — the exact bug dual-token architecture prevents.
    return {
        "accessToken": account.get("access_token", ""),
        "refreshToken": None,  # NEVER expose primary refresh_token
        "expiresAt": account.get("expires_at", 0) * 1000,
        "scopes": scopes,
        "subscriptionType": account.get("subscription_type"),
        "rateLimitTier": account.get("rate_limit_tier"),
        "organizationUuid": account.get("organization_uuid") or None,
    }


# ---------------------------------------------------------------------------
# Unified credential write — the single path for all stores
# ---------------------------------------------------------------------------


def sync_credential_to_all_stores(
    account_id: int,
    account: dict,
    email: str = None,
    display_name: str = None,
) -> bool:
    """Write credential tokens to all stores (legacy compatibility path).

    No production code calls this wrapper; account switches go through
    ``CredentialTransactionEngine``. It returns an honest boolean and stops
    before file publication when the macOS authority cannot be updated. The
    identity step uses the same row builder and safe writer as the engine.

    Args:
        account_id: The account DB id
        account: Full account dict from DB (has access_token, refresh_token, etc.)
        email: Override email (defaults to account["email"])
        display_name: Kept for old callers. The published identity comes
            from the account row, so this value is not used.
    """
    email = email or account.get("email", "")
    oauth_data = build_oauth_data(account)

    cred_path = Path.home() / ".claude" / ".credentials.json"

    # On macOS the Keychain is the runtime authority. Never publish the file
    # target first and then claim success when the authority rejects it.
    kc_data = {"claudeAiOauth": oauth_data, "_jackedAccountId": account_id}
    if not write_platform_credentials(kc_data):
        logger.warning("Credential authority did not accept account %d", account_id)
        return False

    # 1. Write global .credentials.json
    try:
        existing = {}
        if cred_path.exists() and not cred_path.is_symlink():
            try:
                existing = json.loads(cred_path.read_text(encoding="utf-8"))
                if not isinstance(existing, dict):
                    existing = {}
            except (json.JSONDecodeError, OSError):
                existing = {}

        existing["claudeAiOauth"] = oauth_data
        existing["_jackedAccountId"] = account_id
        _write_credential_file(cred_path, existing)
        logger.info("Wrote global .credentials.json for account %d", account_id)
    except Exception as exc:
        logger.warning("Failed to write global .credentials.json: %s", exc)
        return False

    # Read back every declared compatibility surface before reporting success.
    from jacked.credentials.canonical import CredentialPayload
    from jacked.credentials.file_store import FileCredentialStore
    from jacked.credentials.models import CredentialIdentity, StoreStatus
    from jacked.credentials.resolver import (
        FileResolverSnapshotSink,
        ResolverState,
        SnapshotUpdate,
    )

    file_target = CredentialPayload.from_mapping(existing)
    file_observation = FileCredentialStore(
        cred_path, trusted_root=Path.home()
    ).read()
    if (
        file_observation.status is not StoreStatus.OK
        or file_observation.payload is None
        or file_observation.payload.digest != file_target.digest
    ):
        logger.warning("Credential file readback did not match account %d", account_id)
        return False
    evidence = ["authority:file:readback"]
    if sys.platform == "darwin":
        evidence.insert(0, "authority:keychain:readback")

    # 3. Publish the identity to ~/.claude.json from the account row, so the
    # block carries the stored profile and uuid like every engine switch.
    try:
        from jacked.api.claude_identity import build_identity_block, write_identity

        row = {**account, "id": account_id, "email": email}
        if (email or "").strip().casefold() != (
            account.get("email") or ""
        ).strip().casefold():
            # An email override names someone other than the row: neither
            # the row's uuid nor its stored profile belongs to it.
            row["account_uuid"] = None
            row["profile_json"] = None
        block = build_identity_block(row)
        if block is not None:
            write_identity(Path.home(), block)
    except Exception as exc:
        logger.warning(
            "Failed to update ~/.claude.json identity for account %d: %s",
            account_id,
            type(exc).__name__,
        )

    # 4. Write to per-account dir if it exists
    acct_dir = Path.home() / ".claude" / "accounts" / str(account_id)
    acct_cred = acct_dir / ".credentials.json"
    if acct_dir.exists() and acct_dir.is_dir():
        try:
            acct_existing = {}
            if acct_cred.exists() and not acct_cred.is_symlink():
                try:
                    acct_existing = json.loads(
                        acct_cred.read_text(encoding="utf-8")
                    )
                    if not isinstance(acct_existing, dict):
                        acct_existing = {}
                except (json.JSONDecodeError, OSError):
                    acct_existing = {}

            # Per-account files do NOT get _jackedAccountId stamp —
            # account_id is implicit from directory path
            acct_existing["claudeAiOauth"] = oauth_data
            _write_credential_file(acct_cred, acct_existing)
            logger.info(
                "Wrote per-account .credentials.json for account %d", account_id
            )
        except Exception as exc:
            logger.warning(
                "Failed to write per-account .credentials.json for %d: %s",
                account_id,
                exc,
            )

    # The active account (what the menu-bar pill tracks) just changed —
    # wake same-process watchers so the pill re-resolves immediately.
    from jacked import usage_events

    try:
        identity = CredentialIdentity(
            account_id=account_id,
            email=email,
            organization_id=account.get("organization_uuid") or None,
        )
        FileResolverSnapshotSink(cred_path.parent / "jacked-resolver-snapshot.json").publish(
            SnapshotUpdate(
                scope="global",
                state=ResolverState.RESOLVED,
                evidence=tuple(evidence) + ("compatibility:observed-target-unfenced",),
                credential_revision=None,
                desired=identity,
                observed=identity,
            )
        )
    except OSError as exc:
        logger.warning("Failed to publish credential resolver snapshot: %s", exc)
    usage_events.bump()
    return True
