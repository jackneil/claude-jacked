"""Claude Code statusline renderer.

Claude Code runs the registered statusline command on every refresh and
shows the first line of stdout. This module reads the session JSON on
stdin and prints one ANSI-colored line:

  Fable 5 [xhigh] | ctx 63% (633k/1.0M) | 5h 7%->14:00 | 7d 88%->Sat 02:37 | me@co.com · MyOrg · Max 5x

A "<model> (FALLBACK, not <model>)" segment appears between the limits and
the account when a gateway has switched the model mid-session (see
_served_segment); it is absent on any session that never switched.

The installer registers it as `"<abs-python>" -m jacked.statusline` with
the absolute interpreter path resolved at install time (never a bare
`python`/`python3` name -- name resolution is the cross-platform failure
mode this design avoids).

Hard constraints:
- stdlib only, and never import jacked.cli (click + rich cost ~50ms per
  render; this must stay well under the ~300ms refresh budget).
- Always exit 0 with whatever could be rendered. A broken statusline
  must never break or spam the session.
- Absence is normal, not an error: rate_limits appears only after the
  first API response, current_usage is null before the first call and
  after /compact. A missing field drops its segment.
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

_ACCOUNT_CACHE_VERSION = 2


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


def _read_account_cache(cache_path: str, source: dict) -> "str | None":
    """Return a cached segment only when it belongs to this source revision."""
    try:
        with open(cache_path, encoding="utf-8", errors="replace") as fh:
            cached = json.load(fh)
    except (OSError, RecursionError, ValueError):
        return None
    if not isinstance(cached, dict):
        return None
    if cached.get("version") != _ACCOUNT_CACHE_VERSION:
        return None
    if cached.get("source") != source:
        return None
    segment = cached.get("segment")
    return segment if isinstance(segment, str) else None


def _write_account_cache(
    cache_path: str,
    segment: str,
    source: dict,
) -> None:
    """Atomically cache a segment without ever breaking the statusline."""
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(
            dir=os.path.dirname(cache_path),
            prefix=".statusline-account-",
            suffix=".tmp",
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "version": _ACCOUNT_CACHE_VERSION,
                    "source": source,
                    "segment": segment,
                },
                fh,
                separators=(",", ":"),
            )
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


def _account_segment(home: str) -> str:
    """"email · org · plan" from ~/.claude.json .oauthAccount, revision-cached.

    ~/.claude.json is multi-MB and rewritten constantly, so the parsed
    result is cached with a content-bound source signature and reused only
    while that signature matches. Never reads ~/.claude/.credentials.json
    (live OAuth tokens).
    """
    acc_path = os.path.join(home, ".claude.json")
    cache_path = os.path.join(home, ".claude", "statusline-account.cache")
    try:
        with open(acc_path, "rb") as fh:
            content = fh.read()
            source = _account_source_signature(os.fstat(fh.fileno()), content)
    except OSError:
        return ""
    cached = _read_account_cache(cache_path, source)
    if cached is not None:
        return cached

    segment = ""
    try:
        config = json.loads(content.decode("utf-8", errors="replace")) or {}
        account = config.get("oauthAccount") or {}
        if isinstance(account, dict):
            tier = account.get("userRateLimitTier") or account.get(
                "organizationRateLimitTier"
            )
            parts = [
                str(account.get("emailAddress") or ""),
                str(account.get("organizationName") or ""),
                _tier_label(tier),
            ]
            segment = f" {MIDDOT} ".join(p for p in parts if p)
    except (OSError, RecursionError, ValueError):
        return ""
    _write_account_cache(cache_path, segment, source)
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
    if not isinstance(payload, dict):
        payload = {}
    segments = []

    model = payload.get("model") or {}
    name = model.get("display_name") if isinstance(model, dict) else None
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

    served = _served_segment(payload)
    if served:
        segments.append(served)

    account = _account_segment(home)
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
