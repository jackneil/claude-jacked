"""Marker-managed agent-reach rules line for Claude CLAUDE.md and Codex AGENTS.md.

Installed and removed WITH the agent-reach integration (not with ``jacked install``
itself): the runner calls :func:`install_reach_rules` / :func:`remove_reach_rules`
from ``AgentReachRunner.install`` / ``.remove``.

Two independent marker blocks wrap the SAME shipped rules body
(``jacked/data/rules/reach_rules.md``):

* Claude: ``~/.claude/CLAUDE.md`` delimited by ``# jacked-reach-rules-v1`` ..
  ``# end-jacked-reach-rules``. Config-integrity critical: an orphaned or
  duplicated marker layout is refused loudly (never guess-repaired) and a fresh
  append preserves existing content byte-for-byte.
* Codex: ``~/.codex/AGENTS.md`` delimited by the HTML-comment reach markers in
  :mod:`jacked.codex._managed`. Best-effort: a Codex-side failure is logged and
  swallowed so it never fails the main install (mirrors how ``cli.py`` treats the
  Codex install pass).

Nothing here resolves or fetches anything; it only edits local text via the same
whole-line marker-extract-or-bail primitives the rest of the codebase uses.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Mapping, Optional

from jacked.codex._fsutil import (
    _atomic_write_text,
    _extract_block,
    _marker_line_count,
    codex_present,
)
from jacked.codex._managed import install_reach_agents_block, strip_reach_agents_block

logger = logging.getLogger(__name__)

#: Whole-line markers delimiting jacked's reach-rules block in CLAUDE.md. Their own
#: pair, independent of the ``# jacked-behaviors-v2`` block ``jacked install`` writes.
REACH_RULES_START = "# jacked-reach-rules-v1"
REACH_RULES_END = "# end-jacked-reach-rules"

#: Shipped rules body (marker-free; both surfaces wrap it in their own markers).
_RULES_BODY_PATH = Path(__file__).parent.parent / "data" / "rules" / "reach_rules.md"


class ReachRulesError(RuntimeError):
    """CLAUDE.md carries an orphaned/corrupt reach-rules marker layout we won't repair."""


def reach_rules_body() -> str:
    """The shared rules text (no markers), stripped.

    Raises ``FileNotFoundError`` if the shipped data file is missing (a broken
    install); the caller surfaces it. Config integrity: a missing rules body must
    not silently install an empty block.
    """
    return _RULES_BODY_PATH.read_text(encoding="utf-8").strip()


# --------------------------------------------------------------------------- #
# Claude ~/.claude/CLAUDE.md
# --------------------------------------------------------------------------- #

def _claude_md(home: Path | str) -> Path:
    return Path(home) / ".claude" / "CLAUDE.md"


def _block(body: str) -> str:
    return f"{REACH_RULES_START}\n{body}\n{REACH_RULES_END}"


def _append(existing: str, block: str) -> str:
    """Append `block` to `existing` with clean blank-line handling (mirrors
    ``cli._install_behavioral_rules``): a blank line separates prior content from
    the block, and the result ends in a single trailing newline."""
    if not existing.strip():
        return block + "\n"
    if existing.endswith("\n\n"):
        return existing + block + "\n"
    if existing.endswith("\n"):
        return existing + "\n" + block + "\n"
    return existing + "\n\n" + block + "\n"


def preflight_reach_rules(home: Path | str) -> None:
    """Raise :class:`ReachRulesError` if CLAUDE.md already has a corrupt reach-rules
    marker layout, WITHOUT writing anything.

    Called at the very start of an install so a hand-broken/duplicated marker
    layout fails fast, before the expensive, side-effecting network steps (a later
    fatal raise would otherwise leave the uv env + skill dirs written but no state
    file). A clean file, an absent file, or a well-formed existing block all pass.
    """
    path = _claude_md(home)
    if not path.exists():
        return
    existing = path.read_text(encoding="utf-8")
    start_ct = _marker_line_count(existing, REACH_RULES_START)
    end_ct = _marker_line_count(existing, REACH_RULES_END)
    if start_ct == 0 and end_ct == 0:
        return
    if _extract_block(existing, REACH_RULES_START, REACH_RULES_END) is None:
        raise ReachRulesError(
            f"CLAUDE.md at {path} has a corrupted jacked reach-rules marker layout "
            f"(start x{start_ct}, end x{end_ct}); fix it by hand, then retry. "
            f"Markers: '{REACH_RULES_START}' .. '{REACH_RULES_END}'."
        )


def install_reach_rules(home: Path | str) -> str:
    """Install the reach rules block into ``~/.claude/CLAUDE.md``.

    Idempotent and config-integrity critical:

    * no markers present -> append the block, preserving existing content
      byte-for-byte ("installed");
    * our block present and current -> no-op ("unchanged");
    * our block present but drifted -> replace it in place ("updated");
    * orphaned or duplicated markers -> raise :class:`ReachRulesError` (never
      guess-repair a file we might corrupt).

    Returns the action taken. Failure is fatal by design: the runner lets it
    propagate and fail the install.
    """
    body = reach_rules_body()
    block = _block(body)
    path = _claude_md(home)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""

    start_ct = _marker_line_count(existing, REACH_RULES_START)
    end_ct = _marker_line_count(existing, REACH_RULES_END)

    if start_ct == 0 and end_ct == 0:
        _atomic_write_text(path, _append(existing, block))
        return "installed"

    extracted = _extract_block(existing, REACH_RULES_START, REACH_RULES_END)
    if extracted is None:
        raise ReachRulesError(
            f"CLAUDE.md at {path} has a corrupted jacked reach-rules marker layout "
            f"(start x{start_ct}, end x{end_ct}); fix it by hand, then retry. "
            f"Markers: '{REACH_RULES_START}' .. '{REACH_RULES_END}'."
        )

    pre, current, post = extracted
    if current.strip() == block.strip():
        return "unchanged"
    pre = pre.rstrip("\n")
    post = post.lstrip("\n")
    parts = [p for p in (pre, block, post) if p]
    _atomic_write_text(path, "\n\n".join(parts).rstrip("\n") + "\n")
    return "updated"


def remove_reach_rules(home: Path | str) -> bool:
    """Strip the reach rules block from ``~/.claude/CLAUDE.md``, leaving the rest
    byte-identical. Returns True if a block was removed; False when absent.

    An orphaned/corrupt marker layout is left untouched (warning), never
    guess-repaired. Removal is tolerant (does not raise on a corrupt layout) so a
    stray marker can't block uninstall.
    """
    path = _claude_md(home)
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    extracted = _extract_block(existing, REACH_RULES_START, REACH_RULES_END)
    if extracted is None:
        if _marker_line_count(existing, REACH_RULES_START) or _marker_line_count(
            existing, REACH_RULES_END
        ):
            logger.warning(
                "corrupted jacked reach-rules marker layout in %s; leaving it untouched",
                path,
            )
        return False
    pre, _current, post = extracted
    pre = pre.rstrip("\n")
    post = post.lstrip("\n")
    parts = [p for p in (pre, post) if p]
    new = ("\n\n".join(parts).rstrip("\n") + "\n") if parts else ""
    _atomic_write_text(path, new)
    return True


# --------------------------------------------------------------------------- #
# Codex ~/.codex/AGENTS.md (best-effort)
# --------------------------------------------------------------------------- #

def _codex_env(scope_home: Path | str | None, env: Optional[Mapping[str, str]]) -> Optional[Mapping[str, str]]:
    """Env used to resolve Codex's dir. When the runner has an INJECTED home
    (``scope_home`` given, i.e. tests/isolation), override ``CODEX_HOME`` to
    ``<home>/.codex`` so BOTH the presence check and the write land under that
    home and never touch a developer's real ``~/.codex``. On a real run
    (``scope_home is None``) return ``env`` unchanged so a user's own
    ``CODEX_HOME`` is respected."""
    if scope_home is None:
        return env
    base = dict(env) if env is not None else dict(os.environ)
    base["CODEX_HOME"] = str(Path(scope_home) / ".codex")
    return base


def install_reach_rules_codex(
    scope_home: Path | str | None = None, env: Optional[Mapping[str, str]] = None
) -> str:
    """Best-effort install of the reach rules block into Codex's AGENTS.md.

    Skipped when Codex is not present (so no stray ``~/.codex`` is created). Any
    failure is logged and swallowed: a Codex problem must never fail the main
    agent-reach install. Returns "installed" / "skipped" / "error".
    """
    env = _codex_env(scope_home, env)
    try:
        if not codex_present(env):
            return "skipped"
        install_reach_agents_block(reach_rules_body(), env=env)
        return "installed"
    except Exception as e:  # best-effort: a Codex failure never fails install
        logger.warning("agent-reach Codex rules install failed (non-fatal): %s", e)
        return "error"


def remove_reach_rules_codex(
    scope_home: Path | str | None = None, env: Optional[Mapping[str, str]] = None
) -> str:
    """Best-effort strip of the reach rules block from Codex's AGENTS.md.

    Returns "removed" / "absent" / "skipped" / "error". Never raises.
    """
    env = _codex_env(scope_home, env)
    try:
        if not codex_present(env):
            return "skipped"
        return "removed" if strip_reach_agents_block(env=env) else "absent"
    except Exception as e:  # best-effort: mirror the install side
        logger.warning("agent-reach Codex rules removal failed (non-fatal): %s", e)
        return "error"
