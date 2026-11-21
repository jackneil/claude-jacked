"""The workflows pin every github/codeql-action step to one commit.

CodeQL stops a workflow when its init and analyze steps run different
versions ("All steps in a workflow that use github/codeql-action must use
the same version"). Dependabot bumped the two paths in separate pull
requests (#316pr, #317pr), and each pull request failed alone. These tests
fail on a version skew from any source, and they check that Dependabot
updates the paths together.
"""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
CODEQL_USE = re.compile(r"uses:\s*(github/codeql-action/[\w-]+)@([0-9a-f]{40})")


def _codeql_pins() -> dict[str, set[str]]:
    pins: dict[str, set[str]] = {}
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for path, sha in CODEQL_USE.findall(workflow.read_text(encoding="utf-8")):
            pins.setdefault(sha, set()).add(f"{workflow.name}: {path}")
    return pins


def test_every_codeql_action_step_pins_the_same_commit():
    pins = _codeql_pins()
    assert pins, "no github/codeql-action step found: the pattern no longer matches the workflows"
    assert len(pins) == 1, f"github/codeql-action steps pin different commits: {pins}"


def test_dependabot_updates_the_codeql_action_paths_together():
    """Every codeql-action path the workflows use is in one Dependabot group,
    the same group for all of them."""
    config = yaml.safe_load((REPO_ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))
    actions = next(u for u in config["updates"] if u["package-ecosystem"] == "github-actions")
    groups = actions.get("groups", {})
    paths = sorted({entry.split(": ", 1)[1] for used in _codeql_pins().values() for entry in used})
    holders = {
        path: [name for name, group in groups.items()
               if any(fnmatch.fnmatchcase(path, pattern) for pattern in group.get("patterns", []))]
        for path in paths
    }
    assert len(paths) >= 2, paths
    assert all(len(names) == 1 for names in holders.values()), holders
    assert len({names[0] for names in holders.values()}) == 1, holders
