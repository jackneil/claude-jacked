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
