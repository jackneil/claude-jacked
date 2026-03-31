"""Credential helpers: unified write path, atomic file I/O, keychain access.

All credential writes go through sync_credential_to_all_stores().
This is the ONLY module that writes to credential stores.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_keychain_username() -> str:
    """Get the system username for keychain account name.

    Claude Code uses process.env.USER || userInfo().username as the
    keychain account name (-a parameter). We must match this exactly
    or we write to a different keychain entry.
    """
    return os.environ.get("USER") or os.environ.get("USERNAME") or "Claude Code"


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


def update_claude_config_email(
    email: str,
    display_name: str = None,
    organization_uuid: str = None,
    organization_name: str = None,
):
    """Update or create ~/.claude.json with the active account's identity.

    Writes emailAddress, organizationUuid, and organizationName to the
    oauthAccount block.  If the file exists, preserves all other keys.
    Refuses to write through symlinks.  Logs on failure instead of raising.

    >>> update_claude_config_email("test@example.com")  # noqa: no side-effects in test env
    """
    claude_config = Path.home() / ".claude.json"

    if claude_config.is_symlink():
        logger.warning("Refusing to write ~/.claude.json — path is a symlink")
        return

    config: dict = {}
    if claude_config.exists():
        try:
            config = json.loads(claude_config.read_text(encoding="utf-8"))
            if not isinstance(config, dict):
                config = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read ~/.claude.json, creating fresh: %s", exc)
            config = {}

    if "oauthAccount" not in config:
        config["oauthAccount"] = {}
    config["oauthAccount"]["emailAddress"] = email
    if display_name and "displayName" not in config["oauthAccount"]:
        config["oauthAccount"]["displayName"] = display_name

    # Write organization identity to oauthAccount block
    if organization_uuid:
        config["oauthAccount"]["organizationUuid"] = organization_uuid
    elif "organizationUuid" in config["oauthAccount"]:
        del config["oauthAccount"]["organizationUuid"]
    if organization_name:
        config["oauthAccount"]["organizationName"] = organization_name
    elif "organizationName" in config["oauthAccount"]:
        del config["oauthAccount"]["organizationName"]

    try:
        fd, tmp = tempfile.mkstemp(
            dir=str(claude_config.parent),
            prefix=".claude_tmp_",
            suffix=".json",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2)
            try:
                os.chmod(tmp, 0o600)
            except OSError:
                pass
            _safe_replace(tmp, str(claude_config))
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except Exception as exc:
        logger.warning("Failed to write ~/.claude.json: %s", exc)


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
    try:
        username = _get_keychain_username()
        result = subprocess.run(
            ["security", "find-generic-password",
             "-a", username,
             "-s", "Claude Code-credentials", "-w"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
        if result.returncode != 0:
            logger.debug("Keychain read failed: %s", result.stderr.strip())
    except (json.JSONDecodeError, subprocess.SubprocessError, OSError) as exc:
        logger.debug("Keychain read error: %s", exc)
    return None


def read_fresh_active_token(account_id: int) -> str | None:
    """Read the current access token from credential stores for an account.

    After activation, Claude Code refreshes the access token on its own
    schedule, making the DB copy stale.  This reads the live token from
    the same stores Claude Code uses (Keychain first, then file).

    Returns the access token string, or None if the stores don't belong
    to this account or are unreadable.
    """
    # Try keychain first (same precedence as Claude Code on macOS)
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
        except (json.JSONDecodeError, OSError):
            pass

    return None


def write_platform_credentials(data: dict) -> bool:
    """Write credentials to the platform's native credential store.

    macOS: Keychain entry with service "Claude Code-credentials" and
    account name matching the system username.  Uses -X hex encoding
    to match Claude Code's format (avoids plaintext in process args,
    prevents CrowdStrike/process monitor exposure).

    Linux/Windows: no-op (they use .credentials.json)

    Returns True if written successfully, False otherwise.

    >>> write_platform_credentials({}) if sys.platform != "darwin" else True
    True
    """
    if sys.platform != "darwin":
        return True  # no-op on non-macOS (file write is sufficient)
    try:
        username = _get_keychain_username()
        json_data = json.dumps(data, separators=(",", ":"))
        hex_value = json_data.encode("utf-8").hex()

        # Clean up orphan keychain entry from old jacked versions that used
        # -a "Claude Code" instead of -a $USER.  Ignore errors (may not exist).
        if username != "Claude Code":
            subprocess.run(
                ["security", "delete-generic-password",
                 "-a", "Claude Code",
                 "-s", "Claude Code-credentials"],
                capture_output=True, timeout=5,
            )

        # Use -U (update-or-insert) with -X (hex value) to match CC's format.
        result = subprocess.run(
            ["security", "add-generic-password",
             "-U",
             "-a", username,
             "-s", "Claude Code-credentials",
             "-X", hex_value],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("Keychain write failed: %s", result.stderr.strip())
            return False
        return True
    except (subprocess.SubprocessError, OSError) as exc:
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
) -> None:
    """Write credential tokens to all stores atomically.

    This is the ONLY function that writes credentials.  Called from:
    - OAuth _complete_auth() after _store_account()
    - "Set Active" / use_account endpoint
    - Per-account dir preparation (launch.py)

    Writes to: global .credentials.json, macOS Keychain, ~/.claude.json,
    and per-account dir if it exists.  Each step is independent — failures
    are logged but don't prevent subsequent writes.

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

    # 2. Write to macOS Keychain (no-op on Linux/Windows)
    try:
        kc_data = {"claudeAiOauth": oauth_data, "_jackedAccountId": account_id}
        write_platform_credentials(kc_data)
    except Exception as exc:
        logger.warning("Failed to write keychain credentials: %s", exc)

    # 3. Update ~/.claude.json with email + org identity
    try:
        update_claude_config_email(
            email,
            display_name,
            organization_uuid=account.get("organization_uuid") or None,
            organization_name=account.get("organization_name"),
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
