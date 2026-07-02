"""M7: install jacked's artifacts into Codex (+ fix the Claude skill sidecar drop).

Covers install_codex (skills with sidecars -> ~/.agents/skills, commands ->
~/.codex/prompts, rules -> AGENTS.md block), idempotency + prune via its own
manifest, legacy gatekeeper hook pruning that preserves user hooks, uninstall,
and the Claude-side fix that now copies skill sidecar files.
"""

import json

import pytest

from jacked.codex import installer as ins


@pytest.fixture
def data_root(tmp_path):
    """A miniature jacked data/ tree: a skill with a sidecar, a command, rules."""
    root = tmp_path / "data"
    skill = root / "skills" / "demo-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: a demo skill\n---\nbody\n"
    )
    (skill / "measure.js").write_text("// sidecar\nconsole.log('hi');\n")
    (skill / "references").mkdir()
    (skill / "references" / "notes.md").write_text("# notes\n")
    (root / "commands").mkdir(parents=True)
    (root / "commands" / "dcr.md").write_text("---\ndescription: review\n---\nrun dcr\n")
    (root / "rules").mkdir(parents=True)
    (root / "rules" / "jacked_behaviors.md").write_text("# jacked behaviors\nbe blunt\n")
    return root


@pytest.fixture
def homes(tmp_path):
    return {"home": tmp_path / "codex", "agents_home": tmp_path / "agents"}


def _install(data_root, homes, **kw):
    return ins.install_codex(
        data_root, home=homes["home"], agents_home=homes["agents_home"],
        version="1.0", now_iso="now", **kw
    )


# --------------------------------------------------------------------------
# install lands every artifact at the right Codex path
# --------------------------------------------------------------------------

def test_install_lands_all_artifacts(data_root, homes):
    summ = _install(data_root, homes)
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    assert (skills_base / "demo-skill" / "SKILL.md").exists()
    # commands -> prompts
    assert (ins.codex_prompts_dir(homes["home"]) / "dcr.md").exists()
    # rules -> AGENTS.md block
    agents_md = ins.codex_agents_md(homes["home"]).read_text()
    assert ins._AGENTS_BEGIN in agents_md and "be blunt" in agents_md
    # gatekeeper hooks are no longer installed (retired in 0.70.0)
    assert not ins.codex_hooks_json(homes["home"]).exists() or \
        "security_gatekeeper" not in ins.codex_hooks_json(homes["home"]).read_text()
    assert summ.skills == ["demo-skill"] and summ.prompts == ["dcr.md"]
    assert summ.rules and not summ.hooks


def test_install_copies_skill_sidecars(data_root, homes):
    _install(data_root, homes)
    dst = ins.agents_skills_dir(homes["agents_home"]) / "demo-skill"
    assert (dst / "measure.js").read_text().startswith("// sidecar")
    assert (dst / "references" / "notes.md").exists()  # nested sidecar dir too


def test_install_idempotent_no_changes_second_run(data_root, homes):
    first = _install(data_root, homes)
    assert first.changed is True  # net-new
    second = _install(data_root, homes)
    assert second.changed is False  # nothing changed on re-run -> manifest-clean


def test_install_prunes_removed_skill(data_root, homes):
    _install(data_root, homes)
    # Remove the skill from the source, re-install -> it's pruned from Codex.
    import shutil
    shutil.rmtree(data_root / "skills" / "demo-skill")
    summ = _install(data_root, homes)
    assert "skills/demo-skill" in summ.removed
    assert not (ins.agents_skills_dir(homes["agents_home"]) / "demo-skill").exists()


def test_install_replaces_stale_sidecars(data_root, homes):
    _install(data_root, homes)
    # A sidecar removed from source must not linger in the dest after re-install.
    (data_root / "skills" / "demo-skill" / "measure.js").unlink()
    _install(data_root, homes)
    dst = ins.agents_skills_dir(homes["agents_home"]) / "demo-skill"
    assert not (dst / "measure.js").exists()


# --------------------------------------------------------------------------
# AGENTS.md block idempotency + preservation
# --------------------------------------------------------------------------

def test_agents_block_idempotent_and_preserves_user_content(data_root, homes):
    agents_md = ins.codex_agents_md(homes["home"])
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    agents_md.write_text("# My rules\nkeep me\n")
    _install(data_root, homes)
    _install(data_root, homes)  # twice
    text = agents_md.read_text()
    assert text.count(ins._AGENTS_BEGIN) == 1  # not duplicated
    assert "keep me" in text  # user content preserved


# --------------------------------------------------------------------------
# hooks.json merge preserves user hooks
# --------------------------------------------------------------------------

def test_hooks_merge_preserves_user_hooks(data_root, homes):
    """Install prunes LEGACY jacked gatekeeper entries but never user hooks."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"hooks": {
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "./mine.sh"}]}
        ],
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command",
                                       "command": "jacked _hook security_gatekeeper"}]}
        ],
    }}))
    _install(data_root, homes)
    data = json.loads(hp.read_text())
    assert any("./mine.sh" in h["command"]
               for g in data["hooks"].get("PostToolUse", []) for h in g["hooks"])
    assert "security_gatekeeper" not in json.dumps(data)  # legacy entry pruned


# --------------------------------------------------------------------------
# uninstall
# --------------------------------------------------------------------------

def test_uninstall_removes_everything_jacked_added(data_root, homes):
    agents_md = ins.codex_agents_md(homes["home"])
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    agents_md.write_text("# Mine\nkeep\n")
    _install(data_root, homes)
    out = ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    assert not (ins.agents_skills_dir(homes["agents_home"]) / "demo-skill").exists()
    assert not (ins.codex_prompts_dir(homes["home"]) / "dcr.md").exists()
    text = agents_md.read_text()
    assert ins._AGENTS_BEGIN not in text and "keep" in text  # block gone, user kept
    assert not ins.manifest_path(homes["home"]).exists()
    assert "AGENTS.md block" in out["removed"]


def test_uninstall_preserves_user_hooks(data_root, homes):
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"hooks": {
        "PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "./mine.sh"}]}
        ],
        "PreToolUse": [
            {"matcher": "", "hooks": [{"type": "command",
                                       "command": "jacked _hook security_gatekeeper"}]}
        ],
    }}))
    _install(data_root, homes)
    ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    data = json.loads(hp.read_text())
    assert "./mine.sh" in json.dumps(data)  # user hook survives
    assert "security_gatekeeper" not in json.dumps(data)  # jacked entries gone


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------

def test_codex_present_true_when_home_exists(tmp_path, monkeypatch):
    home = tmp_path / ".codex"
    home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(home))
    assert ins.codex_present() is True


# --------------------------------------------------------------------------
# Claude-side fix: skills now copy sidecar files
# --------------------------------------------------------------------------

def test_claude_install_copies_skill_sidecars(tmp_path, monkeypatch):
    """Regression for the SKILL.md-only drop: a real `jacked install` must copy
    skill sidecars (e.g. aesthetic-dogfood-audit/measure.js) into ~/.claude."""
    from click.testing import CliRunner

    from jacked.cli import main

    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    result = CliRunner().invoke(
        main,
        ["install", "--no-tray", "--no-rules", "--no-codex", "--force"],
    )
    assert result.exit_code == 0, result.output
    sidecar = tmp_path / ".claude" / "skills" / "aesthetic-dogfood-audit" / "measure.js"
    assert sidecar.exists(), "skill sidecar must be installed alongside SKILL.md"
