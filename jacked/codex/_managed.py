"""Managed-block writers for Codex AGENTS.md, config.toml MCP table, and hooks.json."""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path
from typing import Mapping, Optional

from .credentials import codex_home
from ._fsutil import (
    _atomic_write_bytes,
    _atomic_write_text,
    _extract_block,
    _marker_line_count,
    codex_config_toml,
    codex_hooks_json,
)

logger = logging.getLogger(__name__)


_AGENTS_BEGIN = "<!-- BEGIN jacked behaviors (managed by `jacked install`) -->"
_AGENTS_END = "<!-- END jacked behaviors (managed by `jacked install`) -->"


def _install_agents_block(path: Path, body: str) -> None:
    block = f"{_AGENTS_BEGIN}\n{body.strip()}\n{_AGENTS_END}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    begin_ct = _marker_line_count(existing, _AGENTS_BEGIN)
    end_ct = _marker_line_count(existing, _AGENTS_END)
    if begin_ct == 0 and end_ct == 0:
        new = (existing.rstrip("\n") + "\n\n" + block) if existing.strip() else block
    else:
        extracted = _extract_block(existing, _AGENTS_BEGIN, _AGENTS_END)
        if extracted is None:
            logger.warning(
                "unexpected jacked marker layout in %s (begin=%d, end=%d); leaving "
                "it untouched rather than risk clobbering your content",
                path, begin_ct, end_ct,
            )
            return
        pre, _block, post = extracted
        pre = pre.rstrip("\n")
        post = post.lstrip("\n")
        parts = [p for p in (pre, block.rstrip("\n"), post) if p]
        new = "\n\n".join(parts).rstrip("\n") + "\n"
    _atomic_write_text(path, new)


def _strip_agents_block(path: Path) -> bool:
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    extracted = _extract_block(existing, _AGENTS_BEGIN, _AGENTS_END)
    if extracted is None:
        if _marker_line_count(existing, _AGENTS_BEGIN) or _marker_line_count(
            existing, _AGENTS_END
        ):
            logger.warning(
                "unexpected jacked marker layout in %s; leaving it untouched", path
            )
        return False
    pre, _block, post = extracted
    pre = pre.rstrip("\n")
    post = post.lstrip("\n")
    parts = [p for p in (pre, post) if p]
    new = ("\n\n".join(parts).rstrip("\n") + "\n") if parts else ""
    _atomic_write_text(path, new)
    return True


# chrome-devtools MCP block markers + body. The block is a marker-wrapped
# `[mcp_servers.chrome-devtools]` TOML table appended to ~/.codex/config.toml. Its
# command/args MIRROR the Claude side (`_install_chrome_devtools_mcp` in
# jacked/cli.py) so the same server backs both CLIs and Codex skills referencing
# `mcp__chrome-devtools__*` resolve. The markers delimit exactly jacked's own entry
# so install can replace it and uninstall can strip it without touching a user's
# own chrome-devtools table.
_MCP_BEGIN = "# BEGIN jacked chrome-devtools MCP (managed by `jacked install`)"
_MCP_END = "# END jacked chrome-devtools MCP"


def _mcp_block_body() -> str:
    """The `[mcp_servers.chrome-devtools]` TOML table body, built from the SAME
    npx package + autoConnect args the Claude side registers (cli.py's
    ``CHROME_DEVTOOLS_NPX_PACKAGE`` / ``CHROME_DEVTOOLS_MODES["autoConnect"]``), so
    the two CLIs never drift on the version/flags. Imported lazily to keep the
    click CLI out of installer-module import time (like `_codex_qa_hook_command`)."""
    from jacked.cli import CHROME_DEVTOOLS_MODES, CHROME_DEVTOOLS_NPX_PACKAGE

    args = [CHROME_DEVTOOLS_NPX_PACKAGE, *CHROME_DEVTOOLS_MODES["autoConnect"]]
    return (
        "[mcp_servers.chrome-devtools]\n"
        'command = "npx"\n'
        f"args = {json.dumps(args)}"
    )


# ---------------------------------------------------------------------------
# chrome-devtools MCP block in config.toml (marker-wrapped TOML append)
# ---------------------------------------------------------------------------

def _mcp_block() -> str:
    """The full marker-wrapped block, marker-to-marker plus a trailing newline."""
    return f"{_MCP_BEGIN}\n{_mcp_block_body()}\n{_MCP_END}\n"


def _write_mcp_verified(cfg: Path, new_text: str, original: Optional[bytes],
                        status: str) -> str:
    """Atomically write `new_text`, then parse-check the result with tomllib. On
    failure, restore the file to `original` bytes exactly (or delete it when we
    created it, i.e. original is None) and return "skipped-unparseable" - never
    leave a broken config. On success return `status`. The atomic write and the
    post-write parse-check are complementary: the first prevents a torn file, the
    second guarantees the (whole) file we produced is valid TOML."""
    _atomic_write_text(cfg, new_text)
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        if original is None:
            cfg.unlink()
        else:
            _atomic_write_bytes(cfg, original)
        logger.warning(
            "chrome-devtools MCP write to %s produced unparseable TOML; "
            "restored the original and skipped registration", cfg,
        )
        return "skipped-unparseable"
    return status


def ensure_chrome_devtools_mcp(
    home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> str:
    """Register jacked's chrome-devtools MCP server in Codex's config.toml.

    Deterministic, marker-wrapped TOML append (never `codex mcp add`). Returns one
    of:
      - "added"               config.toml was missing (created with just our block)
                              OR it parses, has no chrome-devtools entry, and our
                              block was appended at EOF.
      - "updated"             our marked block was present but its body drifted, so
                              it was replaced in place.
      - "unchanged"           our marked block was present and already current.
      - "preexisting"         the config parses and already has an mcp_servers.
                              chrome-devtools entry OUTSIDE our markers (the user's
                              own) - file left byte-untouched; we never fight it.
      - "skipped-unparseable" config.toml exists but tomllib can't parse it (left
                              untouched) OR a write produced broken TOML (restored).

    Existing content is preserved byte-for-byte before an appended block, and any
    write is parse-checked with the original bytes restored on failure.
    """
    home = home or codex_home(env)
    cfg = codex_config_toml(home)
    block = _mcp_block()
    desired_block = f"{_MCP_BEGIN}\n{_mcp_block_body()}\n{_MCP_END}"

    # Missing config.toml -> create it containing only our marked block.
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        return _write_mcp_verified(cfg, block, None, "added")

    original = cfg.read_bytes()
    try:
        text = original.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        logger.warning(
            "Codex config.toml at %s did not parse; leaving it untouched and "
            "skipping chrome-devtools MCP registration", cfg,
        )
        return "skipped-unparseable"

    # Our marked block already present (whole-line markers) -> replace in place iff
    # its body drifted. Whole-line matching so a marker embedded in a user string
    # can't be mistaken for our block. (A duplicate user chrome-devtools table
    # alongside ours would be a TOML duplicate-key error and never reach here.)
    begin_ct = _marker_line_count(text, _MCP_BEGIN)
    end_ct = _marker_line_count(text, _MCP_END)
    if begin_ct or end_ct:
        extracted = _extract_block(text, _MCP_BEGIN, _MCP_END)
        if extracted is None:
            logger.warning(
                "unexpected jacked chrome-devtools marker layout in %s (begin=%d, "
                "end=%d); leaving it untouched and skipping registration",
                cfg, begin_ct, end_ct,
            )
            return "skipped-unparseable"
        pre, current_block, post = extracted
        if current_block.rstrip("\n") == desired_block:
            return "unchanged"
        return _write_mcp_verified(
            cfg, pre + desired_block + "\n" + post, original, "updated"
        )

    # A user's OWN (unmarked) chrome-devtools entry -> never fight it.
    if "chrome-devtools" in (parsed.get("mcp_servers") or {}):
        return "preexisting"

    # Parses, no entry -> append our block at EOF, separated by a blank line,
    # preserving existing content byte-for-byte as the prefix (append only).
    new_text = block if not text else text + "\n\n" + block
    return _write_mcp_verified(cfg, new_text, original, "added")


def _strip_mcp_block(cfg: Path) -> bool:
    """Strip ONLY jacked's marked chrome-devtools block from config.toml, leaving
    the rest byte-identical (the exact inverse of the append in
    ``ensure_chrome_devtools_mcp``: drop the ``\\n\\n`` separator install inserted
    before the block and the block's own trailing newline). A user's own unmarked
    entry is never touched. Returns True if a block was removed. If nothing but our
    block remained, the (jacked-created) file is deleted."""
    if not cfg.exists():
        return False
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    extracted = _extract_block(text, _MCP_BEGIN, _MCP_END)
    if extracted is None:
        if _marker_line_count(text, _MCP_BEGIN) or _marker_line_count(text, _MCP_END):
            logger.warning(
                "unexpected jacked chrome-devtools marker layout in %s; leaving it "
                "untouched", cfg,
            )
        return False
    # `_extract_block` already consumed the block's own trailing newline into
    # `block`, so `post` is clean; drop the blank-line separator install inserted
    # before the block (which lives at the tail of `pre`).
    pre, _block, post = extracted
    if pre.endswith("\n\n"):
        pre = pre[:-2]
    new_text = pre + post
    if new_text:
        _atomic_write_text(cfg, new_text)
    else:
        cfg.unlink()                     # only our block existed -> jacked-created
    return True


# A jacked-managed hook entry is identified by a marker substring in its command
# (present in both the `"jacked" _hook <name>` shim and the `-m jacked _hook
# <name>` fallback forms _build_hook_command emits), so install/uninstall can
# find and replace exactly its own entries and never a user's.
#   - _LEGACY_HOOK_MARKERS: the retired gatekeeper (removed in 0.70.0). Install
#     prunes with THESE ONLY so it never clobbers the qa entry it just wrote.
#   - _QA_HOOK_MARKERS: jacked's Codex QA-suggestion Stop hook.
#   - _HOOK_MARKERS: both, the default for _remove_codex_hooks (uninstall strips
#     everything jacked ever wrote into hooks.json).
_LEGACY_HOOK_MARKERS = ("_hook security_gatekeeper",)
_QA_HOOK_MARKER = "_hook qa_suggest"
_QA_HOOK_MARKERS = (_QA_HOOK_MARKER,)
_HOOK_MARKERS = _LEGACY_HOOK_MARKERS + _QA_HOOK_MARKERS


# ---------------------------------------------------------------------------
# hooks.json (merge, jacked-owned entries only)
# ---------------------------------------------------------------------------

def _is_jacked_hook_group(group: dict, markers: tuple = _HOOK_MARKERS) -> bool:
    # A hand-malformed hooks.json can carry non-dict group entries (a bare
    # string in the list) or a non-list inner "hooks" value (null/scalar);
    # treat anything that isn't our shape as not-ours.
    if not isinstance(group, dict):
        return False
    inner = group.get("hooks")
    if not isinstance(inner, list):
        return False
    for h in inner:
        cmd = h.get("command", "") if isinstance(h, dict) else ""
        # A present-but-non-string `command` (null/int/bool) is not ours; `.get`
        # only defaults a MISSING key, so guard the value type before `in`.
        if isinstance(cmd, str) and any(m in cmd for m in markers):
            return True
    return False


def _remove_codex_hooks(path: Path, markers: tuple = _HOOK_MARKERS) -> bool:
    """Strip jacked-owned hook groups (matching `markers`) from hooks.json,
    leaving user entries and unknown top-level keys intact. Install passes
    _LEGACY_HOOK_MARKERS (gatekeeper only, so the just-written qa entry survives);
    uninstall uses the default (both). Returns True if anything was removed."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        # A non-object root is not ours to rewrite; leave it byte-untouched.
        return False
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            # A non-list event value isn't our shape; leave it byte-untouched.
            continue
        kept = [g for g in groups if not _is_jacked_hook_group(g, markers)]
        if len(kept) != len(groups):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    # Nothing of ours was present: leave the file byte-identical (don't reformat
    # the user's JSON just for having looked at it).
    if not changed:
        return False
    if not hooks:
        data.pop("hooks", None)
    # If the file is now just an empty object jacked created, remove it.
    if not data:
        path.unlink()
    else:
        _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return changed


def _codex_qa_hook_command() -> str:
    """The Stop-hook command jacked writes into Codex's hooks.json.

    Reuses cli's `_build_hook_command` (the SAME upgrade-safe `"jacked" _hook
    <name>` shim / `"{python}" -m jacked _hook <name>` fallback the Claude side
    writes) and appends `--runtime codex` so the shared qa_suggest.py hook prints
    the Codex `$qa` skill invocation instead of Claude's `/qa`. Imported lazily to
    avoid importing the click CLI at installer-module import time and to keep the
    find_bin fallback logic in ONE place (no duplication)."""
    from jacked.cli import _build_hook_command

    return f"{_build_hook_command('qa_suggest')} --runtime codex"


def _install_codex_qa_hook(home: Optional[Path] = None) -> bool:
    """Idempotently ensure Codex's hooks.json Stop event carries jacked's
    QA-suggest entry.

    The entry is ``{"matcher": "", "hooks": [{"type": "command", "command":
    "<...> _hook qa_suggest --runtime codex"}]}``. OUR entry is marker-identified
    by ``_hook qa_suggest`` in its command (like ``_is_jacked_hook_group``): if
    present with a drifted command it's replaced in place (not duplicated); other
    entries and unknown top-level keys are never touched. Returns True when our
    entry is present after the call.

    If hooks.json exists but is unparseable (bad JSON - including a trailing
    comma - or a non-UTF-8 file) OR its root is not a JSON object, we DO NOT
    write: warn and return False, leaving the user's file byte-identical (mirrors
    ``ensure_chrome_devtools_mcp``'s skipped-unparseable contract). Clobbering
    every user hook to force ours in would be worse than skipping."""
    path = codex_hooks_json(home)
    command = _codex_qa_hook_command()

    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            logger.warning(
                "Codex hooks.json at %s did not parse (%s); leaving it untouched "
                "and skipping the QA hook", path, exc,
            )
            return False
        if not isinstance(loaded, dict):
            logger.warning(
                "Codex hooks.json at %s has a non-object root; leaving it untouched "
                "and skipping the QA hook", path,
            )
            return False
        data = loaded

    # A PRESENT-but-wrong-type "hooks"/"Stop" is a malformed structure we don't
    # own; leave it byte-untouched rather than replacing (and dropping) it. An
    # ABSENT key is fine to create.
    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
        data["hooks"] = hooks
    elif not isinstance(hooks, dict):
        logger.warning(
            "Codex hooks.json at %s has a non-object 'hooks' value; leaving it "
            "untouched and skipping the QA hook", path,
        )
        return False
    stop = hooks.get("Stop")
    if stop is None:
        stop = []
        hooks["Stop"] = stop
    elif not isinstance(stop, list):
        logger.warning(
            "Codex hooks.json at %s has a non-list 'Stop' value; leaving it "
            "untouched and skipping the QA hook", path,
        )
        return False

    entry_hooks = [{"type": "command", "command": command}]
    for group in stop:
        if isinstance(group, dict) and _is_jacked_hook_group(group, _QA_HOOK_MARKERS):
            group.setdefault("matcher", "")
            group["hooks"] = entry_hooks  # replace ours in place (command may drift)
            break
    else:
        stop.append({"matcher": "", "hooks": entry_hooks})

    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return True
