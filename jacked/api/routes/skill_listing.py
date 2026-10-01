"""Skill-listing budget routes: report the listing, apply or reset its two settings.

``GET /api/skill-listing`` returns the report from ``jacked.skill_listing``:
how many characters the skill listing needs, the budget Claude Code gives it,
which skills show without a description, the strict-YAML warnings, and the
recommended settings.

``PUT /api/skill-listing`` writes ``skillListingMaxDescChars`` and
``skillListingBudgetFraction`` into ``~/.claude/settings.json`` (the
recommended values, explicit values, or a reset that removes both keys) and
returns the fresh report.

Protection: same as every other dashboard PUT. The process-wide
``HostValidationMiddleware`` (jacked/api/security.py) blocks DNS rebinding and
rejects a foreign ``Origin`` on an unsafe method as CSRF; there is no per-route
auth layer to add. The settings.json write holds the features module's
``_settings_lock`` (looked up at call time, because ``reset_locks`` rebinds it)
so it cannot interleave with a hook/env/plugin toggle in this process.
"""

import asyncio
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jacked import packs, skill_listing
from jacked.memory.settings_io import SettingsUnreadableError

router = APIRouter()

DATA_ROOT = Path(__file__).parent.parent.parent / "data"

WINDOW_MIN = 1_000
WINDOW_MAX = 10_000_000


class SkillListingRequest(BaseModel):
    action: Literal["recommended", "explicit", "reset"]
    max_desc_chars: int | None = None
    budget_fraction: float | None = None
    # None: the window of the configured model (or 1M, flagged, when unknown).
    context_window: int | None = None


def _home() -> Path:
    """Resolved per request so a ``$JACKED_HOME`` override always applies."""
    return packs.jacked_home()


def _report(home: Path, window: int | None) -> dict:
    return skill_listing.build_report(home, context_window=window, data_root=DATA_ROOT)


def _invalid(message: str) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"error": {"message": message, "code": "INVALID_VALUE"}},
    )


def _apply(home: Path, body: SkillListingRequest) -> dict:
    """Blocking: write (or reset) the two keys and build the new report."""
    return skill_listing.apply_action(
        home, body.action, context_window=body.context_window,
        max_desc_chars=body.max_desc_chars, budget_fraction=body.budget_fraction,
        data_root=DATA_ROOT,
    )


@router.get("/skill-listing")
async def get_skill_listing(
    window: int | None = Query(None, ge=WINDOW_MIN, le=WINDOW_MAX),
):
    """The skill-listing report. Read-only; never writes settings.

    Without ``window`` the report uses the window of the configured model.
    """
    return await asyncio.to_thread(_report, _home(), window)


@router.put("/skill-listing")
async def put_skill_listing(body: SkillListingRequest):
    """Apply the recommended or explicit listing settings, or reset them."""
    if body.context_window is not None and not WINDOW_MIN <= body.context_window <= WINDOW_MAX:
        return _invalid(f"context_window must be from {WINDOW_MIN} to {WINDOW_MAX}.")

    from jacked.api.routes import features

    home = _home()
    async with features._settings_lock:
        try:
            return await asyncio.to_thread(_apply, home, body)
        except SettingsUnreadableError:
            return features._settings_unreadable_response()
        except skill_listing.ListingSettingsError as exc:
            return _invalid(str(exc))
