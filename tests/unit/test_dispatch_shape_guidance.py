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


def test_chain_of_command_carries_the_dispatch_shape_budget():
    text = _read("skills/chain-of-command/SKILL.md")
    section = text.split("## Dispatch shape", 1)[1].split("## Scope and revocation", 1)[0]
    assert "SMALL at most 4, MEDIUM at most 8, LARGE at most 16" in section
    assert "Tier first" in section
    assert "Finding verification is main-loop work" in section
    assert "never one per raw finding" in section
    assert "Two review waves, maximum" in section
    assert "Incomplete is not clean" in section
    assert "filter(Boolean)" in section and "resumeFromRunId" in section
    assert "Effort on every `agent()` call" in section
    assert '"ultracode" is subordinate to the tier' in section
    assert "Reviewer engine" in section and "Codex" in section
    assert "Usage-aware fan-out" in section and "jacked usage --json" in section
    assert "One reviewer per artifact, and continue it" in section
    assert "SendMessage" in section
    assert "Research fan-out" in section and "4 lanes by 5 searches" in section
    # The section binds every mechanism, not just /dcr.
    for mechanism in ("Agent tool", "Workflow scripts", "/swarm", "agent teams"):
        assert mechanism in section


def test_chain_of_command_dispatch_shape_is_injected_by_the_session_hook():
    """The hook strips frontmatter and injects the whole skill body; the new
    section must sit inside the body, before the Acknowledgement the hook
    tells the model to skip."""
    text = _read("skills/chain-of-command/SKILL.md")
    assert text.index("## Dispatch shape") < text.index("## Acknowledgement")


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
    assert "13. **Scope and provenance on every review dispatch.**" in text
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
