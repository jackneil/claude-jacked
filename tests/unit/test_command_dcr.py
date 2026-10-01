"""Content-presence regression tests for the /dcr command's REVIEW ENGINE.

The engine section lets a user route the volume reviewers to the OpenAI Codex
CLI (config from `jacked dcr engine`) while judgment stays in the parent
session. These guard the SAFETY contract of that split: judgment never moves
engines, carve-out lenses never leave Claude, failed Codex jobs always fall
back to a Claude reviewer instead of silently dropping a lens, and Codex
findings still pass finding validation before any fix. Like the other command
tests, these are string-presence checks on the LLM instruction document, not
runtime-behavior assertions.

/dcr ships as a SKILL (`jacked/data/skills/dcr/SKILL.md`), not a command file,
so every content assertion here is scoped to the engine body below the
`<!-- ENGINE -->` marker rather than the repo-dispatch preamble above it.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CMD = REPO_ROOT / "jacked" / "data" / "skills" / "dcr" / "SKILL.md"
REPO_WRAPPER = REPO_ROOT / ".claude" / "skills" / "dcr" / "SKILL.md"
REFS = CMD.parent / "references"
LARGE_TIER = REFS / "large-tier.md"
CODEX_ENGINE = REFS / "codex-engine.md"
CONDITIONAL = REFS / "conditional.md"

ENGINE_MARKER = "<!-- ENGINE -->"


def _engine_body(text: str) -> str:
    """The shipped engine: everything after the `<!-- ENGINE -->` marker.

    A shipped skill is `frontmatter + repo-dispatch preamble + <!-- ENGINE --> +
    engine body`. The content contract below is about the ENGINE, so the
    preamble must not be able to satisfy (or break) an assertion.
    """
    assert ENGINE_MARKER in text, f"shipped skill is missing its {ENGINE_MARKER} marker"
    return text.split(ENGINE_MARKER, 1)[1].lstrip("\n")


@pytest.fixture(scope="module")
def dcr() -> str:
    return _engine_body(CMD.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def large_tier() -> str:
    """LARGE-only material (persona/wild-card/pre-mortem pools, analyst prompt),
    read by the engine only at the LARGE tier."""
    return LARGE_TIER.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def conditional() -> str:
    """Condition-gated material (plan mode, specialist lenses, Frontend Design
    reviewer, UX discoverability checklist, Security defensive framing), each
    section read only when its trigger condition holds."""
    return CONDITIONAL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def codex_engine() -> str:
    """CODEX DISPATCH detail, read by the engine only when the engine check
    returns codex usable."""
    return CODEX_ENGINE.read_text(encoding="utf-8")


def test_dcr_skill_exists():
    assert CMD.exists(), "jacked/data/skills/dcr/SKILL.md must exist (ships via the install glob)"


def test_shipped_skill_shape_is_preamble_then_single_engine_marker():
    """Pin the migrated file shape: frontmatter, then the repo-dispatch preamble
    that redirects to a `/jacked-setup`-generated repo skill, then exactly one
    `<!-- ENGINE -->` line. Two markers (or a preamble below the marker) would
    make every engine-scoped assertion in this file read the wrong half."""
    text = CMD.read_text(encoding="utf-8")
    lines = text.splitlines()
    markers = [i for i, line in enumerate(lines) if line.strip() == ENGINE_MARKER]
    assert len(markers) == 1, f"expected exactly one {ENGINE_MARKER} line, found {len(markers)}"
    head = "\n".join(lines[: markers[0]])
    assert head.startswith("---\n")
    assert "`.claude/skills/dcr/SKILL.md` exists" in head, (
        "repo-dispatch preamble must sit ABOVE the engine marker"
    )


def test_engine_section_present_with_both_engines(dcr: str, codex_engine: str):
    assert "## REVIEW ENGINE" in dcr
    assert "### ENGINE CHECK" in dcr
    assert "### CODEX DISPATCH" in dcr
    assert "# /dcr CODEX DISPATCH" in codex_engine
    # both engines named, claude is the default
    assert "**claude** (default)" in dcr
    assert "**codex**" in dcr


def test_engine_check_is_deterministic_and_fails_safe(dcr: str):
    # one CLI call decides the engine
    assert "jacked dcr engine --json" in dcr
    # a missing CLI lands on the Claude default silently, but a PRESENT-and-broken
    # check announces itself (a jacked install exists, so the failure is signal)
    assert "Command not found, or any `\"engine\"` value other than `\"codex\"`" in dcr
    assert "do not mention engines at all" in dcr
    assert "Engine check failed ([short error])" in dcr
    # configured-but-unusable announces the reason then falls back
    assert "Codex engine configured but not usable" in dcr
    # runs once, applies to re-check waves too
    assert "applies to EVERY wave in this run, re-check waves included" in dcr


def test_engine_check_scoped_to_claude_code(dcr: str):
    # the same command file ships to Codex runtimes, where the section is a no-op
    assert "Skip this entire section when NOT running inside Claude Code" in dcr


def test_judgment_never_moves_engines(dcr: str, codex_engine: str):
    assert (
        "lens selection, finding validation, fixes, and the verdict always run "
        "in the parent session regardless of engine" in dcr
    )
    # codex findings still gate through validation before fixes: the rule sits
    # in the always-read validation step, and the codex reference restates it
    assert "Validation is MANDATORY for every Codex CRITICAL/MEDIUM" in dcr
    assert "Codex findings enter FINDING VALIDATION (step 8b)" in codex_engine


def test_carve_outs_stay_on_claude(dcr: str, codex_engine: str):
    assert "Carve-outs stay as Claude Task dispatches regardless of engine" in codex_engine
    assert "**Carve-out BEFORE grouping:**" in codex_engine
    assert "never reviewed on both engines" in codex_engine
    assert "`keep_on_claude`" in codex_engine
    assert "Frontend Design reviewer" in codex_engine
    # the always-read engine intro still states the carve-out in one line
    assert "The `keep_on_claude` lenses and the Frontend Design reviewer always stay on Claude" in dcr


def test_codex_jobs_are_read_only_and_schema_bound(codex_engine: str):
    assert "codex exec --sandbox read-only --ephemeral" in codex_engine
    assert '-c model_reasoning_effort="<effort>"' in codex_engine
    assert "--output-schema" in codex_engine
    # briefs carry the full reviewer contract, including the exclusion list
    assert "the full DO NOT FLAG list" in codex_engine


def test_failed_codex_jobs_fall_back_never_drop(codex_engine: str):
    assert "Respawn THAT reviewer once as a Claude Task subagent" in codex_engine
    assert "Never drop a lens silently" in codex_engine
    assert "never count a failed reviewer as PASS" in codex_engine
    # hung-job handling has the same fallback
    assert "treat it as hung" in codex_engine


def test_missing_codex_reference_falls_back_to_claude(dcr: str):
    assert "references/codex-engine.md not found — reviewers run on Claude." in dcr


def test_report_names_the_engine(dcr: str):
    assert "**Engine:** Codex ([model], effort [effort]" in dcr


def test_repo_wrapper_carries_engine_section():
    """The repo's generated .claude/skills/dcr/SKILL.md wrapper must stay in sync
    on the engine contract (it embeds the full engine body)."""
    wrapper = REPO_WRAPPER.read_text(encoding="utf-8")
    for anchor in (
        "## REVIEW ENGINE",
        "### CODEX DISPATCH",
        "jacked dcr engine --json",
        "references/codex-engine.md",
    ):
        assert anchor in wrapper, f"repo dcr wrapper missing engine anchor: {anchor}"


# ---------------------------------------------------------------------------
# RISK TIER contract (2026-08-20 efficiency rework): depth scales with the
# risk of the change. These pin the cost-control doctrine — small changes get
# one consolidated reviewer, the full fan-out is reserved for LARGE, re-check
# waves verify fixes instead of re-reviewing from scratch, and the wave loop
# has a default cap. Losing any of these silently reverts /dcr to spending
# LARGE-tier tokens on every bugfix.
# ---------------------------------------------------------------------------


def test_risk_tier_section_present(dcr: str):
    assert "### RISK TIER" in dcr
    for tier in ("**SMALL**", "**MEDIUM**", "**LARGE**"):
        assert tier in dcr
    # sensitivity dominates size, and ambiguity escalates
    assert "Sensitivity beats size" in dcr
    assert "take the higher one" in dcr


def test_small_tier_runs_one_consolidated_reviewer(dcr: str):
    assert "ONE consolidated reviewer carries every selected lens" in dcr
    # lens selection decides WHAT, tier decides HOW WIDE
    assert "Reviewer COUNT comes from the RISK TIER, not the lens count" in dcr


def test_security_selection_forces_large_tier(dcr: str):
    assert (
        "If the **Security** or **Access Control** lens ends up selected, "
        "the tier is LARGE by definition" in dcr
    )
    # late selection must promote an earlier SMALL/MEDIUM classification
    assert "PROMOTE the tier to LARGE" in dcr


def test_personas_and_wild_cards_are_large_tier_only(large_tier: str):
    assert "## REVIEWER PERSONAS (LARGE tier only)" in large_tier
    assert "## WILD CARD CHECKS (LARGE tier only)" in large_tier
    assert "MUST have a different persona AND a different wild card" in large_tier


def test_pre_mortem_is_large_tier_only(large_tier: str):
    assert "## PRE-MORTEM FAILURE SCENARIOS (LARGE tier only)" in large_tier
    assert "PRE-MORTEM ANALYST (LARGE tier, Wave 1 only" in large_tier
    assert "You are the PRE-MORTEM ANALYST" in large_tier
    # one-shot in Wave 1, never folded into another reviewer, opus on Fable
    assert "It does NOT re-spawn in later waves" in large_tier
    assert "Do not fold it into another reviewer's prompt" in large_tier
    assert '`model: "opus"` on a Fable-class session' in large_tier


def test_recheck_waves_verify_fixes_not_fresh_review(dcr: str):
    assert "### SUBSEQUENT WAVES — Fix Verification" in dcr
    assert "Do NOT re-review the rest of the code from scratch" in dcr
    # the old doctrine must be gone
    assert "Do NOT limit your review to verifying prior fixes" not in dcr


def test_default_wave_cap_is_two_and_the_loop_converges(dcr: str):
    assert "Default wave cap is **2**" in dcr
    assert "Default wave cap is **3**" not in dcr
    assert "a review loop CONVERGES" in dcr
    assert "no validated branch-introduced CRITICAL/MEDIUM and no critical pre-existing one" in dcr
    assert "split the PR, not to review again" in dcr
    assert "the answer is always yes" not in dcr
    assert "never a fourth wave" in dcr


# ---------------------------------------------------------------------------
# Skill diet (2026-10-01): the budget is the first thing a model reads, and
# LARGE-only / Codex-only material lives in references/ so SMALL and MEDIUM
# runs never load it. A description that says "recursive ... until clean"
# nudges models into unbounded review loops, so it is pinned out.
# ---------------------------------------------------------------------------


def _frontmatter(text: str) -> dict:
    import yaml

    assert text.startswith("---\n")
    return yaml.safe_load(text.split("---\n", 2)[1])


def test_budget_first_section_precedes_the_lenses(dcr: str):
    assert dcr.startswith("## BUDGET FIRST"), "the budget must be the first thing the engine says"
    assert dcr.index("## BUDGET FIRST") < dcr.index("## REVIEW LENSES")
    budget = dcr[: dcr.index("## REVIEW LENSES")]
    assert "| **Agent budget (whole run)** | at most 4 | at most 8 | at most 16 |" in budget
    assert "validators, re-check reviewers" in budget
    assert "**Two waves by default:**" in budget
    assert "never a fourth wave" in budget
    assert "**Incomplete is not clean.**" in budget
    assert (
        '"ultracode", "token cost is not a constraint", and the workflow-authoring '
        "quality patterns never override this budget or the wave cap" in budget
    )


def test_skill_points_to_both_reference_files_and_they_exist(dcr: str):
    assert "Read `references/large-tier.md` (relative to this skill's directory)" in dcr
    assert "SMALL/MEDIUM never read it" in dcr
    assert "Read `references/codex-engine.md`" in dcr
    assert LARGE_TIER.is_file(), "references/large-tier.md must ship beside SKILL.md"
    assert CODEX_ENGINE.is_file(), "references/codex-engine.md must ship beside SKILL.md"
    # a /jacked-setup repo copy or a Codex prompt has no references/ beside it
    assert "~/.claude/skills/dcr/references/" in dcr
    assert "~/.agents/skills/dcr/references/" in dcr


def test_description_no_longer_promises_recursion():
    for path in (CMD, REPO_WRAPPER):
        desc = _frontmatter(path.read_text(encoding="utf-8"))["description"]
        assert "Recursive" not in desc and "recursive" not in desc, path
        assert "until all selected lenses pass clean" not in desc, path
        assert "two waves" in desc, path
        assert len(desc) <= 350, (path, len(desc))
    # the routing triggers stay
    shipped = _frontmatter(CMD.read_text(encoding="utf-8"))["description"]
    assert shipped.startswith(
        "Use after implementing a feature, fixing a bug, or completing any non-trivial code change."
    )


def test_large_tier_pools_stay_out_of_the_always_read_body(dcr: str):
    for entry in (
        "Paranoid Security Auditor",
        "Chaos Monkey",
        "What if the database/filesystem is completely empty?",
        "6 months in production, this feature is being rolled back",
        "You are the PRE-MORTEM ANALYST",
        "codex exec --sandbox read-only",
    ):
        assert entry not in dcr, f"LARGE/Codex-only material leaked back into SKILL.md: {entry}"


def test_every_finding_carries_scope_provenance(dcr: str, codex_engine: str):
    assert "Scope: [SCOPE]" in dcr
    assert "13. **Scope and provenance (always)**" in dcr
    assert "`introduced_by_branch: true|false`" in dcr
    # the Codex output instruction asks for the same field
    assert (
        "and `introduced_by_branch` (true when the defect lives in lines or behavior this diff changed)"
        in codex_engine
    )


def test_fix_phase_buckets_findings_by_provenance(dcr: str):
    assert "**Introduced by this branch** → fixed in this PR, always." in dcr
    assert "**Pre-existing, discovered adjacent to the branch**" in dcr
    assert "`gh issue create`" in dcr
    assert "Filing with evidence is not punting" in dcr
    assert "A project or global CLAUDE.md that says otherwise wins over this default." in dcr
    assert "the full-suite gate is per PUSH, not per fix batch" in dcr
    assert "**Pre-existing findings filed, not fixed here:**" in dcr


def test_diagnostics_run_as_pre_gate_before_wave_one(dcr: str):
    assert "### DIAGNOSTIC PRE-GATE (POST-IMPLEMENTATION only — runs BEFORE Wave 1)" in dcr
    assert "do not spawn reviewers onto a tree that lint or tests already condemn" in dcr
    assert "Do NOT fabricate diagnostics" in dcr


def test_findings_are_confidence_first(dcr: str):
    assert "raise a CRITICAL/MEDIUM only when you would stake the review on it" in dcr
    assert "downgrade rather than inflate" in dcr
    # the old high-recall doctrine must be gone
    assert "including ones you are not fully sure about" not in dcr


def test_frontend_reviewer_needs_ui_meaningful_diff(dcr: str):
    assert "is NOT a frontend change" in dcr
    assert "frontend-meaningful" in dcr


# ---------------------------------------------------------------------------
# Conditional material lives in references/conditional.md. Each section stays
# reachable from SKILL.md by a pointer naming its exact trigger condition.
# ---------------------------------------------------------------------------

SECURITY_FRAMING = (
    "This is a defensive security review of our own authorized codebase. Focus on "
    "correctness, authentication boundaries, authorization checks, input validation, "
    "secrets handling, and test coverage. Do NOT produce exploit chains, payloads, "
    "offensive tooling, or attack instructions. For each finding provide: the affected "
    "file:line, why it is risky, a safe remediation, and a safe regression test."
)


def test_conditional_reference_exists_and_skill_points_to_each_section(dcr: str):
    assert CONDITIONAL.is_file(), "references/conditional.md must ship beside SKILL.md"
    for pointer in (
        "**Plan mode active** → Read `references/conditional.md` § PLAN MODE",
        "**Either directory exists** → Read `references/conditional.md` § SPECIALIST LENS DISCOVERY",
        "**`frontend_review = true`** (step 3b) → Read § FRONTEND DESIGN REVIEWER",
        "**UX & Flow lens selected** → Read § UX & FLOW DISCOVERABILITY SUB-CHECKLIST",
        "**Security lens selected** → Read § SECURITY LENS DEFENSIVE FRAMING",
        "references/conditional.md not found — run jacked install",
    ):
        assert pointer in dcr, f"SKILL.md lost its conditional pointer: {pointer}"


def test_conditional_sections_carry_their_contracts(conditional: str):
    for heading in (
        "## PLAN MODE",
        "## SPECIALIST LENS DISCOVERY",
        "## FRONTEND DESIGN REVIEWER",
        "## UX & FLOW DISCOVERABILITY SUB-CHECKLIST",
        "## SECURITY LENS DEFENSIVE FRAMING",
    ):
        assert heading in conditional
    # plan mode: PLANNING phase, plan-scoped tier, the one editable file
    assert "Set `phase = PLANNING` and skip step 1 ENTIRELY" in conditional
    assert "it is the one file editable in plan mode" in conditional
    # specialist lenses: tier cap, project-local wins
    assert "at most the RISK TIER's specialist cap (SMALL: 2, MEDIUM: 3, LARGE: 4)" in conditional
    assert "project-local wins" in conditional
    # frontend reviewer: fable lane, skill invocation, animation review, one-shot
    assert 'pass `model: "fable"` explicitly' in conditional
    assert "Invoke the frontend-design skill for design context." in conditional
    assert "Invoke the review-animations skill" in conditional
    assert "does NOT enter the re-check loop" in conditional
    assert "do not fake a design review" in conditional
    # UX checklist and Security framing stay verbatim
    assert "> #### Discoverability & Workflow Correctness" in conditional
    assert "3+ for a primary action = flag MEDIUM" in conditional
    assert SECURITY_FRAMING in conditional
    assert "NEVER rephrase a prompt to evade or trick a safety classifier" in conditional


def test_conditional_bodies_stay_out_of_the_always_read_body(dcr: str):
    assert SECURITY_FRAMING not in dcr
    assert "This is a defensive security review of our own authorized codebase" not in dcr
    assert "Discoverability & Workflow Correctness" not in dcr
    assert "3+ for a primary action = flag MEDIUM" not in dcr
    assert "Invoke the review-animations skill" not in dcr


def test_repo_wrapper_carries_tier_contract():
    wrapper = REPO_WRAPPER.read_text(encoding="utf-8")
    for anchor in (
        "## BUDGET FIRST",
        "### RISK TIER",
        "ONE consolidated reviewer carries every selected lens",
        "### SUBSEQUENT WAVES — Fix Verification",
        "Default wave cap is **2**",
    ):
        assert anchor in wrapper, f"repo dcr wrapper missing tier anchor: {anchor}"


def test_repo_wrapper_embeds_engine_body_verbatim():
    """The wrapper contract is 'repo-config header + the shipped engine body
    verbatim' — the engine being everything below the data skill's
    `<!-- ENGINE -->` marker (frontmatter and repo-dispatch preamble dropped).
    Anchor checks alone let the two drift on any non-anchor line; containment
    makes desync impossible to miss."""
    shipped = CMD.read_text(encoding="utf-8")
    assert shipped.startswith("---\n")
    body = _engine_body(shipped)
    wrapper = REPO_WRAPPER.read_text(encoding="utf-8")
    assert body in wrapper, (
        "repo .claude/skills/dcr/SKILL.md no longer embeds the shipped engine body "
        "verbatim — rebuild the wrapper (or re-run /jacked-setup dcr)"
    )


def test_diagnostic_pre_gate_appears_before_wave_one(dcr: str):
    gate = dcr.index("### DIAGNOSTIC PRE-GATE")
    wave1 = dcr.index("### WAVE 1")
    assert gate < wave1, "diagnostics must be specified before Wave 1 spawning"


def test_frontend_trigger_pins_js_logic_exclusion(dcr: str):
    # the .js/.ts path must be conditional on UI-meaningful hunks, not extension alone
    assert "yes ONLY if the diff hunks touch markup/JSX/templates" in dcr
