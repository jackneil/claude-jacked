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
    assert "The full suite runs ONCE per goal, at the very end" in text
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


def _cadence(text: str) -> str:
    """The Gate cadence paragraph a forged brief carries, from its label to its last sentence."""
    start = text.index("Gate cadence:")
    tail = "Run nothing else heavy while the full suite runs."
    return text[start : text.index(tail, start) + len(tail)]


def test_full_suite_runs_once_per_goal_never_per_milestone():
    """Jack, 2026-11-03: a 16-milestone goal ran a full local gate plus a hosted
    CI round per milestone. The full suite and hosted CI run ONCE per goal, at
    the very end; milestones get targeted tests only. Every brief forger, the
    behaviors block and both engines say so, and the two forgers carry the same
    paragraph byte for byte, so no forged brief can bring the per-milestone gate
    back and the copies cannot drift apart."""
    for relative in (
        "rules/jacked_behaviors.md",
        "commands/goal-maker.md",
        "skills/whats-next/SKILL.md",
        "skills/chain-of-command/SKILL.md",
    ):
        text = _read(relative)
        assert "ONCE per goal, at the very end" in text, relative
        assert "never per milestone" in text, relative
    # The old wording lived in exactly these two engines.
    for relative in ("skills/dcr/SKILL.md", "skills/chain-of-command/SKILL.md"):
        assert "per PUSH" not in _read(relative), relative
    assert _read("skills/dcr/SKILL.md").count("never per milestone") == 2  # both fix-phase lines
    goal = _read("commands/goal-maker.md")
    cadence = _cadence(goal)
    assert cadence == _cadence(_read("skills/whats-next/SKILL.md"))
    wrapper = DATA.parents[1] / ".claude" / "skills" / "whats-next" / "SKILL.md"
    assert cadence in wrapper.read_text(encoding="utf-8")
    assert "every test file that imports or reads a changed file" in cadence
    assert (
        "If it fails: fix with targeted tests, re-run the failing tests, "
        "then run the full test command once more." in cadence
    )
    behaviors = _read("rules/jacked_behaviors.md")
    assert "every test file that imports or reads a changed file" in behaviors
    assert "In `/bhag` a cell is the merge unit" in behaviors
    assert (
        "If it fails: fix with targeted tests, re-run the failing tests, "
        "then run the full suite once more." in behaviors
    )
    assert "for a `SKILL.md`, grep its parent directory name" in behaviors
    assert "(outside a goal, once per push; a /bhag cell is its own batch)" in _read("skills/chain-of-command/SKILL.md")
    bhag = _read("commands/bhag.md")
    assert "Run only the targeted tests while building the cell" in bhag
    assert "every test file that imports or reads a changed file" in bhag
    assert "run <repo's real test command> ONCE" in bhag and "never after each edit" in bhag


def test_merge_mode_stacks_milestones_and_merges_once_at_the_end():
    """MERGE mode no longer gates, pushes and merges each milestone before the
    next starts. Every PR opens against main (a stacked base relies on a
    retarget GitHub performs only when the merged head branch is deleted), the
    base is confirmed before each merge, and the ledger records a milestone as
    done at its commit so a resumed run never rebuilds it."""
    goal = _read("commands/goal-maker.md")
    assert "milestones stack" in goal
    assert "no PR, no CI wait and no merge between milestones" in goal
    assert "every PR against `main`" in goal
    assert "only when the merged head branch is deleted" in goal
    assert "`gh pr view <PR#> --json baseRefName` reads the repo's default branch (else STOP)" in goal
    assert "gh pr merge --merge --delete-branch <PR#>" in goal
    assert "on the branch of the milestone that introduced it" in goal
    assert "evidence holds the branch name and the commit SHA" in goal
    assert "the merged PR number in its evidence" in goal
    assert "until the current one's PR has fully merged" not in goal
    assert "branches off the branch of the last DONE milestone" in goal
    assert "A blocked milestone gets no PR and nothing branches off it" in goal
    assert "gh pr update-branch <PR#>" in goal and "shows BEHIND" in goal
    assert "At merge time, append the merged PR number" in goal
    assert "the first done entry with no merged PR number" in goal


def test_the_second_full_run_is_conditional_on_a_failure():
    """A green first run is the one gate. The old wording ("Fix what it finds
    ... Then run the full test command once more") ran a second full suite on a
    green tree, which contradicts ONCE."""
    for relative in (
        "rules/jacked_behaviors.md",
        "commands/goal-maker.md",
        "skills/whats-next/SKILL.md",
    ):
        text = _read(relative)
        assert "If it fails: fix with targeted tests" in text, relative
        assert "Fix what it finds" not in text, relative
        assert "Then run the full" not in text, relative
    assert "On a fail: targeted fix" in _read("skills/chain-of-command/SKILL.md")


REVIEW_COST = "a review fix that changes code costs one re-gate"


def test_reviews_run_beside_the_one_gate_never_before_it():
    """Gate cadence duty 2: an audit that runs before the first gate cost an
    hour on 2026-10-12, and the gate then failed on checks no review can see."""
    for relative in (
        "rules/jacked_behaviors.md",
        "commands/goal-maker.md",
        "skills/whats-next/SKILL.md",
    ):
        text = _read(relative)
        assert "Run any /dcr and /cso review of the combined tree beside that one gate" in text, relative
        assert "as read-only reviews, not before it" in text, relative
        assert REVIEW_COST in text, relative
    dcr = _read("skills/dcr/SKILL.md")
    assert "run this review beside that one gate over the same combined tree, not before it" in dcr
    assert "the end-of-goal review runs beside that gate, not before it" in dcr
    assert dcr.count(REVIEW_COST) == 2  # both fix-phase lines
    bhag = _read("commands/bhag.md")
    assert "Run /dcr and /cso beside the step 4 gate over the same tree, not before it" in bhag
    assert REVIEW_COST in bhag
    assert "Reviews run beside that gate." in _read("skills/chain-of-command/SKILL.md")


def test_verify_item_says_the_test_command_runs_once_at_the_very_end():
    """Nothing pinned the Verify checklist; a brief could keep the Approach
    wording and still ask the judge for a full run per milestone."""
    for relative in ("commands/goal-maker.md", "skills/whats-next/SKILL.md"):
        text = _read(relative)
        assert "run ONCE at the very end of the goal over the combined tree (never per milestone)" in text, relative


def test_merge_mode_records_the_full_gate_so_a_resume_can_see_it():
    """Entries go done at commit, so a run resumed after the last done entry
    could not tell the pre-gate phase from the merge phase. The gate writes a
    line, the merge rule reads the line, and the boot drill runs the gate when
    the line is missing or stale."""
    goal = _read("commands/goal-maker.md")
    assert "append `full gate green at <sha>` to progress.md" in goal
    assert "progress.md holds a `full gate green at <sha>` line that is not stale" in goal
    assert "the local full gate was green" not in goal
    assert "it is stale when the top of the stack has moved past <sha>" in goal
    assert "a targeted CI-failure fix or a `gh pr update-branch` merge" in goal
    assert "that line is missing or stale, the boot drill's next step is the full gate" in goal
    assert "(entry started, verify result, full gate result, blocker, discovered work)" in goal


def test_merge_mode_resolves_the_default_branch_instead_of_assuming_main():
    goal = _read("commands/goal-maker.md")
    assert "`main` means the repo's default branch" in goal
    assert "gh repo view --json defaultBranchRef" in goal
    assert "baseRefName` reads `main`" not in goal


def test_merge_mode_routes_a_done_milestone_the_end_gate_cannot_save():
    goal = _read("commands/goal-maker.md")
    assert "a done milestone leaves the stack like a blocked one" in goal
    assert "never a history rewrite" in goal
    assert "run the full gate once more over the new top of the stack" in goal
    assert "appends the new head SHA to the evidence of each entry whose branch moved" in goal


def _newest_version_row(readme: str) -> str:
    """The first row of the Version History table: `Unreleased` while the
    change waits, the release number once it ships. A release renames the
    row, it does not move the text."""
    lines = readme.splitlines()
    start = lines.index("## Version History")
    return next(line for line in lines[start:] if line.startswith("| **"))


def test_readme_newest_version_row_names_the_cadence_change():
    readme = (DATA.parents[1] / "README.md").read_text(encoding="utf-8")
    row = _newest_version_row(readme)
    assert row.startswith("| **Unreleased**") or row.startswith("| **0.")
    assert "run once per goal" in row
    assert "`full gate green at <sha>`" in row
