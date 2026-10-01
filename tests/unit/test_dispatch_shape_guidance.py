"""The tiered dispatch-shape budget must reach every fan-out mechanism.

Issue #130: `/dcr` carried the right shape (1 / 2 / fan-out reviewers by
tier, fix-verification-only re-checks) but nothing auto-loaded carried it into
hand-written Workflow scripts, so "ultracode" runs fell back to the built-in
"cost is not a constraint" doctrine and a 260-line review spawned 38 agents.
These tests pin the installed guidance, not model behaviour.
"""

from __future__ import annotations

from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "jacked" / "data"


def _read(relative: str) -> str:
    return (DATA / relative).read_text(encoding="utf-8")


def _budget_section(text: str) -> str:
    return text.split("## Budget first", 1)[1].split("## The lanes", 1)[0]


def test_chain_of_command_opens_with_the_budget_before_the_lanes():
    """The budget is the most important part and the skill is injected into
    every session, so it leads: title, one-line purpose, then the budget."""
    text = _read("skills/chain-of-command/SKILL.md")
    body = text.split("---", 2)[2]
    headings = [line for line in body.splitlines() if line.startswith("## ")]
    assert headings[0] == "## Budget first"
    assert text.index("## Budget first") < text.index("## The lanes")
    assert text.index("## Budget first") < text.index("## Hard rules")


def test_chain_of_command_carries_the_dispatch_shape_budget():
    text = _read("skills/chain-of-command/SKILL.md")
    section = _budget_section(text)
    assert "SMALL at most 4, MEDIUM at most 8, LARGE at most 16" in section
    assert "finders, fixers, verifiers, critics" in section
    assert "Tier first" in section
    assert "Finding verification is main-loop work" in section
    assert "No verifier agents at SMALL or MEDIUM" in section
    assert "never one per raw finding" in section
    assert "Two review waves, maximum" in section
    assert "Incomplete is not clean" in section
    assert "filter(Boolean)" in section and "resumeFromRunId" in section
    # The budget section binds every mechanism, not just /dcr.
    for mechanism in ("Agent tool", "Workflow scripts", "/swarm", "agent teams"):
        assert mechanism in section
    # The remaining dispatch-shape rules live under Hard rules.
    assert "Effort on every `agent()` call" in text
    assert "Reviewer engine" in text and "Codex" in text
    assert "Usage-aware fan-out" in text and "jacked usage --json" in text
    assert "One reviewer per artifact, and continue it" in text
    assert "SendMessage" in text
    assert "Research fan-out" in text and "4 lanes by 5 searches" in text


def test_chain_of_command_makes_ultracode_subordinate_to_the_budget():
    """Jack, 2026-10-01: the tier budget beats ultracode."""
    section = _budget_section(_read("skills/chain-of-command/SKILL.md"))
    assert '"ultracode" is subordinate to the tier' in section
    assert "a menu of shapes, never a budget" in section
    assert "this section wins whenever they disagree" in section
    for pattern in ("loop-until-dry", "N refuters per finding", "judge panels"):
        assert pattern in section
    assert '"token cost is not a constraint"' in section
    # The /goal pointer rule travels with the budget.
    assert 'never carries "ultracode" or "use dynamic workflows" into a `/goal` pointer' in section


def test_chain_of_command_stays_compact():
    """Injected into every session by the SessionStart hook: every char costs."""
    from jacked.data.hooks.chain_of_command_context import _strip_frontmatter

    body = _strip_frontmatter(_read("skills/chain-of-command/SKILL.md")).strip()
    assert len(body) < 9500, len(body)


def test_chain_of_command_dispatch_shape_is_injected_by_the_session_hook():
    """The hook strips frontmatter and injects the whole skill body; the budget
    must sit inside the body, before the Acknowledgement the hook tells the
    model to skip."""
    text = _read("skills/chain-of-command/SKILL.md")
    assert text.index("## Budget first") < text.index("## Acknowledgement")


def test_dcr_validates_findings_in_the_main_loop_by_default():
    text = _read("skills/dcr/SKILL.md")
    assert "never one validator per raw finding" in text
    assert "per CLUSTER of related findings" in text
    assert "The FULL suite runs once, on a frozen tree, as the final gate" in text
    assert "Re-check waves review the fix DELTA only" in text


def test_retry_resumes_from_the_journal_and_treats_dead_agents_as_incomplete():
    text = _read("commands/retry.md")
    assert "resumeFromRunId FIRST" in text
    assert "journal.jsonl" in text
    assert "INCOMPLETE, never clean" in text


def test_brief_templates_review_via_dcr_tiers_and_never_carry_ultracode():
    for relative in (
        "skills/whats-next/SKILL.md",
        "commands/goal-maker.md",
        "commands/bhag.md",
    ):
        text = _read(relative)
        assert "/dcr" in text, relative
        # The phrase may only appear inside the prohibition, never as advice.
        for phrase in ("ultracode", "use dynamic workflows"):
            for line in text.splitlines():
                if phrase in line:
                    assert "never" in line, (relative, line)


def test_behaviors_rule_makes_review_triggers_proportional():
    text = _read("rules/jacked_behaviors.md")
    assert "Review depth is proportional" in text
    assert "Exactly one party runs the browser gate per review round" in text


def test_backstop_lets_a_review_loop_converge():
    text = _read("rules/jacked_behaviors.md")
    assert "A review loop is the one exception" in text
    assert "converges by the /dcr rule" in text
    assert "split it rather than review again" in text


def test_chain_of_command_scopes_reviews_and_converges():
    text = _read("skills/chain-of-command/SKILL.md")
    assert "**Scope and provenance on every review dispatch.**" in text
    assert "`introduced_by_branch: true|false`" in text
    assert "A review loop converges" in text
    assert "split the PR instead of reviewing again" in text
    assert "Exhaustive on the FIRST pass of any task; convergent after that." in text
    assert "The full-suite gate is per PUSH, not per fix batch" in text
    assert "LOW findings go to the main loop unverified" in text



def test_dc_planning_loop_converges_instead_of_always_continuing():
    text = _read("commands/dc.md")
    assert "the answer is always yes" not in text
    assert "do NOT run it past convergence either" in text
    assert "reviews the DELTA since the previous cycle" in text


def test_dc_classifies_the_tier_first_and_runs_small_inline():
    text = _read("commands/dc.md")
    body = text.split("---", 2)[2]
    first_heading = next(line for line in body.splitlines() if line.startswith("## "))
    assert "RISK TIER" in first_heading
    assert "/dcr` RISK TIER" in text
    assert "**INLINE in the main loop, ZERO agents spawned**" in text
    assert "Do not spawn any agent at SMALL" in text
    assert "one double-check-reviewer carrying every selected lens" in text
    assert "main reviewer + pre-mortem analyst" in text
    assert "SMALL at most 4, MEDIUM at most 8, LARGE at most 16" in text
    assert "## MULTI-THREAD SPAWNING (LARGE only, inside the budget)" in text
    # The inline review keeps the gates the agent path has.
    assert "DETERMINISM GATE" in text and "NEGATIVE-REQUIREMENTS sub-check" in text
    assert "same VERDICT format" in text
    # Grill mode stays interactive and agent-free.
    assert "GRILL MODE has no tier: it is always inline." in text
