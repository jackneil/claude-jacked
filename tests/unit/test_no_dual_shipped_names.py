"""Guard against a name shipping as BOTH a command and a skill.

`jacked install` copies `jacked/data/commands/*.md` into `~/.claude/commands/`
and `jacked/data/skills/<name>/` into `~/.claude/skills/<name>/`. Claude Code
loads both trees, so a name present in both ships two full copies of the same
instruction document under one `/<name>`: whichever one wins is an accident of
resolution order, and the loser rots silently — edits land in a file nobody
reads. The 2026-09-01 migration moved ten names (dcr, qa, ux, whats-next,
docs-sync, lockdown, demo-video, qa-video, swarm-research, release) from
commands to skills; this test is what keeps a re-added command file from
quietly shadowing its skill.

Plain-pytest style matches the sibling shipped-content contract tests.
"""

from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "jacked" / "data"
COMMANDS = DATA / "commands"
SKILLS = DATA / "skills"


def test_no_name_ships_as_both_command_and_skill():
    command_names = {p.stem for p in COMMANDS.glob("*.md")}
    skill_names = {p.name for p in SKILLS.iterdir() if p.is_dir()}
    collisions = sorted(command_names & skill_names)
    assert not collisions, (
        "these names ship as BOTH jacked/data/commands/<name>.md and "
        f"jacked/data/skills/<name>/: {collisions}. Pick one home — a duplicate "
        "shadows the other at load time and the loser silently goes stale."
    )


def test_both_shipped_trees_are_non_empty():
    """The collision check above passes trivially if either tree is missing, so
    pin that both are actually populated."""
    assert {p.stem for p in COMMANDS.glob("*.md")}, "jacked/data/commands must ship command files"
    assert [p for p in SKILLS.iterdir() if p.is_dir()], "jacked/data/skills must ship skill dirs"
