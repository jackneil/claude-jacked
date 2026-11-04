"""API routes for auto-swap and window keeper settings.

Each write here (the settings PUT, the pause and the resume) runs the gate of
an account switch first: loopback always may; a remote client may only while
remote access is on and its address is inside the scope. These writes turn
auto-swap on and off and clear the hold that follows a manual switch.
"""

import asyncio
import logging
import re as _re
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Query, Request, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from jacked.api.mutation_gate import denied_response, mutation_decision
from jacked.api.remote_access import REASON_OUTSIDE_SCOPE, REASON_REMOTE_ACCESS_OFF
from jacked.api.usage_monitor import _setting_bool, _Snapshot

logger = logging.getLogger(__name__)

router = APIRouter()

# The longest pause, in minutes (24 hours). POST /swap-pause takes at most
# this many minutes, and a PUT may not set a pause that ends later than this
# from now.
MAX_PAUSE_MINUTES = 1440

# Reason-keyed copy for a refused remote change to auto-swap. The two denials
# have different fixes, so they get different sentences. No em-dashes
# (user-facing). The codes are the policy reasons themselves.
_SWAP_DENIED_MESSAGES = {
    REASON_REMOTE_ACCESS_OFF: (
        "Changing auto-swap from this browser is off. Turn on remote access "
        "under Settings > Advanced on the host machine."
    ),
    REASON_OUTSIDE_SCOPE: (
        "This browser's address is outside the remote access scope. Widen the "
        "scope under Settings > Advanced on the host machine."
    ),
}


def _pause_problem(paused_until: Optional[str], now: datetime) -> Optional[tuple[str, str]]:
    """Return the ``(code, message)`` that refuses this pause time, or None.

    ``paused_until`` has passed the ISO check of ``SwapSettings``. None or an
    empty string clears the pause. A time without a time zone is refused,
    because the auto-swap tick ignores it. A time more than
    ``MAX_PAUSE_MINUTES`` after ``now`` is refused, so a PUT gets the cap of
    POST /swap-pause. A time that has passed is valid: it is no pause.

    >>> now = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)
    >>> _pause_problem(None, now) is None and _pause_problem("", now) is None
    True
    >>> _pause_problem("2026-11-03T12:00:00+00:00", now) is None
    True
    >>> _pause_problem("2026-11-03T12:00:01Z", now)[0]
    'PAUSE_TOO_LONG'
    >>> _pause_problem("2026-11-01T12:00:00Z", now) is None
    True
    >>> _pause_problem("2026-11-02T18:00:00", now)[0]
    'PAUSE_NO_TIME_ZONE'
    """
    if not paused_until:
        return None
    ends = datetime.fromisoformat(paused_until.replace("Z", "+00:00"))
    if ends.tzinfo is None:
        return (
            "PAUSE_NO_TIME_ZONE",
            "The pause time must have a time zone, for example 2026-11-02T18:00:00Z.",
        )
    if ends - now > timedelta(minutes=MAX_PAUSE_MINUTES):
        return (
            "PAUSE_TOO_LONG",
            f"A pause can end at most {MAX_PAUSE_MINUTES} minutes "
            f"({MAX_PAUSE_MINUTES // 60} hours) from now.",
        )
    return None


def _same_pause(sent: Optional[str], stored: Optional[str]) -> bool:
    """Return True when ``sent`` is the stored pause: the same text, or the same instant.

    The checks of ``_pause_problem`` apply to a new pause only. A pause that
    was stored before those checks can fail them, and the menu bar saves
    with GET, then PUT of the same body, so that pause comes back unchanged.

    >>> _same_pause("2999-01-01T00:00:00Z", "2999-01-01T00:00:00+00:00")
    True
    >>> _same_pause("2026-11-04T12:00:00+00:00", "2026-11-04T13:00:00+01:00")
    True
    >>> _same_pause("2026-11-04T12:00:00", "2026-11-04T12:00:00")
    True
    >>> _same_pause("2026-11-04T12:00:00", "2026-11-04T12:00:00+00:00")
    False
    >>> _same_pause("2026-11-05T12:00:00Z", "2026-11-04T12:00:00Z")
    False
    >>> _same_pause("2026-11-04T12:00:00Z", None), _same_pause("2026-11-04T12:00:00Z", "")
    (False, False)
    """
    if not sent or not stored:
        return False
    if sent == stored:
        return True
    try:
        sent_at = datetime.fromisoformat(sent.replace("Z", "+00:00"))
        stored_at = datetime.fromisoformat(stored.replace("Z", "+00:00"))
    except ValueError:
        return False
    if sent_at.tzinfo is None or stored_at.tzinfo is None:
        return False
    return sent_at == stored_at


def _pause_refused(problem: tuple[str, str]) -> JSONResponse:
    code, message = problem
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"error": {"message": message, "code": code}},
    )


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
    (5h critical and burn rate). The default is off. Then auto-swap does not
    leave an account for its 5-hour window. It still leaves an account whose
    weekly limit or model limit reaches the critical level of the plan. It
    also still applies the 7-day tier rules: it moves to a candidate of a
    higher tier or to a T0 account that expires earlier, and it leaves a
    drained T0 or T1 account.

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

# The settings that a save writes only when the request sends them. A save
# that does not send one keeps the stored value, and the response reads that
# value back. The pause has its own endpoints, and a manual switch sets a
# 15-minute hold. The 5-hour toggle is newer than some clients: a dashboard
# tab from before the upgrade does not send it, and its save must not turn
# the toggle off.
KEPT_WHEN_NOT_SENT = ("auto_swap_5h_rules_enabled", "auto_swap_paused_until")


def _kept_fields(settings) -> dict:
    """Read the stored values of ``KEPT_WHEN_NOT_SENT``, as GET reads them.

    ``settings`` has the ``get_setting`` shape (a Database or a _Snapshot).
    A missing toggle reads as off. A missing or empty pause reads as None.

    >>> class Store:
    ...     def __init__(self, **values):
    ...         self.values = values
    ...     def get_setting(self, key):
    ...         return self.values.get(key)
    >>> _kept_fields(Store())
    {'auto_swap_5h_rules_enabled': False, 'auto_swap_paused_until': None}
    >>> _kept_fields(Store(auto_swap_5h_rules_enabled="true", auto_swap_paused_until=""))
    {'auto_swap_5h_rules_enabled': True, 'auto_swap_paused_until': None}
    """
    return {
        "auto_swap_5h_rules_enabled": _setting_bool(settings, "auto_swap_5h_rules_enabled", False),
        "auto_swap_paused_until": settings.get_setting("auto_swap_paused_until") or None,
    }


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
            auto_swap_5h_warning=int(_g("auto_swap_5h_warning", "80")),
            auto_swap_5h_critical=int(_g("auto_swap_5h_critical", "90")),
            usage_check_interval=int(_g("usage_check_interval", "300")),
            window_keeper_enabled=_setting_bool(snap, "window_keeper_enabled", False),
            window_keeper_active_start=_g("window_keeper_active_start", "06:00"),
            window_keeper_active_end=_g("window_keeper_active_end", "23:00"),
            window_keeper_prewake=_g("window_keeper_prewake", "04:00"),
            **_kept_fields(snap),
        )
    except (ValueError, ValidationError):
        # Corrupt DB values from before validation was added — return defaults
        logger.warning("Corrupt swap settings in DB, returning defaults")
        return SwapSettings()


@router.put("/swap-settings", response_model=SwapSettings)
async def update_swap_settings(request: Request, body: SwapSettings):
    """Update auto-swap and window keeper settings.

    The fields in ``KEPT_WHEN_NOT_SENT`` (the 5-hour toggle and the pause)
    are written only when the request sends them. A save that does not send
    one keeps the stored value, and the response reports that stored value.
    A new pause that the request sends must have a time zone and must end
    within ``MAX_PAUSE_MINUTES``; otherwise the save is refused as a whole
    (422). The stored pause sent back unchanged (``_same_pause``) is not new.
    """
    allowed, reason, _host = await asyncio.to_thread(
        mutation_decision, request, audit="swap-settings"
    )
    if not allowed:
        return denied_response(reason, _SWAP_DENIED_MESSAGES, code=reason.upper())

    problem = None
    if "auto_swap_paused_until" in body.model_fields_set:
        problem = _pause_problem(body.auto_swap_paused_until, datetime.now(timezone.utc))

    db = _get_db(request)
    if db is None:
        # No stored pause exists, so a pause that fails the checks is new.
        return _pause_refused(problem) if problem is not None else SwapSettings()

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
        "auto_swap_paused_until": body.auto_swap_paused_until or "",
    }
    # The dashboard form never sends the pause, and a dashboard tab from
    # before the 5-hour toggle does not send the toggle; the menu bar sends
    # back what GET returned. The model default of a field that the request
    # did not mention would clear a hold (auto-swap then leaves the account
    # the user just chose) or turn the toggle off.
    for key in KEPT_WHEN_NOT_SENT:
        if key not in body.model_fields_set:
            del values[key]

    # One transaction, in a worker: two overlapping saves can never persist a
    # mix (warning from one, critical from the other). The same worker hop
    # reads the kept fields back, so the response says what is in effect,
    # also for a value this save kept. A pause that fails the checks is
    # refused unless it is the stored pause sent back unchanged; only that
    # case reads the stored pause, just before the write. The asyncio.Event
    # below is set on the loop.
    def _write() -> tuple[Optional[tuple[str, str]], Optional[dict]]:
        if problem is not None and not _same_pause(
            body.auto_swap_paused_until, db.get_setting("auto_swap_paused_until")
        ):
            return problem, None
        db.set_settings(values)
        return None, _kept_fields(_Snapshot(db, KEPT_WHEN_NOT_SENT))

    refused, kept = await asyncio.to_thread(_write)
    if refused is not None:
        return _pause_refused(refused)

    # Wake the sweep loop immediately so changes take effect now
    from jacked.api.usage_monitor import _sweep_wake
    _sweep_wake.set()

    return body.model_copy(update=kept)


@router.post("/swap-pause")
def pause_auto_swap(
    request: Request, minutes: int = Query(default=60, ge=1, le=MAX_PAUSE_MINUTES)
):
    """Pause auto-swap for the specified number of minutes."""
    allowed, reason, _host = mutation_decision(request, audit="swap-pause")
    if not allowed:
        return denied_response(reason, _SWAP_DENIED_MESSAGES, code=reason.upper())
    db = _get_db(request)
    if db is None:
        return {"error": "DB unavailable"}
    pause_until = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    db.set_setting("auto_swap_paused_until", pause_until)
    return {"paused_until": pause_until, "minutes": minutes}


@router.post("/swap-resume")
def resume_auto_swap(request: Request):
    """Resume auto-swap immediately (clear the pause)."""
    allowed, reason, _host = mutation_decision(request, audit="swap-resume")
    if not allowed:
        return denied_response(reason, _SWAP_DENIED_MESSAGES, code=reason.upper())
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
