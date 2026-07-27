"""Claude Code statusline renderer.

Claude Code runs the registered statusline command on every refresh and
shows the first line of stdout. This module reads the session JSON on
stdin and prints one ANSI-colored line:

  Fable 5 [xhigh] | ctx 63% (633k/1.0M) | 5h 7%->14:00 | 7d 88%->Sat 02:37 | me@co.com · MyOrg · Max 5x

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

import json
import os
import sys
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


def _account_segment(home: str) -> str:
    """"email · org · plan" from ~/.claude.json .oauthAccount, mtime-cached.

    ~/.claude.json is multi-MB and rewritten constantly, so the parsed
    result is cached and reused until that file's mtime moves past the
    cache's. Never reads ~/.claude/.credentials.json (live OAuth tokens).
    """
    acc_path = os.path.join(home, ".claude.json")
    cache_path = os.path.join(home, ".claude", "statusline-account.cache")
    try:
        acc_mtime = os.path.getmtime(acc_path)
    except OSError:
        return ""
    try:
        # Strictly newer: an equal mtime (coarse-granularity filesystems)
        # re-parses rather than risking a stale account after a switch.
        if os.path.getmtime(cache_path) > acc_mtime:
            with open(cache_path, encoding="utf-8", errors="replace") as fh:
                return fh.readline().strip("\n")
    except OSError:
        pass

    segment = ""
    try:
        with open(acc_path, encoding="utf-8", errors="replace") as fh:
            account = (json.load(fh) or {}).get("oauthAccount") or {}
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
    except (OSError, ValueError):
        return ""
    try:
        with open(cache_path, "w", encoding="utf-8") as fh:
            fh.write(segment + "\n")
    except OSError:
        pass
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
