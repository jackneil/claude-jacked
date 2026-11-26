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
from ._generate import CODEX_POLICY_PATH, _command_codex_policy


def command_skill_extras(cmd: Path) -> Optional[dict]:
    """Return the extra files of the skill made from ``cmd``, or None when it has none."""
    policy = _command_codex_policy(cmd)
    return {CODEX_POLICY_PATH: policy} if policy is not None else None


def command_skill_parts(cmd: Path, content: str, write: Callable) -> tuple[str, Callable]:
    """Return the expected hash and the copy function of the skill made from ``cmd``.

    ``write(dst, content, extras)`` writes the skill folder. The copy
    function calls it with the same extras that the hash has.
    """
    extras = command_skill_extras(cmd)
    return _sha_solo_skill(content, extras), lambda _src, dst: write(dst, content, extras)
