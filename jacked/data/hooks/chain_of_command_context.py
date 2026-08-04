#!/usr/bin/env python3
"""SessionStart hook: auto-load the chain-of-command dispatch policy.

Reads ~/.claude/skills/chain-of-command/SKILL.md, strips its YAML frontmatter,
and prints a short framing preamble followed by the skill body to stdout. A
SYNCHRONOUS SessionStart hook's stdout is injected into the session context, so
this makes the dispatch policy active from the very first turn of every new
Claude Code session.

Absence of the skill file means the user disabled the feature via the
dashboard's skill toggle: exit 0 printing NOTHING. Every failure path (missing
file, permission error, bad encoding) is silent for the same reason: this hook
must NEVER break a session start.
"""

import sys
from pathlib import Path

# Framing wrapped around the injected policy. Kept plain (no em-dashes): a
# session-start injection is an automatic load, so the model must NOT treat it
# as a user invocation to acknowledge.
PREAMBLE = (
    "=== CHAIN OF COMMAND (auto-loaded by jacked) ===\n"
    "The dispatch policy below is ACTIVE for this session from the start. "
    "This is an automatic load, not a user invocation: do NOT announce or "
    "acknowledge the policy (skip the Acknowledgement section), just apply it "
    "on every dispatch. The user can disable this auto-load by toggling off "
    "the chain-of-command skill in the jacked dashboard (Settings > Features), "
    "and can revoke or amend the policy live at any time; their instruction wins."
)


def _skill_path() -> Path:
    """Resolve the chain-of-command SKILL.md path at call time.

    Computed on each call (not a module constant) so tests can redirect it by
    patching Path.home().
    """
    return Path.home() / ".claude" / "skills" / "chain-of-command" / "SKILL.md"


def _strip_frontmatter(text: str) -> str:
    """Remove a leading YAML frontmatter block (--- ... ---) if present.

    Only strips when the very first line is a '---' fence with a matching
    closing fence; otherwise the text is returned unchanged.

    >>> _strip_frontmatter("---\\nname: x\\n---\\nbody")
    'body'
    >>> _strip_frontmatter("no frontmatter here")
    'no frontmatter here'
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1:]).lstrip("\n")
    # No closing fence: treat as no frontmatter and return unchanged.
    return text


def render() -> str:
    """Build the block to inject: the framing preamble plus the policy body.

    Returns "" when the skill file is absent (the dashboard toggle removed it),
    unreadable, or empty. The hook input is irrelevant here, so this takes no
    payload. Split out of main() so the combined SessionStart hook
    (``jacked.data.hooks.session_start``) can run this step in-process, in a
    fixed order, without a second stdin read.

    >>> isinstance(render(), str)
    True
    """
    try:
        skill_path = _skill_path()
        if not skill_path.exists():
            # Skill file removed via the dashboard toggle: stay silent.
            return ""
        raw = skill_path.read_text(encoding="utf-8")
    except Exception:
        # Permission, encoding, or any other read failure: never break startup.
        return ""

    body = _strip_frontmatter(raw).strip()
    if not body:
        return ""

    return f"{PREAMBLE}\n\n{body}"


def main():
    """Inject the chain-of-command policy into session context via stdout.

    Reads and discards stdin (never blocks; tolerates empty/invalid input).
    If the skill file is absent or unreadable, prints nothing and exits 0.

    >>> callable(main)
    True
    """
    # Drain stdin so the hook never blocks; its contents are irrelevant here.
    try:
        sys.stdin.read()
    except Exception:
        pass

    block = render()
    if block:
        print(block)


if __name__ == "__main__":
    main()
