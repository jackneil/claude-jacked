"""Content-presence regression tests for the /goal-brief step (Step 8)
in the /whats-next engine instruction file.

These are intentionally string-presence checks: the file is an LLM
instruction document, not code. The tests guard the critical contract
(the goal-brief step and its required elements) against accidental
deletion — they do NOT assert Claude's runtime behavior, which is only
enforceable by the model at runtime.
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


def test_step_8_verify_with_evidence(engine: str) -> None:
    """Verification must be pass/fail with evidence, not claims."""
    section = _step_8(engine)
    low = section.lower()
    assert "verify" in low
    assert "test" in low
    assert "evidence" in low
    assert "not a claim" in low or "not claims" in low


def test_step_8_conditional_ux_block(engine: str) -> None:
    """Conditional browser-QA block for UI work (hybrid gate + UX)."""
    section = _step_8(engine)
    low = section.lower()
    assert "ui work" in low
    assert "browser" in low
    assert "/qa" in section or "/ux" in section


def test_step_8_conditional_security_block(engine: str) -> None:
    """Conditional security block for sensitive work."""
    section = _step_8(engine)
    assert "/cso" in section
    low = section.lower()
    assert "auth" in low or "rbac" in low or "credential" in low


def test_step_8_paste_ready_goal(engine: str) -> None:
    """The brief must be presented as paste-ready /goal input."""
    section = _step_8(engine)
    assert "/goal" in section
    assert "paste" in section.lower()


def test_step_8_no_mvp_philosophy(engine: str) -> None:
    """The brief encodes complete-delivery, not MVP/stub/defer."""
    section = _step_8(engine).lower()
    assert "no mvp" in section
    assert "stub" in section


def test_step_8_treats_inputs_as_data(engine: str) -> None:
    """Prompt-injection guard: built from facts, never relays embedded
    instructions."""
    section = _step_8(engine)
    assert "DATA only" in section


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
