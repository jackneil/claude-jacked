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

from jacked.api.remote_access import credential_policy_editable, read_enabled_scope

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


# The reader moved to jacked/api/remote_access.py when the credential-mutation
# gate started reading the same setting. resolve_bind() calls it too, so the
# bind decision and the switching decision cannot diverge. Kept under the old
# module-local name because this module's own call sites (and their tests)
# resolve it as a module global.
_read_enabled_scope = read_enabled_scope


# This setting is a live authorization bit, not a preference: read_enabled_scope
# is consulted on every credential-mutation request, so flipping it on grants
# remote credential control immediately, and flipping it off does NOT revoke
# reachability until the service restarts and rebinds. Both windows mean a
# remote peer that could write it would own the host's credentials. So writes
# are loopback-only, whatever the setting currently says.
_SETTINGS_LOCAL_ONLY_MESSAGE = (
    "Remote access settings can only be changed from the machine running "
    "jacked. Open the dashboard on the host to change them."
)


def _is_loopback_client(request: Request) -> bool:
    """Whether this request came from the machine running jacked."""
    client = request.client
    return credential_policy_editable(client.host if client else None)


def _settings_local_only() -> JSONResponse:
    """403 for a write attempted from anywhere but the host."""
    return JSONResponse(
        status_code=403,
        content={
            "error": {
                "message": _SETTINGS_LOCAL_ONLY_MESSAGE,
                "code": "REMOTE_ACCESS_SETTINGS_LOCAL_ONLY",
            }
        },
        headers={"Cache-Control": "no-store"},
    )


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
    """Current remote-access setting plus the live effective bind state.

    Readable from anywhere the dashboard is reachable: a remote viewer may see
    the policy that applies to it. ``editable`` tells that viewer whether it
    may CHANGE it, so the UI can disable the controls instead of offering a
    toggle that 403s.
    """
    return {**_read_state(_get_db(request)), "editable": _is_loopback_client(request)}


@router.put("/remote-access")
async def update_remote_access(request: Request, body: RemoteAccessSettings):
    """Persist the remote-access setting. Does NOT restart — the client calls
    POST /remote-access/restart to apply, so save and apply stay distinct.

    Loopback only: see ``_settings_local_only``."""
    if not _is_loopback_client(request):
        return _settings_local_only()
    db = _get_db(request)
    if db is not None:
        db.set_setting("remote_access_enabled", "true" if body.enabled else "false")
        db.set_setting("remote_access_scope", body.scope)
    return {**_read_state(db), "editable": True}


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
    ``restart_started`` event, then a delayed restart on a daemon thread.

    Loopback only, like the PUT: applying is what makes a saved setting real,
    and it tears the process down."""
    if not _is_loopback_client(request):
        # Checked before the lock, so a refused remote call can never wedge a
        # later local apply behind a 409.
        return _settings_local_only()

    # Capture the lock instance so the daemon thread releases exactly the one
    # it acquired, even if the module global is later rebound.
    lock = _restart_lock
    if not lock.acquire(blocking=False):
        return JSONResponse(
            status_code=409, content={"detail": "Restart already in progress"}
        )

    # Everything between acquire() and a SUCCESSFUL Thread.start() must release
    # the lock on any failure, or the lock leaks permanently and every future
    # restart returns 409 forever until the process is restarted. The DB read
    # (a sqlite "database is locked" is possible right after the preceding PUT
    # writer) and Thread.start() (thread exhaustion) can both raise.
    try:
        # Include the just-saved settings so every client can decide how to
        # react: a remote browser that sees enabled=false knows this page will
        # not reconnect (loopback-only re-bind) and shows a terminal message
        # instead of health-polling forever. Read from the DB so the payload
        # reflects what was actually persisted by the preceding PUT.
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
    except BaseException:
        # The daemon thread never started (or the body raised before it did),
        # so its finally-release will never run — release here so the endpoint
        # doesn't wedge on a stuck lock.
        lock.release()
        raise
    return {"status": "started"}
