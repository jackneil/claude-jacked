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
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Mapping, Optional

from .credentials import (
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


class CodexImportError(Exception):
    """A Codex account could not be added (not logged in / keyring-only / no CLI)."""


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
            status.reason or "no Codex account found — run `codex login` first"
        )

    auth = read_auth_json(home, env) or {}
    tokens = auth.get("tokens") or {}
    if not (tokens.get("access_token") or auth.get("OPENAI_API_KEY")):
        raise CodexImportError(
            "Codex auth.json has no usable token — run `codex login` first"
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
    codex = shutil.which("codex")
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
    """Force file storage, optionally drive ``codex login``, then import.

    ``run_login`` (CLI) drives the interactive login when no account is present;
    the web route passes ``run_login=False`` and reports that login is needed.
    ``login_runner`` is injectable for tests.
    """
    home = home if home is not None else codex_home(env)
    ensure_file_storage(home, env)

    status = detect_codex_account(home, env)
    needs_login = status.identity is None or not status.identity.email
    if needs_login and run_login:
        (login_runner or _run_codex_login)(home, env)

    return import_codex_account(db, home=home, env=env, make_active=make_active)
