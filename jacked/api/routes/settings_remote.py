"""API routes for the remote-access (network bind) setting.

Flipping remote access changes WHERE the unauthenticated dashboard listens, so
it is a security decision. It persists to the settings DB, the single source of
truth that ``resolve_bind`` reads at every boot/restart. Saving (PUT) and
applying (POST restart) are deliberately separate, mirroring the upgrade
endpoint: the DB write is cheap and reversible, the restart is the disruptive
part the client stages behind its own confirm + overlay.
"""

import logging
import os
import threading
import time
from typing import Literal

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()

# Guards the "already restarting" window. A plain threading.Lock, NOT an
# asyncio.Lock: the actual restart runs on a daemon thread (off the event
# loop), so an asyncio primitive bound to the request's loop would be the wrong
# tool. Unlike system.py's _upgrade_lock — an asyncio.Lock the lifespan must
# rebind via reset_locks() on every tray restart because the tray spins up a
# NEW event loop each time — a threading.Lock has no loop affinity. It survives
# loop churn untouched, so it needs no reset_locks() wiring at all.
_restart_lock = threading.Lock()


class RemoteAccessSettings(BaseModel):
    enabled: bool
    scope: Literal["tailscale", "all"]


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


def _read_enabled_scope(db) -> tuple[bool, str]:
    """Read ``(enabled, scope)`` from the DB using the bare-string convention
    (``'true'``/``'false'``); absent keys mean off + the default scope. Shared
    by the GET/PUT response body and the restart broadcast payload."""
    enabled = False
    scope = "tailscale"
    if db is not None:
        enabled = db.get_setting("remote_access_enabled") == "true"
        stored_scope = db.get_setting("remote_access_scope")
        if stored_scope in ("tailscale", "all"):
            scope = stored_scope
    return enabled, scope


def _read_state(db) -> dict:
    """Assemble the GET/PUT response body from the DB + the live BindPlan.

    ``effective`` is the plan the server actually bound (published by the
    serving path), or an honest ``'unknown'`` when nothing is registered
    (TestClient, ``webux --reload``) — we never fabricate a live mode.
    """
    from jacked.service.bind import get_active_plan

    enabled, scope = _read_enabled_scope(db)

    plan = get_active_plan()
    if plan is not None:
        effective = plan.as_effective()
    else:
        effective = {
            "mode": "unknown",
            "addresses": [os.environ.get("JACKED_HOST") or "127.0.0.1"],
            "tailscale_ip": None,
            "fallback_reason": None,
        }
    return {"enabled": enabled, "scope": scope, "effective": effective}


@router.get("/remote-access")
async def get_remote_access(request: Request):
    """Current remote-access setting plus the live effective bind state."""
    return _read_state(_get_db(request))


@router.put("/remote-access")
async def update_remote_access(request: Request, body: RemoteAccessSettings):
    """Persist the remote-access setting. Does NOT restart — the client calls
    POST /remote-access/restart to apply, so save and apply stay distinct."""
    db = _get_db(request)
    if db is not None:
        db.set_setting("remote_access_enabled", "true" if body.enabled else "false")
        db.set_setting("remote_access_scope", body.scope)
    return _read_state(db)


async def _restart_broadcast(ws_registry, event: str, payload: dict) -> None:
    if ws_registry:
        try:
            await ws_registry.broadcast(event, payload)
        except Exception:
            logger.exception("Failed to broadcast %s", event)


@router.post("/remote-access/restart")
async def restart_remote_access(request: Request):
    """Broadcast a restart notice, then restart the service to apply the new
    bind. Mirrors POST /api/upgrade: 409 if one is already in flight, a WS
    ``restart_started`` event, then a delayed restart on a daemon thread."""
    # Capture the lock instance so the daemon thread releases exactly the one
    # it acquired, even if the module global is later rebound.
    lock = _restart_lock
    if not lock.acquire(blocking=False):
        return JSONResponse(
            status_code=409, content={"detail": "Restart already in progress"}
        )

    # Include the just-saved settings so every client can decide how to react:
    # a remote browser that sees enabled=false knows this page will not
    # reconnect (loopback-only re-bind) and shows a terminal message instead of
    # health-polling forever. Read from the DB so the payload reflects what was
    # actually persisted by the preceding PUT.
    enabled, scope = _read_enabled_scope(_get_db(request))
    ws_registry = getattr(request.app.state, "ws_registry", None)
    await _restart_broadcast(
        ws_registry,
        "restart_started",
        {
            "message": "Applying network settings...",
            "enabled": enabled,
            "scope": scope,
        },
    )

    def _do_restart():
        try:
            # Let the WS frame + this HTTP response flush before we tear down.
            time.sleep(1.5)
            from jacked.service.restart import restart_service_now

            restart_service_now()
        finally:
            # Reached only if restart_service_now RETURNED — the tray handler
            # path (in-process restart). Release so a later apply can run. The
            # execv path never returns, so the lock stays held for that
            # process's brief remaining life, which is fine.
            lock.release()

    threading.Thread(
        target=_do_restart, name="jacked-remote-access-restart", daemon=True
    ).start()
    return {"status": "started"}
