"""Content-presence regression tests for the /dcr command's REVIEW ENGINE.

The engine section lets a user route the volume reviewers to the OpenAI Codex
CLI (config from `jacked dcr engine`) while judgment stays in the parent
session. These guard the SAFETY contract of that split: judgment never moves
engines, carve-out lenses never leave Claude, failed Codex jobs always fall
back to a Claude reviewer instead of silently dropping a lens, and Codex
findings still pass finding validation before any fix. Like the other command
tests, these are string-presence checks on the LLM instruction document, not
runtime-behavior assertions.
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
CMD = REPO_ROOT / "jacked" / "data" / "commands" / "dcr.md"
REPO_WRAPPER = REPO_ROOT / ".claude" / "commands" / "dcr.md"


@pytest.fixture(scope="module")
def dcr() -> str:
    return CMD.read_text(encoding="utf-8")


def test_dcr_command_exists():
    assert CMD.exists(), "jacked/data/commands/dcr.md must exist (ships via the install glob)"


def test_engine_section_present_with_both_engines(dcr: str):
    assert "## REVIEW ENGINE" in dcr
    assert "### ENGINE CHECK" in dcr
    assert "### CODEX DISPATCH" in dcr
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


def test_judgment_never_moves_engines(dcr: str):
    assert (
        "lens selection, finding validation, fixes, and the verdict always run "
        "in the parent session regardless of engine" in dcr
    )
    # codex findings still gate through validation before fixes
    assert "validation is MANDATORY for every Codex CRITICAL/MEDIUM" in dcr


def test_carve_outs_stay_on_claude(dcr: str):
    assert "Carve-outs stay as Claude Task dispatches regardless of engine" in dcr
    assert "`keep_on_claude`" in dcr
    assert "Frontend Design reviewer" in dcr


def test_codex_jobs_are_read_only_and_schema_bound(dcr: str):
    assert "codex exec --sandbox read-only --ephemeral" in dcr
    assert '-c model_reasoning_effort="<effort>"' in dcr
    assert "--output-schema" in dcr
    # briefs carry the full reviewer contract, including the exclusion list
    assert "the full DO NOT FLAG list" in dcr


def test_failed_codex_jobs_fall_back_never_drop(dcr: str):
    assert "Respawn THAT reviewer once as a Claude Task subagent" in dcr
    assert "Never drop a lens silently" in dcr
    assert "never count a failed reviewer as PASS" in dcr
    # hung-job handling has the same fallback
    assert "treat it as hung" in dcr


def test_report_names_the_engine(dcr: str):
    assert "**Engine:** Codex ([model], effort [effort]" in dcr


def test_repo_wrapper_carries_engine_section():
    """The repo's generated .claude/commands/dcr.md wrapper must stay in sync
    on the engine contract (it embeds the full command body)."""
    wrapper = REPO_WRAPPER.read_text(encoding="utf-8")
    for anchor in (
        "## REVIEW ENGINE",
        "### CODEX DISPATCH",
        "jacked dcr engine --json",
        "Respawn THAT reviewer once as a Claude Task subagent",
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
    assert "## RISK TIER" in dcr
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


def test_personas_and_wild_cards_are_large_tier_only(dcr: str):
    assert "## REVIEWER PERSONAS (LARGE tier only)" in dcr
    assert "## WILD CARD CHECKS (LARGE tier only)" in dcr


def test_pre_mortem_is_large_tier_only(dcr: str):
    assert "## PRE-MORTEM FAILURE SCENARIOS (LARGE tier only)" in dcr
    assert "PRE-MORTEM ANALYST (LARGE tier, Wave 1 only" in dcr


def test_recheck_waves_verify_fixes_not_fresh_review(dcr: str):
    assert "### SUBSEQUENT WAVES — Fix Verification" in dcr
    assert "Do NOT re-review the rest of the code from scratch" in dcr
    # the old doctrine must be gone
    assert "Do NOT limit your review to verifying prior fixes" not in dcr


def test_default_wave_cap_is_three(dcr: str):
    assert "Default wave cap is **3**" in dcr


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


def test_repo_wrapper_carries_tier_contract():
    wrapper = REPO_WRAPPER.read_text(encoding="utf-8")
    for anchor in (
        "## RISK TIER",
        "ONE consolidated reviewer carries every selected lens",
        "### SUBSEQUENT WAVES — Fix Verification",
        "Default wave cap is **3**",
    ):
        assert anchor in wrapper, f"repo dcr wrapper missing tier anchor: {anchor}"


def test_repo_wrapper_embeds_engine_body_verbatim():
    """The wrapper contract is 'repo-config header + the shipped engine body
    verbatim (front matter stripped)'. Anchor checks alone let the two drift on
    any non-anchor line; containment makes desync impossible to miss."""
    shipped = CMD.read_text(encoding="utf-8")
    assert shipped.startswith("---\n")
    body = shipped[shipped.index("\n---\n", 4) + len("\n---\n"):].lstrip("\n")
    wrapper = REPO_WRAPPER.read_text(encoding="utf-8")
    assert body in wrapper, (
        "repo .claude/commands/dcr.md no longer embeds the shipped engine body "
        "verbatim — rebuild the wrapper (or re-run /jacked-setup dcr)"
    )


def test_diagnostic_pre_gate_appears_before_wave_one(dcr: str):
    gate = dcr.index("### DIAGNOSTIC PRE-GATE")
    wave1 = dcr.index("### WAVE 1")
    assert gate < wave1, "diagnostics must be specified before Wave 1 spawning"


def test_frontend_trigger_pins_js_logic_exclusion(dcr: str):
    # the .js/.ts path must be conditional on UI-meaningful hunks, not extension alone
    assert "yes ONLY if the diff hunks touch markup/JSX/templates" in dcr
