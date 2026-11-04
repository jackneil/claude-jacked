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

from fastapi import status
from fastapi.responses import JSONResponse

from jacked.providers import CODEX, GROK, provider_of

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


async def add_grok(db, remote: bool):
    """Import the signed-in Grok login, or open its sign-in in a browser profile.

    The sign-in runs the Grok CLI and a browser on the computer that runs
    jacked, so a remote dashboard (``remote``) gets the terminal step.
    """
    from jacked.grok.accounts import GrokAccountError, import_grok_account
    from jacked.web.pending_profile import adopt_pending_profile, pending_login_account

    try:
        # The imported login is the live one, so it is the active Grok account.
        acct = await asyncio.to_thread(import_grok_account, db, make_active=True)
    except GrokAccountError as exc:
        logger.info("Grok add found no usable login")
        if remote:
            return grok_login_required(f"{exc} {_GROK_LOGIN_ON_HOST}")
        started = await _start_grok_login(db, pending_login_account(GROK))
        if isinstance(started, JSONResponse):
            return started
        started["needs_login"] = True
        started["message"] = (
            f"{started['message']} Then add the Grok account again, so jacked imports it."
        )
        return started
    await asyncio.to_thread(adopt_pending_profile, GROK, acct.get("email") or "")
    return {
        "provider": GROK,
        "imported": True,
        "account_id": acct["id"],
        "email": acct["email"],
    }


async def _start_grok_login(db, account: dict):
    """Start ``grok login`` for ``account``. A start failure is a 400."""
    from jacked.web.external_login import LoginStartError, start_provider_login

    try:
        return await asyncio.to_thread(start_provider_login, GROK, account, db)
    except LoginStartError as exc:
        return grok_login_required(f"{exc} {_GROK_LOGIN_ON_HOST}")


async def reauth_grok(db, account: dict, remote: bool):
    """Open the Grok sign-in for ``account`` in its browser profile.

    ``grok login`` replaces the live auth.json. When another login is live,
    its rotated refresh token would be lost, so the re-auth is refused.
    """
    from jacked.grok.accounts import live_login_owner

    if remote:
        return grok_login_required(_GROK_LOGIN_ON_HOST)
    signed_in, owner = await asyncio.to_thread(live_login_owner, db)
    if signed_in and owner != account["id"]:
        return _error(
            "GROK_SIGNED_IN_AS_OTHER",
            "Grok is signed in to a different account. A sign-in here replaces "
            "that login. Switch to this account first, then re-authenticate it.",
            http_status=status.HTTP_409_CONFLICT,
        )
    return await _start_grok_login(db, account)


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
            return _error("CODEX_SWAP_FAILED", str(exc))
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
            return _error("GROK_SWAP_FAILED", str(exc))
        return JSONResponse(status_code=status.HTTP_200_OK, content=result)
    return _error("PROVIDER_NOT_SWITCHABLE", "jacked cannot switch this kind of account.")


def forget_slot(db, account: dict) -> None:
    """Clear a deleted account's active pointer and remove its auth slot.

    The slot holds a live refresh token, so it must not outlive the row.
    """
    account_id = account["id"]
    provider = provider_of(account)
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
