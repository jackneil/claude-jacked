"""Claude Code statusline renderer.

Claude Code runs the registered statusline command on every refresh and
shows the first line of stdout. This module reads the session JSON on
stdin and prints one ANSI-colored line:

  Fable 5 [xhigh] | ctx 63% (633k/1.0M) | 5h 7%->14:00 | 7d 88%->Sat 02:37 | Fable 96%->Sat 10:59 | me@co.com · MyOrg · Max 5x

A "<model> (FALLBACK, not <model>)" segment appears between the limits and
the account when a gateway has switched the model mid-session (see
_served_segment); it is absent on any session that never switched.

The installer registers it as `"<abs-python>" -m jacked.statusline` with
the absolute interpreter path resolved at install time (never a bare
`python`/`python3` name -- name resolution is the cross-platform failure
mode this design avoids).

Data sources:
- stdin (Claude Code): model, effort, context window, and the ONLY two
  rate-limit windows the payload carries -- five_hour and seven_day.
- ~/.claude.json: the signed-in account (email, organization, plan tier).
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
- Never read ~/.claude/.credentials.json. That file holds live OAuth
  tokens; the statusline resolves the account from ~/.claude.json
  instead. This is a deliberate security boundary, not an oversight.
"""

import hashlib
import json
import os
import sys
import tempfile
import time

RESET = "\033[0m"
BOLD_CYAN = "\033[1;36m"
YELLOW = "\033[33m"
MAGENTA = "\033[35m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
CAVE = "\033[38;5;172m"

SEP = f" {DIM}|{RESET} "
ARROW = "→"
MIDDOT = "·"

# Plan-tier labels for the values seen in ~/.claude.json oauthAccount
# rate-limit tiers (after stripping the "default_claude_" prefix).
_TIER_LABELS = {
    "max_5x": "Max 5x",
    "max_20x": "Max 20x",
    "pro": "Pro",
    "free": "Free",
}

_ACCOUNT_CACHE_VERSION = 3
_USAGE_CACHE_VERSION = 1

# How far behind jacked's cached copy of the usage API may fall before the
# model-scoped segment stops being presented as live. Fresh renders plain,
# stale renders behind a dim "~", anything older is dropped rather than
# shown as if it were current.
_USAGE_FRESH_SECONDS = 2 * 3600
_USAGE_MAX_AGE_SECONDS = 24 * 3600

# The usage cache key carries a coarse wall-clock bucket as well as the
# database revision. Without it, a segment cached while the jacked service
# is stopped (frozen database mtime) would keep claiming to be fresh
# forever; with it, staleness is re-evaluated at worst this many seconds
# late while a running service still invalidates instantly on every write.
_USAGE_CACHE_BUCKET_SECONDS = 600

_EMPTY_ACCOUNT_FACTS = {"segment": "", "email": "", "org_uuid": ""}


def _home() -> str:
    """Resolve the home dir. $JACKED_HOME wins so tests can redirect."""
    return os.environ.get("JACKED_HOME") or os.path.expanduser("~")


def _round_pct(value) -> "int | None":
    """Round a raw percentage float (7.000000000000001) to an int.

    >>> _round_pct(7.000000000000001)
    7
    >>> _round_pct(59.5)
    60
    >>> _round_pct(84.6)
    85
    >>> _round_pct(0)
    0
    >>> _round_pct(None) is None
    True
    >>> _round_pct("7") is None
    True
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return round(value)


def _pct_color(pct: int) -> str:
    """Pressure color: green <60, yellow 60-84, red >=85."""
    if pct >= 85:
        return RED
    if pct >= 60:
        return YELLOW
    return GREEN


def _fmt_tokens(n: int) -> str:
    """512 / 80k / 1.0M -- M kicks in at >=999500 so rounding never shows 1000k.

    >>> _fmt_tokens(512)
    '512'
    >>> _fmt_tokens(80000)
    '80k'
    >>> _fmt_tokens(999499)
    '999k'
    >>> _fmt_tokens(999500)
    '1.0M'
    >>> _fmt_tokens(1000000)
    '1.0M'
    >>> _fmt_tokens(1500000)
    '1.5M'
    """
    if n >= 999500:
        m10 = (n + 50000) // 100000
        return f"{m10 // 10}.{m10 % 10}M"
    if n >= 1000:
        return f"{(n + 500) // 1000}k"
    return str(n)


def _fmt_reset(epoch, now: "float | None" = None) -> str:
    """Epoch seconds -> "14:00" when under 24h away, else "Sat 02:37"."""
    if isinstance(epoch, bool) or not isinstance(epoch, (int, float)):
        return ""
    if now is None:
        now = time.time()
    fmt = "%H:%M" if (epoch - now) < 86400 else "%a %H:%M"
    try:
        return time.strftime(fmt, time.localtime(epoch))
    except (OverflowError, OSError, ValueError):
        return ""


def _sum_usage(usage) -> "int | None":
    """Sum the four token counters of context_window.current_usage."""
    if not isinstance(usage, dict):
        return None
    total = 0
    for key in (
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "output_tokens",
    ):
        value = usage.get(key) or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            value = 0
        total += int(value)
    return total


def _tier_label(raw) -> str:
    """Map a rate-limit tier id to its display label.

    >>> _tier_label("default_claude_max_5x")
    'Max 5x'
    >>> _tier_label("default_claude_max_20x")
    'Max 20x'
    >>> _tier_label("default_claude_pro")
    'Pro'
    >>> _tier_label("custom_thing")
    'custom_thing'
    >>> _tier_label(None)
    ''
    """
    if not isinstance(raw, str) or not raw:
        return ""
    stripped = raw[len("default_claude_"):] if raw.startswith("default_claude_") else raw
    return _TIER_LABELS.get(stripped, stripped)


def _account_source_signature(stat_result, content: bytes) -> dict:
    """Revision fields plus a digest that survives file-identifier reuse."""
    return {
        "mtime_ns": stat_result.st_mtime_ns,
        "ctime_ns": stat_result.st_ctime_ns,
        "size": stat_result.st_size,
        "device": stat_result.st_dev,
        "inode": stat_result.st_ino,
        "digest": hashlib.blake2b(content, digest_size=16).hexdigest(),
    }


def _read_cache(cache_path: str, version: int, source: dict) -> "dict | None":
    """Return a cache record only when it belongs to this source revision."""
    try:
        with open(cache_path, encoding="utf-8", errors="replace") as fh:
            cached = json.load(fh)
    except (OSError, RecursionError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("version") != version:
        return None
    if cached.get("source") != source:
        return None
    return cached


def _write_cache(
    cache_path: str,
    prefix: str,
    version: int,
    source: dict,
    payload: dict,
) -> None:
    """Atomically cache a record without ever breaking the statusline.

    Every failure is swallowed: a cache that cannot be written is a
    slower render, never a broken one.
    """
    record = {"version": version, "source": source}
    record.update(payload)
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(cache_path),
            prefix=prefix,
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(record, fh, separators=(",", ":"))
            fh.write("\n")
        os.replace(tmp, cache_path)
        tmp = None
    except OSError:
        pass
    finally:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _read_account_cache(cache_path: str, source: dict) -> "dict | None":
    """Cached account facts for this source revision, or None on any miss."""
    cached = _read_cache(cache_path, _ACCOUNT_CACHE_VERSION, source)
    if cached is None:
        return None
    facts = {}
    for key in _EMPTY_ACCOUNT_FACTS:
        value = cached.get(key)
        if not isinstance(value, str):
            return None
        facts[key] = value
    return facts


def _write_account_cache(cache_path: str, facts: dict, source: dict) -> None:
    """Persist the parsed ~/.claude.json facts against their revision."""
    _write_cache(
        cache_path,
        ".statusline-account-",
        _ACCOUNT_CACHE_VERSION,
        source,
        facts,
    )


def _account_facts(home: str) -> dict:
    """Everything the statusline needs out of ~/.claude.json, parsed ONCE.

    Returns {"segment", "email", "org_uuid"}: the rendered
    "email · org · plan" segment, plus the identity the model-scoped usage
    lookup matches on. Both come from the same parse deliberately --
    ~/.claude.json is multi-MB and rewritten constantly, so a second reader
    doing its own read + parse would blow the refresh budget.

    The result is cached with a content-bound source signature and reused
    only while that signature matches. Never reads
    ~/.claude/.credentials.json (live OAuth tokens).
    """
    acc_path = os.path.join(home, ".claude.json")
    cache_path = os.path.join(home, ".claude", "statusline-account.cache")
    try:
        with open(acc_path, "rb") as fh:
            content = fh.read()
            source = _account_source_signature(os.fstat(fh.fileno()), content)
    except OSError:
        return dict(_EMPTY_ACCOUNT_FACTS)
    cached = _read_account_cache(cache_path, source)
    if cached is not None:
        return cached

    facts = dict(_EMPTY_ACCOUNT_FACTS)
    try:
        config = json.loads(content.decode("utf-8", errors="replace")) or {}
        account = config.get("oauthAccount") if isinstance(config, dict) else None
        if isinstance(account, dict):
            tier = account.get("userRateLimitTier") or account.get(
                "organizationRateLimitTier"
            )
            facts["email"] = str(account.get("emailAddress") or "")
            facts["org_uuid"] = str(account.get("organizationUuid") or "")
            parts = [
                facts["email"],
                str(account.get("organizationName") or ""),
                _tier_label(tier),
            ]
            facts["segment"] = f" {MIDDOT} ".join(p for p in parts if p)
    except (OSError, RecursionError, ValueError):
        return dict(_EMPTY_ACCOUNT_FACTS)
    _write_account_cache(cache_path, facts, source)
    return facts


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


def _models_in(chunk: str) -> list:
    """Assistant model ids from whole JSONL lines inside a text chunk.

    Lines that do not parse are skipped, which is what makes a partial line
    at a seek boundary harmless. "<synthetic>" is Claude Code's marker for
    locally-generated messages and never names a real model.

    >>> _models_in('{"type":"assistant","message":{"model":"a/b"}}')
    ['a/b']
    >>> _models_in('{"type":"user","message":{"model":"a/b"}}')
    []
    >>> _models_in('trunc{"type":"assistant"\\n{"type":"assistant","message":{"model":"x"}}')
    ['x']
    >>> _models_in('{"type":"assistant","message":{"model":"<synthetic>"}}')
    []
    """
    out = []
    for line in chunk.split("\n"):
        if '"model"' not in line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        message = entry.get("message")
        if not isinstance(message, dict):
            continue
        model = message.get("model")
        if isinstance(model, str) and model and model != "<synthetic>":
            out.append(model)
    return out


def _served_segment(payload) -> str:
    """Warn when the model answering is not the one the session started on.

    Claude Code's payload only ever carries the model the session was
    *configured* with, so a gateway that does fallback routing (OpenRouter
    presets, claude-code-router, LiteLLM) can silently switch the model
    that actually answers -- to a pricier or weaker one -- with nothing on
    screen to show it. The transcript does record the serving model on
    every assistant message, so compare the newest against the session's
    first and speak up only when they differ.

    Renders nothing in the normal case: on a session that never switched,
    the model segment already names the model, and a second copy would be
    noise. That also keeps this free for the majority of users.

    Deliberately does NOT consult environment variables for the expected
    model: a statusline subprocess is not guaranteed to inherit the
    session's environment, and that would fail silently on exactly the
    gateway setups this exists for. The session's own first turn is the
    reliable baseline.

    Reads are bounded to the head and tail of the file. Transcripts reach
    100MB+ and this runs on every refresh, so a full scan would grow
    without limit.
    """
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not path:
        return ""
    tail_bytes = 262144
    # A single assistant message can be larger than the first window (a big
    # file read, a long system prompt), and a window that lands mid-line
    # yields nothing parseable. Grow the head read until a model turns up,
    # with a hard cap so this stays bounded on any file.
    head_windows = (65536, 262144, 1048576, 4194304)
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            first = []
            read_bytes = 0
            for window in head_windows:
                if window > size and read_bytes >= size:
                    break
                fh.seek(0)
                read_bytes = min(size, window)
                first = _models_in(fh.read(read_bytes).decode("utf-8", "replace"))
                if first or read_bytes >= size:
                    break
            if size > read_bytes:
                fh.seek(max(0, size - tail_bytes))
                last = _models_in(fh.read().decode("utf-8", "replace"))
            else:
                last = first
    except (OSError, ValueError):
        return ""
    if not first or not last:
        return ""
    expected, served = first[0], last[-1]
    if served == expected:
        return ""
    short_served = served.rsplit("/", 1)[-1]
    short_expected = expected.rsplit("/", 1)[-1]
    return f"{RED}{short_served} (FALLBACK, not {short_expected}){RESET}"


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
            if used is not None and isinstance(size, (int, float)) and not isinstance(size, bool):
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

    # Parsed once here so the account segment and the model-scoped usage
    # lookup share a single read of the multi-MB ~/.claude.json.
    facts = _account_facts(home)

    scoped = _model_usage_segment(home, facts, model_name, now)
    if scoped:
        segments.append(scoped)

    served = _served_segment(payload)
    if served:
        segments.append(served)

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
