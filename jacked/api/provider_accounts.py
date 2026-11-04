"""Account routes for the providers whose CLI owns the login: Codex and Grok.

``jacked.api.routes.auth`` calls these for a non-Claude account. They are
here because that module is over its size budget (module size ratchet).

Codex and Grok rotate their own refresh tokens. jacked never calls their
token endpoints. It switches an account by swapping the CLI's auth.json
under a lock, after it captures the outgoing login into its slot.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path
from typing import Callable, Optional

from fastapi import status
from fastapi.responses import JSONResponse

from jacked.providers import CLAUDE, CODEX, GROK, provider_of

logger = logging.getLogger(__name__)

_GROK_LOGIN_ON_HOST = (
    "Run `grok login` on the computer that runs jacked. Then add the Grok "
    "account again."
)


def _error(code: str, message: str, http_status: int = status.HTTP_400_BAD_REQUEST,
           **extra) -> JSONResponse:
    return JSONResponse(
        status_code=http_status,
        content={"error": {"message": message, "code": code}, **extra},
    )


def grok_login_required(message: str) -> JSONResponse:
    """The 400 that tells the user to sign in to Grok on the jacked computer."""
    return _error("GROK_LOGIN_REQUIRED", message, needs_login=True, command="grok login")


async def add_codex(db):
    """Import the signed-in Codex login (``~/.codex/auth.json``).

    A refusal is a 400 ``needs_login`` with the error's own code:
    ``CODEX_LOGIN_REQUIRED`` when a ``codex login`` fixes it,
    ``CODEX_STORE_UNSWITCHABLE`` when the store must first be set to
    ``"file"``. The message is the backend reason.
    """
    from jacked.codex.accounts import CodexImportError, add_codex_account

    try:
        acct = await asyncio.to_thread(add_codex_account, db, run_login=False)
    except CodexImportError as exc:
        logger.info("Codex add refused: code=%s", exc.code)
        return _error(exc.code, exc.public_message, needs_login=True, command="codex login")
    return {
        "provider": CODEX,
        "imported": True,
        "account_id": acct["id"],
        "email": acct["email"],
        "plan": acct.get("subscription_type"),
    }


def ws_notifier(app) -> Optional[Callable[[str, dict], None]]:
    """A thread-safe sender of one WebSocket event to the dashboard, or None.

    The login watcher runs on its own thread. The sender schedules the
    broadcast on the event loop of this request. A loop that has stopped
    (the tray restarted the server) drops the event, and the next dashboard
    load shows the new state.
    """
    registry = getattr(app.state, "ws_registry", None)
    if registry is None:
        return None
    loop = asyncio.get_running_loop()

    def _notify(topic: str, payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(registry.broadcast(topic, payload), loop)
        except RuntimeError:
            logger.debug("Dropped the %s event: the server loop has stopped", topic)

    return _notify


async def add_grok(db, remote: bool, notify=None):
    """Import a terminal Grok login, or open a sign-in for a new Grok account.

    A usable live login that matches no stored account came from a terminal
    ``grok login``. It is imported and is the active Grok account. Else the
    sign-in runs in an isolated home, so the live login stays. The sign-in
    runs the Grok CLI and a browser on the computer that runs jacked, so a
    remote dashboard (``remote``) gets the terminal step.
    """
    from jacked.grok.accounts import GrokAccountError, import_grok_account, live_login_owner
    from jacked.web.pending_profile import adopt_pending_profile, pending_login_account

    signed_in, owner = await asyncio.to_thread(live_login_owner, db)
    if signed_in and owner is None:
        try:
            acct = await asyncio.to_thread(import_grok_account, db, make_active=True)
        except GrokAccountError as exc:
            return grok_login_required(f"{exc.public_message} {_GROK_LOGIN_ON_HOST}")
        await asyncio.to_thread(adopt_pending_profile, GROK, acct.get("email") or "")
        return {
            "provider": GROK,
            "imported": True,
            "account_id": acct["id"],
            "email": acct["email"],
        }
    if remote:
        logger.info("Grok add from a remote dashboard: no new login to import")
        return grok_login_required(
            f"jacked found no new Grok login to import. {_GROK_LOGIN_ON_HOST}"
        )
    started = await _start_grok_login(db, pending_login_account(GROK), None, notify)
    if isinstance(started, JSONResponse):
        return started
    started["needs_login"] = True
    return started


async def _start_grok_login(db, account: dict, target_id: Optional[int], notify):
    """Start ``grok login`` in an isolated home for ``account``.

    When the CLI exits, the login is stored for ``target_id`` (a re-auth) or
    for the account it signs in (Add Account), and ``notify`` sends the result
    to the dashboard. A start failure is a 400. A sign-in that still runs is
    a 409.
    """
    from jacked.grok.login_import import finish_login, prepare_login_env
    from jacked.web.external_login import (
        LoginInProgressError,
        LoginStartError,
        start_provider_login,
    )

    def _on_exit(outcome: str) -> None:
        payload = finish_login(db, outcome, target_id=target_id)
        if notify is not None:
            notify("provider_login_finished", payload)

    try:
        return await asyncio.to_thread(
            start_provider_login, GROK, account, db,
            prepare=prepare_login_env, on_exit=_on_exit,
        )
    except LoginInProgressError as exc:
        return _error("GROK_LOGIN_IN_PROGRESS", exc.public_message, status.HTTP_409_CONFLICT)
    except LoginStartError as exc:
        return grok_login_required(f"{exc.public_message} {_GROK_LOGIN_ON_HOST}")


async def reauth_grok(db, account: dict, remote: bool, notify=None):
    """Open the Grok sign-in for ``account`` in its browser profile.

    The sign-in runs in an isolated home, so it never replaces the live login
    of a different account. The new login replaces the live file only when
    ``account`` is the live account.
    """
    if remote:
        return grok_login_required(_GROK_LOGIN_ON_HOST)
    return await _start_grok_login(db, account, account["id"], notify)


def no_browser_reauth(account: dict) -> JSONResponse:
    """The 400 for a re-auth that jacked cannot run in a browser (Codex)."""
    if provider_of(account) == CODEX:
        return _error(
            "CODEX_NOT_OAUTH",
            "Codex accounts re-authenticate with `codex login`, then Add Account > Codex.",
        )
    return _error("NOT_OAUTH", "jacked cannot re-authenticate this account in a browser.")


async def switch(db, account: dict) -> JSONResponse:
    """Make a Codex or Grok ``account`` the live login of its CLI.

    The swap is SYNC and its file lock sleeps up to seconds. The Codex lock is
    shared with the async usage poll, so a swap on the event loop would
    freeze every coroutine (the lock-holding poll too) and deadlock. Both run
    in a worker thread, as the Claude swap path does (usage_monitor).
    """
    account_id = account["id"]
    provider = provider_of(account)
    if provider == CODEX:
        from jacked.codex.switching import CodexSwapError, swap_codex_account

        try:
            result = await asyncio.to_thread(swap_codex_account, db, account_id)
        except CodexSwapError as exc:
            return _error("CODEX_SWAP_FAILED", exc.public_message)
        return JSONResponse(
            status_code=status.HTTP_200_OK,
            content={
                "success": True,
                "provider": CODEX,
                "account_id": account_id,
                "restart_required": result.restart_required,
                "message": (
                    "Switched the active Codex account. Restart Codex (or the "
                    "Codex app/IDE) to pick it up. Codex caches auth at startup."
                ),
            },
        )
    if provider == GROK:
        from jacked.grok.accounts import GrokAccountError, swap_grok_account

        try:
            result = await asyncio.to_thread(swap_grok_account, db, account_id)
        except GrokAccountError as exc:
            return _error("GROK_SWAP_FAILED", exc.public_message)
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    return _error("PROVIDER_NOT_SWITCHABLE", "jacked cannot switch this kind of account.")


def forget_slot(db, account: dict) -> None:
    """Remove the credential files of a deleted account.

    A Claude account: its ``~/.claude/accounts/<id>`` directory. A Codex or
    Grok account: its active pointer and its auth slot. The slot holds a live
    refresh token, so it must not outlive the row.
    """
    account_id = account["id"]
    provider = provider_of(account)
    if provider == CLAUDE:
        acct_dir = Path.home() / ".claude" / "accounts" / str(account_id)
        if acct_dir.exists() and acct_dir.is_dir() and not acct_dir.is_symlink():
            shutil.rmtree(acct_dir, ignore_errors=True)
        return
    try:
        if db.get_active_account_id(provider) == account_id:
            db.delete_setting(db.active_account_setting_key(provider))
    except Exception:
        logger.debug("clearing the %s active pointer failed", provider, exc_info=True)
    try:
        if provider == CODEX:
            from jacked.codex.switching import codex_account_home

            slot = codex_account_home(account_id)
        elif provider == GROK:
            from jacked.grok.accounts import grok_home, slot_dir

            slot = slot_dir(account_id, grok_home())
        else:
            return
        if slot.exists() and slot.is_dir() and not slot.is_symlink():
            shutil.rmtree(slot, ignore_errors=True)
    except Exception:
        logger.debug("removing the %s slot dir failed", provider, exc_info=True)
