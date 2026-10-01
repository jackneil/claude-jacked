"""Every frontmatter block jacked ships must parse as strict YAML.

Claude Code reads frontmatter leniently, so an unquoted ``: `` inside a
description still shows up there. Strict parsers do not: PyYAML, Codex, and
other agent runtimes reject the block, and the skill or agent loses its name
and description. On 2026-10-01 eight shipped agents and one hank skill carried
this defect. These tests keep the whole class out.
"""

from pathlib import Path

import pytest
import yaml

from jacked.codex._generate import _split_command_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA = REPO_ROOT / "jacked" / "data"


def _frontmatter_files():
    patterns = [
        (DATA, "skills/*/SKILL.md"),
        (DATA, "commands/*.md"),
        (DATA, "agents/*.md"),
        (REPO_ROOT, ".claude/skills/*/SKILL.md"),
    ]
    files = []
    for root, pattern in patterns:
        files.extend(p for p in sorted(root.glob(pattern)) if p.read_text(encoding="utf-8").startswith("---\n"))
    return files


def _yaml_block(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---\n", 4)
    assert end != -1, f"{path} opens a frontmatter fence but never closes it"
    return text[4:end]


FILES = _frontmatter_files()


def test_the_sweep_finds_every_kind_of_shipped_file():
    kinds = {p.relative_to(REPO_ROOT).parts[2] if p.is_relative_to(DATA) else ".claude" for p in FILES}
    assert {"skills", "commands", "agents", ".claude"} <= kinds
    assert len(FILES) > 40


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_frontmatter_is_strict_yaml_with_a_description(path):
    meta = yaml.safe_load(_yaml_block(path))
    assert isinstance(meta, dict), f"{path}: frontmatter is not a mapping"
    assert isinstance(meta.get("description"), str) and meta["description"].strip(), (
        f"{path}: no description after a strict YAML parse"
    )


AGENTS_AND_COMMANDS = [p for p in FILES if p.parent.name in ("agents", "commands")]


@pytest.mark.parametrize("path", AGENTS_AND_COMMANDS, ids=lambda p: p.name)
def test_codex_line_parser_agrees_with_yaml_on_the_description(path):
    """The Codex installer parses frontmatter line by line (PyYAML is not a
    runtime dependency). Its description must equal what a YAML parser reads,
    or Codex shows escaped junk such as a literal backslash-quote."""
    expected = " ".join(yaml.safe_load(_yaml_block(path))["description"].split())
    meta, _body = _split_command_frontmatter(path.read_text(encoding="utf-8"))
    assert " ".join(meta["description"].split()) == expected
