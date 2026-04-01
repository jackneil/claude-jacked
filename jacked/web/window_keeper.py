"""Window keeper — schedule evaluation (pure) + ping logic (async).

Keeps 5-hour usage windows rolling by pinging idle accounts with a
lightweight API call using the account's OAuth access token.
Schedule-aware: active hours, quiet hours, pre-wake activation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pure schedule helpers
# ---------------------------------------------------------------------------

def is_active_hours(
    now: datetime,
    start: str = "06:00",
    end: str = "23:00",
) -> bool:
    """Return True if *now* falls within [start, end).

    *start* and *end* are HH:MM strings.  Does NOT handle midnight-
    crossing ranges (the default 06:00-23:00 doesn't cross).
    """
    s_h, s_m = map(int, start.split(":"))
    e_h, e_m = map(int, end.split(":"))

    start_mins = s_h * 60 + s_m
    end_mins = e_h * 60 + e_m
    now_mins = now.hour * 60 + now.minute

    return start_mins <= now_mins < end_mins


def is_prewake_time(
    now: datetime,
    prewake: str = "04:00",
    check_interval_min: float = 5,
) -> bool:
    """Return True if *now* falls within [prewake, prewake + interval).

    Used to spin up an account's window just before active hours so
    the window is already fresh when the user starts working.
    """
    p_h, p_m = map(int, prewake.split(":"))
    prewake_dt = now.replace(hour=p_h, minute=p_m, second=0, microsecond=0)
    prewake_end = prewake_dt + timedelta(minutes=check_interval_min)
    return prewake_dt <= now < prewake_end


def needs_ping(resets_at: str | None) -> bool:
    """Return True if an account's 5-hour window needs refreshing.

    - ``None`` → never opened → needs ping
    - Past timestamp → window expired → needs ping
    - Future timestamp → window still active → no ping needed
    """
    if resets_at is None:
        return True
    expiry = datetime.fromisoformat(resets_at.replace("Z", "+00:00"))
    # Always compare in UTC — the .replace("Z", "+00:00") ensures
    # timezone-aware parsing for standard API responses.
    now = datetime.now(timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry <= now


# ---------------------------------------------------------------------------
# Async ping
# ---------------------------------------------------------------------------

async def ping_account(
    cc_access_token: str,
    timeout: int = 30,
) -> bool:
    """Open/refresh a 5-hour usage window via a minimal API call.

    Makes a single haiku inference call with the account's access token.
    This is ~10x faster than the old subprocess approach and avoids
    credential resolution bugs (the subprocess used the keychain, which
    has the active account's creds — not the target account's).

    Returns True on success (HTTP 200), False on any failure.
    """
    import httpx
    from jacked.web.oauth import OAUTH_BETA_HEADER

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Authorization": f"Bearer {cc_access_token}",
                    "Content-Type": "application/json",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": OAUTH_BETA_HEADER,
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        if resp.status_code == 200:
            return True
        if resp.status_code == 401:
            logger.warning("ping_account: 401 — token expired, will refresh next cycle")
        elif resp.status_code == 429:
            logger.warning("ping_account: 429 — rate limited")
        else:
            logger.warning("ping_account: HTTP %d — %s", resp.status_code, resp.text[:200])
        return False
    except Exception:
        logger.exception("ping_account: unexpected error")
        return False
