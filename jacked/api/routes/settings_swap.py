"""API routes for auto-swap and window keeper settings."""

import logging
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter()


class SwapSettings(BaseModel):
    auto_swap_enabled: bool = False
    auto_swap_5h_warning: int = 80
    auto_swap_5h_critical: int = 90
    auto_swap_7d_threshold: int = 85
    usage_check_interval: int = 300
    window_keeper_enabled: bool = False
    window_keeper_active_start: str = "06:00"
    window_keeper_active_end: str = "23:00"
    window_keeper_prewake: str = "04:00"


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


@router.get("/swap-settings", response_model=SwapSettings)
async def get_swap_settings(request: Request):
    """Get current auto-swap and window keeper settings."""
    db = _get_db(request)
    if db is None:
        return SwapSettings()

    def _g(key, default):
        val = db.get_setting(key)
        return val if val is not None else default

    return SwapSettings(
        auto_swap_enabled=_g("auto_swap_enabled", "false") == "true",
        auto_swap_5h_warning=int(_g("auto_swap_5h_warning", "80")),
        auto_swap_5h_critical=int(_g("auto_swap_5h_critical", "90")),
        auto_swap_7d_threshold=int(_g("auto_swap_7d_threshold", "85")),
        usage_check_interval=int(_g("usage_check_interval", "300")),
        window_keeper_enabled=_g("window_keeper_enabled", "false") == "true",
        window_keeper_active_start=_g("window_keeper_active_start", "06:00"),
        window_keeper_active_end=_g("window_keeper_active_end", "23:00"),
        window_keeper_prewake=_g("window_keeper_prewake", "04:00"),
    )


@router.put("/swap-settings", response_model=SwapSettings)
async def update_swap_settings(request: Request, body: SwapSettings):
    """Update auto-swap and window keeper settings."""
    db = _get_db(request)
    if db is None:
        return SwapSettings()

    db.set_setting("auto_swap_enabled", str(body.auto_swap_enabled).lower())
    db.set_setting("auto_swap_5h_warning", str(body.auto_swap_5h_warning))
    db.set_setting("auto_swap_5h_critical", str(body.auto_swap_5h_critical))
    db.set_setting("auto_swap_7d_threshold", str(body.auto_swap_7d_threshold))
    db.set_setting("usage_check_interval", str(body.usage_check_interval))
    db.set_setting("window_keeper_enabled", str(body.window_keeper_enabled).lower())
    db.set_setting("window_keeper_active_start", body.window_keeper_active_start)
    db.set_setting("window_keeper_active_end", body.window_keeper_active_end)
    db.set_setting("window_keeper_prewake", body.window_keeper_prewake)

    return body


@router.get("/swap-log")
async def get_swap_log(request: Request, limit: int = 50):
    """Get recent swap events."""
    db = _get_db(request)
    if db is None:
        return []
    return db.list_swaps(limit=limit)
