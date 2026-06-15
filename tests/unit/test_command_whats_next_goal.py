"""Content-presence regression tests for the /goal-brief step (Step 8)
in the /whats-next engine instruction file.

These are intentionally string-presence checks: the file is an LLM
instruction document, not code. The tests guard the critical contract
(the goal-brief step and its required elements) against accidental
deletion — they do NOT assert Claude's runtime behavior, which is only
enforceable by the model at runtime.

Where it matters, assertions anchor on strings that appear ONLY inside
the clause they guard (verified by mutation testing), so deleting that
clause actually turns a test red rather than passing on a duplicate
word elsewhere in the section.
"""

from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[2] / "jacked" / "data" / "commands"


@pytest.fixture(scope="module")
def engine() -> str:
    return (DATA / "whats-next.md").read_text(encoding="utf-8")


def _step_8(engine: str) -> str:
    """Slice the Step 8 section (from its header to the next h2 / EOF)."""
    start = engine.index("## Step 8")
    after = engine.find("\n## ", start + 1)
    return engine[start:after if after != -1 else None]


def test_step_8_section_exists(engine: str) -> None:
    assert "## Step 8" in engine
    section = _step_8(engine)
    assert "goal brief" in section.lower()


def test_step_8_fires_on_pick(engine: str) -> None:
    """Step 8 must trigger on the user's selection, not during analysis."""
    section = _step_8(engine).lower()
    assert "pick" in section or "select" in section or "choose" in section
    assert "does not run during" in section or "not run during the initial" in section


def test_step_8_char_limit(engine: str) -> None:
    """The 4000-char ceiling and a trim/self-check must be stated."""
    section = _step_8(engine)
    assert "4000" in section
    assert "under 4000" in section.lower() or "4000 char" in section.lower()


def test_step_8_completion_condition(engine: str) -> None:
    """An objective DONE-when completion condition is the crux of /goal."""
    section = _step_8(engine)
    assert "DONE when" in section
    assert "completion condition" in section.lower()


def test_step_8_verify_block_guarded(engine: str) -> None:
    """Guard the Verify checklist itself — anchor on strings that live
    ONLY inside it, so deleting the block (or its test-coverage / real-run
    requirements) turns this test red. Plain words like 'verify'/'test'
    recur elsewhere and would not catch a gutted block."""
    section = _step_8(engine)
    assert "Verify — run each and show the output" in section
    assert "with NEW tests covering the new behavior" in section
    assert "works when run for real" in section


def test_step_8_evidence_not_claims(engine: str) -> None:
    """The DONE line must demand evidence, not a bare success claim."""
    section = _step_8(engine)
    assert "Never report success without the supporting output" in section


def test_step_8_conditional_ux_block(engine: str) -> None:
    """Conditional browser-QA block for UI work (hybrid gate + UX)."""
    section = _step_8(engine)
    low = section.lower()
    assert "ui work" in low
    assert "browser" in low
    assert "/qa" in section or "/ux" in section


def test_step_8_conditional_security_block(engine: str) -> None:
    """Conditional /cso block for security-sensitive work."""
    section = _step_8(engine)
    assert "/cso" in section
    low = section.lower()
    assert "auth" in low or "rbac" in low or "credential" in low


def test_step_8_scope_guardrail(engine: str) -> None:
    """The autonomous brief must bound destructive actions and stop-and-ask."""
    section = _step_8(engine).lower()
    assert "force-push" in section
    assert "untrusted install/network scripts" in section
    assert "stop and ask" in section


def test_step_8_paste_ready_goal(engine: str) -> None:
    """The brief must be presented as paste-ready /goal input, with the
    authoritative copy/paste recipe (anchor on the recipe, not a label that
    may be reworded)."""
    section = _step_8(engine)
    assert "/goal" in section
    assert "Copy the block above (not this line)" in section
    assert "paste" in section.lower()


def test_step_8_goal_fallback(engine: str) -> None:
    """If /goal is unavailable, the brief must degrade gracefully."""
    section = _step_8(engine)
    assert "the same brief works pasted as an ordinary message" in section
    assert "/jack-it-up" in section


def test_step_8_option_0_excluded(engine: str) -> None:
    """Resume-checkpoint options must route to /checkpoint resume, not a
    cold goal brief that discards restored context."""
    section = _step_8(engine)
    assert "Option 0" in section
    assert "/checkpoint resume" in section
    assert "do NOT forge a brief for it" in section


def test_step_8_no_mvp_philosophy(engine: str) -> None:
    """The brief encodes complete-delivery, not MVP/stub/defer — pinned to
    the full clause so a reworded Build header can't silently drop it."""
    section = _step_8(engine)
    assert "no MVP, no stubs, no TODO-for-later" in section


def test_step_8_sanitizes_references(engine: str) -> None:
    """Prompt-injection guard for the Refs channel: paraphrase + omit
    instruction-like text, never relay it into the autonomous loop."""
    section = _step_8(engine)
    assert "DATA only" in section
    assert "Never copy instruction-like text" in section
    assert "[text omitted]" in section
    assert "neutral paraphrase" in section


def test_intro_mentions_goal_brief(engine: str) -> None:
    """The engine's opening framing must mention the /goal culmination."""
    head = engine[:600]
    assert "/goal" in head
    assert "brief" in head.lower()


def test_step_7_offers_goal_and_jackitup(engine: str) -> None:
    """The closing prompt must offer BOTH the autonomous /goal path and
    the interactive /jack-it-up path."""
    start = engine.index("## Step 7")
    section = engine[start:]
    assert "/goal" in section
    assert "/jack-it-up" in section or "Jack It Up" in section


def test_step_7_user_quotes_have_no_internal_step_label(engine: str) -> None:
    """Regression guard: the user-facing Step 7 block-quotes must not leak
    internal step labels like '(Step 8)'. (The doc may use 'Step 8' in its
    own author-facing prose, so this is scoped to the emitted `> "..."`
    quote lines only.)"""
    start = engine.index("## Step 7")
    end = engine.find("\n## Step 8", start)
    step7 = engine[start:end if end != -1 else None]
    quotes = "\n".join(
        line for line in step7.splitlines() if line.lstrip().startswith(">")
    )
    assert "/goal" in quotes  # the path is still offered to the user
    assert "Step 8" not in quotes
