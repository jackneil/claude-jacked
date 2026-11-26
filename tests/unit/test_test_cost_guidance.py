"""Content checks for the shipped text about the cost of a test.

The files are LLM instruction documents, so these are string checks. They
guard the approved wording against accidental deletion or drift.
"""

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
DATA = REPO / "jacked" / "data"

FORGERS = ("commands/goal-maker.md", "skills/whats-next/SKILL.md", "commands/bhag.md")

TESTING_LENS_COST = (
    "test cost (a new test that repeats an existing proof, pins the implementation, "
    "asserts that a removed thing stays removed, or is slow for what it proves)"
)


def _read(relative: str) -> str:
    return (DATA / relative).read_text(encoding="utf-8")


@pytest.mark.parametrize("relative", FORGERS)
def test_no_brief_template_asks_for_new_tests_for_each_milestone(relative):
    assert "NEW tests" not in _read(relative)


def test_bhag_keeps_the_ban_on_a_weakened_test_and_names_the_one_exception():
    text = _read("commands/bhag.md")
    assert "NEVER delete, skip, weaken, or loosen an existing test or assertion to go green" in text
    assert "removes behavior on purpose" in text


def test_the_rules_block_has_the_test_cost_rule_between_its_markers():
    text = _read("rules/jacked_behaviors.md")
    assert "A test has a cost" in text.split("# end-jacked-behaviors")[0]


def test_the_gate_cadence_rule_runs_the_full_suite_through_the_recorder_and_asks_seen_first():
    rule = next(line for line in _read("rules/jacked_behaviors.md").splitlines() if "Gate cadence" in line)
    assert "jacked tests run --kind full" in rule and "{junit}" in rule and "jacked tests seen" in rule


def test_the_repository_wrappers_have_the_new_wording():
    whats_next = (REPO / ".claude" / "skills" / "whats-next" / "SKILL.md").read_text(encoding="utf-8")
    dcr = (REPO / ".claude" / "skills" / "dcr" / "SKILL.md").read_text(encoding="utf-8")
    assert "with a test at the owning boundary for each contract of a done milestone" in whats_next
    assert "NEW tests" not in whats_next
    assert TESTING_LENS_COST in dcr
