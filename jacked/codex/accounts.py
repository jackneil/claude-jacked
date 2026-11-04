"""Add / import an OpenAI Codex account into jacked.

The add flow forces file-based credential storage (so jacked can later manage
the account by swapping ``auth.json``), optionally drives ``codex login`` for the
browser OAuth, then persists a ``provider='codex'`` account row from the decoded
identity. Tokens stay where Codex keeps them — ``~/.codex/auth.json`` — and are
the source of truth for usage (app-server) and switching; the DB row holds
identity + a credential snapshot only, and is never replayed.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

from jacked.findbin import find_bin
from jacked.public_errors import PublicError

from .credentials import (
    CodexCredentialStatus,
    codex_home,
    detect_codex_account,
    ensure_file_storage,
    read_auth_json,
)

logger = logging.getLogger(__name__)

# Codex manages its own short-lived token refresh, so from jacked's perspective a
# Codex account does not "expire" like a Claude OAuth token — re-login need is
# detected from usage failures + auth.json last_refresh, not a countdown. Store a
# far-future sentinel so the row is valid and never shows as perpetually expired.
CODEX_EXPIRES_SENTINEL = 4102444800  # 2100-01-01T00:00:00Z


class CodexImportError(PublicError):
    """A Codex account could not be added (not logged in / keyring-only / no CLI).

    The message is written for the user, so the dashboard shows it as it is.

    ``code`` is the stable reason the dashboard keys on. The fix for this
    class is a ``codex login`` on the machine that runs jacked.

    >>> CodexImportError("no Codex account").code
    'CODEX_LOGIN_REQUIRED'
    """

    code = "CODEX_LOGIN_REQUIRED"


class CodexStoreUnswitchableError(CodexImportError):
    """The Codex store is one jacked cannot manage, and no login follows.

    Codex keeps the live credential in the OS keyring (``"keyring"``, or
    ``"auto"`` with no usable ``auth.json``), so ``auth.json`` is missing or
    a leftover. A ``codex login`` alone writes the keyring again. The fix is
    a config change first: set ``cli_auth_credentials_store = "file"``, then
    run ``codex login``, then add the account again.

    >>> err = CodexStoreUnswitchableError("keyring")
    >>> isinstance(err, CodexImportError), err.code
    (True, 'CODEX_STORE_UNSWITCHABLE')
    """

    code = "CODEX_STORE_UNSWITCHABLE"


def import_codex_account(
    db,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    make_active: bool = False,
) -> dict:
    """Persist (or update) a ``provider='codex'`` account from the live auth.json.

    Raises ``CodexImportError`` when there is no readable Codex identity (e.g.
    not logged in, or credentials live only in the OS keyring).
    """
    home = home if home is not None else codex_home(env)
    status = detect_codex_account(home, env)
    ident = status.identity
    if ident is None or not ident.email:
        raise CodexImportError(
            status.reason or "no Codex account found. Run `codex login` first."
        )

    auth = read_auth_json(home, env) or {}
    tokens = auth.get("tokens") or {}
    if not (tokens.get("access_token") or auth.get("OPENAI_API_KEY")):
        raise CodexImportError(
            "Codex auth.json has no usable token. Run `codex login` first."
        )

    acct = db.create_account(
        email=ident.email,
        # Do NOT store the live Codex token: switching reads ~/.codex/auth.json
        # slots and usage uses the app-server, so a DB copy is never read back —
        # storing it (especially a non-expiring OPENAI_API_KEY) is needless
        # secret-at-rest. The column is NOT NULL, so write a non-secret marker.
        access_token="codex-managed",
        expires_at=CODEX_EXPIRES_SENTINEL,
        # Codex rotates + refreshes its own (single-use) tokens; jacked never
        # replays a refresh token, so we don't store one for Codex.
        refresh_token=None,
        subscription_type=ident.plan,
        organization_uuid=ident.account_id,  # org/workspace sentinel ("" if none)
        provider="codex",
    )

    # Capture this (currently-live) account's auth.json into its per-account slot
    # so switching/launch have its credentials. Best-effort — a missing slot just
    # means switching to it will report "add it while logged in first".
    try:
        from .switching import seed_codex_slot

        seed_codex_slot(acct["id"], base=home, env=env)
    except Exception:  # pragma: no cover - capture is best-effort
        logger.debug("seed_codex_slot failed for account %s", acct.get("id"), exc_info=True)

    # A freshly-imported Codex account is signed in → valid. create_account
    # defaults to 'unknown', and the Claude Anthropic-token validator would
    # otherwise mark it 'invalid' (its stored token is a sentinel, not a real
    # Anthropic token). Codex validity is handled by validate_account's codex
    # branch (signed-in check) + the app-server usage fetch.
    db.update_account(acct["id"], validation_status="valid")
    acct = db.get_account(acct["id"]) or acct

    if make_active:
        db.set_active_account_id(acct["id"], provider="codex")
    logger.info("Imported Codex account %s (id=%s)", ident.email, acct.get("id"))
    return acct


def _run_codex_login(home: Path, env: Optional[Mapping[str, str]]) -> None:
    """Drive the interactive ``codex login`` browser OAuth flow."""
    codex = find_bin("codex")
    if not codex:
        raise CodexImportError("the `codex` CLI is not installed or not on PATH")
    run_env = dict(env if env is not None else os.environ)
    run_env["CODEX_HOME"] = str(home)
    proc = subprocess.run([codex, "login"], env=run_env)  # noqa: S603 — fixed argv
    if proc.returncode != 0:
        raise CodexImportError(f"`codex login` failed (exit {proc.returncode})")


def add_codex_account(
    db,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    *,
    run_login: bool = False,
    make_active: bool = False,
    login_runner: Optional[Callable[[Path, Optional[Mapping[str, str]]], None]] = None,
) -> dict:
    """Check the Codex store, force file storage, optionally drive
    ``codex login``, then import.

    ``run_login`` (CLI) drives the interactive login when one is needed; the
    web route passes ``run_login=False`` and reports that login is needed.
    ``login_runner`` is injectable for tests.

    The checks run before any write. Forcing file storage flips Codex to read
    ``auth.json``. Under ``"keyring"``, or ``"auto"`` with no usable
    ``auth.json``, the live credential is in the keyring and any
    ``auth.json`` is a leftover, so importing it would replace a working
    sign-in with stale credentials. Such a store needs a login, the same as
    a missing sign-in:

    - With a login to follow (``jacked codex add``), the store is set to
      ``"file"`` first, then ``codex login`` writes a fresh ``auth.json``,
      then that is imported.
    - With no login to follow (the web route, ``--no-login``), the call
      refuses first (``CodexStoreUnswitchableError``) and changes nothing.
    """
    home = home if home is not None else codex_home(env)

    status = detect_codex_account(home, env)
    if not run_login:
        _refuse_unless_importable(status)
    needs_login = _needs_login(status)
    if needs_login and login_runner is None and not find_bin("codex"):
        # Refuse before the store flip: without the CLI no login can write
        # the auth.json that a "file" store needs.
        raise CodexImportError("the `codex` CLI is not installed or not on PATH")
    config_path = home / "config.toml"
    before = config_path.read_bytes() if config_path.exists() else None
    ensure_file_storage(home, env)
    if needs_login:
        try:
            (login_runner or _run_codex_login)(home, env)
        except BaseException:
            # The login did not write auth.json: put the store back the way
            # it was, or Codex would read an absent file.
            _restore_config(config_path, before)
            raise

    return import_codex_account(db, home=home, env=env, make_active=make_active)


def _restore_config(config_path: Path, before: Optional[bytes]) -> None:
    """Restore ``config.toml`` to ``before`` (None: it did not exist)."""
    try:
        if before is None:
            config_path.unlink(missing_ok=True)
        else:
            config_path.write_bytes(before)
    except OSError as exc:
        logger.warning(
            "Could not restore config.toml after a failed Codex login (%s); "
            "cli_auth_credentials_store may now read \"file\"",
            type(exc).__name__,
        )


_NO_SIGN_IN = "no Codex account is signed in. Run `codex login` first."


def _identity_missing(status: CodexCredentialStatus) -> bool:
    return status.identity is None or not status.identity.email


def _needs_login(status: CodexCredentialStatus) -> bool:
    """A login must write ``auth.json`` before the import: no identity, or a
    store jacked cannot switch (its ``auth.json`` is a leftover).

    >>> from types import SimpleNamespace as NS
    >>> who = NS(email="dev@example.com")
    >>> _needs_login(NS(swappable=True, identity=who))
    False
    >>> _needs_login(NS(swappable=False, identity=who))
    True
    >>> _needs_login(NS(swappable=True, identity=None))
    True
    """
    return not status.swappable or _identity_missing(status)


def _refuse_unless_importable(status: CodexCredentialStatus) -> None:
    """Raise unless the live ``auth.json`` can be imported with no login.

    The verdict is the store's, not "an identity parses": a keyring store
    with a leftover ``auth.json`` parses fine and is still refused. The log
    line names the store mode and the code, never an email or a token.
    """
    if not status.swappable:
        error_cls = (
            CodexImportError
            if status.store_mode == "file"  # file store, credential missing
            else CodexStoreUnswitchableError
        )
        why, message = "swappable=False", status.reason or _NO_SIGN_IN
    elif _identity_missing(status):
        error_cls, why, message = CodexImportError, "identity=missing", _NO_SIGN_IN
    else:
        return
    logger.info(
        "Codex add refused before any write: store_mode=%s %s code=%s",
        status.store_mode,
        why,
        error_cls.code,
    )
    raise error_cls(message)
