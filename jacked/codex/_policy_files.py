"""The Codex policy file of a skill that the Codex install makes from a command.

A command with ``disable-model-invocation: true`` is one that only the user
may start. Codex ignores that key and reads its own policy file,
``agents/openai.yaml``. So the skill that the install makes from such a
command gets that file. The hash and the write take the same extra files,
so the manifest of the next install matches the folder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ._fsutil import _sha_solo_skill
from ._generate import _split_command_frontmatter

# The Codex policy file of a skill that only the user may start. Codex
# documentation (read 2026-11-26): with false, "Codex won't implicitly invoke
# the skill based on user prompt; explicit $skill invocation still works".
CODEX_POLICY_PATH = "agents/openai.yaml"
CODEX_NO_IMPLICIT = "policy:\n  allow_implicit_invocation: false\n"


def _command_codex_policy(cmd: Path) -> Optional[str]:
    """The Codex policy file text for the skill made from command `cmd`.

    Returns `CODEX_NO_IMPLICIT` when the frontmatter of the command sets
    ``disable-model-invocation`` to a true value, else None. Claude Code does
    not start such a command by itself, and with this file Codex does not
    start the generated skill by itself. An invocation by name still works."""
    from jacked.skill_listing import truthy

    meta, _ = _split_command_frontmatter(cmd.read_text(encoding="utf-8"))
    if truthy(meta.get("disable-model-invocation")):
        return CODEX_NO_IMPLICIT
    return None


def command_skill_extras(cmd: Path) -> Optional[dict]:
    """Return the extra files of the skill made from ``cmd``, or None when it has none."""
    policy = _command_codex_policy(cmd)
    return {CODEX_POLICY_PATH: policy} if policy is not None else None


def command_skill_parts(cmd: Path, content: str, write: Callable) -> tuple[str, Callable]:
    """Return the expected hash and the copy function of the skill made from ``cmd``.

    ``write(dst, content, extras)`` writes the skill folder. The copy
    function calls it with the same extras that the hash has. ``write`` is a
    test seam: the installer passes a lambda that looks up
    ``installer._write_solo_skill`` at call time, so a test can patch it.
    """
    extras = command_skill_extras(cmd)
    return _sha_solo_skill(content, extras), lambda _src, dst: write(dst, content, extras)
