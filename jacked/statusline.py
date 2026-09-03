"""Claude Code statusline renderer.

Claude Code runs the registered statusline command on every refresh and
shows the first line of stdout. This module reads the session JSON on
stdin and prints one ANSI-colored line:

  Fable 5 [xhigh] | ctx 63% (633k/1.0M) | 5h 7%->14:00 | 7d 88%->Sat 02:37 | Fable 96%->Sat 10:59 | me@co.com

A "<served> (FALLBACK, not <configured>)" segment appears between the limits
and the account when the model actually ANSWERING is not the model the
session is configured with right now -- a serving mismatch introduced by a
gateway, never the user's own /model choice (see _served_segment). It is
absent on every normally-served session, and it clears itself the moment the
user switches models deliberately.

The installer registers it as `"<abs-python>" -m jacked.statusline` with
the absolute interpreter path resolved at install time (never a bare
`python`/`python3` name -- name resolution is the cross-platform failure
mode this design avoids).

Data sources:
- stdin (Claude Code): model, effort, context window, and the ONLY two
  rate-limit windows the payload carries -- five_hour and seven_day.
- ${CLAUDE_CONFIG_DIR:-~/.claude}/jacked-resolver-snapshot.json: an atomic,
  secret-free account observation published by jacked's canonical resolver.
- ~/.claude/jacked.db, READ-ONLY: jacked's cache of the Anthropic usage
  API, which is where the "Fable 96%" model-scoped weekly segment comes
  from. The stdin payload has no model-scoped limit at all, and its
  aggregate seven_day number systematically UNDER-reports the binding
  constraint: on every real account measured, the model-scoped weekly
  percentage sits above the aggregate one (96% vs 76%, 100% vs 90%), so
  "7d" alone hides the limit that actually stops the session. jacked polls
  that API on a timer, so the segment is marked stale past
  _USAGE_FRESH_SECONDS and dropped past _USAGE_MAX_AGE_SECONDS rather
  than presenting a lagging number as live.

Hard constraints:
- stdlib only, and never import jacked.cli (click + rich cost ~50ms per
  render; this must stay well under the ~300ms refresh budget). sqlite3
  costs ~12ms to import and is therefore deferred to the one code path
  that needs it.
- Always exit 0 with whatever could be rendered. A broken statusline
  must never break or spam the session.
- Absence is normal, not an error: rate_limits appears only after the
  first API response, current_usage is null before the first call and
  after /compact. A missing field, a missing database, a locked database
  or a missing row drops its segment silently.
- Never write to jacked.db, never create it, never migrate it. The jacked
  service owns that file and writes it concurrently.
- Never read Claude credential or metadata files. A stale, conflicting, or
  incomplete resolver snapshot renders the desired account plus runtime
  unknown instead of inferring identity.
"""

import json
import os
import sys
import time

from jacked.statusline_account import account_facts
from jacked.statusline_cache import _read_cache, _write_cache
from jacked.statusline_common import (
    ARROW,
    BOLD_CYAN,
    CAVE,
    DIM,
    GREEN as GREEN,
    MAGENTA,
    MIDDOT as MIDDOT,
    RED as RED,
    RESET,
    SEP,
    YELLOW,
    _fmt_reset,
    _fmt_tokens,
    _pct_color,
    _round_pct,
    _sum_usage,
    _tier_label as _tier_label,
)
from jacked.statusline_transcript import _cost_segment, _served_segment

_USAGE_CACHE_VERSION = 1
_USAGE_FRESH_SECONDS = 2 * 3600
_USAGE_MAX_AGE_SECONDS = 24 * 3600
_USAGE_CACHE_BUCKET_SECONDS = 600


def _home() -> str:
    """Resolve the home dir. $JACKED_HOME wins so tests can redirect."""
    return os.environ.get("JACKED_HOME") or os.path.expanduser("~")


def _account_facts(home: str, now: float | None = None) -> dict:
    """Resolve account facts while preserving the patchable JSON reader."""
    return account_facts(home, time.time() if now is None else now, json_load=json.load)


def _iso_to_epoch(value) -> "float | None":
    """ISO-8601 timestamp string -> epoch seconds, or None if unparseable.

    The stdin payload gives resets_at as epoch seconds, but the Anthropic
    usage API (and so jacked's cache of it) gives an ISO-8601 string with
    an offset, e.g. "2026-08-15T14:59:59.089483+00:00". A timestamp with
    no offset is read as UTC, which is what that API emits.
    """
    if not isinstance(value, str) or not value:
        return None
    # Deferred: only the database path needs datetime, and most renders
    # never reach it.
    import datetime

    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.timezone.utc)
    try:
        return parsed.timestamp()
    except (OSError, OverflowError, ValueError):
        return None


def _normalize_model(name) -> str:
    """Lowercased alphanumerics only, for tolerant model-name comparison.

    >>> _normalize_model("Fable 5")
    'fable5'
    >>> _normalize_model("Claude Opus 4.5")
    'claudeopus45'
    >>> _normalize_model(None)
    ''
    """
    if not isinstance(name, str):
        return ""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def _model_matches(payload_name, bucket_name) -> bool:
    """True when two model names plausibly name the same model.

    The stdin payload names the model more fully than the usage API's
    scope does ("Fable 5" vs "Fable"), and either side may be the longer
    one as naming shifts, so normalized containment in EITHER direction
    counts as a match. Case, spaces, dots and dashes are ignored.

    >>> _model_matches("Fable 5", "Fable")
    True
    >>> _model_matches("Opus", "Claude Opus 4.5")
    True
    >>> _model_matches("Sonnet 5", "Fable")
    False
    >>> _model_matches("", "Fable")
    False
    """
    left = _normalize_model(payload_name)
    right = _normalize_model(bucket_name)
    if not left or not right:
        return False
    return left in right or right in left


def _bucket_model_name(entry) -> str:
    """The display name a weekly_scoped limit is scoped to, or ""."""
    scope = entry.get("scope") if isinstance(entry, dict) else None
    model = scope.get("model") if isinstance(scope, dict) else None
    name = model.get("display_name") if isinstance(model, dict) else None
    return name if isinstance(name, str) else ""


def _scoped_bucket(limits, model_name) -> "dict | None":
    """Pick the model-scoped weekly limit worth showing, or None.

    Prefers the bucket scoped to the model this session is actually
    running; falls back to whichever scoped bucket the API marks active.
    """
    if not isinstance(limits, list):
        return None
    active = None
    for entry in limits:
        if not isinstance(entry, dict) or entry.get("kind") != "weekly_scoped":
            continue
        if _model_matches(model_name, _bucket_model_name(entry)):
            return entry
        if active is None and entry.get("is_active") is True:
            active = entry
    return active


def _usage_source_signature(
    stat_result,
    email: str,
    org_uuid: str,
    model_name: str,
    now: float,
) -> dict:
    """Revision key for the model-scoped usage segment.

    Deliberately stats the database rather than digesting it: jacked.db
    reaches tens of megabytes and hashing it on every refresh would cost
    more than the query it is meant to avoid. Every jacked write bumps
    mtime_ns, which is the revision signal that matters. The account and
    the model are part of the key because switching either one changes
    which number is correct, and the wall-clock bucket is part of it so a
    cached segment cannot claim to be fresh forever.
    """
    return {
        "mtime_ns": stat_result.st_mtime_ns,
        "size": stat_result.st_size,
        "device": stat_result.st_dev,
        "inode": stat_result.st_ino,
        "account": email + "\x00" + org_uuid,
        "model": model_name,
        "clock": int(now // _USAGE_CACHE_BUCKET_SECONDS),
    }


def _read_usage_cache(cache_path: str, source: dict) -> "str | None":
    """Cached usage segment for this revision, or None on any miss."""
    cached = _read_cache(cache_path, _USAGE_CACHE_VERSION, source)
    if cached is None:
        return None
    segment = cached.get("segment")
    return segment if isinstance(segment, str) else None


def _write_usage_cache(cache_path: str, segment: str, source: dict) -> None:
    """Persist the usage segment against its revision (empty ones too)."""
    _write_cache(
        cache_path,
        ".statusline-usage-",
        _USAGE_CACHE_VERSION,
        source,
        {"segment": segment},
    )


def _read_cached_usage(db_path: str, email: str, org_uuid: str):
    """One account's cached usage limits from jacked.db, or (None, None).

    Opens the database READ-ONLY with a short timeout and never writes,
    creates or migrates it -- the jacked service owns that file and writes
    it concurrently, so a lock, a partial write or schema drift must
    degrade to no segment rather than to an exception or a hang.

    Matches on email AND organization_uuid together. One email can own
    several organizations (this machine has two under one address), so the
    email alone resolves ambiguously and would show another org's numbers.
    Newest cache wins if the pair somehow matches more than one row.
    """
    # Deferred: importing sqlite3 costs ~12ms, and a render that hits the
    # cache or has no database must not pay it.
    import sqlite3

    # "?" and "#" would otherwise be read as URI syntax rather than as
    # part of the path.
    uri = "file:" + db_path.replace("?", "%3f").replace("#", "%23") + "?mode=ro"
    try:
        con = sqlite3.connect(uri, uri=True, timeout=0.25)
    except (sqlite3.Error, OSError, ValueError):
        return None, None
    try:
        row = con.execute(
            "SELECT cached_usage_raw, usage_cached_at FROM accounts "
            "WHERE email = ? AND organization_uuid = ? "
            "ORDER BY usage_cached_at DESC LIMIT 1",
            (email, org_uuid),
        ).fetchone()
    except (sqlite3.Error, OSError, ValueError):
        return None, None
    finally:
        try:
            con.close()
        except sqlite3.Error:
            pass
    if not row:
        return None, None
    raw, cached_at = row[0], row[1]
    if not isinstance(raw, str) or not raw:
        return None, None
    try:
        blob = json.loads(raw)
    except (RecursionError, ValueError):
        return None, None
    if not isinstance(blob, dict):
        return None, None
    return blob.get("limits"), cached_at


def _build_usage_segment(
    db_path: str,
    email: str,
    org_uuid: str,
    model_name: str,
    now: float,
) -> str:
    """Render "Fable 96%->Sat 10:59" from jacked's cached usage, or ""."""
    limits, cached_at = _read_cached_usage(db_path, email, org_uuid)
    if limits is None:
        return ""
    if isinstance(cached_at, bool) or not isinstance(cached_at, (int, float)):
        return ""
    age = now - cached_at
    if age > _USAGE_MAX_AGE_SECONDS:
        # More than a day behind: drop it rather than mislead.
        return ""
    bucket = _scoped_bucket(limits, model_name)
    if bucket is None:
        return ""
    pct = _round_pct(bucket.get("percent"))
    if pct is None:
        return ""
    label = _bucket_model_name(bucket)
    if not label:
        # The whole point of this segment is naming WHICH model's weekly
        # cap is binding. An unlabeled percentage beside the existing 7d
        # number is noise, so drop it.
        return ""
    seg = f"{label} {_pct_color(pct)}{pct}%{RESET}"
    if age > _USAGE_FRESH_SECONDS:
        seg = f"{DIM}~{RESET}" + seg
    reset = _fmt_reset(_iso_to_epoch(bucket.get("resets_at")), now)
    if reset:
        seg += f"{ARROW}{reset}"
    return seg


def _model_usage_segment(home: str, facts: dict, model_name: str, now: float) -> str:
    """Model-scoped weekly usage for this session's model, revision-cached.

    Claude Code's payload carries no model-scoped rate limit at all, and
    its aggregate seven_day number under-reports the binding constraint --
    the per-model weekly percentage runs higher on every real account
    measured. jacked already polls and caches the usage API that does
    carry it, so read that cache instead of inventing a new API call.
    """
    email = facts.get("email") or ""
    org_uuid = facts.get("org_uuid") or ""
    if not email or not org_uuid:
        return ""
    db_path = os.path.join(home, ".claude", "jacked.db")
    cache_path = os.path.join(home, ".claude", "statusline-usage.cache")
    try:
        source = _usage_source_signature(
            os.stat(db_path), email, org_uuid, model_name, now
        )
    except (OSError, ValueError):
        return ""
    cached = _read_usage_cache(cache_path, source)
    if cached is not None:
        return cached
    try:
        segment = _build_usage_segment(db_path, email, org_uuid, model_name, now)
    except (OSError, ValueError):
        return ""
    _write_usage_cache(cache_path, segment, source)
    return segment


def _caveman_segment(home: str) -> str:
    """Badge for the caveman plugin's flag file, when present."""
    flag = os.path.join(home, ".claude", ".caveman-active")
    try:
        with open(flag, encoding="utf-8", errors="replace") as fh:
            mode = fh.readline().strip()
    except OSError:
        return ""
    if not mode or mode == "full":
        return f"{CAVE}[CAVEMAN]{RESET}"
    return f"{CAVE}[CAVEMAN:{mode.upper()}]{RESET}"


def render(payload, home: "str | None" = None, now: "float | None" = None) -> str:
    """Build the one-line statusline from a parsed payload dict."""
    if home is None:
        home = _home()
    if now is None:
        now = time.time()
    if not isinstance(payload, dict):
        payload = {}
    segments = []

    model = payload.get("model") or {}
    name = model.get("display_name") if isinstance(model, dict) else None
    model_name = name if isinstance(name, str) else ""
    if isinstance(name, str) and name:
        seg = f"{BOLD_CYAN}{name}{RESET}"
        effort = payload.get("effort") or {}
        level = effort.get("level") if isinstance(effort, dict) else None
        if isinstance(level, str) and level:
            seg += f" {YELLOW}[{level}]{RESET}"
        if payload.get("fast_mode") is True:
            seg += f" {MAGENTA}[fast]{RESET}"
        segments.append(seg)

    ctx = payload.get("context_window") or {}
    if isinstance(ctx, dict):
        pct = _round_pct(ctx.get("used_percentage"))
        if pct is not None:
            seg = f"ctx {_pct_color(pct)}{pct}%{RESET}"
            used = _sum_usage(ctx.get("current_usage"))
            size = ctx.get("context_window_size")
            if (
                used is not None
                and isinstance(size, (int, float))
                and not isinstance(size, bool)
            ):
                seg += f" ({_fmt_tokens(used)}/{_fmt_tokens(int(size))})"
            segments.append(seg)

    limits = payload.get("rate_limits") or {}
    if isinstance(limits, dict):
        for key, label in (("five_hour", "5h"), ("seven_day", "7d")):
            window = limits.get(key) or {}
            if not isinstance(window, dict):
                continue
            pct = _round_pct(window.get("used_percentage"))
            if pct is None:
                continue
            seg = f"{label} {_pct_color(pct)}{pct}%{RESET}"
            reset = _fmt_reset(window.get("resets_at"), now)
            if reset:
                seg += f"{ARROW}{reset}"
            segments.append(seg)

    # Parsed once here so the account segment and model-scoped usage lookup
    # share one read of the resolver's atomic, secret-free snapshot.
    facts = _account_facts(home, now)

    scoped = _model_usage_segment(home, facts, model_name, now)
    if scoped:
        segments.append(scoped)

    served = _served_segment(payload)
    if served:
        segments.append(served)

    cost = _cost_segment(payload)
    if cost:
        segments.append(cost)

    account = facts["segment"]
    if account:
        segments.append(account)
    badge = _caveman_segment(home)
    if badge:
        segments.append(badge)
    return SEP.join(segments)


def main() -> int:
    """Read stdin, render, print. Exit 0 no matter what."""
    try:
        if sys.platform == "win32":
            # Legacy cp1252/cp437 consoles cannot encode the arrow or the
            # middle dot; replace rather than die (same guard as jacked.cli).
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, OSError, ValueError):
                pass
        raw = sys.stdin.read()
        try:
            payload = json.loads(raw) if raw and raw.strip() else {}
        except ValueError:
            payload = {}
        line = render(payload)
        print(line)
    except BaseException:
        # Never break the session over a statusline bug. Set
        # JACKED_STATUSLINE_DEBUG=1 to see the traceback on stderr
        # (Claude Code only reads stdout).
        if os.environ.get("JACKED_STATUSLINE_DEBUG"):
            import traceback

            traceback.print_exc()
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
