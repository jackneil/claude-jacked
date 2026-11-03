"""API routes for auto-swap and window keeper settings."""

import asyncio
import logging
import re as _re
from typing import Optional

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from jacked.api.usage_monitor import _setting_bool, _Snapshot

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
    """The auto-swap and window keeper settings that the dashboard reads and writes.

    ``auto_swap_5h_rules_enabled`` controls the 5-hour rules of auto-swap
    (5h critical and burn rate). The default is off. Then auto-swap leaves an
    account only when its weekly limit or its model limit reaches the
    critical level of the plan.

    The model ignores a field that it does not know. A dashboard page or a
    menu bar from before a field was removed can still send that field (for
    example ``auto_swap_7d_threshold``), and its save still works.

    >>> SwapSettings().auto_swap_5h_rules_enabled
    False
    >>> "auto_swap_7d_threshold" in SwapSettings(auto_swap_7d_threshold=70).model_dump()
    False
    """

    auto_swap_enabled: bool = False
    auto_swap_5h_rules_enabled: bool = False
    auto_swap_5h_warning: int = Field(default=80, ge=0, le=100)
    auto_swap_5h_critical: int = Field(default=90, ge=0, le=100)
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
                f"active_end ({self.window_keeper_active_end}). Ranges that cross midnight are not supported."
            )
        return self


def _get_db(request: Request):
    return getattr(request.app.state, "db", None)


SWAP_SETTING_KEYS = (
    "auto_swap_enabled",
    "auto_swap_5h_rules_enabled",
    "auto_swap_5h_warning",
    "auto_swap_5h_critical",
    "usage_check_interval",
    "auto_swap_paused_until",
    "window_keeper_enabled",
    "window_keeper_active_start",
    "window_keeper_active_end",
    "window_keeper_prewake",
)


@router.get("/swap-settings", response_model=SwapSettings)
def get_swap_settings(request: Request):
    """Get current auto-swap and window keeper settings."""
    db = _get_db(request)
    if db is None:
        return SwapSettings()

    # One snapshot: the thresholds are validated as a set (warning below
    # critical), so they must never mix two saves. The booleans go through
    # the reader that the auto-swap tick and the window keeper use, so the
    # dashboard shows what they do with the same stored string.
    snap = _Snapshot(db, SWAP_SETTING_KEYS)

    def _g(key, default):
        val = snap.get_setting(key)
        return val if val is not None else default

    try:
        return SwapSettings(
            auto_swap_enabled=_setting_bool(snap, "auto_swap_enabled", False),
            auto_swap_5h_rules_enabled=_setting_bool(snap, "auto_swap_5h_rules_enabled", False),
            auto_swap_5h_warning=int(_g("auto_swap_5h_warning", "80")),
            auto_swap_5h_critical=int(_g("auto_swap_5h_critical", "90")),
            usage_check_interval=int(_g("usage_check_interval", "300")),
            auto_swap_paused_until=_g("auto_swap_paused_until", None) or None,
            window_keeper_enabled=_setting_bool(snap, "window_keeper_enabled", False),
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
    """Update auto-swap and window keeper settings.

    The pause (``auto_swap_paused_until``) is written only when the request
    sends it. The pause has its own endpoints, and a manual switch sets a
    15-minute hold, so a dashboard save that does not send it keeps it.
    """
    db = _get_db(request)
    if db is None:
        return SwapSettings()

    values = {
        "auto_swap_enabled": str(body.auto_swap_enabled).lower(),
        "auto_swap_5h_rules_enabled": str(body.auto_swap_5h_rules_enabled).lower(),
        "auto_swap_5h_warning": str(body.auto_swap_5h_warning),
        "auto_swap_5h_critical": str(body.auto_swap_5h_critical),
        "usage_check_interval": str(body.usage_check_interval),
        "window_keeper_enabled": str(body.window_keeper_enabled).lower(),
        "window_keeper_active_start": body.window_keeper_active_start,
        "window_keeper_active_end": body.window_keeper_active_end,
        "window_keeper_prewake": body.window_keeper_prewake,
    }
    # The dashboard form never sends the pause; the menu bar sends back what
    # GET returned. Clearing a hold the request did not mention would let
    # auto-swap leave the account the user just chose.
    if "auto_swap_paused_until" in body.model_fields_set:
        values["auto_swap_paused_until"] = body.auto_swap_paused_until or ""

    # One transaction, in a worker: two overlapping saves can never persist a
    # mix (warning from one, critical from the other). The same worker hop
    # reads the stored pause back, so the response says what is in effect,
    # also for a pause this save kept. The asyncio.Event below is set on the
    # loop.
    def _write() -> Optional[str]:
        db.set_settings(values)
        return db.get_setting("auto_swap_paused_until")

    stored_pause = await asyncio.to_thread(_write)

    # Wake the sweep loop immediately so changes take effect now
    from jacked.api.usage_monitor import _sweep_wake
    _sweep_wake.set()

    return body.model_copy(update={"auto_swap_paused_until": stored_pause or None})


@router.post("/swap-pause")
def pause_auto_swap(request: Request, minutes: int = Query(default=60, ge=1, le=1440)):
    """Pause auto-swap for the specified number of minutes."""
    db = _get_db(request)
    if db is None:
        return {"error": "DB unavailable"}
    from datetime import datetime, timezone, timedelta
    pause_until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    db.set_setting("auto_swap_paused_until", pause_until)
    return {"paused_until": pause_until, "minutes": minutes}


@router.post("/swap-resume")
def resume_auto_swap(request: Request):
    """Resume auto-swap immediately (clear the pause)."""
    db = _get_db(request)
    if db is None:
        return {"error": "DB unavailable"}
    db.set_setting("auto_swap_paused_until", "")
    return {"resumed": True}


@router.get("/swap-log")
def get_swap_log(request: Request, limit: int = Query(default=50, ge=1, le=500)):
    """Get recent swap events (with status/residency) plus trailing-24h committed count."""
    db = _get_db(request)
    if db is None:
        return {"swaps": [], "swaps_last_24h": 0}
    return {
        "swaps": db.list_swaps(limit=limit),
        "swaps_last_24h": db.swaps_last_24h(committed_only=True),
    }


@router.get("/decision-log")
def get_decision_log(
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
