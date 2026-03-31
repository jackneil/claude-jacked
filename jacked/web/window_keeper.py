"""Window keeper — schedule evaluation (pure) + ping logic (async).

Keeps 5-hour usage windows rolling by pinging idle accounts with a
lightweight ``claude -p`` call using the account's OAuth refresh token.
Schedule-aware: active hours, quiet hours, pre-wake activation.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from datetime import datetime, timedelta

from jacked.findbin import find_bin

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
    expiry = datetime.fromisoformat(resets_at)
    return expiry <= datetime.now()


# ---------------------------------------------------------------------------
# Async ping
# ---------------------------------------------------------------------------

# Security note: The refresh token is passed via env var, visible to
# process monitors.  This is acceptable for a localhost-only tool.

async def ping_account(
    cc_refresh_token: str,
    scopes: str,
    timeout: int = 30,
) -> bool:
    """Ping Claude Code with *cc_refresh_token* to start/refresh a window.

    Returns True on success (returncode 0), False on any failure.
    """
    claude_bin = find_bin("claude")
    if claude_bin is None:
        logger.warning("ping_account: claude binary not found — skipping ping")
        return False

    env = os.environ.copy()
    env["CLAUDE_CODE_OAUTH_REFRESH_TOKEN"] = cc_refresh_token
    env["CLAUDE_CODE_OAUTH_SCOPES"] = scopes
    env.pop("ANTHROPIC_API_KEY", None)

    try:
        proc = await asyncio.create_subprocess_exec(
            claude_bin, "-p", ".", "--max-turns", "1",
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("ping_account: timed out after %ds — killing process", timeout)
        proc.kill()
        return False
    except Exception:
        logger.exception("ping_account: unexpected error")
        return False

    return proc.returncode == 0
