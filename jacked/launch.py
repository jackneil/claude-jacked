"""Launch Claude Code with evidence-qualified account selection.

CLAUDE_CONFIG_DIR still separates configuration and local credential inputs.
Where the certified topology puts the authority outside that directory (the
macOS Keychain), launch activation explicitly updates and verifies the global
authority first; where the credential file itself is the authority, the launch
directory is what Claude reads and the global file is left alone.

Directory structure:
    ~/.claude/accounts/<account_id>/.credentials.json
"""

import asyncio
import json
import logging
import os
import shutil
import subprocess
import tempfile
import threading
import time as _time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import click

from jacked.findbin import find_bin
from jacked.api.credential_helpers import (
    _malformed_expiry,
    _safe_replace,
    build_oauth_data,
    cc_token_known_dead,
    clear_ownership_cache,
    forget_ownership_verdicts,
    ownership_refusal_is_final,
    expiry_seconds,
    is_primary_publication,
    live_payload_ownership,
)
from jacked.credentials.refresh_guard import (
    CC_REAUTH_REQUIRED_CODE,
    refuses_refresh_downgrade,
)
from jacked.credentials.runtime import scoped_launch_needs_global_activation
from jacked.launch_sync_writes import sync_held_access, write_rotation
from jacked.web.auth import should_refresh, should_refresh_cc
from jacked.web.database import Database

logger = logging.getLogger(__name__)

ACCOUNTS_DIR = Path.home() / ".claude" / "accounts"


def _activate_launch_credentials(account: dict, db: Database):
    """Activate the certified credential authority before starting Claude.

    Kept as a small boundary so launch tests can prove behavior without ever
    touching a developer's real Keychain.
    """
    from jacked.credentials.models import SwitchContext
    from jacked.credentials.runtime import activate_account

    return activate_account(
        db,
        account,
        SwitchContext.LAUNCH,
        f"launch-{uuid.uuid4().hex}",
    )

# Keys safe to copy from global .claude.json into per-account dirs.
# Excludes identity (userID, anonymousId, oauthAccount), project permissions,
# and all internal caches — those must never leak across accounts.
_SAFE_CONFIG_KEYS = frozenset({
    "autoUpdates", "autoUpdatesProtectedForNative", "showSpinnerTree",
    "claudeInChromeDefaultEnabled", "penguinModeOrgEnabled",
    "hasCompletedOnboarding", "lastOnboardingVersion", "hasSeenTasksHint",
    "hasCompletedClaudeInChromeOnboarding", "effortCalloutDismissed",
    "opusProMigrationComplete", "sonnet1m45MigrationComplete",
    "officialMarketplaceAutoInstallAttempted", "officialMarketplaceAutoInstalled",
    "lastReleaseNotesSeen", "installMethod",
})


def _seed_claude_config(config_dir: Path) -> None:
    """Seed per-account .claude.json with safe global settings.

    Only runs when .claude.json is missing or incomplete (no hasCompletedOnboarding).
    Copies only UX/onboarding keys — never identity, analytics, or project data.
    Fails closed: only an ABSENT file starts from {}. A file that cannot be
    read or parsed, or that is not a JSON object, is never rewritten.
    """
    claude_json = config_dir / ".claude.json"

    if claude_json.is_symlink():
        logger.warning("Per-account .claude.json is a symlink — skipping seed")
        return

    # Read existing per-account config (may not exist yet)
    local: dict = {}
    try:
        local = json.loads(claude_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        local = {}  # Expected on first launch
    except (ValueError, OSError) as exc:
        logger.warning(
            "Per-account .claude.json for %s cannot be read (%s); not seeding it",
            config_dir.name,
            type(exc).__name__,
        )
        return
    if not isinstance(local, dict):
        logger.warning(
            "Per-account .claude.json for %s is not a JSON object; not seeding it",
            config_dir.name,
        )
        return

    if local.get("hasCompletedOnboarding"):
        return  # Already seeded — don't overwrite per-session changes

    # Read global config
    global_config = Path.home() / ".claude.json"
    if not global_config.is_file() or global_config.is_symlink():
        return
    try:
        source = json.loads(global_config.read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            return
    except (json.JSONDecodeError, OSError) as exc:
        logger.debug("Failed to read global .claude.json: %s", exc)
        return

    for key in _SAFE_CONFIG_KEYS:
        if key in source:
            local[key] = source[key]

    # Atomic write (matches the _seed_workspace_trust pattern)
    fd, tmp = tempfile.mkstemp(
        dir=str(config_dir), prefix=".claude_json_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(local, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(tmp, str(claude_json))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.debug("Failed to write per-account .claude.json")


def _seed_workspace_trust(config_dir: Path) -> None:
    """Copy workspace trust records from global config into per-account config.

    Unlike _seed_claude_config (which runs once), this runs every launch so
    newly trusted workspaces propagate to per-account dirs.  Only copies
    hasTrustDialogAccepted and hasCompletedProjectOnboarding — not allowedTools,
    MCP servers, or cost data.  Skips project paths that already exist in the
    per-account config (non-destructive).
    """
    claude_json = config_dir / ".claude.json"
    if claude_json.is_symlink():
        return

    global_config = Path.home() / ".claude.json"
    if not global_config.is_file() or global_config.is_symlink():
        return

    try:
        source = json.loads(global_config.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return
    global_projects = source.get("projects") if isinstance(source, dict) else None
    if not isinstance(global_projects, dict):
        return

    # Read per-account config. Fail closed: only an ABSENT file starts from
    # {}; never clobber a file we cannot parse or that is not an object.
    local: dict = {}
    try:
        local = json.loads(claude_json.read_text(encoding="utf-8"))
    except FileNotFoundError:
        local = {}
    except (ValueError, OSError) as exc:
        logger.debug(
            "Cannot parse per-account .claude.json for trust seeding (%s)",
            type(exc).__name__,
        )
        return
    if not isinstance(local, dict):
        logger.debug("Per-account .claude.json is not a JSON object; trust not seeded")
        return
    if not isinstance(local.get("projects", {}), dict):
        logger.debug("Per-account .claude.json projects is not an object; trust not seeded")
        return

    local_projects = local.setdefault("projects", {})
    changed = False

    for path, entry in global_projects.items():
        if not isinstance(entry, dict):
            continue
        if not entry.get("hasTrustDialogAccepted"):
            continue
        # Don't overwrite existing per-account project entries
        if path in local_projects:
            continue
        minimal = {"hasTrustDialogAccepted": True}
        if entry.get("hasCompletedProjectOnboarding"):
            minimal["hasCompletedProjectOnboarding"] = True
        local_projects[path] = minimal
        changed = True

    if not changed:
        return

    # Atomic write
    fd, tmp = tempfile.mkstemp(
        dir=str(config_dir), prefix=".claude_json_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(local, f, indent=2)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(tmp, str(claude_json))
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        logger.debug("Failed to write workspace trust to %s", claude_json)


def _seed_oauth_account(config_dir: Path, account: dict, db=None) -> None:
    """Seed the account identity into the per-account .claude.json.

    The block comes from ``claude_identity.build_identity_block``, the same
    builder as every global switch, so it carries the stored profile and
    the account uuid when jacked knows them. When the existing block is the
    same account (same email, org and uuid), the write merges and keeps the
    fields that Claude Code wrote. Otherwise the write replaces the block.
    If Claude Code signs in as a different Claude account inside this
    per-account dir, the next launch replaces that identity. The block is
    not proof of token ownership: _sync_tokens_from_file() uses the shared
    ownership rule in credential_helpers.

    A symlinked or malformed .claude.json is never written.
    """
    if not account.get("email"):
        return

    from jacked.api.claude_identity import build_identity_block, write_identity

    block = build_identity_block(account, db=db)
    if block is None:
        return
    result = write_identity(config_dir, block, scoped=True)
    if not result.ok:
        logger.debug(
            "Account %s: per-account identity not seeded (%s)",
            account.get("id"),
            result.status,
        )


# Shared resources to symlink from global ~/.claude/ into per-account dirs.
_SHARED_SYMLINKS_FILES = ("settings.json", "CLAUDE.md")
_SHARED_SYMLINKS_DIRS = ("plugins", "agents", "commands", "skills", "projects")


def _ensure_shared_symlinks(config_dir: Path) -> None:
    """Symlink shared resources from global ~/.claude/ into per-account dir.

    Skips resources that don't exist globally or already have correct symlinks.
    On Windows, falls back to directory junctions if symlink fails.
    """
    global_dir = Path.home() / ".claude"

    for name in _SHARED_SYMLINKS_FILES + _SHARED_SYMLINKS_DIRS:
        source = global_dir / name
        target = config_dir / name

        if not source.exists():
            continue

        # Already a correct symlink — skip
        if target.is_symlink():
            try:
                if target.resolve() == source.resolve():
                    continue
            except OSError:
                pass
            # Wrong target — remove and recreate
            target.unlink()

        # Non-symlink file/dir exists (Claude Code created it) — back up
        # and replace with symlink so global resources are always used.
        if target.exists():
            backup = target.with_suffix(target.suffix + ".bak")
            try:
                if backup.exists():
                    # Already backed up before — just remove the stale copy
                    if target.is_dir():
                        shutil.rmtree(target)
                    else:
                        target.unlink()
                else:
                    target.rename(backup)
            except OSError:
                logger.warning("Failed to replace %s with symlink (non-fatal)", name)
                continue

        is_dir = source.is_dir()
        try:
            os.symlink(source, target, target_is_directory=is_dir)
        except OSError:
            if is_dir and os.name == "nt":
                try:
                    from jacked.winproc import NO_WINDOW
                    subprocess.run(
                        ["cmd", "/c", "mklink", "/J", str(target), str(source)],
                        capture_output=True, timeout=5,
                        creationflags=NO_WINDOW,
                    )
                except Exception:
                    logger.warning("Failed to create junction for %s", name)
            else:
                logger.warning("Failed to symlink %s (non-fatal)", name)


def _authority_keeps_own_lineage(activation, account_id: int) -> bool:
    """True when the activation refused only to put a null refresh token over
    this account's own live one, and the authority still names the account.

    >>> from types import SimpleNamespace as NS
    >>> from jacked.credentials.models import CredentialIdentity, SwitchOutcome
    >>> refused = NS(outcome=SwitchOutcome.UNUSABLE, code="CC_REAUTH_REQUIRED",
    ...              observed_identity=CredentialIdentity(account_id=3))
    >>> _authority_keeps_own_lineage(refused, 3), _authority_keeps_own_lineage(refused, 4)
    (True, False)
    """
    from jacked.credentials.models import SwitchOutcome

    return (
        activation.outcome is SwitchOutcome.UNUSABLE
        and activation.code == CC_REAUTH_REQUIRED_CODE
        and activation.observed_identity.account_id == account_id
    )


def _activate_global_authority(account: dict, db: Database, account_id: int):
    """Switch the global credential authority and report what was observed.

    On macOS the Keychain outranks CLAUDE_CONFIG_DIR, so a scoped launch must
    also switch the global authority. Unknown builds fail closed here. One
    refusal lets the launch go on: the authority already holds this
    account's own live refresh token, and the row would replace it with null
    (CC_REAUTH_REQUIRED). Claude then starts with those tokens.

    >>> # Tested via test_launch.py and test_refresh_downgrade_guard.py
    """
    from jacked.credentials.models import SwitchOutcome

    activation = _activate_launch_credentials(account, db)
    if _authority_keeps_own_lineage(activation, account_id):
        # The engine refused only to put a null refresh token over this
        # account's own live one (#149i): the authority already holds the
        # account's working tokens, so Claude starts with them.
        click.echo(
            f"Note: account {account_id}: {activation.message}. Claude starts "
            "with the current credentials of this account.",
            err=True,
        )
        evidence = (
            "launch:scoped-file-readback",
            "launch:global-authority:live-refresh-kept",
        )
        return activation.observed_identity, evidence, f"switch:{activation.operation_id}"
    truthful_outcomes = {
        SwitchOutcome.COMMITTED,
        SwitchOutcome.COMMITTED_DEGRADED,
        SwitchOutcome.OBSERVED_TARGET_UNFENCED,
    }
    if (
        activation.outcome not in truthful_outcomes
        or activation.observed_identity.account_id != account_id
    ):
        reason = activation.message or activation.outcome.value.replace("_", " ")
        raise click.ClickException(
            f"Could not establish Claude credentials for account {account_id}: {reason}"
        )
    click.echo(
        "Note: this certified Claude build uses the global credential authority. "
        "Launching this account also changes the default for future Claude "
        "sessions; existing sessions keep their current credentials.",
        err=True,
    )
    evidence = (
        "launch:scoped-file-readback",
        f"launch:global-authority:{activation.outcome.value}",
    )
    return activation.observed_identity, evidence, f"switch:{activation.operation_id}"


def _read_back_scoped_credentials(
    account: dict,
    config_dir: Path,
    account_id: int,
    *,
    written_oauth: dict | None = None,
    kept_rotation: bool = False,
):
    """Prove the launch directory holds the credentials the launch wrote.

    "Observed" must mean read back. The scoped file was just written; read it
    through the strict store and compare the OAuth object to what the launch
    wrote (``written_oauth``: the row's pair from build_oauth_data, or the
    file's own rotation when a deferred rescue kept it) before claiming
    anything. The scoped file is unstamped by design, so only the
    organization comes from the readback. A kept rotation is unproven, and
    the evidence says so.
    """
    from jacked.credentials.file_store import FileCredentialStore
    from jacked.credentials.models import CredentialIdentity

    click.echo(
        "Note: this platform reads credentials from the launch directory; "
        "the default account for other Claude sessions is unchanged.",
        err=True,
    )
    readback = FileCredentialStore(
        config_dir / ".credentials.json", trusted_root=config_dir
    ).read()
    written = readback.payload.to_mapping() if readback.payload is not None else None
    expected = written_oauth if written_oauth is not None else build_oauth_data(account)
    if written is None or written.get("claudeAiOauth") != expected:
        raise click.ClickException(
            f"Credentials for account {account_id} did not read back from the "
            f"launch directory: {readback.reason or 'content mismatch'}"
        )
    observed = CredentialIdentity(
        account_id=account_id,
        email=account.get("email"),
        organization_id=readback.payload.identity.organization_id,
    )
    evidence = ("launch:scoped-file-readback", "launch:global-authority:skipped")
    if kept_rotation:
        evidence += ("launch:scoped-file:unchecked-rotation-kept",)
    return observed, evidence, f"launch:{uuid.uuid4()}"


def _publish_launch_credentials(
    account: dict,
    db: Database,
    config_dir: Path,
    account_id: int,
    *,
    written_oauth: dict | None = None,
    kept_rotation: bool = False,
) -> None:
    """Establish the credentials Claude will read, then publish the evidence.

    The token-free observation is written beside the launch directory for
    session hooks. It is labelled global on purpose: the scoped file is a
    launch input, not the certified credential authority. ``written_oauth``
    is the OAuth object the launch wrote to the scoped file, which the
    file-authority readback verifies; ``kept_rotation`` says that object is
    the file's own unchecked rotation, not the row's pair.
    """
    from jacked.credentials.models import CredentialIdentity
    from jacked.credentials.resolver import (
        FileResolverSnapshotSink,
        ResolverState,
        SnapshotUpdate,
    )

    if scoped_launch_needs_global_activation():
        observed, evidence, revision = _activate_global_authority(
            account, db, account_id
        )
    else:
        observed, evidence, revision = _read_back_scoped_credentials(
            account,
            config_dir,
            account_id,
            written_oauth=written_oauth,
            kept_rotation=kept_rotation,
        )
    try:
        FileResolverSnapshotSink(config_dir / "jacked-resolver-snapshot.json").publish(
            SnapshotUpdate(
                scope="global",
                state=ResolverState.RESOLVED,
                evidence=evidence,
                credential_revision=revision,
                desired=CredentialIdentity(
                    account_id=account_id,
                    email=account.get("email"),
                    organization_id=account.get("organization_uuid") or None,
                ),
                observed=observed,
            )
        )
    except OSError as exc:
        logger.warning(
            "Failed to publish launch credential evidence for account %d: %s",
            account_id,
            exc,
        )


def _keeps_scoped_lineage(account: dict, existing: dict) -> bool:
    """True when the row's pair would put a null refresh token over the
    per-account file's live one of this account (refresh_guard, #149i).

    The file is unstamped, so the shared ownership rule does not require a
    stamp. A refusal is logged once with the row id and no token.

    >>> row = {"id": 4, "cc_access_token": "at", "cc_refresh_token": None,
    ...        "cc_expires_at": 9999999999, "cc_refresh_failure_type": "invalid_grant"}
    >>> _keeps_scoped_lineage(row, {"claudeAiOauth": {"accessToken": "at", "refreshToken": "rt"}})
    True
    >>> _keeps_scoped_lineage(row, {})
    False
    """
    reason = refuses_refresh_downgrade(build_oauth_data(account), existing, account)
    if reason is None:
        return False
    logger.warning("Account %d: %s", int(account["id"]), reason)
    return True


def prepare_account_dir(account: dict, db: Database) -> Path:
    """Create account config and verify its certified credential authority.

    Returns the directory path (for use as CLAUDE_CONFIG_DIR).

    Before any refresh, the function copies a rotation that the last session
    left in the per-account .credentials.json into the row, when the launch
    token sync proves it (#158i). A refresh would spend the refresh token of
    the row, which Claude Code already spent when it rotated, and the write
    below would replace the rotated pair. A refused or deferred sync changes
    nothing, and the write below replaces the file, as before.

    Limit: the profile request can prove only an access token that is not
    expired. A rotation whose access token expired before this launch stays
    unproven: the sync defers, the file keeps it, and the pre-launch Claude
    Code refresh is skipped, because that rotation may have spent the row's
    refresh token. A pair an earlier session proved to belong to another
    account is remembered beside the file by its digests and is replaced
    without a request, expired or not.

    The write never puts a null refresh token over a live one of this
    account (#149i). When the row publishes no refresh token (a dead chain)
    and the file holds the account's own working one, the launch keeps the
    file's tokens, logs why, and starts.

    >>> # Tested via test_launch.py, test_launch_token_sync.py and test_refresh_downgrade_guard.py
    """
    account_id = account["id"]
    if account_id <= 0:
        raise click.ClickException(f"Invalid account ID: {account_id}")
    # Defense-in-depth: never write a Codex account into the Claude credential
    # stores / launch it as Claude Code (resolve_account already blocks the CLI
    # path; this guards any other caller).
    if (account.get("provider") or "claude") == "codex":
        raise click.ClickException(
            f"Account {account_id} is a Codex account. Use `jacked codex launch {account_id}`."
        )

    config_dir = ACCOUNTS_DIR / str(account_id)

    # Refuse redirects in the credential path (defense-in-depth). These checks
    # come first, because the token sync below reads the directory.
    # is_symlink() uses lstat and also catches a broken link. Checking
    # exists() first misses broken Windows links and lets mkdir fail outside
    # the intended, user-facing refusal path.
    if config_dir.is_symlink():
        raise click.ClickException(
            f"Account dir is a symlink; refusing to use: {config_dir}"
        )
    if os.name == "nt":
        from jacked.service.windows_state import (
            reject_windows_reparse_ancestors,
        )

        try:
            reject_windows_reparse_ancestors(config_dir)
        except (OSError, ValueError) as exc:
            raise click.ClickException(
                "Account dir path contains an unsafe Windows reparse point; "
                f"refusing to use: {config_dir}"
            ) from exc

    # A rotation that the exit-time sync of the last session could not prove
    # (a failed profile request) goes into the row before a refresh spends
    # the row's refresh token. The sync proves ownership before it writes.
    # It reads the database file with raw sqlite3, so it runs only when the
    # database has a file (not ":memory:", and not a stand-in object).
    db_file = getattr(db, "db_path", None)
    rescue = "unchanged"
    if isinstance(db_file, (str, Path)) and str(db_file) != ":memory:":
        rescue = _sync_tokens_from_file(config_dir, str(db_file))
        # Re-read the row whatever the sync answered: a sync that lost its
        # compare-and-swap ("unchanged") lost it to a writer that stored
        # NEWER tokens, and those, not this function's earlier snapshot,
        # are what the scoped file must carry.
        account = db.get_account(account_id)
        if not account:
            raise click.ClickException(
                f"Account {account_id} disappeared after the token sync"
            )

    # Refresh primary token if near-expiry (refresh_account_token is async)
    if should_refresh(account):
        from jacked.web.auth import refresh_account_token

        try:
            asyncio.run(refresh_account_token(account_id, db))
        except Exception as exc:
            logger.warning("Pre-launch token refresh failed: %s", exc)
        # Re-read account to get fresh tokens
        account = db.get_account(account_id)
        if not account:
            raise click.ClickException(f"Account {account_id} disappeared after refresh")

    # Refresh CC token if near-expiry (independent from primary).
    # Never rotate a Claude Code refresh token a running session holds:
    # the active account's, any row the live authority or the session
    # tracker names, and nothing at all when the active account is unknown
    # (invariant I2 fails closed; the same gate as the background loops).
    # See docs/architecture/oauth-and-credential-flows.md §7.2.
    file_pair_differs = _scoped_pair_differs(config_dir, account)
    if rescue == "deferred" and file_pair_differs:
        # The file holds a rotation jacked could not prove. The row's refresh
        # token may already be spent by that rotation: exchanging it would
        # only earn an invalid_grant. Claude Code refreshes from the file.
        logger.warning(
            "Account %d: skipping the pre-launch Claude Code refresh; the "
            "per-account file holds an unchecked rotation",
            account_id,
        )
    elif should_refresh_cc(account):
        from jacked.api.credential_helpers import cc_rotation_block

        block = cc_rotation_block(account_id, db)
        if block is None:
            from jacked.web.auth import refresh_cc_token

            try:
                asyncio.run(refresh_cc_token(account_id, db))
            except Exception as exc:
                logger.warning("Pre-launch CC token refresh failed: %s", exc)
            account = db.get_account(account_id)
            if not account:
                raise click.ClickException(f"Account {account_id} disappeared after CC refresh")
        elif block == "active":
            # Reconcile live creds into DB before the downstream Keychain
            # write so we don't roll back Claude Code's fresh token
            # (architecture doc §7.2 — launch.py:489 stale write gap).
            try:
                from jacked.api.credential_helpers import reconcile_credentials_from_live_store
                reconciled = reconcile_credentials_from_live_store(account_id, db)
                account = db.get_account(account_id) or account
            except Exception:
                reconciled = True
                logger.debug("Pre-launch reconcile failed", exc_info=True)
            if reconciled is False:
                # Claude Code's latest tokens for this account may not be saved
                # yet; the launch's credential write would overwrite them.
                raise click.ClickException(
                    f"Account {account_id} is being updated right now. Try again."
                )
            logger.info(
                "Account %d: skipping pre-launch CC refresh (account is the "
                "active Claude Code session — Claude Code manages its own "
                "token rotation).",
                account_id,
            )
        else:
            logger.info(
                "Account %d: skipping pre-launch CC refresh (%s)", account_id, block
            )

    # Pre-launch CC token warnings
    cc_at = account.get("cc_access_token")
    cc_rt = account.get("cc_refresh_token")
    cc_exp = account.get("cc_expires_at")
    if not cc_at:
        logger.warning(
            "Account %d: No CC token — using primary token (non-refreshable). "
            "Authorize CC via dashboard for stable sessions.",
            account_id,
        )
    elif cc_exp and cc_exp < _time.time() and not cc_rt:
        logger.warning(
            "Account %d: CC token expired and no refresh token — Claude Code "
            "session may fail. Re-authorize CC via dashboard.",
            account_id,
        )

    # Create dir with user-only permissions
    config_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(str(config_dir), 0o700)
    except OSError:
        pass

    # Seed .claude.json from global config to skip first-run setup screens
    _seed_claude_config(config_dir)
    # Propagate workspace trust (runs every launch, not gated by onboarding)
    _seed_workspace_trust(config_dir)
    # Seed oauthAccount identity so Claude Code doesn't open browser to verify
    _seed_oauth_account(config_dir, account, db=db)
    # Symlink shared resources (settings, plugins, etc.) from global dir
    _ensure_shared_symlinks(config_dir)

    cred_path = config_dir / ".credentials.json"

    # Refuse symlinks (is_symlink uses lstat — catches broken symlinks too)
    if cred_path.is_symlink():
        raise click.ClickException(
            f"Credential path is a symlink. Refusing to write: {cred_path}"
        )

    # Read existing file to preserve other keys (Claude Code may have added data)
    existing = {}
    if cred_path.exists():
        try:
            existing = json.loads(cred_path.read_text(encoding="utf-8"))
            if not isinstance(existing, dict):
                existing = {}
        except (json.JSONDecodeError, OSError):
            existing = {}

    kept_rotation = rescue == "deferred" and isinstance(existing.get("claudeAiOauth"), dict)
    if kept_rotation:
        # The file's tokens could not be checked (the profile request failed):
        # a refused token is overwritten, an unchecked one is kept. The row's
        # spent lineage over the only live rotation would be the real loss.
        logger.warning(
            "Account %d: the per-account file holds tokens whose ownership could "
            "not be checked (lookup failed); keeping them for this launch",
            account_id,
        )
    elif _keeps_scoped_lineage(account, existing):
        # The file holds this account's own live refresh token and the row
        # would replace it with null: the launch keeps the file's tokens.
        pass
    else:
        if file_pair_differs:
            # Attributable: the file carried a pair that is not the row's and
            # the sync answered "%s" (refused: another owner, or unprovable).
            logger.warning(
                "Account %d: replacing the per-account file's token pair with the "
                "row's (sync result: %s)",
                account_id,
                rescue,
            )
        existing["claudeAiOauth"] = build_oauth_data(account)
    # No _jackedAccountId stamp — account_id is derived from directory path

    # Atomic write
    fd, tmp = tempfile.mkstemp(
        dir=str(config_dir), prefix=".credentials_tmp_", suffix=".json"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2)
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

    if not kept_rotation:
        _forget_foreign_pair(config_dir)

    _publish_launch_credentials(
        account,
        db,
        config_dir,
        account_id,
        written_oauth=existing["claudeAiOauth"],
        kept_rotation=kept_rotation,
    )

    return config_dir


def _account_hint(db: Database) -> str:
    """Format the launchable accounts as a hint for error messages.

    >>> # Tested via test_launch.py (TestResolveAccount)
    """
    try:
        accounts = db.list_accounts(include_inactive=True)
    except Exception:
        return ""
    if not accounts:
        return ""
    lines = [
        f"  {a['id']}: {a.get('email') or '?'} ({a.get('provider') or 'claude'})"
        for a in sorted(accounts, key=lambda a: a.get("id") or 0)
    ]
    return "\nAvailable accounts:\n" + "\n".join(lines)


def _match_listing(accounts: list) -> str:
    """Format substring-matched accounts as 'ID (email, org)' pairs.

    >>> _match_listing([{"id": 2, "email": "a@x.com", "organization_name": "X"}])
    '2 (a@x.com, X)'
    >>> _match_listing([{"id": 3, "email": "b@y.com"}])
    '3 (b@y.com)'
    """
    parts = []
    for a in accounts:
        org = a.get("organization_name")
        label = f"{a['id']} ({a.get('email') or '?'}"
        parts.append(label + (f", {org})" if org else ")"))
    return ", ".join(parts)


def resolve_account(account_ref, db: Database) -> dict:
    """Resolve account reference to a full account dict.

    account_ref can be: int (ID), str with @ (email), str without @
    (unique part of an email), or None (use currently active account).

    >>> # Tested via test_launch.py
    """
    if not find_bin("claude"):
        raise click.ClickException(
            "claude not found in PATH. Install with: npm install -g @anthropic-ai/claude-code"
        )

    account = None

    if account_ref is None:
        # No file/DB/Keychain precedence or token matching is allowed here.
        # An omitted account means the exact-build canonical resolver must have
        # reached consensus on one stamped account.
        from jacked.api.credential_helpers import read_active_account_id

        acct_id = read_active_account_id()
        if acct_id is not None:
            account = db.get_account(acct_id)

        if not account:
            raise click.ClickException(
                "No active account detected. Specify an account: jacked claude <id>."
                + _account_hint(db)
            )
    elif isinstance(account_ref, str) and "@" in account_ref:
        try:
            # `jacked claude <email>` resolves a Claude account; scope to the
            # provider so a same-email Codex account never collides.
            account = db.get_account_by_email(account_ref, provider="claude")
        except ValueError as exc:
            # Ambiguous: multiple orgs for the same email
            raise click.ClickException(str(exc))
        if not account:
            raise click.ClickException(
                f"No account found for email: {account_ref}." + _account_hint(db)
            )
    else:
        # Try as integer ID first
        try:
            acct_id = int(account_ref)
        except (ValueError, TypeError):
            acct_id = None

        if acct_id is not None:
            account = db.get_account(acct_id)
            if not account:
                raise click.ClickException(
                    f"Account {acct_id} not found. The number is the account ID "
                    "(shown in 'jacked usage'), not the row position."
                    + _account_hint(db)
                )
        else:
            # Not an ID and not a full email: match a unique part of an email.
            # Scope the winner to Claude accounts so a same-email Codex account
            # never collides (same rule as the exact-email branch above).
            needle = str(account_ref).lower()
            matches = [
                a
                for a in db.list_accounts(include_inactive=True)
                if needle in (a.get("email") or "").lower()
            ]
            claude_matches = [
                a for a in matches if (a.get("provider") or "claude") == "claude"
            ]
            if len(claude_matches) == 1:
                account = claude_matches[0]
            elif len(claude_matches) > 1:
                raise click.ClickException(
                    f"'{account_ref}' matches more than one account: "
                    f"{_match_listing(claude_matches)}. "
                    "Use the account ID or the full email."
                )
            elif matches:
                raise click.ClickException(
                    f"'{account_ref}' matches only Codex accounts: "
                    f"{_match_listing(matches)}. "
                    "Launch Codex with: jacked codex launch <id>"
                )
            else:
                raise click.ClickException(
                    f"No account matches '{account_ref}'." + _account_hint(db)
                )

    if account.get("is_deleted"):
        raise click.ClickException(
            f"Account {account.get('id')} ({account.get('email')}) has been deleted"
        )
    if not account.get("access_token"):
        raise click.ClickException(
            f"Account {account.get('id')} ({account.get('email')}) has no access token. "
            "Try /login in Claude Code first."
        )
    # A Codex account must NEVER be launched as Claude Code: its access_token is a
    # sentinel ("codex-managed"), and prepare_account_dir would overwrite the
    # global 'Claude Code-credentials' keychain with it + launch `claude`,
    # breaking the user's real Claude Code login. Codex has its own launcher.
    if (account.get("provider") or "claude") == "codex":
        raise click.ClickException(
            f"Account {account.get('id')} ({account.get('email')}) is a Codex account. "
            f"Launch it with: jacked codex launch {account.get('id')}"
        )

    return account


_IDENTITY_COLS = ("email", "organization_uuid", "account_uuid")
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
        fd, tmp = tempfile.mkstemp(dir=str(config_dir), prefix=".jacked-foreign-pair_", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh)
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        _safe_replace(tmp, str(marker))
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


def _settle_unowned(
    conn,
    snapshot: dict,
    account_id: int,
    cols: set,
    outcome: str,
    *,
    config_dir: Path | None = None,
    oauth: dict | None = None,
) -> str:
    """A refusal holds only for the row it was decided against.

    The ownership verdict compared the profile with the row as it was read
    before the request. A writer that changed the row identity meanwhile
    (a primary re-auth moves the row to another organization without
    touching the Claude Code token) makes that refusal stale: the file's
    pair may belong to the NEW identity. The sync then defers, and the
    next pass decides against the new row (the verdict cache is keyed by
    the identity, so it asks again). A deferral is returned as is.
    """
    if outcome != "refused":
        return outcome
    fresh = _sync_row(conn, account_id, cols)
    changed = fresh is None or any(
        fresh.get(c) != snapshot.get(c) for c in _IDENTITY_COLS if c in cols
    )
    if not changed:
        logger.warning(
            "Token sync BLOCKED for account %d: ownership not proven", account_id
        )
        if (
            config_dir is not None
            and isinstance(oauth, dict)
            and ownership_refusal_is_final(snapshot, oauth.get("accessToken"))
        ):
            # The profile named another account: remember the pair, so the
            # next launch replaces it without a request, expired or not.
            _remember_foreign_pair(config_dir, oauth, snapshot)
            logger.info(
                "Account %d: the per-account file holds another account's "
                "tokens; the next launch replaces them",
                account_id,
            )
        return outcome
    forget_ownership_verdicts(account_id)
    logger.info(
        "Token sync for account %d: the row identity changed during the "
        "ownership check; deciding again on the next pass",
        account_id,
    )
    return "deferred"


def _sync_row(conn, account_id: int, cols: set) -> dict | None:
    """The row fields that the launch token sync uses, or None for no row.

    The function reads only the columns that this schema has, so a database
    from before a migration gives a row without those keys. A database with
    no accounts table has no row.
    """
    if "id" not in cols:
        return None
    names = [
        name
        for name in (
            "id",
            "email",
            "organization_uuid",
            "account_uuid",
            "access_token",
            # Read by the dead-chain rule: a primary refresh token is never
            # adopted as the Claude Code lineage.
            "refresh_token",
            "cc_access_token",
            "cc_refresh_token",
            "cc_expires_at",
            "cc_refresh_failure_type",
            "keeper_state",
        )
        if name in cols
    ]
    # The column names come from the fixed tuple above, never from input.
    row = conn.execute(
        f"SELECT {', '.join(names)} FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    return dict(zip(names, row)) if row else None


# Rows whose deferral was already logged at WARNING in this process.
_deferral_warned: set[int] = set()


def _scoped_pair_differs(config_dir: Path, account: dict) -> bool:
    """True when the per-account file holds an access or refresh token that
    is not the row's (a rotation the row does not have). Unreadable: False.

    >>> import tempfile, json as _json
    >>> d = Path(tempfile.mkdtemp()); row = {"cc_access_token": "a", "cc_refresh_token": "r"}
    >>> _scoped_pair_differs(d, row)
    False
    >>> _ = (d / ".credentials.json").write_text(_json.dumps({"claudeAiOauth": {"accessToken": "a", "refreshToken": "r"}}))
    >>> _scoped_pair_differs(d, row)
    False
    >>> _ = (d / ".credentials.json").write_text(_json.dumps({"claudeAiOauth": {"accessToken": "a2", "refreshToken": "r2"}}))
    >>> _scoped_pair_differs(d, row)
    True
    """
    path = config_dir / ".credentials.json"
    try:
        if not path.exists() or path.is_symlink():
            return False
        oauth = json.loads(path.read_text(encoding="utf-8")).get("claudeAiOauth")
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(oauth, dict):
        return False
    access, refresh = oauth.get("accessToken"), oauth.get("refreshToken")
    if not access and not refresh:
        return False
    return access != account.get("cc_access_token") or refresh != account.get("cc_refresh_token")


def _log_deferral(account_id: int, message: str) -> None:
    """`jacked claude` logs at WARNING, so the first deferral of a row in a
    process is a WARNING a user can see; the retries every 30 s stay at INFO."""
    level = logging.INFO if account_id in _deferral_warned else logging.WARNING
    _deferral_warned.add(account_id)
    logger.log(level, message, account_id)


def _is_expired_rotation(account: dict, oauth: dict) -> bool:
    """True when the file holds an expired access token beside a refresh
    token that is not the row's: a rotation nobody can prove any more.

    >>> row = {"cc_refresh_token": "rt-row"}
    >>> _is_expired_rotation(row, {"accessToken": "a", "refreshToken": "rt-new", "expiresAt": 1000})
    True
    >>> _is_expired_rotation(row, {"accessToken": "a", "refreshToken": "rt-row", "expiresAt": 1000})
    False
    >>> _is_expired_rotation(row, {"accessToken": "a", "refreshToken": "rt-new"})
    False
    """
    from jacked.api.credential_helpers import expiry_seconds

    refresh = oauth.get("refreshToken")
    if not isinstance(refresh, str) or not refresh or refresh == account.get("cc_refresh_token"):
        return False
    expires = expiry_seconds(oauth.get("expiresAt"))
    return expires is not None and expires <= _time.time()


def _unowned_outcome(account: dict, payload: dict, account_id: int) -> str | None:
    """The sync result when the tokens in ``payload`` are not proven to be the
    tokens of ``account``, or None when they are.

    The shared ownership rule decides (credential_helpers.live_payload_ownership).
    The per-account file has no jacked stamp, so the rule does not require
    one. The sync thread has no event loop, so the rule can send its one
    profile request. The log lines give the row id only.

    >>> _unowned_outcome({"id": 3, "cc_access_token": "a1"},
    ...                  {"claudeAiOauth": {"accessToken": "a1"}}, 3) is None
    True
    """
    oauth = payload.get("claudeAiOauth") if isinstance(payload, dict) else None
    if isinstance(oauth, dict) and _is_expired_rotation(account, oauth):
        # Nobody can prove an expired access token, but its refresh token
        # may be the only live lineage (Claude Code rotated offline, and the
        # exit sync could not ask). It is not foreign, it is unproven: keep
        # it out of the row, and keep it in the file for the next session.
        _log_deferral(
            account_id,
            "Token sync deferred for account %d: the rotated access token expired "
            "before its owner could be checked; the file keeps the rotation",
        )
        return "deferred"
    owned = live_payload_ownership(account, payload, require_stamp=False)
    if owned is True:
        return None
    if owned is None:
        _log_deferral(
            account_id,
            "Token sync deferred for account %d: ownership lookup failed; jacked "
            "tries again on the next poll",
        )
        return "deferred"
    # The caller logs the refusal once it stands (_settle_unowned): a refusal
    # decided against a row whose identity changed meanwhile is deferred
    # instead, and must not read as BLOCKED.
    return "refused"


def _sync_tokens_from_file(config_dir: Path, db_path: str) -> str:
    """Copy the tokens in the per-account .credentials.json into the DB row.

    The directory name is the account id. The tokens go into the row only
    when the shared ownership rule proves that they are the tokens of the
    row (credential_helpers.live_payload_ownership): the row holds the
    access token or the refresh token already, or one profile request names
    the owner of the row. The per-account .claude.json is not proof, because
    jacked and Claude Code both write it from the token that a process holds.
    The other import rules of reconcile also apply: an unreadable expiry is
    refused, the primary-token fallback is not imported, an older pair does
    not replace newer tokens, and a dead chain (invalid_grant) follows the
    shared dead-chain rule (credential_helpers.dead_chain_adoption): a NEW
    refresh token is adopted and the breaker cleared in the same
    compare-and-swap write, while the failed token gives the access token and
    its expiry only (#244i).

    The function uses raw sqlite3 and not the Database class, because it
    runs in a thread.

    The result is one of these values:

    - ``"imported"``: the tokens are the tokens of the row, and the row
      holds them now. This call wrote them or their pass-through fields, or
      the row had them before.
    - ``"refused"``: the rule shows that the tokens are not the tokens of
      the row, or that nobody can prove it (for example, the token is
      expired, or its expiry is not a real time). Nothing is written.
    - ``"deferred"``: the profile request failed, or an error stopped the
      sync. Nothing is written. A later call can decide.
    - ``"unchanged"``: there is nothing to copy (no file, no access token, a
      directory name that is not an id, no row, a row without a Claude Code
      token, the primary-token fallback, or a pair older than the row's
      pair), or another writer changed the row first. Nothing is written.

    >>> # Tested via test_launch_token_sync*.py and test_launch_sync_dead_chain.py
    """
    cred_path = config_dir / ".credentials.json"
    try:
        if not cred_path.exists() or cred_path.is_symlink():
            return "unchanged"
        data = json.loads(cred_path.read_text(encoding="utf-8"))
        oauth = data.get("claudeAiOauth", {})
        access_token = oauth.get("accessToken")
        if not access_token:
            return "unchanged"

        try:
            account_id = int(config_dir.name)
        except (ValueError, TypeError):
            return "unchanged"

        # The import rules of reconcile (credential_helpers._reconcile_decision)
        # apply here too. The local rules run first, so only a token that
        # they cannot place costs a profile request. First: a payload whose
        # expiry is not a real time is not a Claude Code rotation, so nothing
        # in it is taken. The result settles, so the loop does not read it
        # again.
        if _malformed_expiry(oauth.get("expiresAt")):
            logger.warning(
                "Token sync BLOCKED for account %d: unreadable expiry", account_id
            )
            return "refused"

        import sqlite3

        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")

            # Check if cc_* columns exist (handles pre-migration edge case)
            cursor = conn.execute("PRAGMA table_info(accounts)")
            cols = {row[1] for row in cursor.fetchall()}
            has_cc_cols = "cc_access_token" in cols

            account = _sync_row(conn, account_id, cols)
            if account is None:
                return "unchanged"

            refresh_token = oauth.get("refreshToken")
            expires_at = expiry_seconds(oauth.get("expiresAt"))

            if has_cc_cols:
                # Credential files contain CC tokens — sync to cc_* columns.
                # CAS: only write if DB value hasn't changed under us.
                old_cc_access_token = account.get("cc_access_token")
                if old_cc_access_token == access_token:
                    # The row holds this access token, so the tokens are the
                    # row's with no profile request.
                    return sync_held_access(conn, account, oauth, cols, account_id)

                # If cc_access_token is NULL in DB, skip sync. NULL means
                # CC auth was never done — file contains primary fallback
                # tokens that shouldn't be stored in cc_* columns.
                # In both cases, only the OAuth flow or async refresh should
                # populate cc_* columns from NULL.
                if old_cc_access_token is None:
                    logger.debug(
                        "Skipping CC sync for account %d — cc_access_token "
                        "is NULL (awaiting CC auth or post-invalidation)",
                        account_id,
                    )
                    return "unchanged"

                # jacked's own primary-fallback publication is not a Claude
                # Code token: importing it would merge the two chains.
                if is_primary_publication(account, access_token):
                    logger.debug(
                        "Token sync skipped for account %d: the file holds "
                        "the primary-token fallback",
                        account_id,
                    )
                    return "unchanged"

                # A pair a previous session proved to belong to another account
                # (a /login inside this directory) is refused with no request,
                # even when its access token expired, so the launch replaces it.
                if _known_foreign_pair(config_dir, oauth, account):
                    logger.warning(
                        "Token sync BLOCKED for account %d: the file holds another "
                        "account's tokens (remembered from an earlier check)",
                        account_id,
                    )
                    return "refused"

                # Never move a row backward: an older pair in the file (on
                # macOS, the launch-time pair after reconcile took Claude
                # Code's newer pair from the Keychain) must not replace newer
                # tokens. Unless the row's token is KNOWN dead: then an owned
                # token that merely expires sooner is the one that works
                # (#179i).
                row_expires = account.get("cc_expires_at") or 0
                if (
                    expires_at
                    and row_expires
                    and expires_at < row_expires
                    and not cc_token_known_dead(account)
                ):
                    logger.info(
                        "Token sync skipped for account %d: the file holds "
                        "older tokens than the row",
                        account_id,
                    )
                    return "unchanged"

                # The checks above are local. Only a token they cannot place
                # costs a profile request.
                outcome = _unowned_outcome(account, data, account_id)
                if outcome is not None:
                    return _settle_unowned(
                        conn, account, account_id, cols, outcome,
                        config_dir=config_dir, oauth=oauth,
                    )
                return write_rotation(conn, account, oauth, cols, account_id)
            else:
                # Pre-migration fallback: write to primary columns
                if account.get("access_token") == access_token:
                    return "imported"  # unchanged
                outcome = _unowned_outcome(account, data, account_id)
                if outcome is not None:
                    return _settle_unowned(
                        conn, account, account_id, cols, outcome,
                        config_dir=config_dir, oauth=oauth,
                    )

                now_iso = datetime.now(timezone.utc).isoformat()
                updates = [
                    "access_token = ?",
                    "validation_status = 'valid'",
                    "consecutive_failures = 0",
                    "last_error = NULL",
                    "updated_at = ?",
                ]
                params_legacy: list = [access_token, now_iso]
                if refresh_token:
                    updates.append("refresh_token = ?")
                    params_legacy.append(refresh_token)
                if expires_at is not None:
                    updates.append("expires_at = ?")
                    params_legacy.append(expires_at)
                params_legacy.append(account_id)

                conn.execute(
                    f"UPDATE accounts SET {', '.join(updates)} WHERE id = ?",
                    params_legacy,
                )
                conn.commit()
                logger.debug(
                    "Synced refreshed tokens for account %d (pre-migration)",
                    account_id,
                )
                return "imported"
        finally:
            conn.close()
    except Exception as exc:
        # A half-written file or a locked database: nothing is written, and
        # the sync loop tries again on its next tick.
        logger.debug("Token sync failed (non-fatal): %s", exc)
        return "deferred"


def _sleep_tick(stop: threading.Event, seconds: int = 30) -> bool:
    """Sleep one poll tick, a second at a time. True when ``stop`` was set.

    The launch sets ``stop`` when Claude Code exits, so the loop ends within
    a second and the exit-time sync runs alone.
    """
    for _ in range(seconds):
        if stop.is_set():
            return True
        _time.sleep(1)
    return stop.is_set()


def _token_sync_loop(
    proc, config_dir: Path, db_path: str, stop: threading.Event | None = None
) -> None:
    """Daemon thread: poll credential file and sync changes to DB.

    A sync runs after each change of the file mtime. A "deferred" sync (the
    profile request failed, or an error stopped it) keeps the old mtime, so
    the next tick tries again when the file does not change. The loop ends
    when the process exits or when ``stop`` is set.
    """
    stop = stop if stop is not None else threading.Event()
    cred_path = config_dir / ".credentials.json"
    last_mtime = None
    try:
        initial_mtime = cred_path.stat().st_mtime if cred_path.exists() else None
    except OSError:
        initial_mtime = None
    if initial_mtime is not None:
        # A rotation that landed between the launch's own write and this
        # thread's start would otherwise look "already processed" and stay in
        # the file until the next change; sync it before accepting the mtime.
        if _sync_tokens_from_file(config_dir, db_path) != "deferred":
            last_mtime = initial_mtime

    while proc.poll() is None:
        if _sleep_tick(stop):
            break
        try:
            mtime = cred_path.stat().st_mtime if cred_path.exists() else None
        except OSError:
            continue
        if mtime is not None and mtime != last_mtime:
            if _sync_tokens_from_file(config_dir, db_path) != "deferred":
                last_mtime = mtime


def _close_sessions_by_pid(pid: int, db_path: str) -> None:
    """Mark all open sessions with the given PID as ended."""
    import sqlite3

    try:
        ts = datetime.now(timezone.utc).isoformat()
        conn = sqlite3.connect(db_path, timeout=2.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout = 5000")
            conn.execute(
                "UPDATE session_accounts SET ended_at = ? WHERE pid = ? AND ended_at IS NULL",
                (ts, pid),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.debug("Session close by PID failed: %s", exc)


def launch_claude(
    config_dir: Path, claude_args: tuple, db_path: str | None = None
) -> None:
    """Launch claude as a subprocess with credential sync.  Always raises SystemExit.

    Runs claude with CLAUDE_CONFIG_DIR set. A daemon thread syncs refreshed
    tokens back to DB every 30s. On exit, marks sessions ended by PID.

    >>> # Tested via test_launch.py with mock subprocess
    """
    env = os.environ.copy()
    env["CLAUDE_CONFIG_DIR"] = str(config_dir)
    # Never inherit a parent shell's certification/binding claims. The current
    # launch is global unless this exact snapshot proves otherwise.
    for key in (
        "JACKED_CREDENTIAL_SCOPE",
        "JACKED_LAUNCH_NONCE",
        "JACKED_CREDENTIAL_REVISION",
        "JACKED_SCOPED_CREDENTIAL_CERTIFIED",
    ):
        env.pop(key, None)
    env["JACKED_CREDENTIAL_SCOPE"] = "unknown"
    try:
        snapshot = json.loads(
            (config_dir / "jacked-resolver-snapshot.json").read_text(
                encoding="utf-8"
            )
        )
        observed = snapshot.get("observed")
        revision = snapshot.get("credential_revision")
        now = _time.time()
        expected_account_id = int(config_dir.name)
        valid_global_observation = (
            snapshot.get("schema_version") == 1
            and snapshot.get("scope") == "global"
            and snapshot.get("state") == "resolved"
            and isinstance(observed, dict)
            and observed.get("account_id") == expected_account_id
            and not isinstance(snapshot.get("published_at"), bool)
            and isinstance(snapshot.get("published_at"), (int, float))
            and snapshot["published_at"] <= now + 300
            and not isinstance(snapshot.get("fresh_until"), bool)
            and isinstance(snapshot.get("fresh_until"), (int, float))
            and snapshot["fresh_until"] >= now
            and isinstance(revision, str)
            and bool(revision)
        )
        if valid_global_observation:
            env["JACKED_CREDENTIAL_SCOPE"] = "global"
            env["JACKED_CREDENTIAL_REVISION"] = revision
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
        pass

    # resolve_account checks find_bin("claude"), which also finds the standalone
    # install in ~/.local/bin when PATH misses it; spawn that same binary.
    claude_bin = find_bin("claude") or "claude"
    proc = subprocess.Popen([claude_bin, *claude_args], env=env)

    stop_sync = threading.Event()
    if db_path:
        sync_thread = threading.Thread(
            target=_token_sync_loop,
            args=(proc, config_dir, db_path, stop_sync),
            daemon=True,
        )
        sync_thread.start()

    try:
        proc.wait()
    except KeyboardInterrupt:
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.terminate()

    if db_path:
        # The loop's own sync must not run beside the exit-time sync (two
        # profile requests, two writers on the row): stop the loop and wait
        # for a sync in flight. A loop stuck past the wait is a daemon thread
        # and dies with the process.
        stop_sync.set()
        sync_thread.join(timeout=15)
        if sync_thread.is_alive():
            logger.debug("Token sync loop did not stop before the exit sync")
        # The loop can hold a cached "profile request failed" verdict for up
        # to 5 minutes. The last sync must not reuse it: with the cache
        # clear, a token that needs a profile request gets one fresh request.
        clear_ownership_cache()
        exit_result = _sync_tokens_from_file(config_dir, db_path)
        if exit_result == "refused":
            logger.warning(
                "Account %s: the exit-time token sync refused the per-account "
                "file's tokens; the next launch writes the row's tokens over them",
                config_dir.name,
            )
        _close_sessions_by_pid(proc.pid, db_path)

    raise SystemExit(proc.returncode or 0)
