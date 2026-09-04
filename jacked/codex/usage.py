"""Codex usage via the ``codex app-server`` JSON-RPC interface.

Codex (ChatGPT-plan-backed) exposes up to two rate-limit windows. The
officially documented, machine-readable source is ``codex app-server``
(newline-delimited JSON-RPC over stdin/stdout): after ``initialize`` +
``initialized`` we call ``account/rateLimits/read`` and get
``result.rateLimits.primary`` / ``.secondary``, each with ``usedPercent``,
``windowDurationMins`` and ``resetsAt`` (unix epoch). Historically primary was
the 5h window and secondary the weekly one, but the shape is NOT positional
anymore: weekly-only accounts report a lone primary with
``windowDurationMins=10080`` and ``secondary: null``. So we classify each
window by its duration (positional only as a fallback) and normalize to
jacked's ``five_hour``/``seven_day`` shape, writing the same cache columns the
Anthropic path uses, so every downstream consumer (menubar, panel, auto-swap)
is unchanged. A window an AUTHORITATIVE payload no longer reports gets its
cache column CLEARED — otherwise a dead window's last reading survives forever
(a weekly-only account kept showing a stale 100% "7d" from a window that
expired days earlier). A partial or unrecognized payload never clears: it
preserves the last known value and logs a WARNING instead.

This is NOT the ToS-risky chatgpt.com backend scrape and NOT the rollout-jsonl
file (null since gpt-5.4) — it's the supported RPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import shutil
import time
from pathlib import Path
from typing import Mapping, Optional

from jacked.service.menubar_summary import _epoch_to_iso

from .credentials import codex_home

logger = logging.getLogger(__name__)

# Whole handshake + read budget. A usage read is fast (~1-2s); this is a safety
# cap for a hung app-server. Kept tight because fetch_codex_usage holds the swap
# lock for this whole duration, and a user swap waits on that lock.
_APP_SERVER_TIMEOUT = 12.0


class CodexUsageError(Exception):
    """The codex app-server call failed (no binary, timeout, RPC error)."""


# A window under a day (24 * 60 minutes) is the "short" (5h-style) window;
# anything longer is the weekly one. Today's real durations are 300 and 10080.
_SHORT_WINDOW_MAX_MINS = 1440

# Payload anomalies already warned about this process — steady-state repeats
# (an old codex client on every poll) drop to DEBUG so WARNING stays signal.
_warned_shapes: set = set()


def _usable_duration(dur) -> bool:
    """A duration we may classify by: a finite positive number.

    ``bool`` is excluded explicitly — JSON ``true`` is an ``int`` to Python
    and would otherwise classify as a 5h window (``True < 1440``). NaN and
    non-positive values fall back to positional mapping too.
    """
    if not isinstance(dur, (int, float)) or isinstance(dur, bool):
        return False
    return math.isfinite(dur) and dur > 0


def _classify_windows(primary, secondary) -> dict:
    """Assign the app-server windows to the ``five_hour``/``seven_day`` slots.

    Classify by ``windowDurationMins`` when usable (weekly-only accounts
    report a lone primary with 10080); fall back to the legacy positional
    mapping (primary=5h, secondary=weekly) only when the duration is missing
    or unusable. A window that would land in an occupied slot is dropped
    rather than misfiled — the cache columns are literally "5h" and "7d", and
    a wrong column is worse than an empty one.

    Returns ``{"five_hour": window|None, "seven_day": window|None,
    "dropped": bool, "fallback": bool}``. ``dropped``/``fallback`` flag the
    two states where the payload shape was NOT fully understood — both log a
    WARNING (a shape change must be visible in default-level logs, not
    silently misfiled: that is how the positional bug shipped) and both veto
    cache clearing downstream.
    """
    slots: dict = {
        "five_hour": None,
        "seven_day": None,
        "dropped": False,
        "fallback": False,
    }
    for positional_slot, window in (
        ("five_hour", primary),
        ("seven_day", secondary),
    ):
        if not window:
            continue
        dur = window.get("windowDurationMins")
        if _usable_duration(dur):
            slot = "five_hour" if dur < _SHORT_WINDOW_MAX_MINS else "seven_day"
        else:
            slot = positional_slot
            slots["fallback"] = True
            # WARNING once per distinct shape, DEBUG after: an old codex
            # client that simply lacks the field is a stable steady state,
            # and a per-poll WARNING would train people to ignore warnings.
            key = f"fallback:{positional_slot}:{dur!r}"
            log = logger.debug if key in _warned_shapes else logger.warning
            _warned_shapes.add(key)
            log(
                "codex rate-limit window has no usable windowDurationMins "
                "(got %r) — using positional %s mapping; the app-server "
                "payload shape may have changed or the codex client is old",
                dur, positional_slot,
            )
        if slots[slot] is None:
            slots[slot] = window
        else:
            slots["dropped"] = True
            logger.warning(
                "dropping codex rate-limit window that also classifies as "
                "%s: %r — the app-server payload shape may have changed",
                slot, window,
            )
    return slots


def normalize_rate_limits(result: Mapping) -> dict:
    """Map an ``account/rateLimits/read`` result to jacked's two-window shape.

    Returns ``{"five_hour": {"utilization", "resets_at", "reported"},
    "seven_day": {...}, "windows_complete", "plan_type", "credits",
    "reset_credits", "by_limit"}``. Utilization is the app-server
    ``usedPercent``; resets_at is an ISO string. A window Codex did not
    report comes back as ``{"utilization": None, "resets_at": None,
    "reported": False}``.

    ``windows_complete`` is True only when the payload was AUTHORITATIVE
    about which windows exist: both window keys present (an explicit null
    counts as present), every present window classified by a recognized
    duration, and nothing dropped. Only then may an empty slot be read as
    "this window no longer exists" — a partial or unrecognized payload must
    preserve the last known cache, never clear it.
    """
    result = result or {}
    rl = result.get("rateLimits") or {}
    slots = _classify_windows(rl.get("primary"), rl.get("secondary"))
    five = slots["five_hour"] or {}
    seven = slots["seven_day"] or {}
    return {
        "five_hour": {
            "utilization": five.get("usedPercent"),
            "resets_at": _epoch_to_iso(five.get("resetsAt")),
            "reported": slots["five_hour"] is not None,
        },
        "seven_day": {
            "utilization": seven.get("usedPercent"),
            "resets_at": _epoch_to_iso(seven.get("resetsAt")),
            "reported": slots["seven_day"] is not None,
        },
        "windows_complete": (
            "primary" in rl
            and "secondary" in rl
            and not slots["dropped"]
            and not slots["fallback"]
        ),
        "plan_type": rl.get("planType"),
        "credits": rl.get("credits"),
        "reset_credits": result.get("rateLimitResetCredits"),
        "by_limit": result.get("rateLimitsByLimitId"),
    }


async def _send(proc: asyncio.subprocess.Process, obj: dict) -> None:
    assert proc.stdin is not None
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    await proc.stdin.drain()


async def _read_result(proc: asyncio.subprocess.Process, want_id: int) -> dict:
    """Read newline-delimited JSON-RPC until the message with ``id == want_id``.

    Notifications (no/other id) are skipped. Raises ``CodexUsageError`` on a
    JSON-RPC ``error`` or if the stream closes first.
    """
    assert proc.stdout is not None
    while True:
        line = await proc.stdout.readline()
        if not line:
            raise CodexUsageError("codex app-server closed the stream unexpectedly")
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue  # tolerate non-JSON log lines
        if msg.get("id") != want_id:
            continue  # a notification or a different request's reply
        if msg.get("error"):
            raise CodexUsageError(f"codex app-server error: {msg['error']}")
        return msg.get("result") or {}


async def _drive(proc: asyncio.subprocess.Process, client_version: str) -> dict:
    await _send(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "jacked",
                    "title": "jacked",
                    "version": client_version,
                }
            },
        },
    )
    await _read_result(proc, 1)
    await _send(proc, {"jsonrpc": "2.0", "method": "initialized", "params": {}})
    await _send(
        proc,
        {"jsonrpc": "2.0", "id": 2, "method": "account/rateLimits/read", "params": {}},
    )
    return await _read_result(proc, 2)


def _client_version() -> str:
    try:
        from jacked import __version__

        return str(__version__)
    except Exception:
        return "0"


async def read_codex_rate_limits(
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    timeout: float = _APP_SERVER_TIMEOUT,
    codex_bin: Optional[str] = None,
) -> dict:
    """Drive ``codex app-server`` and return the raw ``rateLimits`` result.

    ``home`` selects which account's usage is read (via ``CODEX_HOME``) — per
    Codex's single-active-account model. ``codex_bin`` is injectable for tests.
    Raises ``CodexUsageError`` on any failure.
    """
    home = home if home is not None else codex_home(env)
    codex = codex_bin or shutil.which("codex")
    if not codex:
        raise CodexUsageError("the `codex` CLI is not installed or not on PATH")

    run_env = dict(env if env is not None else os.environ)
    run_env["CODEX_HOME"] = str(home)

    try:
        proc = await asyncio.create_subprocess_exec(
            codex,
            "app-server",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # DEVNULL, not PIPE: we only read stdout, and an undrained stderr
            # PIPE can fill the OS buffer (~64KB) and stall the child mid-write.
            stderr=asyncio.subprocess.DEVNULL,
            env=run_env,
        )
    except (OSError, ValueError) as exc:
        raise CodexUsageError(f"failed to start codex app-server: {exc}") from exc

    try:
        return await asyncio.wait_for(_drive(proc, _client_version()), timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise CodexUsageError("codex app-server timed out") from exc
    finally:
        await _terminate(proc)


async def _terminate(proc: asyncio.subprocess.Process) -> None:
    """Stop the app server and drain its pipes before the event loop closes."""

    if proc.returncode is None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    cleanup = asyncio.create_task(proc.communicate())
    try:
        await asyncio.wait_for(asyncio.shield(cleanup), timeout=3)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await cleanup


def live_codex_account_id(db, env: Optional[Mapping[str, str]] = None) -> Optional[int]:
    """The jacked Codex account currently live in the shared ~/.codex root, or
    None. Codex reports usage for whichever account is in the shared auth.json,
    so ONLY this account may be polled against the root — polling a non-active
    account there would cache the wrong numbers (and risk rotating its tokens).
    """
    from .credentials import extract_identity, read_auth_json
    from .switching import find_codex_account_id

    auth = read_auth_json(codex_home(env), env)
    if not auth:
        return None
    return find_codex_account_id(db, extract_identity(auth))


async def fetch_codex_usage(
    account_id: int,
    db,
    state: Optional[dict] = None,
    home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
    codex_bin: Optional[str] = None,
) -> Optional[dict]:
    """Fetch + normalize + cache Codex usage. Mirrors the Anthropic leaf in
    ``web/auth.fetch_usage`` (same cache columns, same error bookkeeping).

    Returns the raw app-server result on success, ``None`` on failure, or a
    ``{"_cached": True}`` sentinel when a swap holds the lock (poll skipped).
    """
    # Serialize against swap_codex_account on the shared root: the app-server we
    # spawn can rotate the root's single-use refresh token, so it must not
    # overlap a swap (that would lose the rotation, #15502). Non-blocking — if a
    # swap holds the lock, skip this poll and keep the cached usage.
    from .credentials import extract_identity, read_auth_json
    from .switching import _codex_swap_lock, find_codex_account_id

    lock_base = home if home is not None else codex_home(env)
    with _codex_swap_lock(lock_base, retries=1) as locked:
        if not locked:
            logger.debug("Codex usage poll skipped for %s — swap in progress", account_id)
            return {"_cached": True}
        # TOCTOU guard: the caller's live-account gate runs OUTSIDE this lock,
        # so a swap can complete in the gap and land another account in the
        # root. Re-resolve the root's identity under the lock — a read of the
        # wrong account must never be written (or clear columns) under this
        # account_id. No auth.json means an unswappable rig (tests, custom
        # home): nothing to re-verify, proceed.
        auth = read_auth_json(lock_base, env)
        if auth is not None:
            live_id = find_codex_account_id(db, extract_identity(auth))
            if live_id != account_id:
                logger.info(
                    "Codex usage poll skipped for %s — root now holds account %s",
                    account_id, live_id,
                )
                return {"_cached": True}
        try:
            result = await read_codex_rate_limits(
                home=home, env=env, codex_bin=codex_bin
            )
        except CodexUsageError as exc:
            logger.warning(
                "Codex usage fetch failed for account %s: %s", account_id, exc
            )
            try:
                db.record_account_error(account_id, str(exc))
            except Exception:  # pragma: no cover - bookkeeping must not mask failure
                logger.debug("record_account_error failed", exc_info=True)
            return None

    norm = normalize_rate_limits(result)
    five = norm["five_hour"]
    seven = norm["seven_day"]
    # An all-None read (empty/partial app-server result) is a soft failure — do
    # NOT stamp it fresh + mark valid, or a degraded read masquerades as healthy.
    if five["utilization"] is None and seven["utilization"] is None:
        logger.warning("Codex usage for account %s returned no windows", account_id)
        try:
            # SOFT failure: a brand-new account (pre-first-use) or an API-key
            # account legitimately has no plan windows — record it without
            # incrementing consecutive_failures, or it would be starved out of
            # auto-swap fallback (get_fallback_account excludes failures >= 3).
            db.record_account_error(
                account_id,
                "codex app-server returned no rate limits",
                increment_failures=False,
            )
        except Exception:  # pragma: no cover
            logger.debug("record_account_error failed", exc_info=True)
        return None
    # A window Codex AUTHORITATIVELY no longer reports must CLEAR its cache
    # column — a weekly-only account's dead 5h/7d reading would otherwise
    # persist forever (stale 100% displayed and fed to auto-swap). But only
    # a complete, fully-understood payload may clear: a partial read, an
    # unrecognized shape (fallback/drop), or a window present without a
    # usedPercent preserves the last known value instead of destroying it.
    clear_five = norm["windows_complete"] and not five["reported"]
    clear_seven = norm["windows_complete"] and not seven["reported"]
    db.update_account_usage_cache(
        account_id,
        five_hour=five["utilization"],
        seven_day=seven["utilization"],
        # A resets_at only travels WITH its percent — a window that carries a
        # resetsAt but no usedPercent must not pair a fresh reset time with
        # the preserved stale percent.
        five_hour_resets_at=(
            five["resets_at"] if five["utilization"] is not None else None
        ),
        seven_day_resets_at=(
            seven["resets_at"] if seven["utilization"] is not None else None
        ),
        clear_five_hour=clear_five,
        clear_seven_day=clear_seven,
        raw=result,
    )
    # The rate-limits read carries the CURRENT ChatGPT plan — keep the stored
    # subscription in sync so an upgrade/downgrade (Plus <-> Pro) shows up on
    # the dashboard without re-adding the account. (At add time the plan comes
    # from the id_token claim and was never refreshed before this.)
    plan = norm.get("plan_type")
    if plan:
        try:
            acct = db.get_account(account_id)
            if acct and acct.get("subscription_type") != plan:
                db.update_account(account_id, subscription_type=plan)
                logger.info(
                    "Codex account %s plan changed: %s -> %s",
                    account_id, acct.get("subscription_type"), plan,
                )
        except Exception:  # pragma: no cover - bookkeeping must not mask usage
            logger.debug("codex plan sync failed", exc_info=True)
    try:
        db.clear_account_errors(account_id)
    except Exception:  # pragma: no cover
        logger.debug("clear_account_errors failed", exc_info=True)
    if state is not None:
        state["last_fetched_at"] = time.time()
    # The one line the on-call reads: carry the window values AND any clear on
    # it, so a card that blanks on the dashboard is explainable from default
    # logs (a clear is data disappearing — it must never be silent).
    logger.info(
        "Codex usage fetched for account %s: 5h=%s 7d=%s",
        account_id,
        _window_log_state(five, clear_five),
        _window_log_state(seven, clear_seven),
    )
    return result


def _window_log_state(window: Mapping, cleared: bool) -> str:
    if cleared:
        return "cleared (window no longer reported)"
    if window["utilization"] is None:
        return "kept (not in this read)"
    return f"{window['utilization']}%"
