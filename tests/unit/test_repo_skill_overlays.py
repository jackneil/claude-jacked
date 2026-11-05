"""Facts that the repo-config overlays of .claude/skills state about this repo.

An overlay is hand-checked text above the shipped engine body. Each test here
compares an overlay claim with the file that owns the fact, so the claim cannot
go stale without a failure.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SKILLS = REPO_ROOT / ".claude" / "skills"
TESTS_YML = REPO_ROOT / ".github" / "workflows" / "tests.yml"


def _overlay(name: str) -> str:
    text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
    return text.split("<!-- ENGINE - DO NOT EDIT BELOW THIS LINE -->", 1)[0]


def _gate_commands() -> str:
    overlay = _overlay("release")
    start = overlay.index("- **Gate Commands**:")
    return overlay[start : overlay.index("- **Publish Mechanism**:", start)]


def test_release_gate_runs_every_command_of_the_ci_workflow():
    """The gate once said "`uv export --frozen` audited with `pip-audit`", which
    is prose, is weaker than CI and cannot run: pip-audit is not in the dev
    group. Each command of tests.yml must appear, with only the temp folder
    changed."""
    workflow = TESTS_YML.read_text(encoding="utf-8")
    gate = _gate_commands()
    for command in (
        "uv lock --check",
        "uv run --frozen ruff check .",
        "uv run --frozen python -m pytest",
    ):
        assert f"run: {command}" in workflow, command
        assert command in gate, command
    for prefix in ("uv export --frozen", "sed -E "):
        line = next(
            row.strip() for row in workflow.splitlines() if row.strip().startswith(prefix)
        )
        assert line.replace("$RUNNER_TEMP", "$GATE_TMP") in gate, prefix
    script = textwrap.dedent(re.search(r"<<'PY'\n(.*?)\n\s*PY\n", workflow, re.S).group(1))
    for line in script.splitlines():
        assert line in gate, line
    audit = next(row.strip() for row in workflow.splitlines() if row.strip().startswith("pip-audit "))
    assert audit.replace("$RUNNER_TEMP", "$GATE_TMP") in gate
    zizmor = re.search(r"run: (zizmor .*)", workflow).group(1)
    assert zizmor in gate
    pin_age = re.search(r"run: python (scripts/ci/check_pin_age\.py .*)", workflow).group(1)
    assert f"uv run --frozen python {pin_age}" in gate


def test_pin_age_gate_covers_every_lock_at_its_dependabot_cooldown():
    """#312i: the pin-age step is the only proof that a Dependabot bump did not
    drag in a fresh transitive pin. It must read every lock Dependabot writes,
    at the default-days of the Dependabot entry that writes it."""
    import yaml

    workflow = TESTS_YML.read_text(encoding="utf-8")
    command = re.search(r"run: python scripts/ci/check_pin_age\.py (.*)", workflow).group(1).split()
    min_days = float(command[command.index("--min-days") + 1])
    paths = command[command.index("--min-days") + 2 :]
    assert paths == ["uv.lock", ".github/requirements/*.txt"]

    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    uv_entries = {e["directory"]: e for e in config["updates"] if e["package-ecosystem"] == "uv"}
    assert set(uv_entries) == {"/", "/.github/requirements"}
    for directory, entry in uv_entries.items():
        assert entry["cooldown"]["default-days"] == min_days, directory
    # pyproject's exclude-newer comment states the same 7-day floor.
    assert "≥ 7 days ago is the minimum standard" in (REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert min_days == 7


def test_release_gate_takes_the_audit_tools_from_the_hash_locked_file():
    gate = _gate_commands()
    locked = "--with-requirements .github/requirements/ci-audit.txt"
    assert (REPO_ROOT / ".github" / "requirements" / "ci-audit.txt").exists()
    assert f"{locked} pip-audit " in gate
    assert f"{locked} zizmor " in gate


def test_release_gate_keeps_the_build_check_and_drops_the_pre_commit_claim():
    """pre-commit runs no OSV audit, lock check, ruff or pytest, so it is no
    mirror of CI."""
    gate = _gate_commands()
    assert re.search(r'twine"? check', gate)
    assert "pre-commit" not in gate


def test_release_gate_builds_like_the_release_job():
    """The release build (build-dist.yml) uses the hash-locked hatchling with
    --no-isolation. The gate once ran `uv build`, which fetches an unpinned
    backend, so it could pass on a tool set that the release job never uses."""
    workflow = (REPO_ROOT / ".github" / "workflows" / "build-dist.yml").read_text(encoding="utf-8")
    gate = _gate_commands()
    for pattern in (
        r"--require-hashes --only-binary=:all: -r \.github/requirements/ci-publish\.txt",
        r"-m build --no-isolation",
        r'twine"? check',
        r'cyclonedx-py"? environment -o',
    ):
        assert re.search(pattern, workflow), pattern
        assert re.search(pattern, gate), pattern
    assert "uv build" not in gate
    assert "uvx twine" not in gate
    # The lock is compiled for one Python; the gate must build on the same one.
    workflow_python = re.search(r'python-version: "([0-9.]+)"', workflow).group(1)
    assert f'uv venv -q -p {workflow_python} "$GATE_TMP/pub"' in gate
    assert f"--python-version {workflow_python}" in (
        REPO_ROOT / ".github" / "requirements" / "ci-publish.txt"
    ).read_text(encoding="utf-8")


def test_dcr_wild_card_scopes_data_framing_to_the_context_data_steps():
    """The chain-of-command step prints instructions by design, so a wild card
    that demands data framing on every step reports a false finding."""
    overlay = _overlay("dcr")
    card = next(line for line in overlay.splitlines() if "Hook-content trust boundary" in line)
    assert "context-data SessionStart step" in card
    assert "git reality, open work, memory recall" in card
    assert "the chain-of-command step is the one exception" in card
    assert "does every new or changed SessionStart step" not in card


def test_qa_and_ux_name_the_real_location_of_the_integrity_panel():
    """Hooks is a section of Settings > Features, not a tab of Settings."""
    settings = (REPO_ROOT / "jacked" / "data" / "web" / "js" / "components" / "settings.js").read_text(
        encoding="utf-8"
    )
    assert ">Hooks</h3>" in settings
    for name in ("qa", "ux"):
        overlay = _overlay(name)
        assert "Settings > Features, Hooks section" in overlay, name
        assert "Settings > Hooks" not in overlay, name


def test_whats_next_overlay_carries_no_counts_that_go_stale():
    line = next(row for row in _overlay("whats-next").splitlines() if row.startswith("- **Lifecycle**"))
    assert line == "- **Lifecycle**: Beta"
