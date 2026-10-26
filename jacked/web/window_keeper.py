"""Window keeper — schedule evaluation (pure) + ping logic (async).

Keeps 5-hour usage windows rolling by pinging idle accounts with a
lightweight API call using the account's OAuth access token.
Schedule-aware: active hours, quiet hours, pre-wake activation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

logger = logging.getLogger(__name__)

# Response headers that carry the unified (subscription) rate-limit windows.
# Values are epoch seconds for ``*-reset`` and a 0..1 fraction for
# ``*-utilization`` (observed on OAuth subscription responses; the
# claude-rate-monitor and pi-usage packages read the same headers).
RATELIMIT_HEADER_PREFIX = "anthropic-ratelimit-"
UNIFIED_5H_RESET_HEADER = "anthropic-ratelimit-unified-5h-reset"
UNIFIED_7D_RESET_HEADER = "anthropic-ratelimit-unified-7d-reset"

_MESSAGE_MAX_CHARS = 200


# ---------------------------------------------------------------------------
# Pure schedule helpers
# ---------------------------------------------------------------------------

def _hhmm_minutes(value: str) -> int:
    h, m = map(int, value.split(":"))
    return h * 60 + m


def is_active_hours(
    now: datetime,
    start: str = "06:00",
    end: str = "23:00",
) -> bool:
    """Return True if *now* falls within [start, end).

    *start* and *end* are HH:MM strings.  Does NOT handle midnight-
    crossing ranges (the default 06:00-23:00 doesn't cross).
    """
    start_mins = _hhmm_minutes(start)
    end_mins = _hhmm_minutes(end)
    now_mins = now.hour * 60 + now.minute

    return start_mins <= now_mins < end_mins


def prewake_due(
    now: datetime,
    prewake: str = "04:00",
    active_start: str = "06:00",
    active_end: str = "23:00",
    done_date: str | None = None,
) -> bool:
    """Return True when this sweep should run the once-per-day pre-wake.

    The pre-wake opens each account's window shortly before active hours
    so the window is already running when the user starts work. It fires
    on the FIRST sweep at or after *prewake* on a local date whose marker
    (*done_date*, a local ISO date) is not set yet. The old slice check
    ("now is inside [prewake, prewake + interval)") missed whole days
    when the sweep slept through the slice on a closed laptop.

    It does not fire:

    - before *prewake*;
    - inside active hours (the normal keeper pass already covers it);
    - after the active start on the same date when the pre-wake time is
      before the active start (that day's pre-wake moment has passed).

    >>> d = datetime(2026, 10, 27, 3, 58)
    >>> prewake_due(d)
    False
    >>> prewake_due(d.replace(hour=4, minute=6))
    True
    >>> prewake_due(d.replace(hour=4, minute=6), done_date="2026-10-27")
    False
    >>> prewake_due(d.replace(hour=9), active_start="10:00")
    True
    >>> prewake_due(d.replace(hour=9))
    False
    >>> prewake_due(d.replace(hour=23, minute=30))
    False
    """
    if done_date == now.date().isoformat():
        return False
    now_mins = now.hour * 60 + now.minute
    prewake_mins = _hhmm_minutes(prewake)
    if now_mins < prewake_mins:
        return False
    if is_active_hours(now, start=active_start, end=active_end):
        return False
    start_mins = _hhmm_minutes(active_start)
    if prewake_mins < start_mins and now_mins >= start_mins:
        return False
    return True


def _parse_iso_utc(value: str) -> datetime:
    expiry = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry


def needs_ping(resets_at: str | None) -> bool:
    """Return True if an account's 5-hour window needs refreshing.

    - ``None`` → never opened → needs ping
    - Past timestamp → window expired → needs ping
    - Future timestamp → window still active → no ping needed
    - Unparseable value → needs ping (logged). A malformed row must never
      raise out of the sweep and skip every later account.

    >>> needs_ping(None)
    True
    >>> needs_ping("2099-12-31T23:59:59Z")
    False
    >>> needs_ping("not a date")
    True
    >>> needs_ping(12345)
    True
    """
    if resets_at is None:
        return True
    try:
        expiry = _parse_iso_utc(resets_at)
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "Window keeper: unparseable 5h reset value (type %s); treating "
            "the window as lapsed",
            type(resets_at).__name__,
        )
        return True
    return expiry <= datetime.now(timezone.utc)


def needs_7d_ping(resets_at: str | None, usage_cached_at: int | None) -> bool:
    """Return True if an account's 7-day window has reset but not restarted.

    The 7-day window resets at `cached_7d_resets_at`. A new 7d window
    only STARTS counting when an API call is made to the account. If
    the reset happened but no call has been made since, the window is
    "floating" — cached data still shows the old usage.

    Returns True when:
    - resets_at is None → never opened
    - resets_at is in the past AND usage_cached_at is older than resets_at
      (the reset happened but we haven't refreshed since)
    - resets_at is unparseable → a ping rewrites the cache (logged)

    Returns False when:
    - resets_at is in the future → window still active
    - resets_at is in the past but data was refreshed after the reset
      (new window already running)

    >>> needs_7d_ping("garbage", None)
    True
    >>> needs_7d_ping("2099-01-01T00:00:00Z", None)
    False
    """
    if resets_at is None:
        return True
    try:
        expiry = _parse_iso_utc(resets_at)
    except (ValueError, TypeError, AttributeError):
        logger.warning(
            "Window keeper: unparseable 7d reset value (type %s); treating "
            "the window as lapsed",
            type(resets_at).__name__,
        )
        return True

    now = datetime.now(timezone.utc)
    if expiry > now:
        return False

    if usage_cached_at is None:
        return True
    try:
        return float(usage_cached_at) < expiry.timestamp()
    except (TypeError, ValueError):
        return True


# ---------------------------------------------------------------------------
# Token fingerprint + header parsing
# ---------------------------------------------------------------------------

def token_fingerprint(token: str | None) -> str | None:
    """Return a short, non-reversible fingerprint of a token.

    Used to notice that an account's token CHANGED (re-auth, rotation)
    without keeping or logging the token itself.

    >>> token_fingerprint(None) is None
    True
    >>> len(token_fingerprint("abc"))
    8
    >>> token_fingerprint("abc") == token_fingerprint("abc")
    True
    """
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


def parse_reset_value(value: str | None) -> str | None:
    """Convert a rate-limit reset header value into a UTC ISO timestamp.

    Accepts epoch seconds (the observed form), epoch milliseconds, or an
    ISO timestamp. Returns None when the value is absent or unparseable.

    >>> parse_reset_value("1774933200")
    '2026-03-31T05:00:00+00:00'
    >>> parse_reset_value("1774933200000")
    '2026-03-31T05:00:00+00:00'
    >>> parse_reset_value("2026-03-31T05:00:00Z")
    '2026-03-31T05:00:00+00:00'
    >>> parse_reset_value(None) is None
    True
    >>> parse_reset_value("soon") is None
    True
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        epoch = float(text)
    except ValueError:
        try:
            return _parse_iso_utc(text).isoformat()
        except (ValueError, TypeError):
            return None
    if epoch > 1e12:
        epoch /= 1000.0
    if epoch <= 0:
        return None
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _parse_retry_after(value: str | None) -> float | None:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date).

    >>> _parse_retry_after("30")
    30.0
    >>> _parse_retry_after(None) is None
    True
    >>> _parse_retry_after("whenever") is None
    True
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        seconds = float(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
        if when is None:
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        seconds = when.timestamp() - time.time()
    if seconds != seconds:  # NaN
        return None
    return max(0.0, seconds)


# ---------------------------------------------------------------------------
# Async ping
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PingResult:
    """Outcome of one keeper ping. Never carries the token.

    ``message`` is the response body text (or the exception class name for
    a transport error), truncated to 200 characters, with any copy of the
    token removed. ``rate_limit`` holds every ``anthropic-ratelimit-*``
    response header, keyed by its lowercase name, with the raw value.
    """

    ok: bool
    status: int | None = None
    error_type: str | None = None
    message: str = ""
    request_id: str | None = None
    retry_after: float | None = None
    rate_limit: dict[str, str] = field(default_factory=dict)
    transport_error: bool = False

    @property
    def is_auth_failure(self) -> bool:
        return self.status in (401, 403)

    @property
    def is_rate_limited(self) -> bool:
        return self.status == 429

    @property
    def is_transient(self) -> bool:
        """Transport error or server-side (5xx) failure: retry next sweep."""
        return self.transport_error or (
            self.status is not None and self.status >= 500
        )

    def reset_5h_iso(self) -> str | None:
        return parse_reset_value(self.rate_limit.get(UNIFIED_5H_RESET_HEADER))

    def reset_7d_iso(self) -> str | None:
        return parse_reset_value(self.rate_limit.get(UNIFIED_7D_RESET_HEADER))


def _error_type_from_body(text: str) -> str | None:
    """``error.type`` from an Anthropic JSON error body, else None.

    >>> _error_type_from_body('{"type":"error","error":{"type":"authentication_error"}}')
    'authentication_error'
    >>> _error_type_from_body("<html>") is None
    True
    """
    try:
        body = json.loads(text)
    except (ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    err = body.get("error")
    if isinstance(err, dict):
        etype = err.get("type")
        if isinstance(etype, str) and etype:
            return etype[:64]
    return None


def _redact(text: str, token: str) -> str:
    if token and token in text:
        text = text.replace(token, "[redacted]")
    return text


async def ping_account(
    access_token: str,
    timeout: int = 30,
    *,
    account_id: int | None = None,
) -> PingResult:
    """Open/refresh a 5-hour usage window via a minimal API call.

    Makes a single haiku inference call with the given access token and
    returns a :class:`PingResult` with the status, the Anthropic error
    type, the request id, Retry-After, and the unified rate-limit headers.
    A failed ping logs ONE values-free line (no token, no body).
    """
    import httpx
    from jacked.web.oauth import OAUTH_BETA_HEADER

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(connect=10, read=timeout, write=10, pool=5)) as client:
            resp = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "Authorization": f"Bearer {access_token}",
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
    except Exception as exc:  # noqa: BLE001 - every transport failure is a result
        logger.warning(
            "Window keeper ping failed: account=%s status=None "
            "error_type=transport exception=%s",
            account_id,
            type(exc).__name__,
        )
        return PingResult(
            ok=False,
            error_type="transport",
            message=type(exc).__name__,
            transport_error=True,
        )

    try:
        header_items = list(resp.headers.items())
    except Exception:  # noqa: BLE001 - a malformed response object
        header_items = []
    lowered = {str(k).lower(): str(v) for k, v in header_items}
    rate_limit = {
        k: v for k, v in lowered.items() if k.startswith(RATELIMIT_HEADER_PREFIX)
    }
    status = resp.status_code
    if status == 200:
        return PingResult(
            ok=True,
            status=200,
            request_id=lowered.get("request-id"),
            rate_limit=rate_limit,
        )

    try:
        text = resp.text if isinstance(resp.text, str) else ""
    except Exception:  # noqa: BLE001
        text = ""
    error_type = _error_type_from_body(text)
    if error_type is None and status == 429:
        error_type = "rate_limited"
    result = PingResult(
        ok=False,
        status=status,
        error_type=error_type,
        message=_redact(text, access_token)[:_MESSAGE_MAX_CHARS],
        request_id=lowered.get("request-id"),
        retry_after=_parse_retry_after(lowered.get("retry-after")),
        rate_limit=rate_limit,
    )
    logger.warning(
        "Window keeper ping failed: account=%s status=%s error_type=%s "
        "request_id=%s retry_after=%s",
        account_id,
        status,
        result.error_type,
        result.request_id,
        result.retry_after,
    )
    return result
