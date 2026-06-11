"""API routes for auto-swap and window keeper settings."""

import logging
import re as _re
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

logger = logging.getLogger(__name__)

router = APIRouter()


def _validate_time(v: str) -> str:
    if not _re.fullmatch(r"\d{2}:\d{2}", v):
        raise ValueError(f"Invalid time format: {v!r} (expected HH:MM)")
    h, m = map(int, v.split(":"))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Invalid time: {v!r} (hours 0-23, minutes 0-59)")
    return v


class SwapSettings(BaseModel):
    auto_swap_enabled: bool = False
    auto_swap_5h_warning: int = Field(default=80, ge=0, le=100)
    auto_swap_5h_critical: int = Field(default=90, ge=0, le=100)
    auto_swap_7d_threshold: int = Field(default=85, ge=0, le=100)
    usage_check_interval: int = Field(default=300, ge=60, le=3600)
    auto_swap_paused_until: Optional[str] = None
    window_keeper_enabled: bool = False

    @field_validator("auto_swap_paused_until")
    @classmethod
    def validate_pause_timestamp(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v == "":
            return v
        from datetime import datetime
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            raise ValueError(f"Invalid ISO timestamp: {v!r}")
        return v
    window_keeper_active_start: str = "06:00"
    window_keeper_active_end: str = "23:00"
    window_keeper_prewake: str = "04:00"

    @field_validator("window_keeper_active_start", "window_keeper_active_end", "window_keeper_prewake")
    @classmethod
    def validate_time_format(cls, v: str) -> str:
        return _validate_time(v)

    @model_validator(mode="after")
    def check_warning_below_critical(self) -> "SwapSettings":
        if self.auto_swap_5h_warning >= self.auto_swap_5h_critical:
            raise ValueError(
                f"warning ({self.auto_swap_5h_warning}) must be less than "
                f"critical ({self.auto_swap_5h_critical})"
            )
        return self

    @model_validator(mode="after")
    def check_active_hours_not_crossing_midnight(self) -> "SwapSettings":
        # Always validate — even when disabled, so bad values can't be
        # persisted and then silently reset to defaults when re-enabled.
        start_h, start_m = map(int, self.window_keeper_active_start.split(":"))
        end_h, end_m = map(int, self.window_keeper_active_end.split(":"))
        if start_h * 60 + start_m >= end_h * 60 + end_m:
            raise ValueError(
                f"active_start ({self.window_keeper_active_start}) must be before "
                f"active_end ({self.window_keeper_active_end}) — midnight-crossing ranges not supported"
            )
        return self


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

    try:
        return SwapSettings(
            auto_swap_enabled=_g("auto_swap_enabled", "false") == "true",
            auto_swap_5h_warning=int(_g("auto_swap_5h_warning", "80")),
            auto_swap_5h_critical=int(_g("auto_swap_5h_critical", "90")),
            auto_swap_7d_threshold=int(_g("auto_swap_7d_threshold", "85")),
            usage_check_interval=int(_g("usage_check_interval", "300")),
            auto_swap_paused_until=_g("auto_swap_paused_until", None) or None,
            window_keeper_enabled=_g("window_keeper_enabled", "false") == "true",
            window_keeper_active_start=_g("window_keeper_active_start", "06:00"),
            window_keeper_active_end=_g("window_keeper_active_end", "23:00"),
            window_keeper_prewake=_g("window_keeper_prewake", "04:00"),
        )
    except (ValueError, ValidationError):
        # Corrupt DB values from before validation was added — return defaults
        logger.warning("Corrupt swap settings in DB, returning defaults")
        return SwapSettings()


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
    if body.auto_swap_paused_until:
        db.set_setting("auto_swap_paused_until", body.auto_swap_paused_until)
    else:
        db.set_setting("auto_swap_paused_until", "")

    # Wake the sweep loop immediately so changes take effect now
    from jacked.api.usage_monitor import _sweep_wake
    _sweep_wake.set()

    return body


@router.post("/swap-pause")
async def pause_auto_swap(request: Request, minutes: int = Query(default=60, ge=1, le=1440)):
    """Pause auto-swap for the specified number of minutes."""
    db = _get_db(request)
    if db is None:
        return {"error": "DB unavailable"}
    from datetime import datetime, timezone, timedelta
    pause_until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    db.set_setting("auto_swap_paused_until", pause_until)
    return {"paused_until": pause_until, "minutes": minutes}


@router.post("/swap-resume")
async def resume_auto_swap(request: Request):
    """Resume auto-swap immediately (clear the pause)."""
    db = _get_db(request)
    if db is None:
        return {"error": "DB unavailable"}
    db.set_setting("auto_swap_paused_until", "")
    return {"resumed": True}


@router.get("/swap-log")
async def get_swap_log(request: Request, limit: int = Query(default=50, ge=1, le=500)):
    """Get recent swap events (with status/residency) plus trailing-24h committed count."""
    db = _get_db(request)
    if db is None:
        return {"swaps": [], "swaps_last_24h": 0}
    return {
        "swaps": db.list_swaps(limit=limit),
        "swaps_last_24h": db.swaps_last_24h(committed_only=True),
    }


@router.get("/decision-log")
async def get_decision_log(
    request: Request,
    limit: int = Query(default=100, ge=1, le=1000),
    action: list[str] = Query(default=[]),
):
    """Get recent decision log entries with optional action filter."""
    db = _get_db(request)
    if db is None:
        return []
    actions = action if action else None
    return db.list_decisions(limit=limit, actions=actions)
