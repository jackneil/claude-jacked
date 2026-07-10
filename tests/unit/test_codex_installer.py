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


def _manifest(homes):
    """The Codex manifest as written by the last install (skills/prompts dicts)."""
    return json.loads(ins.manifest_path(homes["home"]).read_text())


def _add_skill(data_root, name, desc="a skill"):
    d = data_root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: {desc}\n---\nbody\n")


def _add_command(data_root, name):
    (data_root / "commands" / name).write_text(f"---\ndescription: {name}\n---\nrun\n")


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


def test_install_excludes_claude_only_skills(data_root, homes):
    """chain-of-command is a Claude-only model-dispatch policy; the Codex pass must
    skip it (Codex has no multi-model dispatch), while ordinary skills still land."""
    coc = data_root / "skills" / "chain-of-command"
    coc.mkdir(parents=True)
    (coc / "SKILL.md").write_text(
        "---\nname: chain-of-command\ndescription: fable plans, opus codes\n---\nbody\n"
    )
    summ = _install(data_root, homes)
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    assert not (skills_base / "chain-of-command").exists()
    assert "chain-of-command" not in summ.skills
    assert "chain-of-command" not in _manifest(homes)["skills"]  # never recorded
    # the ordinary skill is unaffected by the exclusion
    assert (skills_base / "demo-skill" / "SKILL.md").exists()
    assert "demo-skill" in summ.skills


def test_install_excludes_recover_and_chain_of_command(data_root, homes):
    """Both Claude-only skills are skipped by the Codex pass: chain-of-command (a
    Claude model-dispatch policy) and recover (recovers crashed CLAUDE CODE sessions
    from ~/.claude/projects). Neither lands on disk nor in the manifest; an ordinary
    skill still does."""
    _add_skill(data_root, "chain-of-command", "fable plans, opus codes")
    _add_skill(data_root, "recover", "recover a crashed Claude Code session")
    summ = _install(data_root, homes)
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    manifest = _manifest(homes)
    for name in ("chain-of-command", "recover"):
        assert not (skills_base / name).exists()          # absent from dir
        assert name not in summ.skills
        assert name not in manifest["skills"]             # absent from manifest
    # the ordinary skill lands, in both dir and manifest
    assert (skills_base / "demo-skill" / "SKILL.md").exists()
    assert "demo-skill" in summ.skills and "demo-skill" in manifest["skills"]


def test_install_excludes_claude_only_commands(data_root, homes):
    """swarm / goal-maker / browser-reset / jacked-setup are wired to Claude Code
    machinery Codex has no analog for; the Codex pass must skip them. Only ordinary
    commands land in ~/.codex/prompts and in manifest["prompts"]."""
    for name in ("swarm.md", "goal-maker.md", "browser-reset.md", "jacked-setup.md"):
        _add_command(data_root, name)
    summ = _install(data_root, homes)
    prompts_dst = ins.codex_prompts_dir(homes["home"])
    manifest = _manifest(homes)
    for name in ("swarm.md", "goal-maker.md", "browser-reset.md", "jacked-setup.md"):
        assert not (prompts_dst / name).exists()          # absent from prompts dir
        assert name not in summ.prompts
        assert name not in manifest["prompts"]            # absent from manifest
    # the ordinary command lands, in both prompts dir and manifest
    assert (prompts_dst / "dcr.md").exists()
    assert "dcr.md" in summ.prompts and "dcr.md" in manifest["prompts"]


def test_install_prunes_stale_now_excluded_artifacts(data_root, homes):
    """Upgrade path: a skill/command that jacked shipped before but now excludes must
    have its previously-installed copy DELETED, be reported in summary.removed, and be
    gone from the fresh manifest, even though the (now-excluded) source still exists."""
    # A prior install had recover + swarm.md on disk and in the manifest.
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    prompts_dst = ins.codex_prompts_dir(homes["home"])
    (skills_base / "recover").mkdir(parents=True)
    (skills_base / "recover" / "SKILL.md").write_text("stale\n")
    prompts_dst.mkdir(parents=True, exist_ok=True)
    (prompts_dst / "swarm.md").write_text("stale\n")
    ins._write_manifest(
        homes["home"], "0.9",
        {"recover": "sha256:stale"}, {"swarm.md": "sha256:stale"},
        False, False, "before",
    )
    # The sources still exist but are now Claude-only, so they must be pruned.
    _add_skill(data_root, "recover", "recover a crashed Claude Code session")
    _add_command(data_root, "swarm.md")
    summ = _install(data_root, homes)
    assert "skills/recover" in summ.removed
    assert "prompts/swarm.md" in summ.removed
    assert not (skills_base / "recover").exists()
    assert not (prompts_dst / "swarm.md").exists()
    manifest = _manifest(homes)
    assert "recover" not in manifest["skills"]
    assert "swarm.md" not in manifest["prompts"]


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


def test_claude_install_includes_chain_of_command(tmp_path, monkeypatch):
    """chain-of-command is a Claude-only skill: it ships to ~/.claude/skills on a
    real `jacked install` (the Codex exclusion above must not affect Claude)."""
    from click.testing import CliRunner

    from jacked.cli import main

    monkeypatch.setenv("JACKED_HOME", str(tmp_path))
    result = CliRunner().invoke(
        main,
        ["install", "--no-tray", "--no-rules", "--no-codex", "--force"],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / ".claude" / "skills" / "chain-of-command" / "SKILL.md").exists(), \
        "chain-of-command must be installed into Claude Code"
