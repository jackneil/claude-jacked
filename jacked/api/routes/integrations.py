"""Agent-reach integration routes: status, install/update/remove, channels, override.

Thin async wrappers over :class:`jacked.integrations.agent_reach.AgentReachRunner`
(the single implementation shared with the ``jacked reach`` CLI). Every mutating
op runs the runner's blocking subprocess work in a worker thread
(``asyncio.to_thread``) so the event loop is never blocked, and a module-level
:class:`asyncio.Lock` serializes them so two installs/updates cannot interleave.

Break-glass override persistence lives in the DB ``settings`` table. The runner
takes ``get_setting``/``set_setting`` callables; the setter maps a ``None`` value
to a row delete because :meth:`Database.set_setting` only stores strings. The
three override keys are also in ``system._PROTECTED_SETTING_KEYS`` so the generic
settings endpoint cannot rewrite them.
"""
from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, StrictBool

from jacked.integrations.agent_reach import AgentReachRunner

logger = logging.getLogger(__name__)

router = APIRouter()

# Serialize mutating ops (install/update/remove/channel/override). Held across
# the awaited to_thread so a second request queues instead of racing the same
# uv/agent-reach subprocesses. Rebound per event loop by reset_locks().
_reach_lock = asyncio.Lock()


def reset_locks() -> None:
    """Rebind the module lock to the current event loop.

    See ``routes.auth.reset_locks``: the tray restarts uvicorn in a new loop
    while this module stays imported, so the old lock would raise "bound to a
    different event loop" on the next acquire. Registered in main.py's lifespan
    reset tuple and enforced by tests/unit/api/test_reset_locks.py.
    """
    global _reach_lock
    _reach_lock = asyncio.Lock()


def _error(status_code: int, message: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": message, "code": code}},
    )


def _runner(request: Request) -> AgentReachRunner | None:
    """Build a runner wired to the request's DB, or None if the DB is down."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        return None

    def _get(key: str):
        return db.get_setting(key)

    def _set(key: str, value):
        if value is None:
            db.delete_setting(key)
        else:
            db.set_setting(key, value)

    return AgentReachRunner(_get, _set)


async def _run_op(request: Request, op_name: str) -> JSONResponse | dict:
    """Run a no-arg mutating runner op off-thread under the module lock."""
    runner = _runner(request)
    if runner is None:
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable", "DB_UNAVAILABLE")
    async with _reach_lock:
        try:
            result = await asyncio.to_thread(getattr(runner, op_name))
        except Exception as exc:  # noqa: BLE001 - surface the runner's message
            logger.exception("agent-reach %s failed", op_name)
            return _error(
                status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc), f"REACH_{op_name.upper()}_ERROR"
            )
    return {"ok": True, "op": op_name, "result": result}


@router.get("/agent-reach")
async def get_agent_reach(request: Request):
    """Full status: runner status (pin, install, drift, override, doctor) + update stub."""
    runner = _runner(request)
    if runner is None:
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable", "DB_UNAVAILABLE")
    try:
        result = await asyncio.to_thread(runner.status)
    except Exception as exc:  # noqa: BLE001
        logger.exception("agent-reach status failed")
        return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc), "REACH_STATUS_ERROR")
    # upstream_check is the update-availability field; GitHub polling is a
    # later milestone, so it is an explicit null for now (not omitted).
    return {**result, "upstream_check": None}


@router.post("/agent-reach/install")
async def install_agent_reach(request: Request):
    """Install agent-reach at the authoritative pin (off-thread, lock-serialized)."""
    return await _run_op(request, "install")


@router.post("/agent-reach/update")
async def update_agent_reach(request: Request):
    """Re-install at the authoritative pin (override if set, else shipped pin)."""
    return await _run_op(request, "update")


@router.post("/agent-reach/remove")
async def remove_agent_reach(request: Request):
    """Uninstall agent-reach, delete state, and clear any override."""
    return await _run_op(request, "remove")


@router.post("/agent-reach/channels/{name}")
async def enable_channel(name: str, request: Request):
    """Install a channel's pinned backends. Unknown/invalid channel -> 422."""
    runner = _runner(request)
    if runner is None:
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable", "DB_UNAVAILABLE")
    async with _reach_lock:
        try:
            hint = await asyncio.to_thread(runner.enable_channel, name)
        except Exception as exc:  # noqa: BLE001
            # Unknown channel and "not installed yet" are client-correctable.
            return _error(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc), "REACH_CHANNEL_ERROR")
    return {"ok": True, "channel": name, "hint": hint}


class OverrideRequest(BaseModel):
    ref: str
    # StrictBool so a string "true" is rejected (must be a real JSON boolean).
    confirm_unvetted: StrictBool = False


@router.put("/agent-reach/override")
async def set_override(body: OverrideRequest, request: Request):
    """Set break-glass override. Requires confirm_unvetted to be exactly true."""
    if body.confirm_unvetted is not True:
        return _error(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "confirm_unvetted must be true: a break-glass override installs UNVETTED upstream code",
            "CONFIRMATION_REQUIRED",
        )
    runner = _runner(request)
    if runner is None:
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable", "DB_UNAVAILABLE")
    async with _reach_lock:
        try:
            result = await asyncio.to_thread(runner.set_override, body.ref, ack=True)
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent-reach set_override failed")
            return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc), "REACH_OVERRIDE_ERROR")
    return {"ok": True, "override": result}


@router.delete("/agent-reach/override")
async def clear_override(request: Request):
    """Clear the break-glass override and revert to the shipped pin."""
    runner = _runner(request)
    if runner is None:
        return _error(status.HTTP_503_SERVICE_UNAVAILABLE, "Database unavailable", "DB_UNAVAILABLE")
    async with _reach_lock:
        try:
            await asyncio.to_thread(runner.clear_override)
        except Exception as exc:  # noqa: BLE001
            logger.exception("agent-reach clear_override failed")
            return _error(status.HTTP_500_INTERNAL_SERVER_ERROR, str(exc), "REACH_OVERRIDE_ERROR")
    return {"ok": True, "cleared": True}
