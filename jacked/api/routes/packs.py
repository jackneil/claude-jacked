"""Skill-pack routes — enable/disable jacked-curated upstream skill bundles.

A pack is a named bundle of skills living in a third-party GitHub repo,
installed live through the ``npx skills`` CLI (see ``jacked.packs``). These
routes are the dashboard surface: list the registry with per-pack install
status, and toggle a pack on/off. All the orchestration, filesystem
verification and npx handling lives in ``jacked.packs``; this module only
adapts it to HTTP.

Enable/disable run the (slow, 10-60s) npx subprocess off the event loop via
``asyncio.to_thread`` so a running install never blocks other requests.

Protection: state-changing requests are guarded by the process-wide
``HostValidationMiddleware`` (jacked/api/security.py), same as every other
PUT in the dashboard — Host-header validation blocks DNS rebinding and a
foreign ``Origin`` on an unsafe method is rejected as CSRF. There is no
per-route auth layer to add; matching the features PUT means adding nothing
extra here.
"""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jacked import packs

logger = logging.getLogger(__name__)

router = APIRouter()

# --- Constants ---

DATA_ROOT = Path(__file__).parent.parent.parent / "data"


def _home() -> Path:
    """Home directory for pack state + status, resolved per request.

    Delegates to the one canonical resolver (``packs.jacked_home``) so the
    dashboard, the CLI, and the core module can never drift on where state and
    skills live. Evaluated inside each handler (never cached at import) so an
    env override — set for tests or unusual installs — always takes effect.
    """
    return packs.jacked_home()


# --- Pydantic models ---

class PackToggleRequest(BaseModel):
    enabled: bool


# --- Helpers ---

def _install_in_thread(pack: packs.Pack, home: Path) -> packs.PackOpResult:
    """Install a pack. Runs inside ``asyncio.to_thread`` — npx is blocking.

    Whether to also install the codex-side copy is decided here (not by the
    caller) so the detection runs off the event loop too. A broken/absent
    codex install degrades to claude-code only rather than failing the toggle.
    """
    try:
        from jacked.codex.installer import codex_present

        include_codex = bool(codex_present())
    except Exception:
        logger.debug(
            "Codex detection for skill packs failed; targeting claude-code only",
            exc_info=True,
        )
        include_codex = False
    return packs.install_pack(pack, home, include_codex=include_codex)


def _toggle_response(
    result: packs.PackOpResult, pack: packs.Pack, enabled: bool, home: Path
) -> dict:
    """Shape the PUT response body from an op result + fresh on-disk status."""
    fresh = packs.pack_status(pack, home)
    fresh["enabled"] = enabled
    return {
        "ok": result.ok,
        "message": result.message,
        "installed": result.installed,
        "missing": result.missing,
        "removed": result.removed,
        "skipped": result.skipped,
        "pack": fresh,
    }


# --- GET /api/packs ---

@router.get("/packs")
async def list_packs():
    """Registry packs with per-pack install status and enable flags.

    Side-effect free: ``pack_status`` only reads disk (lockfile + skill dirs).
    Sorted by pack name so the dashboard order is stable.
    """
    home = _home()
    registry = packs.load_registry(DATA_ROOT)

    items = []
    for name in sorted(registry):
        pack = registry[name]
        st = packs.pack_status(pack, home)
        # "enabled" reflects EFFECTIVE state (an explicit decision, or the
        # registry default when the user never toggled it) so the dashboard
        # toggle matches what a plain `jacked install` would install.
        st["enabled"] = packs.is_effectively_enabled(pack, home)
        st["default"] = pack.default
        st["explicit"] = packs.pack_state(home, name) is not None
        items.append(st)

    return {
        "npx_available": packs.find_npx() is not None,
        "packs": items,
    }


# --- PUT /api/packs/{name} ---

@router.put("/packs/{name}")
async def toggle_pack(name: str, body: PackToggleRequest):
    """Enable (install) or disable (remove) a pack.

    Enable: record intent first, then install off-thread. The body carries a
    failure (ok=False, actionable message) with HTTP 200 — only a bad request
    (unknown pack) returns 4xx. npx-missing is handled inside install_pack,
    which returns the Node install message; we surface it unchanged.

    Disable: record disabled intent first (durable — survives a crash mid-remove
    so a restart never resurrects the pack as enabled), then remove off-thread.
    The remove result rides back in the body regardless (the user asked for it
    off; intent wins even if removal hit a snag the message explains).
    """
    home = _home()
    registry = packs.load_registry(DATA_ROOT)
    pack = registry.get(name)
    if pack is None:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={"error": {"message": f"Unknown pack: {name}", "code": "INVALID_PACK"}},
        )

    if body.enabled:
        packs.set_enabled(home, name, True)
        result = await asyncio.to_thread(_install_in_thread, pack, home)
    else:
        packs.set_enabled(home, name, False)
        result = await asyncio.to_thread(packs.remove_pack, pack, home)

    return _toggle_response(result, pack, body.enabled, home)
