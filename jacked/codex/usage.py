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
is unchanged. A window Codex no longer reports gets its cache column CLEARED —
otherwise a dead window's last reading survives forever (a weekly-only account
kept showing a stale 100% "7d" from a window that expired days earlier).

This is NOT the ToS-risky chatgpt.com backend scrape and NOT the rollout-jsonl
file (null since gpt-5.4) — it's the supported RPC.
"""

from __future__ import annotations

import asyncio
import json
import logging
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


# A window under a day is the "short" (5h-style) window; anything longer is
# the weekly one. Today's real durations are 300 and 10080 minutes.
_SHORT_WINDOW_MAX_MINS = 1440


def _classify_windows(primary, secondary) -> dict:
    """Assign the app-server windows to the ``five_hour``/``seven_day`` slots.

    Classify by ``windowDurationMins`` when present (weekly-only accounts
    report a lone primary with 10080); fall back to the legacy positional
    mapping (primary=5h, secondary=weekly) only when the duration is missing.
    A window that would land in an occupied slot is dropped rather than
    misfiled — the cache columns are literally "5h" and "7d", and a wrong
    column is worse than an empty one.
    """
    slots: dict = {"five_hour": None, "seven_day": None}
    for positional_slot, window in (
        ("five_hour", primary),
        ("seven_day", secondary),
    ):
        if not window:
            continue
        dur = window.get("windowDurationMins")
        if isinstance(dur, (int, float)) and not isinstance(dur, bool):
            slot = "five_hour" if dur < _SHORT_WINDOW_MAX_MINS else "seven_day"
        else:
            slot = positional_slot
        if slots[slot] is None:
            slots[slot] = window
        else:
            logger.debug("dropping extra %s rate-limit window: %r", slot, window)
    return slots


def normalize_rate_limits(result: Mapping) -> dict:
    """Map an ``account/rateLimits/read`` result to jacked's two-window shape.

    Returns ``{"five_hour": {"utilization", "resets_at"}, "seven_day": {...},
    "plan_type", "credits", "reset_credits", "by_limit"}``. Utilization is the
    app-server ``usedPercent``; resets_at is an ISO string. A window Codex did
    not report comes back as ``{"utilization": None, "resets_at": None}``.
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
        },
        "seven_day": {
            "utilization": seven.get("usedPercent"),
            "resets_at": _epoch_to_iso(seven.get("resetsAt")),
        },
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
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=3)
    except (asyncio.TimeoutError, ProcessLookupError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


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
    from .switching import _codex_swap_lock

    lock_base = home if home is not None else codex_home(env)
    with _codex_swap_lock(lock_base, retries=1) as locked:
        if not locked:
            logger.debug("Codex usage poll skipped for %s — swap in progress", account_id)
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
    # A window Codex no longer reports must CLEAR its cache column, not skip
    # it — a weekly-only account's dead 5h/7d reading would otherwise persist
    # forever (stale 100% displayed and fed to auto-swap).
    db.update_account_usage_cache(
        account_id,
        five_hour=five["utilization"],
        seven_day=seven["utilization"],
        five_hour_resets_at=five["resets_at"],
        seven_day_resets_at=seven["resets_at"],
        clear_five_hour=five["utilization"] is None,
        clear_seven_day=seven["utilization"] is None,
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
    logger.info("Codex usage fetched for account %s", account_id)
    return result
