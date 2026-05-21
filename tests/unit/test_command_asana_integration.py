"""Content-presence regression tests for the Asana integration in
jacked-setup and whats-next instruction files.

These are intentionally string-presence checks: the files are LLM
instruction documents, not code. The tests guard against regression
(accidental deletion of critical sections), not behavior — the
behavior is enforced by Opus at runtime."""

from pathlib import Path

import pytest

DATA = Path(__file__).resolve().parents[2] / "jacked" / "data" / "commands"


@pytest.fixture(scope="module")
def whats_next_engine() -> str:
    return (DATA / "whats-next.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def jacked_setup() -> str:
    return (DATA / "jacked-setup.md").read_text(encoding="utf-8")


def test_engine_declares_step_3_5(whats_next_engine: str) -> None:
    """Engine must declare a Step 3.5 dedicated to Asana signals."""
    assert "## Step 3.5: Pull Asana Signals" in whats_next_engine


def test_engine_step_3_5_skips_when_unconfigured(whats_next_engine: str) -> None:
    """The Step 3.5 block must instruct Opus to skip cleanly if the
    integration isn't configured — graceful degradation requirement
    from the spec."""
    start = whats_next_engine.index("## Step 3.5: Pull Asana Signals")
    after = whats_next_engine.find("\n## ", start + 1)
    section = whats_next_engine[start:after if after != -1 else None]
    assert "Skip" in section or "skip" in section
    assert "Asana Integration" in section  # references the config block
    assert "Access" in section  # references the access field


def test_engine_step_5_acknowledges_asana(whats_next_engine: str) -> None:
    """Synthesis step must treat Asana as a candidate source alongside
    GitHub and TODOs — without granting it a tier bonus."""
    assert "Asana" in whats_next_engine
    start = whats_next_engine.index("## Step 5: Synthesize and Rank")
    after = whats_next_engine.find("\n## ", start + 1)
    section = whats_next_engine[start:after if after != -1 else None]
    assert "Asana" in section


def test_engine_evidence_line_example_includes_asana(whats_next_engine: str) -> None:
    """Step 6's Evidence-line example must show an Asana token so
    Opus emits the metadata consistently. We assert the literal example
    string rather than slicing Step 6 because Step 6 contains a fenced
    code block whose own `##` headers confuse naive section locators."""
    assert "Asana 1200012345 in Engineering Backlog" in whats_next_engine


def test_setup_probes_for_asana_access(jacked_setup: str) -> None:
    """jacked-setup must probe for at least the three documented access
    methods (MCP, CLI, PAT) before writing the Asana section."""
    start = jacked_setup.index("### For `whats-next`:")
    after = jacked_setup.find("\n### ", start + 1)
    section = jacked_setup[start:after if after != -1 else None]
    assert "mcp" in section.lower()
    assert "cli" in section.lower() or "asana --version" in section
    assert "ASANA_PERSONAL_ACCESS_TOKEN" in section


def test_setup_standalone_template_includes_asana_section(jacked_setup: str) -> None:
    """The whats-next standalone template must include the
    `## Asana Integration` block (with both populated and
    install-hint branches)."""
    start = jacked_setup.index("### whats-next standalone template:")
    after = jacked_setup.find("\n### ", start + 1)
    section = jacked_setup[start:after if after != -1 else None]
    assert "## Asana Integration" in section
    assert "Access" in section
    assert "none" in section.lower()
    assert "Workspaces" in section or "workspace" in section.lower()


def test_setup_install_hint_branch_mentions_plugin_and_pat(jacked_setup: str) -> None:
    """When access is `none`, the emitted block must self-document
    enablement — the spec requires this for cloners without jacked."""
    start = jacked_setup.index("### whats-next standalone template:")
    after = jacked_setup.find("\n### ", start + 1)
    section = jacked_setup[start:after if after != -1 else None]
    assert "plugin install asana" in section.lower()
    assert "ASANA_PERSONAL_ACCESS_TOKEN" in section
