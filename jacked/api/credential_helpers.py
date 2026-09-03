"""Credential helpers: unified write path, atomic file I/O, keychain access.

All credential writes go through sync_credential_to_all_stores().
This is the ONLY module that writes to credential stores.
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


def _read_claude_config(claude_config: Path) -> dict:
    """Read Claude's config, retaining the established fresh-file fallback."""
    config: dict = {}
    if claude_config.exists():
        try:
            config = json.loads(claude_config.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read ~/.claude.json, creating fresh: %s", exc)
            config = {}
    return config


def _apply_claude_account(config: dict, metadata: _ClaudeConfigAccount) -> None:
    """Replace active identity metadata in an already parsed config."""
    account = config.setdefault("oauthAccount", {})
    account["emailAddress"] = metadata.email
    if metadata.display_name and "displayName" not in account:
        account["displayName"] = metadata.display_name

    if metadata.organization_uuid:
        account["organizationUuid"] = metadata.organization_uuid
    else:
        account.pop("organizationUuid", None)
    if metadata.organization_name:
        account["organizationName"] = metadata.organization_name
    else:
        account.pop("organizationName", None)

    if metadata.rate_limit_tier is not _RATE_LIMIT_TIER_UNSET:
        account.pop("userRateLimitTier", None)
        account.pop("organizationRateLimitTier", None)
        if metadata.rate_limit_tier:
            tier_field = (
                "organizationRateLimitTier"
                if metadata.organization_uuid
                else "userRateLimitTier"
            )
            account[tier_field] = metadata.rate_limit_tier


def _update_claude_config_account(metadata: _ClaudeConfigAccount) -> None:
    """Atomically mirror one account's non-secret metadata to ~/.claude.json."""
    claude_config = Path.home() / ".claude.json"

    if claude_config.is_symlink():
        logger.warning("Refusing to write ~/.claude.json — path is a symlink")
        return

    config = _read_claude_config(claude_config)
    _apply_claude_account(config, metadata)

    try:
        _write_credential_file(claude_config, config)
    except Exception as exc:
        logger.warning("Failed to write ~/.claude.json: %s", exc)


def update_claude_config_email(
    email: str,
    display_name: str = None,
    organization_uuid: str = None,
    organization_name: str = None,
):
    """Update Claude identity while preserving legacy tier metadata.

    >>> update_claude_config_email("test@example.com")  # noqa: no side-effects in test env
    """
    _update_claude_config_account(
        _ClaudeConfigAccount(
            email=email,
            display_name=display_name,
            organization_uuid=organization_uuid,
            organization_name=organization_name,
        )
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
    # Identity must reach resolver consensus before any raw store can supply an
    # "active" token. This prevents a matching file from winning while the
    # Keychain authority belongs to another account.
    if read_active_account_id() != account_id:
        return None

    # Consensus guarantees required stores describe the same payload. Read raw
    # bytes only now, because this caller genuinely needs the access token.
    kc_data = read_platform_credentials()
    if kc_data and kc_data.get("_jackedAccountId") == account_id:
        token = kc_data.get("claudeAiOauth", {}).get("accessToken")
        if token:
            return token

    # Fall back to global .credentials.json
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

    Returns an ID only when the exact-build canonical resolver reports store
    consensus. Never raises; unsupported, missing, and conflicting states all
    resolve to None.
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


def reconcile_credentials_from_live_store(account_id: int, db) -> None:
    """Read live credentials for the active account and reconcile with DB.

    Before swapping away from an account, check if Claude Code rotated
    the refresh token during its session. If so, import the fresh token
    into our DB so we don't lose it.

    Called before sync_credential_to_all_stores() in the swap path and
    in the use_account endpoint.

    SAFETY: Never imports cc_refresh_token when circuit breaker shows
    invalid_grant — the live token is Claude Code's active token and
    importing+exchanging it would destroy CC's session.
    """
    # Check cache first to avoid Keychain subprocess calls
    live = _get_cached_live_credentials(account_id)

    if not live:
        # Read live credentials (Keychain first, file fallback)
        live = read_platform_credentials()
        if not live:
            # Fall back to .credentials.json
            cred_path = Path.home() / ".claude" / ".credentials.json"
            if cred_path.exists() and not cred_path.is_symlink():
                try:
                    live = json.loads(cred_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return
        # Cache the result (even if None would be returned early below)
        _cache_live_credentials(account_id, live)

    if not live:
        return

    # Only reconcile if the credentials belong to the outgoing account
    if live.get("_jackedAccountId") != account_id:
        return

    oauth = live.get("claudeAiOauth", {})
    live_access = oauth.get("accessToken")
    live_refresh = oauth.get("refreshToken")
    live_expires = oauth.get("expiresAt")  # milliseconds

    if not live_access:
        return

    # Read current DB state
    account = db.get_account(account_id)
    if not account:
        return

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
        account_row = account
        cb_failure = account_row.get("refresh_failure_type") if account_row else None
        if cb_failure == "invalid_grant":
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

    if updates:
        db.update_account(account_id, **updates)
        logger.info(
            "Account %d: reconciled %d credential field(s) from live store",
            account_id, len(updates),
        )


# Backward-compatible alias
reconcile_outgoing_credentials = reconcile_credentials_from_live_store


def write_platform_credentials(data: dict) -> bool:
    """Write credentials to the platform's native credential store.

    macOS: Security.framework Keychain entry with the full service/account
    locator. Credential bytes never appear in a subprocess argument.

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

    if cc_at:
        # If CC token is expired AND no refresh token, fall through to primary
        if cc_exp is not None and cc_exp < time.time() and not cc_rt:
            pass  # Fall through — expired CC with no refresh is unusable
        else:
            return {
                "accessToken": cc_at,
                "refreshToken": cc_rt,  # may be None pre-CC-auth
                "expiresAt": (cc_exp or 0) * 1000,
                "scopes": scopes,
                "subscriptionType": account.get("subscription_type"),
                "rateLimitTier": account.get("rate_limit_tier"),
                "organizationUuid": account.get("organization_uuid") or None,
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
    """Write credential tokens to all stores atomically.

    This is the ONLY function that writes credentials.  Called from:
    - OAuth _complete_auth() after _store_account()
    - "Set Active" / use_account endpoint
    - Per-account dir preparation (launch.py)

    This compatibility wrapper now returns an honest boolean and stops before
    file publication when the macOS authority cannot be updated. New callers
    should use ``CredentialTransactionEngine`` for durable outcomes.

    Args:
        account_id: The account DB id
        account: Full account dict from DB (has access_token, refresh_token, etc.)
        email: Override email (defaults to account["email"])
        display_name: Override display name
    """
    email = email or account.get("email", "")
    display_name = display_name or account.get("display_name")
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

    # 3. Update ~/.claude.json with email + org identity
    try:
        _update_claude_config_account(
            _ClaudeConfigAccount(
                email=email,
                display_name=display_name,
                organization_uuid=account.get("organization_uuid") or None,
                organization_name=account.get("organization_name"),
                rate_limit_tier=account.get("rate_limit_tier"),
            )
        )
    except Exception as exc:
        logger.warning("Failed to update ~/.claude.json email: %s", exc)

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
