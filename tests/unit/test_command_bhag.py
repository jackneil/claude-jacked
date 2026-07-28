"""Content-presence regression tests for the /bhag command — the autonomous,
full-coverage-matrix build-out that can merge to main in a loop.

These guard the SAFETY contract of a dangerous capability: the double-gate
that protects auto-merge, the degrade-to-staged default, and the per-iteration
guardrails. Like the other command tests, these are string-presence checks
on the LLM instruction document, not runtime-behavior assertions.
"""

from pathlib import Path

import pytest

CMD = Path(__file__).resolve().parents[2] / "jacked" / "data" / "commands" / "bhag.md"


@pytest.fixture(scope="module")
def bhag() -> str:
    return CMD.read_text(encoding="utf-8")


def test_bhag_command_exists():
    assert CMD.exists(), "jacked/data/commands/bhag.md must exist (ships via the install glob)"


def test_frontmatter_marks_it_deliberate_and_points_to_whats_next(bhag: str):
    head = bhag[: bhag.index("---", 3) + 3] if bhag.startswith("---") else bhag[:600]
    assert "description:" in head
    low = head.lower()
    assert "only" in low                        # deliberate invocation
    assert "autonomous" in low
    assert "/whats-next" in head                # names the safe everyday alternative


def test_safety_gate_is_first_and_double_gated(bhag: str):
    assert "SAFETY GATE" in bhag
    # Gate 1: declared maturity, with the real Lifecycle vocabulary
    assert "Lifecycle" in bhag
    assert "Greenfield" in bhag and "Alpha" in bhag          # eligible (pre-production)
    assert "Beta" in bhag and "Growth" in bhag and "Maintenance" in bhag  # NOT eligible
    # Gate 2: explicit human authorization
    assert "authoriz" in bhag.lower()
    assert "not serving live users" in bhag


def test_auto_merge_is_conservative_default_staged(bhag: str):
    assert "MERGE mode" in bhag and "STAGED mode" in bhag
    assert "When in doubt, STAGED" in bhag                   # conservative default
    assert "Never auto-merge on inference" in bhag
    # the staged variant must explicitly forbid merging
    assert "Do NOT merge to main" in bhag


def test_per_iteration_guardrails(bhag: str):
    assert "WAIT for all CI checks" in bhag                  # block on CI, never merge on pending
    assert "every CI check reports" in bhag                  # merge only when checks PASSED
    assert "gh pr merge --merge" in bhag                     # pinned merge method (true merge commit)
    assert "`--squash`" in bhag and "`--admin`" in bhag      # forbids history-rewrite / protection bypass
    assert "A red iteration is NEVER merged" in bhag
    assert "force-push" in bhag                              # never force-push / rewrite history
    assert "BLOCKED" in bhag                                 # stop, don't merge a bad iteration
    assert "backstop" in bhag.lower()                        # stuck-detection backstop (never caps successful work)
    assert "stuck-detection" in bhag.lower()                 # backstop = stuck-detection, NOT a turn/merge/cost cap
    assert "cap on successful work" in bhag.lower()          # explicit: completed cells are success, never a halt


def test_separate_command_rationale(bhag: str):
    low = bhag.lower()
    # documented reason it is its own command and not an auto-triggering skill
    assert "separate command" in low
    assert "auto-triggering skill" in low or "auto-trigger" in low


def test_brief_is_file_backed_with_pointer(bhag: str):
    """The loop brief is a verbose FILE (no size cap); /goal gets only a short
    self-bootstrapping pointer that pastes the Loop/STOP/DONE criteria into the
    transcript on turn one (the judge can't read files)."""
    assert ".claude/goals/" in bhag
    assert "verbose by design, no size cap" in bhag
    assert "never squeeze the brief" in bhag
    assert "pointer-goal" in bhag
    assert "paste its complete Loop steps" in bhag
    assert "wc -c" in bhag                     # the pointer is still measured
