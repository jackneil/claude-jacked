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


def _rules_block() -> tuple[str, str]:
    """Return (text before the block, the block) of the shipped rules file.

    The markers come from the installer helpers, so this file holds no copy of
    a marker line. The start is found by the same version prefix that the
    installer searches for.
    """
    from jacked.cli import _behavioral_rules_end_marker, _behavioral_rules_marker

    text = _read("rules/jacked_behaviors.md")
    start_prefix = _behavioral_rules_marker().rstrip("0123456789")
    start = text.index(start_prefix)
    start_line_end = text.index("\n", start) + 1
    end = text.index(_behavioral_rules_end_marker(), start_line_end)
    return text[:start], text[start_line_end:end]


def test_the_rules_block_has_the_test_cost_rule_between_its_markers():
    before, block = _rules_block()
    assert "- **A test has a cost: each gate pays for it again.**" in block
    assert "A test has a cost" not in before
    # The sentence that names /test-diet ships with that skill, not here.
    assert "/test-diet" not in block


def test_goal_maker_tests_line_has_the_approved_text():
    line = next(row for row in _read("commands/goal-maker.md").splitlines() if row.startswith("- **Tests**"))
    assert (
        "the repo's real test command green; a test at the boundary that owns each new behavior, "
        "one for each contract, and the tests that the change made obsolete deleted in the same "
        'change (never kept as "stays removed" checks).'
    ) in line


def test_dc_testing_lens_row_has_the_test_cost_item():
    assert "| 8 | **Testing** | Unit test coverage, edge case tests, regression detection, test quality, " + TESTING_LENS_COST + " |" in _read("commands/dc.md")


def test_test_coverage_engineer_has_the_test_cost_paragraph():
    text = " ".join(_read("agents/test-coverage-engineer.md").split())
    assert (
        "**A test has a cost.** Each test runs in each gate from now on. Before you add one, name "
        "the defect that it catches and say why the present tests miss it, and put it at the "
        "boundary that owns the behavior. Do not add a second test for a contract that a present "
        "test proves. Do not add a test that asserts that removed code stays removed. Report the "
        "run time of the tests that you added."
    ) in text


def test_test_coverage_improver_lists_the_two_new_refusals():
    assert (
        "- Duplicate existing test coverage\n"
        "- Assert that removed code or a removed feature stays removed\n"
        "- Have no defect that you can name before you write them\n"
    ) in _read("agents/test-coverage-improver.md")


def test_the_gate_cadence_rule_runs_the_full_suite_through_the_recorder_and_asks_seen_first():
    rule = next(line for line in _read("rules/jacked_behaviors.md").splitlines() if "Gate cadence" in line)
    assert "jacked tests run --kind full" in rule and "{junit}" in rule and "jacked tests seen" in rule


def test_the_repository_wrappers_have_the_new_wording():
    whats_next = (REPO / ".claude" / "skills" / "whats-next" / "SKILL.md").read_text(encoding="utf-8")
    dcr = (REPO / ".claude" / "skills" / "dcr" / "SKILL.md").read_text(encoding="utf-8")
    assert "with a test at the owning boundary for each contract of a done milestone" in whats_next
    assert "NEW tests" not in whats_next
    assert TESTING_LENS_COST in dcr
