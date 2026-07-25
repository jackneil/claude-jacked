"""M7: install jacked's artifacts into Codex (+ fix the Claude skill sidecar drop).

Covers install_codex (skills with sidecars -> ~/.agents/skills, commands ->
~/.codex/prompts, rules -> AGENTS.md block), idempotency + prune via its own
manifest, legacy gatekeeper hook pruning that preserves user hooks, uninstall,
and the Claude-side fix that now copies skill sidecar files.
"""

import json
import logging
import shutil
import tomllib
from pathlib import Path

import pytest
import yaml

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


def _add_agent(data_root, name, desc, body):
    """Write a frontmattered Claude subagent .md (name/description + markdown body).

    Includes Claude-only `tools:` / `model:` keys so tests can assert the Codex
    TOML never carries them over."""
    d = data_root / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n"
        "tools: All tools\nmodel: inherit\n---\n" + body
    )


def _body_after_frontmatter(text):
    """Everything after a leading ---...--- frontmatter block (whole text if none).

    Generic splitter used to compare a generated SKILL.md's body against a
    command's body without coupling to the installer's internals."""
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n"):]


def _frontmatter(text):
    """Parse the leading ---...--- YAML frontmatter block of `text` into a dict."""
    assert text.startswith("---\n"), text[:40]
    end = text.find("\n---\n", 4)
    assert end != -1, text[:80]
    return yaml.safe_load(text[4:end])


def _skill_dir(homes, name):
    return ins.agents_skills_dir(homes["agents_home"]) / name


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
    # legacy gatekeeper hooks are no longer installed (retired in 0.70.0), but
    # the QA-suggest Stop hook IS installed now.
    hooks_text = ins.codex_hooks_json(homes["home"]).read_text()
    assert "security_gatekeeper" not in hooks_text
    assert "_hook qa_suggest" in hooks_text
    # commands now ALSO ship as Codex skills (see below), so the command dcr.md
    # yields a `dcr` skill alongside the ordinary demo-skill.
    assert summ.skills == ["demo-skill", "dcr"] and summ.prompts == ["dcr.md"]
    assert summ.rules and summ.hooks


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
    # Record the REAL dir hash so the prune hash-gate sees jacked's own unmodified
    # copy (the user hasn't edited it) and proceeds to delete the now-excluded skill.
    ins._write_manifest(
        homes["home"], "0.9",
        {"recover": ins._sha_dir(skills_base / "recover")},
        {"swarm.md": "sha256:stale"}, {},
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
# commands ALSO ship as Codex skills (OpenAI deprecated ~/.codex/prompts on
# 2026-01-22 in favor of the agentskills.io skills surface)
# --------------------------------------------------------------------------

def test_command_ships_as_skill_frontmatter_and_body(data_root, homes):
    """Every non-excluded command yields ~/.agents/skills/<stem>/SKILL.md whose
    frontmatter parses as YAML (name == stem, non-empty description) and whose
    body is byte-identical to the command's content after its own frontmatter.
    The ~/.codex/prompts copy is still written (unchanged back-compat behavior)."""
    _add_command(data_root, "release.md")  # a second, non-excluded command
    _install(data_root, homes)
    prompts_dst = ins.codex_prompts_dir(homes["home"])
    for stem, cmd_name in (("dcr", "dcr.md"), ("release", "release.md")):
        skill_md = _skill_dir(homes, stem) / "SKILL.md"
        assert skill_md.exists()
        text = skill_md.read_text()
        meta = _frontmatter(text)
        assert meta["name"] == stem
        assert isinstance(meta["description"], str) and meta["description"].strip()
        cmd_text = (data_root / "commands" / cmd_name).read_text()
        assert _body_after_frontmatter(text) == _body_after_frontmatter(cmd_text)
        # prompt still written for back-compat during the deprecation window
        assert (prompts_dst / cmd_name).exists()


def test_command_skill_passes_through_argument_hint(data_root, homes):
    """A command that declares `argument-hint` has it carried onto the skill
    frontmatter (and parses as valid YAML)."""
    (data_root / "commands" / "cleanup.md").write_text(
        '---\ndescription: clean up\nargument-hint: "[--dry-run | --auto-safe]"\n'
        "model: inherit\n---\ndo cleanup\n"
    )
    _install(data_root, homes)
    meta = _frontmatter((_skill_dir(homes, "cleanup") / "SKILL.md").read_text())
    assert meta["name"] == "cleanup"
    assert meta["argument-hint"] == "[--dry-run | --auto-safe]"
    assert "model" not in meta  # only name/description/argument-hint pass through


def test_command_skill_overwrites_pointer_wrapper(data_root, homes):
    """A pointer-wrapper skill AND a same-name command in the data root: after
    install the skill dir holds ONLY the command-derived SKILL.md (no stale
    wrapper sidecars), and manifest["skills"][name] tracks the command content
    (changes when the command changes, not the wrapper)."""
    wrapper = data_root / "skills" / "dcr"
    wrapper.mkdir(parents=True)
    (wrapper / "SKILL.md").write_text(
        "---\nname: dcr\ndescription: pointer wrapper\n---\n"
        "read ~/.claude/commands/dcr.md\n"
    )
    (wrapper / "references").mkdir()
    (wrapper / "references" / "stale.md").write_text("stale sidecar\n")
    # the fixture already carries command dcr.md (body "run dcr\n")
    _install(data_root, homes)
    dst = _skill_dir(homes, "dcr")
    assert sorted(p.name for p in dst.iterdir()) == ["SKILL.md"]  # sidecar gone
    text = (dst / "SKILL.md").read_text()
    assert _body_after_frontmatter(text) == "run dcr\n"  # command body, not wrapper
    assert "pointer wrapper" not in text
    # manifest reflects the command-derived content and changes with the command
    before = _manifest(homes)["skills"]["dcr"]
    (data_root / "commands" / "dcr.md").write_text(
        "---\ndescription: review\n---\nrun dcr DIFFERENTLY\n"
    )
    _install(data_root, homes)
    assert _manifest(homes)["skills"]["dcr"] != before


def test_excluded_commands_produce_no_skill(data_root, homes):
    """Claude-only commands (swarm etc.) yield neither a prompt nor a skill dir."""
    for name in ("swarm.md", "goal-maker.md", "browser-reset.md", "jacked-setup.md"):
        _add_command(data_root, name)
    _install(data_root, homes)
    manifest = _manifest(homes)
    for stem in ("swarm", "goal-maker", "browser-reset", "jacked-setup"):
        assert not _skill_dir(homes, stem).exists()
        assert stem not in manifest["skills"]
    assert (_skill_dir(homes, "dcr") / "SKILL.md").exists()  # ordinary one lands
    assert "dcr" in manifest["skills"]


def test_prune_removes_command_skill_and_prompt(data_root, homes):
    """Deleting a command from the data root prunes BOTH its prompt and its
    command-derived skill, and reports both in summary.removed."""
    _add_command(data_root, "foo.md")
    _install(data_root, homes)
    prompts_dst = ins.codex_prompts_dir(homes["home"])
    assert (_skill_dir(homes, "foo") / "SKILL.md").exists()
    assert (prompts_dst / "foo.md").exists()
    (data_root / "commands" / "foo.md").unlink()
    summ = _install(data_root, homes)
    assert "prompts/foo.md" in summ.removed
    assert "skills/foo" in summ.removed
    assert not _skill_dir(homes, "foo").exists()
    assert not (prompts_dst / "foo.md").exists()


def test_uninstall_removes_command_derived_skills(data_root, homes):
    """Uninstall (manifest-driven) removes command-derived skills too."""
    _install(data_root, homes)
    assert (_skill_dir(homes, "dcr") / "SKILL.md").exists()
    out = ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    assert not _skill_dir(homes, "dcr").exists()
    assert "skills/dcr" in out["removed"]


def test_ds_store_does_not_flip_ownership_or_spawn_backups(data_root, homes):
    """A Finder .DS_Store inside jacked's OWN dir must not read as a user
    modification: reinstall would otherwise back the dir up on every run
    (unbounded junk) while claiming it "preserved your existing skill"."""
    _install(data_root, homes)
    # Both flavors: a command-derived skill (written with src_dir=None, so the
    # source-subset fallback can't rescue it) and an ordinary source skill.
    for name in ("dcr", "demo-skill"):
        (_skill_dir(homes, name) / ".DS_Store").write_bytes(b"\x00junk")
    summ = _install(data_root, homes)
    assert summ.preserved == []
    assert not (homes["agents_home"] / "jacked-backups").exists()


def test_uninstall_removes_owned_dir_despite_ds_store(data_root, homes):
    """Same tolerance on the delete gate: a dropping is not a user edit, so
    uninstall still removes jacked's dir instead of leaving junk behind."""
    _install(data_root, homes)
    (_skill_dir(homes, "dcr") / ".DS_Store").write_bytes(b"\x00junk")
    out = ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    assert not _skill_dir(homes, "dcr").exists()
    assert "skills/dcr" in out["removed"]


def test_real_commands_generate_parseable_skill_frontmatter():
    """Integration guard against the REAL data/commands: every non-excluded
    command's _command_skill_md yields frontmatter yaml.safe_load parses with a
    name matching the stem and a non-empty description."""
    import jacked

    cmd_dir = Path(jacked.__file__).parent / "data" / "commands"
    cmds = sorted(cmd_dir.glob("*.md"))
    assert cmds, "real jacked data/commands must be present"
    checked = 0
    for cmd in cmds:
        if cmd.name in ins._CLAUDE_ONLY_COMMANDS:
            continue
        meta = _frontmatter(ins._command_skill_md(cmd))
        assert meta.get("name") == cmd.stem, cmd.name
        assert isinstance(meta.get("description"), str) and meta["description"].strip(), \
            cmd.name
        checked += 1
    assert checked  # sanity: we actually exercised real commands


# --------------------------------------------------------------------------
# Agents -> ~/.codex/agents/<stem>.toml (Codex custom-agent TOMLs). jacked ships
# Claude Code subagent definitions (data/agents/*.md); Codex reads TOML with
# name/description/developer_instructions and NO model pin (Codex picks its own).
# --------------------------------------------------------------------------

# A body that stresses TOML escaping: a double quote, a literal backslash, and a
# triple-backtick fence must all round-trip verbatim through json.dumps quoting.
_AGENT_BODY = (
    'You review "code" with care.\n'
    "A Windows path C:\\\\Users and a regex \\d+ stay intact.\n"
    "```python\nprint('fence')\n```\n"
)


def test_agents_ship_as_codex_tomls(data_root, homes):
    """Two synthetic agents -> two TOMLs under the codex home's agents dir; each
    parses with tomllib and carries non-empty name/description/developer_instructions,
    name == stem, and developer_instructions == the markdown body verbatim (quotes,
    backslashes, and a code fence all survive the escaping)."""
    _add_agent(data_root, "reviewer", "reviews code", _AGENT_BODY)
    _add_agent(data_root, "planner", "plans work", "Plan it.\n")
    summ = _install(data_root, homes)
    agents_dir = ins.codex_agents_dir(homes["home"])
    for stem, desc, body in (
        ("reviewer", "reviews code", _AGENT_BODY),
        ("planner", "plans work", "Plan it.\n"),
    ):
        toml_path = agents_dir / f"{stem}.toml"
        assert toml_path.exists()
        data = tomllib.loads(toml_path.read_text())
        assert data["name"] == stem
        assert isinstance(data["description"], str) and data["description"].strip()
        assert data["description"] == desc
        assert data["developer_instructions"] == body  # verbatim, not truncated
        assert stem in summ.agents


def test_agent_toml_omits_tools_and_model(data_root, homes):
    """The generated TOML carries ONLY name/description/developer_instructions:
    the Claude-only `tools:` / `model:` frontmatter keys are never pinned (Codex
    picks its own model)."""
    _add_agent(data_root, "reviewer", "reviews code", "Do the review.\n")
    _install(data_root, homes)
    toml_path = ins.codex_agents_dir(homes["home"]) / "reviewer.toml"
    data = tomllib.loads(toml_path.read_text())
    assert set(data) == {"name", "description", "developer_instructions"}
    assert "tools" not in data and "model" not in data
    assert "model" not in toml_path.read_text()  # no model pin anywhere in the file


def test_agent_toml_description_fallback_to_first_body_line(data_root, homes):
    """An agent with no `description` frontmatter falls back to the first non-empty
    body line."""
    (data_root / "agents").mkdir(parents=True, exist_ok=True)
    (data_root / "agents" / "nodesc.md").write_text(
        "---\nname: nodesc\n---\n\nFirst real line.\nSecond line.\n"
    )
    _install(data_root, homes)
    data = tomllib.loads(
        (ins.codex_agents_dir(homes["home"]) / "nodesc.toml").read_text()
    )
    assert data["description"] == "First real line."


def test_agents_recorded_in_manifest_and_summary(data_root, homes):
    """The manifest gains an `agents` dict listing the shipped agents, and the
    summary's `agents` list matches."""
    _add_agent(data_root, "reviewer", "reviews code", "Review.\n")
    _add_agent(data_root, "planner", "plans work", "Plan.\n")
    summ = _install(data_root, homes)
    manifest = _manifest(homes)
    assert set(manifest["agents"]) == {"reviewer", "planner"}
    assert set(summ.agents) == {"reviewer", "planner"}


def test_agents_prune_on_source_removal(data_root, homes):
    """An agent shipped before but whose source is deleted has its TOML removed,
    is reported in summary.removed as `agents/<name>`, and is gone from the manifest."""
    _add_agent(data_root, "reviewer", "reviews code", "Review.\n")
    _install(data_root, homes)
    agents_dir = ins.codex_agents_dir(homes["home"])
    assert (agents_dir / "reviewer.toml").exists()
    (data_root / "agents" / "reviewer.md").unlink()
    summ = _install(data_root, homes)
    assert "agents/reviewer" in summ.removed
    assert not (agents_dir / "reviewer.toml").exists()
    assert "reviewer" not in _manifest(homes)["agents"]


def test_uninstall_removes_agent_tomls(data_root, homes):
    """Uninstall (manifest-driven) removes the agent TOMLs it installed."""
    _add_agent(data_root, "reviewer", "reviews code", "Review.\n")
    _install(data_root, homes)
    agents_dir = ins.codex_agents_dir(homes["home"])
    assert (agents_dir / "reviewer.toml").exists()
    out = ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    assert not (agents_dir / "reviewer.toml").exists()
    assert "agents/reviewer" in out["removed"]


def test_agents_idempotent_no_changes_second_run(data_root, homes):
    """Agents are folded into the changed computation: a second unchanged install
    still reports changed=False."""
    _add_agent(data_root, "reviewer", "reviews code", _AGENT_BODY)
    first = _install(data_root, homes)
    assert first.changed is True
    second = _install(data_root, homes)
    assert second.changed is False


def test_real_agents_generate_parseable_tomls():
    """Integration guard against the REAL data/agents: every shipped agent .md
    produces TOML tomllib parses with all 3 required fields non-empty. >= 10 today
    so the assert doesn't rot when agents are added."""
    import jacked

    agents_dir = Path(jacked.__file__).parent / "data" / "agents"
    agents = sorted(agents_dir.glob("*.md"))
    assert len(agents) >= 10, f"expected >= 10 real agents, found {len(agents)}"
    for agent_md in agents:
        data = tomllib.loads(ins._agent_toml(agent_md))
        assert data["name"] == agent_md.stem, agent_md.name
        assert isinstance(data["description"], str) and data["description"].strip(), \
            agent_md.name
        assert isinstance(data["developer_instructions"], str) and \
            data["developer_instructions"].strip(), agent_md.name
        # No stray YAML quote chars left on the value edges (the multi-line
        # quoted-scalar path strips the surrounding pair).
        assert not data["description"].startswith('"'), agent_md.name
        assert not data["description"].endswith('"'), agent_md.name


def test_multiline_quoted_description_parses_fully():
    """double-check-reviewer's description is a YAML double-quoted scalar spanning
    many lines. The line-folding path must return the FULL text (continuation
    lines folded to spaces, quote pair stripped) - a regression here silently
    truncates the description to its first physical line."""
    import jacked

    agent_md = Path(jacked.__file__).parent / "data" / "agents" / "double-check-reviewer.md"
    meta, _ = ins._split_command_frontmatter(agent_md.read_text())
    desc = meta["description"]
    assert len(desc) > 1000, len(desc)          # full scalar, not the first line
    assert not desc.startswith('"') and not desc.endswith('"')
    assert desc.endswith("</example>")           # the scalar's real final text
    # Continuation lines must not have been misparsed as frontmatter keys.
    assert set(meta) <= {"name", "description", "model", "color", "tools", "argument-hint"}, sorted(meta)


def test_prior_manifest_without_agents_key_is_backward_compatible(data_root, homes):
    """A PRIOR manifest lacking the `agents` key loads without crashing and prunes
    no agents (nothing was shipped before)."""
    # Hand-write a legacy manifest with no `agents` key (older jacked shape).
    ins.manifest_path(homes["home"]).parent.mkdir(parents=True, exist_ok=True)
    ins.manifest_path(homes["home"]).write_text(json.dumps({
        "version": "0.9", "written_at": "before",
        "skills": {}, "prompts": {}, "rules": False, "hooks": False,
    }))
    _add_agent(data_root, "reviewer", "reviews code", "Review.\n")
    summ = _install(data_root, homes)  # must not raise
    assert not any(r.startswith("agents/") for r in summ.removed)
    assert "reviewer" in _manifest(homes)["agents"]


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
# M4: Codex rules-body adapter (_codex_rules_body / _CODEX_ADAPTER)
#
# The rules body was authored for Claude Code: it maintains CLAUDE.md and the
# shipped skills speak Claude vocabulary. For Codex the body's CLAUDE.md refs
# are rewritten to AGENTS.md and a runtime-adapter section is appended.
# --------------------------------------------------------------------------

# A crafted rules body carrying the two Claude tokens the rewrite targets: bare
# CLAUDE.md references and the `CLAUDE.md`, `AGENTS.md` filename enumeration the
# real behaviors list (the rename turns that pair into a duplicate to collapse).
# Also a lowercase ~/.claude path, which must survive untouched.
_RULES_WITH_CLAUDE = (
    "# jacked behaviors\n"
    "- Maintain your CLAUDE.md as the rules file; graduate lessons to CLAUDE.md.\n"
    "- Markdown exceptions: Claude-instruction files `CLAUDE.md`, `AGENTS.md`, `lessons.md`.\n"
    "- read ~/.claude/jacked-reference.md for details.\n"
)


def _real_rules_text():
    import jacked

    return (Path(jacked.__file__).parent / "data" / "rules" / "jacked_behaviors.md").read_text()


def _rewritten_source(out):
    """The rewritten source body: everything before the appended adapter section.

    The adapter is appended verbatim and deliberately keeps one CLAUDE.md (its
    "CLAUDE.md means AGENTS.md" mapping line), so the no-CLAUDE.md guarantee is
    about the rewritten SOURCE, not the constant adapter."""
    return out.split(ins._CODEX_ADAPTER)[0]


def test_codex_rules_body_rewrites_claude_md():
    """Every CLAUDE.md in the source body becomes AGENTS.md; the lowercase
    ~/.claude path is untouched (only the uppercase instruction-file token is
    renamed, not lowercase directory paths)."""
    out = ins._codex_rules_body(_RULES_WITH_CLAUDE)
    source = _rewritten_source(out)
    assert "CLAUDE.md" not in source                    # all four renamed
    assert "Maintain your AGENTS.md as the rules file" in source
    assert "~/.claude/jacked-reference.md" in source     # lowercase path preserved


def test_codex_rules_body_collapses_duplicate_enumeration():
    """The `CLAUDE.md`, `AGENTS.md` enumeration collapses to a single `AGENTS.md`
    after the rename, keeping the rest of the line intact (lessons.md still there)."""
    out = ins._codex_rules_body(_RULES_WITH_CLAUDE)
    assert "`AGENTS.md`, `AGENTS.md`" not in out          # backticked dup gone
    assert "AGENTS.md, AGENTS.md" not in out              # bare dup gone too
    assert "`AGENTS.md`, `lessons.md`" in out             # enumeration otherwise intact


def test_codex_rules_body_appends_adapter_verbatim():
    """The adapter is appended verbatim, blank-line separated, as the tail of the
    output; spot-check a few of its exact mapping phrases."""
    out = ins._codex_rules_body(_RULES_WITH_CLAUDE)
    assert out.endswith(ins._CODEX_ADAPTER)               # adapter is the tail
    assert "\n\n" + ins._CODEX_ADAPTER in out             # separated by a blank line
    assert "## Codex runtime adapter" in out
    assert "spawn parallel subagents natively" in out
    assert "ignore Anthropic model names" in out
    assert "use `codex mcp add ...`" in out


def test_codex_rules_body_real_data_guard():
    """Against the REAL jacked_behaviors.md: the rewritten source body carries no
    CLAUDE.md and no duplicate enumeration, the output ends with the adapter, and
    the ONLY CLAUDE.md left is the adapter's intentional mapping line."""
    out = ins._codex_rules_body(_real_rules_text())
    assert "CLAUDE.md" not in _rewritten_source(out)      # source refs all renamed
    assert "`AGENTS.md`, `AGENTS.md`" not in out
    assert "AGENTS.md, AGENTS.md" not in out
    assert out.rstrip("\n").endswith(ins._CODEX_ADAPTER.rstrip("\n"))
    # the adapter deliberately keeps exactly one CLAUDE.md (the mapping line)
    assert out.count("CLAUDE.md") == ins._CODEX_ADAPTER.count("CLAUDE.md") == 1


# --------------------------------------------------------------------------
# M4: adapter lands in the installed AGENTS.md managed block
# --------------------------------------------------------------------------

def _install_with_claude_rules(data_root, homes):
    (data_root / "rules" / "jacked_behaviors.md").write_text(_RULES_WITH_CLAUDE)
    return _install(data_root, homes)


def test_installed_agents_block_contains_adapter(data_root, homes):
    """The installed managed block carries the runtime-adapter section (heading +
    key mapping lines) after the rewritten rules body."""
    _install_with_claude_rules(data_root, homes)
    agents_md = ins.codex_agents_md(homes["home"]).read_text()
    assert ins._AGENTS_BEGIN in agents_md and ins._AGENTS_END in agents_md
    assert "## Codex runtime adapter" in agents_md
    assert "spawn parallel subagents natively" in agents_md
    assert "use `codex mcp add ...`" in agents_md
    assert "Maintain your AGENTS.md as the rules file" in agents_md  # body rewritten


def test_installed_agents_block_no_source_claude_md_leak(data_root, homes):
    """No source-derived CLAUDE.md survives into the installed AGENTS.md and no
    duplicate enumeration artifact remains; the only CLAUDE.md is the adapter's
    single intentional mapping line."""
    _install_with_claude_rules(data_root, homes)
    agents_md = ins.codex_agents_md(homes["home"]).read_text()
    assert "`AGENTS.md`, `AGENTS.md`" not in agents_md
    assert "AGENTS.md, AGENTS.md" not in agents_md
    assert agents_md.count("CLAUDE.md") == ins._CODEX_ADAPTER.count("CLAUDE.md") == 1


def test_installed_adapter_idempotent(data_root, homes):
    """Installing twice leaves exactly one managed block AND one adapter section
    (no growth)."""
    _install_with_claude_rules(data_root, homes)
    _install(data_root, homes)  # second run, rules already CLAUDE-flavored
    agents_md = ins.codex_agents_md(homes["home"]).read_text()
    assert agents_md.count(ins._AGENTS_BEGIN) == 1
    assert agents_md.count(ins._AGENTS_END) == 1
    assert agents_md.count("## Codex runtime adapter") == 1


def test_installed_adapter_preserves_user_content(data_root, homes):
    """Pre-existing user content outside the managed block survives the transform,
    and the adapter section lands alongside it."""
    agents_md = ins.codex_agents_md(homes["home"])
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    agents_md.write_text("# My own rules\nnever delete me\n")
    _install_with_claude_rules(data_root, homes)
    text = agents_md.read_text()
    assert "never delete me" in text               # user content survives
    assert "## Codex runtime adapter" in text        # adapter present


def test_uninstall_strips_adapter(data_root, homes):
    """Uninstall strips the whole managed block, adapter section included, while
    keeping user content."""
    agents_md = ins.codex_agents_md(homes["home"])
    agents_md.parent.mkdir(parents=True, exist_ok=True)
    agents_md.write_text("# Mine\nkeep\n")
    _install_with_claude_rules(data_root, homes)
    assert "## Codex runtime adapter" in agents_md.read_text()
    ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    text = agents_md.read_text()
    assert "## Codex runtime adapter" not in text    # adapter gone
    assert ins._AGENTS_BEGIN not in text
    assert "keep" in text                            # user content kept


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
# QA-suggest Stop hook in ~/.codex/hooks.json (Codex parity for /qa -> $qa)
#
# jacked installs a QA-suggestion Stop hook into Claude Code; the Codex pass
# installs the SAME runtime-portable hook with `--runtime codex` so the
# suggestion reads `$qa` (the Codex skill invocation) instead of `/qa`. Our
# entry is marker-identified by `_hook qa_suggest` in its command; the command
# is built by cli's _build_hook_command (the SAME upgrade-safe shim / -m fallback
# the Claude side writes). Install prunes ONLY the legacy gatekeeper (never its
# own just-written qa entry); uninstall strips both.
# --------------------------------------------------------------------------

def _hooks(homes):
    """Parse ~/.codex/hooks.json (the whole object)."""
    return json.loads(ins.codex_hooks_json(homes["home"]).read_text())


def _qa_groups(homes):
    """Every Stop group whose command carries the jacked qa_suggest marker."""
    stop = _hooks(homes).get("hooks", {}).get("Stop", [])
    return [
        g for g in stop
        if any("_hook qa_suggest" in h.get("command", "") for h in g.get("hooks", []))
    ]


def test_qa_hook_entry_created_with_exact_schema(data_root, homes):
    """A single Stop entry is written with the exact jacked schema, and its
    command reuses the shim form + the `--runtime codex` flag."""
    _install(data_root, homes)
    groups = _qa_groups(homes)
    assert len(groups) == 1
    group = groups[0]
    assert set(group) == {"matcher", "hooks"}
    assert group["matcher"] == ""
    assert len(group["hooks"]) == 1
    hook = group["hooks"][0]
    assert set(hook) == {"type", "command"}
    assert hook["type"] == "command"
    # command is the upgrade-safe shim/-m form + the codex runtime flag
    assert hook["command"].endswith(" _hook qa_suggest --runtime codex")


def test_qa_hook_command_uses_build_hook_command_shim(data_root, homes):
    """The command matches cli._build_hook_command('qa_suggest') + the runtime
    flag (no duplicated find_bin fallback logic)."""
    from jacked.cli import _build_hook_command

    _install(data_root, homes)
    expected = _build_hook_command("qa_suggest") + " --runtime codex"
    assert _qa_groups(homes)[0]["hooks"][0]["command"] == expected


def test_qa_hook_idempotent_single_entry_and_unchanged(data_root, homes):
    """Two installs leave EXACTLY one qa entry and the second run reports
    changed=False when nothing else changed."""
    first = _install(data_root, homes)
    assert first.changed is True
    assert first.hooks is True
    second = _install(data_root, homes)
    assert len(_qa_groups(homes)) == 1
    assert second.changed is False
    assert second.hooks is True


def test_qa_hook_manifest_records_hooks_true(data_root, homes):
    """The Codex manifest flips hooks -> True once the qa hook is installed."""
    _install(data_root, homes)
    assert _manifest(homes)["hooks"] is True


def test_qa_hook_preserves_user_entries_and_unknown_keys(data_root, homes):
    """Installing the qa hook preserves the user's own hook groups AND any
    unknown top-level keys in hooks.json; it only appends its own Stop group."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({
        "version": 2,                       # unknown top-level key must survive
        "hooks": {
            "PostToolUse": [
                {"matcher": "Bash",
                 "hooks": [{"type": "command", "command": "./mine.sh"}]}
            ],
            "Stop": [
                {"matcher": "",
                 "hooks": [{"type": "command", "command": "./user-stop.sh"}]}
            ],
        },
    }))
    _install(data_root, homes)
    data = _hooks(homes)
    assert data["version"] == 2             # unknown key preserved
    # user's own PostToolUse + Stop entries untouched
    assert any(h.get("command") == "./mine.sh"
               for g in data["hooks"]["PostToolUse"] for h in g["hooks"])
    assert any(h.get("command") == "./user-stop.sh"
               for g in data["hooks"]["Stop"] for h in g["hooks"])
    # exactly one jacked qa entry was appended alongside the user's Stop entry
    assert len(_qa_groups(homes)) == 1
    assert len(data["hooks"]["Stop"]) == 2


def test_qa_hook_replaced_in_place_when_command_drifts(data_root, homes):
    """A stale jacked qa entry (old command) is replaced in place, not
    duplicated; user entries in the same event are left intact."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"hooks": {"Stop": [
        {"matcher": "", "hooks": [{"type": "command", "command": "./user-stop.sh"}]},
        {"matcher": "", "hooks": [{"type": "command",
                                   "command": "/old/path/jacked _hook qa_suggest"}]},
    ]}}))
    _install(data_root, homes)
    groups = _qa_groups(homes)
    assert len(groups) == 1                                   # replaced, not duped
    assert groups[0]["hooks"][0]["command"].endswith(" --runtime codex")
    data = _hooks(homes)
    assert any(h.get("command") == "./user-stop.sh"
               for g in data["hooks"]["Stop"] for h in g["hooks"])  # user kept


def test_install_time_prune_does_not_remove_qa_entry(data_root, homes):
    """install_codex prunes the LEGACY gatekeeper before installing the qa hook;
    the prune must use gatekeeper-only markers so it never clobbers the qa entry
    on a re-install. After two installs a legacy entry is gone but qa survives."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(json.dumps({"hooks": {"PreToolUse": [
        {"matcher": "", "hooks": [{"type": "command",
                                   "command": "jacked _hook security_gatekeeper"}]}
    ]}}))
    _install(data_root, homes)
    _install(data_root, homes)               # second pass runs prune-then-install
    assert len(_qa_groups(homes)) == 1       # qa entry never pruned by itself
    assert "security_gatekeeper" not in ins.codex_hooks_json(homes["home"]).read_text()


def test_qa_hook_newly_added_flagged_only_when_new(data_root, homes):
    """summary.hooks_added is True the first install (drives the /hooks trust
    notice) and False on an unchanged re-install."""
    first = _install(data_root, homes)
    assert first.hooks_added is True
    second = _install(data_root, homes)
    assert second.hooks_added is False


def test_uninstall_strips_qa_and_legacy_but_not_user_entries(data_root, homes):
    """Uninstall strips jacked's qa entry AND any legacy gatekeeper entry, but
    never a user's own hook; it reports 'hooks.json qa_suggest' in removed."""
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
    _install(data_root, homes)               # installs qa, prunes gatekeeper
    assert len(_qa_groups(homes)) == 1
    out = ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    assert "hooks.json qa_suggest" in out["removed"]
    data = json.loads(hp.read_text())
    assert "qa_suggest" not in json.dumps(data)          # ours stripped
    assert "security_gatekeeper" not in json.dumps(data)  # legacy stripped
    assert "./mine.sh" in json.dumps(data)                # user hook survives


def test_prior_manifest_without_hooks_key_is_backward_compatible(data_root, homes):
    """A PRIOR manifest lacking the 'hooks' key loads without crashing; the fresh
    run installs the qa hook and records hooks=True."""
    ins.manifest_path(homes["home"]).parent.mkdir(parents=True, exist_ok=True)
    ins.manifest_path(homes["home"]).write_text(json.dumps({
        "version": "0.9", "written_at": "before",
        "skills": {}, "prompts": {}, "agents": {}, "rules": False,
    }))
    summ = _install(data_root, homes)        # must not raise
    assert summ.hooks is True
    assert _manifest(homes)["hooks"] is True
    assert len(_qa_groups(homes)) == 1


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


# --------------------------------------------------------------------------
# M6: chrome-devtools MCP registration in ~/.codex/config.toml. jacked
# auto-installs a `chrome-devtools` MCP server for Claude Code (npx
# chrome-devtools-mcp@latest --autoConnect); the Codex pass mirrors that server
# via a marker-wrapped `[mcp_servers.chrome-devtools]` TOML append so Codex
# skills referencing mcp__chrome-devtools__* resolve. Never fights a user's own
# entry; never leaves a broken config.
# --------------------------------------------------------------------------

def _cfg(homes):
    return ins.codex_config_toml(homes["home"])


def _mkhome(homes):
    homes["home"].mkdir(parents=True, exist_ok=True)
    return homes["home"]


def test_mcp_fresh_config_created(homes):
    """No config.toml -> jacked creates one holding only our marked block; it
    parses, mcp_servers.chrome-devtools carries the SAME npx args as the Claude
    side, and the status is 'added'."""
    home = _mkhome(homes)
    status = ins.ensure_chrome_devtools_mcp(home)
    assert status == "added"
    cfg = _cfg(homes)
    text = cfg.read_text()
    assert ins._MCP_BEGIN in text and ins._MCP_END in text
    data = tomllib.loads(text)
    srv = data["mcp_servers"]["chrome-devtools"]
    assert srv["command"] == "npx"
    assert srv["args"] == ["chrome-devtools-mcp@latest", "--autoConnect"]


def test_mcp_appends_preserving_user_config(homes):
    """An existing user config (a [mcp_servers.other] table + arbitrary top-level
    keys) keeps its bytes as an exact prefix; our block is appended, the combined
    file parses, and the status is 'added'."""
    home = _mkhome(homes)
    cfg = _cfg(homes)
    user = (
        'model = "gpt-5"\n'
        'approval_policy = "on-request"\n'
        "\n"
        "[mcp_servers.other]\n"
        'command = "other-server"\n'
        'args = ["--flag"]\n'
    )
    cfg.write_bytes(user.encode())
    status = ins.ensure_chrome_devtools_mcp(home)
    assert status == "added"
    new_bytes = cfg.read_bytes()
    assert new_bytes.startswith(user.encode())  # user content byte-for-byte prefix
    data = tomllib.loads(cfg.read_text())        # combined file parses
    assert data["model"] == "gpt-5"
    assert "other" in data["mcp_servers"]
    assert data["mcp_servers"]["chrome-devtools"]["command"] == "npx"


def test_mcp_preexisting_unmarked_entry_untouched(homes):
    """A user's OWN (unmarked) chrome-devtools entry is never fought: the file is
    byte-untouched and the status is 'preexisting'."""
    home = _mkhome(homes)
    cfg = _cfg(homes)
    user = (
        "[mcp_servers.chrome-devtools]\n"
        'command = "npx"\n'
        'args = ["chrome-devtools-mcp", "--browserUrl", "http://localhost:9222"]\n'
    )
    cfg.write_bytes(user.encode())
    before = cfg.read_bytes()
    status = ins.ensure_chrome_devtools_mcp(home)
    assert status == "preexisting"
    assert cfg.read_bytes() == before          # byte-untouched
    assert ins._MCP_BEGIN not in cfg.read_text()  # we never added our block


def test_mcp_second_run_unchanged_single_block(homes):
    """Two runs leave EXACTLY one marked block and the second returns 'unchanged'."""
    home = _mkhome(homes)
    assert ins.ensure_chrome_devtools_mcp(home) == "added"
    assert ins.ensure_chrome_devtools_mcp(home) == "unchanged"
    text = _cfg(homes).read_text()
    assert text.count(ins._MCP_BEGIN) == 1
    assert text.count(ins._MCP_END) == 1


def test_mcp_updates_changed_marked_block(homes):
    """A marked block whose body drifted is replaced in place ('updated'), the new
    args land, and user content around the block is preserved."""
    home = _mkhome(homes)
    cfg = _cfg(homes)
    stale = (
        'model = "gpt-5"\n\n'
        + ins._MCP_BEGIN + "\n"
        + "[mcp_servers.chrome-devtools]\n"
        + 'command = "npx"\n'
        + 'args = ["OLD-ARGS"]\n'
        + ins._MCP_END + "\n"
    )
    cfg.write_bytes(stale.encode())
    status = ins.ensure_chrome_devtools_mcp(home)
    assert status == "updated"
    text = cfg.read_text()
    data = tomllib.loads(text)
    assert data["mcp_servers"]["chrome-devtools"]["args"] == [
        "chrome-devtools-mcp@latest", "--autoConnect"
    ]
    assert data["model"] == "gpt-5"            # surrounding user content preserved
    assert text.count(ins._MCP_BEGIN) == 1


def test_mcp_unparseable_config_untouched(homes):
    """A config.toml tomllib can't parse is left byte-untouched; status is
    'skipped-unparseable' (never touch, never break the user's file)."""
    home = _mkhome(homes)
    cfg = _cfg(homes)
    broken = b"this is = = not valid toml [[[\n"
    cfg.write_bytes(broken)
    status = ins.ensure_chrome_devtools_mcp(home)
    assert status == "skipped-unparseable"
    assert cfg.read_bytes() == broken


def test_mcp_uninstall_restores_user_config_byte_identical(homes):
    """Install onto a user config, then uninstall: our marked block is gone, the
    user content is byte-identical, and it's reported in removed."""
    home = _mkhome(homes)
    cfg = _cfg(homes)
    user = 'model = "gpt-5"\n\n[mcp_servers.other]\ncommand = "other"\n'
    cfg.write_bytes(user.encode())
    ins.ensure_chrome_devtools_mcp(home)
    assert ins._MCP_BEGIN in cfg.read_text()   # our block present after install
    out = ins.uninstall_codex(home=home, agents_home=homes["agents_home"])
    assert "config.toml chrome-devtools MCP" in out["removed"]
    assert cfg.read_bytes() == user.encode()   # byte-identical restore


def test_mcp_uninstall_never_removes_unmarked_user_entry(homes):
    """Uninstall strips ONLY our marked block: a user's own unmarked chrome-devtools
    entry is never removed and the file stays byte-untouched."""
    home = _mkhome(homes)
    cfg = _cfg(homes)
    user = '[mcp_servers.chrome-devtools]\ncommand = "npx"\nargs = ["custom"]\n'
    cfg.write_bytes(user.encode())
    assert ins.ensure_chrome_devtools_mcp(home) == "preexisting"
    out = ins.uninstall_codex(home=home, agents_home=homes["agents_home"])
    assert cfg.read_bytes() == user.encode()   # untouched
    assert "config.toml chrome-devtools MCP" not in out["removed"]


def test_mcp_recorded_in_manifest_and_summary(data_root, homes):
    """install_codex records the MCP status in the manifest under 'mcp' and on the
    summary; the first run is 'added' and folds into changed."""
    first = _install(data_root, homes)
    assert first.mcp == "added"
    assert first.changed is True
    assert _manifest(homes)["mcp"] == "added"
    second = _install(data_root, homes)
    assert second.mcp == "unchanged"
    assert second.changed is False             # unchanged MCP doesn't force changed


def test_prior_manifest_without_mcp_key_is_backward_compatible(data_root, homes):
    """A PRIOR manifest lacking the 'mcp' key loads without crashing; the fresh run
    registers the MCP and records the new status."""
    ins.manifest_path(homes["home"]).parent.mkdir(parents=True, exist_ok=True)
    ins.manifest_path(homes["home"]).write_text(json.dumps({
        "version": "0.9", "written_at": "before",
        "skills": {}, "prompts": {}, "agents": {}, "rules": False, "hooks": False,
    }))
    summ = _install(data_root, homes)          # must not raise
    assert summ.mcp == "added"                 # config.toml is fresh this run
    assert _manifest(homes)["mcp"] == "added"


# ---------------------------------------------------------------------------
# Vendored clone-website skill (asserts on the REAL bundled data/, not the
# synthetic data_root fixture). Vendored from JCodesMore/ai-website-cloner-
# template (MIT) with two jacked adaptations; ships to Codex like any skill.
# ---------------------------------------------------------------------------

_CLONE_WEBSITE_DIR = (
    Path(__file__).resolve().parents[2]
    / "jacked" / "data" / "skills" / "clone-website"
)


def test_clone_website_skill_bundled_with_license_parses_and_ships_to_codex():
    """The vendored clone-website skill is a well-formed bundled skill: SKILL.md
    plus a LICENSE sidecar (MIT vendoring), parseable YAML frontmatter naming the
    skill with an em-dash-free description, both jacked adaptations present in the
    body, and NOT held back from Codex."""
    skill_md = _CLONE_WEBSITE_DIR / "SKILL.md"
    assert skill_md.exists(), "jacked/data/skills/clone-website/SKILL.md must exist"
    assert (_CLONE_WEBSITE_DIR / "LICENSE").exists(), (
        "vendored skill must ship its MIT LICENSE sidecar"
    )

    text = skill_md.read_text(encoding="utf-8")
    meta = _frontmatter(text)  # raises if the frontmatter is not valid YAML
    assert meta["name"] == "clone-website"
    desc = meta["description"]
    assert desc and desc.strip(), "description must be non-empty"
    assert "—" not in desc, "description must not contain an em-dash (U+2014)"

    body = _body_after_frontmatter(text)
    assert "Authorization gate" in body        # jacked Pre-Flight 0 addition
    assert "bootstrap the base project" in body  # jacked Pre-Flight 3 addition

    # Ships to Codex, unlike the Claude-only skills (chain-of-command, recover).
    assert "clone-website" not in ins._CLAUDE_ONLY_SKILLS


# ---------------------------------------------------------------------------
# Hardening pass (recursive-review findings)
# ---------------------------------------------------------------------------

# FIX 1: never destroy a user's own ~/.agents/skills/<name> on a name collision.

def _backups_of(skills_base: Path, name: str) -> list:
    """Preserved copies of `name`, which live OUTSIDE the live skills tree."""
    root = skills_base.parent / "jacked-backups" / "skills"
    return sorted(root.glob(f"{name}-*")) if root.is_dir() else []


def test_user_owned_skill_dir_preserved_in_backups_dir(data_root, homes):
    """A user owns dirs whose names collide with a jacked skill (demo-skill) and a
    command-derived skill (dcr). Neither is jacked-owned yet (no prior manifest),
    so install must back each up under ~/.agents/jacked-backups/skills (user copy
    intact, NOT left beside the live skills where it would load as a duplicate),
    record it in summary.preserved, and install its own copy. A SECOND install
    must NOT re-backup the now-jacked-owned dirs."""
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    for name in ("demo-skill", "dcr"):
        d = skills_base / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: MY OWN {name}\n---\nmine\n"
        )
        (d / "my-notes.txt").write_text("do not delete\n")

    summ = _install(data_root, homes)

    for name in ("demo-skill", "dcr"):
        backups = _backups_of(skills_base, name)
        assert len(backups) == 1, f"{name} user copy must be preserved"
        backup = backups[0]
        assert (backup / "my-notes.txt").read_text() == "do not delete\n"
        assert "MY OWN" in (backup / "SKILL.md").read_text()
        assert f"skills/{name}" in summ.preserved
        # No stray duplicate left in the live skills tree.
        assert not list(skills_base.glob(f"{name}.pre-jacked*"))
    # jacked's own content now occupies the real dirs (user content is gone from them)
    assert "MY OWN" not in (skills_base / "demo-skill" / "SKILL.md").read_text()
    assert (skills_base / "demo-skill" / "measure.js").exists()  # jacked sidecar landed
    assert _body_after_frontmatter(
        (skills_base / "dcr" / "SKILL.md").read_text()
    ) == "run dcr\n"

    # Second install: dirs are now jacked-owned -> no re-backup; backups untouched.
    summ2 = _install(data_root, homes)
    assert summ2.preserved == []
    for name in ("demo-skill", "dcr"):
        backups = _backups_of(skills_base, name)
        assert len(backups) == 1
        assert (backups[0] / "my-notes.txt").read_text() == "do not delete\n"


def test_user_dir_created_after_an_install_is_still_preserved(data_root, homes):
    """The name is in the prior manifest, but the CONTENT is the user's: they
    created their own dir under a name jacked already shipped. Ownership is
    decided by content, so the second install preserves it instead of
    overwriting it."""
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    _install(data_root, homes)                       # records demo-skill + dcr
    for name in ("demo-skill", "dcr"):
        shutil.rmtree(skills_base / name)
        d = skills_base / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: MINE\n---\nmine\n")
        (d / "my-notes.txt").write_text("do not delete\n")

    summ = _install(data_root, homes)

    for name in ("demo-skill", "dcr"):
        backups = _backups_of(skills_base, name)
        assert len(backups) == 1, f"{name} must be preserved, not overwritten"
        assert (backups[0] / "my-notes.txt").read_text() == "do not delete\n"
        assert f"skills/{name}" in summ.preserved
        assert "MINE" not in (skills_base / name / "SKILL.md").read_text()


def test_jacked_owned_dir_is_overwritten_in_place(data_root, homes):
    """The counterpart: an untouched jacked dir, and a jacked dir whose SOURCE
    moved (new content + a new sidecar), are both refreshed in place with no
    backup."""
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    _install(data_root, homes)

    src = data_root / "skills" / "demo-skill"
    (src / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: a demo skill\n---\nbody v2\n"
    )
    (src / "extra.js").write_text("// added upstream\n")

    summ = _install(data_root, homes)

    assert summ.preserved == []
    assert _backups_of(skills_base, "demo-skill") == []
    assert "body v2" in (skills_base / "demo-skill" / "SKILL.md").read_text()
    assert (skills_base / "demo-skill" / "extra.js").exists()


def test_install_skips_a_failing_skill_and_continues(data_root, homes, monkeypatch):
    """One unwritable skill must not abort the whole Codex pass."""
    real = ins._copy_tree

    def boom(src, dst):
        if src.name == "demo-skill":
            raise OSError(13, "Permission denied")
        return real(src, dst)

    monkeypatch.setattr(ins, "_copy_tree", boom)
    summ = _install(data_root, homes)

    assert "demo-skill" not in summ.skills     # skipped, and not recorded
    assert "dcr" in summ.skills                # the command-derived skill landed
    assert summ.rules and summ.prompts         # the rest of the pass completed


def test_wrapper_skill_overwritten_by_command_not_self_preserved(data_root, homes):
    """Regression: the real data ships BOTH a pointer-wrapper skill and a same-name
    command (e.g. skills/dcr + commands/dcr.md). Step 1 writes the wrapper, step 2
    overwrites it with command content IN THE SAME RUN. jacked's own step-1 output
    must NOT be mistaken for user content and backed up just because the (empty)
    prior manifest hasn't recorded it yet."""
    # Add a wrapper skill whose name collides with the fixture's dcr.md command.
    wrapper = data_root / "skills" / "dcr"
    wrapper.mkdir(parents=True)
    (wrapper / "SKILL.md").write_text(
        "---\nname: dcr\ndescription: pointer wrapper\n---\nread the command\n"
    )
    skills_base = ins.agents_skills_dir(homes["agents_home"])

    summ = _install(data_root, homes)  # fresh: no prior manifest, no user dirs

    # No spurious self-backup of jacked's own wrapper.
    assert _backups_of(skills_base, "dcr") == []
    assert summ.preserved == []
    # Command content won (precedence), wrapper content is gone.
    assert _body_after_frontmatter(
        (skills_base / "dcr" / "SKILL.md").read_text()
    ) == "run dcr\n"


def test_preserve_does_not_clobber_an_earlier_backup(homes):
    """Two preservations of the same name must produce TWO backups. Even within
    the same timestamp second, the second takes the next free suffix, so an
    earlier preserved copy is never silently destroyed."""
    skills_base = ins.agents_skills_dir(homes["agents_home"])

    def _own(text: str) -> Path:
        d = skills_base / "demo-skill"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(text)
        return d

    preserved: list = []
    for body in ("PRECIOUS EARLIER COPY\n", "SECOND USER COPY\n"):
        ins._preserve_user_skill_dir(
            _own(body), "sha256:not-what-is-there", "demo-skill", {}, preserved,
        )

    backups = _backups_of(skills_base, "demo-skill")
    assert len(backups) == 2
    assert sorted(b.joinpath("SKILL.md").read_text() for b in backups) == [
        "PRECIOUS EARLIER COPY\n", "SECOND USER COPY\n",
    ]
    assert preserved == ["skills/demo-skill", "skills/demo-skill"]


def test_install_prune_keeps_user_modified_dropped_skill(data_root, homes):
    """Upgrade path: jacked shipped `demo-skill` before, the user edited it, and the
    new data root no longer ships it. Install-prune must NOT rmtree a dir whose
    content no longer matches the manifest hash (same hash-gate as uninstall) -
    this runs automatically on every upgrade, a higher-exposure path than uninstall."""
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    # First install records demo-skill in the manifest.
    _install(data_root, homes)
    assert (skills_base / "demo-skill" / "SKILL.md").exists()
    # User edits their copy, then jacked stops shipping demo-skill.
    (skills_base / "demo-skill" / "SKILL.md").write_text("USER EDIT\n")
    shutil.rmtree(data_root / "skills" / "demo-skill")

    summ = _install(data_root, homes)

    assert (skills_base / "demo-skill" / "SKILL.md").read_text() == "USER EDIT\n"
    assert "skills/demo-skill" not in summ.removed
    assert "skills/demo-skill" in summ.preserved


@pytest.mark.parametrize("payload", [
    '{"hooks": {"Stop": "a-string"}}',   # Stop not a list
    '{"hooks": {"Stop": 5}}',            # Stop an int
    '{"hooks": {"Stop": {"k": "v"}}}',   # Stop an object
    '{"hooks": "not-an-object"}',        # hooks not an object
])
def test_malformed_hooks_shape_left_untouched(data_root, homes, payload):
    """hooks.json that is valid JSON but whose hooks/Stop value is the wrong TYPE
    must NOT crash the install and must be left byte-identical - the whole Codex
    pass previously aborted (AttributeError/TypeError) on these."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text(payload)

    summ = _install(data_root, homes)  # must not raise

    assert hp.read_text() == payload  # byte-identical
    # The rest of the Codex pass still landed.
    assert summ.skills and summ.rules


def test_bare_string_in_stop_list_preserved_not_crashing(data_root, homes):
    """A Stop LIST that contains a non-dict entry (a bare string) is a valid list,
    so jacked appends its own entry rather than skipping - but must NOT crash on
    the bare entry and must preserve it."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text('{"hooks": {"Stop": ["a-bare-string"]}}')

    _install(data_root, homes)  # must not raise

    data = json.loads(hp.read_text())
    assert "a-bare-string" in data["hooks"]["Stop"]        # user entry survived
    assert any(isinstance(g, dict) and isinstance(g.get("hooks"), list)
               and g["hooks"] and "_hook qa_suggest" in g["hooks"][0]["command"]
               for g in data["hooks"]["Stop"])              # jacked entry added


@pytest.mark.parametrize("group", [
    '{"matcher": "", "hooks": null}',   # inner hooks null (non-iterable)
    '{"matcher": "", "hooks": 5}',      # inner hooks scalar
    '{"matcher": "", "hooks": [{"type": "command", "command": null}]}',  # command null
    '{"matcher": "", "hooks": [{"type": "command", "command": 5}]}',     # command int
])
def test_non_iterable_inner_hooks_does_not_crash(data_root, homes, group):
    """A Stop list holding a proper dict group whose inner "hooks" value is a
    non-list scalar (null/int) must NOT crash the install (it previously raised
    TypeError from _is_jacked_hook_group and aborted the whole Codex pass). The
    malformed group is preserved and jacked appends its own entry."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    hp.write_text('{"hooks": {"Stop": [' + group + ']}}')

    summ = _install(data_root, homes)  # must not raise

    assert summ.skills and summ.rules  # whole pass completed
    stop = json.loads(hp.read_text())["hooks"]["Stop"]
    assert len(stop) == 2  # malformed group preserved + jacked entry appended


def test_byte_identical_user_dir_not_backed_up(data_root, homes):
    """If a colliding dir already holds EXACTLY what jacked would install, there's
    nothing to preserve: no backup is made and nothing is reported."""
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    # Pre-place demo-skill byte-identical to the source skill dir.
    import shutil as _sh
    _sh.copytree(data_root / "skills" / "demo-skill", skills_base / "demo-skill")
    summ = _install(data_root, homes)
    assert "skills/demo-skill" not in summ.preserved
    assert _backups_of(skills_base, "demo-skill") == []


def test_uninstall_leaves_user_modified_skill_dir(data_root, homes):
    """Uninstall only rmtrees a skill dir whose CURRENT hash still matches the
    manifest. A dir the user modified (hash mismatch) is left in place and noted in
    `skipped`; an untouched dir is still removed."""
    _install(data_root, homes)
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    dcr = skills_base / "dcr"
    assert dcr.is_dir()
    (dcr / "SKILL.md").write_text("I rewrote this myself\n")  # hash now differs
    out = ins.uninstall_codex(home=homes["home"], agents_home=homes["agents_home"])
    assert dcr.is_dir()  # left in place, not destroyed
    assert (dcr / "SKILL.md").read_text() == "I rewrote this myself\n"
    assert "skills/dcr" in out["skipped"]
    assert "skills/dcr" not in out["removed"]
    # an untouched skill is removed normally
    assert not (skills_base / "demo-skill").exists()
    assert "skills/demo-skill" in out["removed"]


# FIX 2: hooks.json unparseable / non-dict must skip, never clobber.

def test_hooks_unparseable_left_untouched_and_skipped(data_root, homes, caplog):
    """A hooks.json with a trailing comma (invalid JSON) is left byte-identical;
    the QA hook is not installed and a warning is logged."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    broken = b'{\n  "hooks": {"Stop": []},\n}\n'  # trailing comma -> invalid
    hp.write_bytes(broken)
    with caplog.at_level(logging.WARNING):
        summ = _install(data_root, homes)
    assert hp.read_bytes() == broken  # byte-identical: never clobbered
    assert summ.hooks is False
    assert "_hook qa_suggest" not in hp.read_text()
    assert any("did not parse" in r.getMessage() for r in caplog.records)


def test_hooks_non_dict_root_left_untouched_and_skipped(data_root, homes, caplog):
    """A hooks.json whose JSON root is an array (not an object) is left
    byte-identical; the QA hook is not installed and a warning is logged."""
    hp = ins.codex_hooks_json(homes["home"])
    hp.parent.mkdir(parents=True, exist_ok=True)
    arr = b'["not", "an", "object"]\n'
    hp.write_bytes(arr)
    with caplog.at_level(logging.WARNING):
        summ = _install(data_root, homes)
    assert hp.read_bytes() == arr  # byte-identical
    assert summ.hooks is False
    assert any("non-object root" in r.getMessage() for r in caplog.records)


# FIX 3: every mutable Codex file is written through _atomic_write_text.

def test_mutable_files_written_atomically(data_root, homes, monkeypatch):
    """AGENTS.md, config.toml, hooks.json, and the manifest are all written via the
    atomic writer (torn-write safety)."""
    calls: list[str] = []
    real = ins._atomic_write_text

    def spy(path, text):
        calls.append(Path(path).name)
        return real(path, text)

    # The atomic writer moved to jacked.codex._fsutil and is imported into both
    # the installer facade (manifest write) and _managed (AGENTS.md / config.toml /
    # hooks.json writes); patch both resolution sites so the spy sees every write.
    monkeypatch.setattr(ins, "_atomic_write_text", spy)
    monkeypatch.setattr("jacked.codex._managed._atomic_write_text", spy)
    _install(data_root, homes)
    assert "AGENTS.md" in calls
    assert "config.toml" in calls
    assert "hooks.json" in calls
    assert ins.manifest_path(homes["home"]).name in calls


# FIX 5: emoji-bearing command description stays YAML-valid (ensure_ascii=False).

def test_command_with_emoji_description_yaml_parses(data_root, homes):
    """A command whose description carries astral-plane emoji yields a SKILL.md
    whose frontmatter YAML-parses and round-trips the emoji (lone-surrogate escapes
    would make strict YAML reject it). The name value is quoted too."""
    (data_root / "commands" / "shipit.md").write_text(
        "---\ndescription: \U0001F680 ship it fast \U0001F3AF\n---\ndo the thing\n",
        encoding="utf-8",
    )
    _install(data_root, homes)
    text = (_skill_dir(homes, "shipit") / "SKILL.md").read_text(encoding="utf-8")
    assert 'name: "shipit"' in text  # name is now quoted for symmetry
    meta = _frontmatter(text)  # raises if the YAML is invalid
    assert meta["name"] == "shipit"
    assert meta["description"] == "\U0001F680 ship it fast \U0001F3AF"


# FIX 6: a generated agent TOML that won't parse is skipped, never written.

def test_agent_toml_with_control_char_skipped(data_root, homes, caplog):
    """An agent body carrying U+007F (DEL) - which json emits literally but TOML
    basic strings forbid - produces unparseable TOML, so it is skipped with a
    warning and no file is written; a clean sibling agent still lands."""
    (data_root / "agents").mkdir(parents=True, exist_ok=True)
    (data_root / "agents" / "bad.md").write_text(
        "---\nname: bad\ndescription: has a control char\n---\nbody with \x7f del\n",
        encoding="utf-8",
    )
    _add_agent(data_root, "good", "fine agent", "clean body\n")
    with caplog.at_level(logging.WARNING):
        summ = _install(data_root, homes)
    agents_dir = ins.codex_agents_dir(homes["home"])
    assert not (agents_dir / "bad.toml").exists()  # unparseable -> never written
    assert "bad" not in summ.agents
    assert "bad" not in _manifest(homes)["agents"]
    assert (agents_dir / "good.toml").exists()  # clean one still lands
    assert "good" in summ.agents
    assert any("bad.md" in r.getMessage() for r in caplog.records)


# FIX 8a: marker matching is whole-line; user prose that embeds the marker is safe.

def test_agents_marker_spoof_in_user_prose_preserved(data_root, homes):
    """A line of user prose that MENTIONS the BEGIN marker as a substring (not as
    its own line) must not be mistaken for a managed block. A re-install replaces
    only the real block and keeps the spoof line intact."""
    _install(data_root, homes)
    agents_md = ins.codex_agents_md(homes["home"])
    real = agents_md.read_text(encoding="utf-8")
    spoof = f"User note: my docs mention {ins._AGENTS_BEGIN} inline, keep it.\n"
    agents_md.write_text(spoof + real, encoding="utf-8")
    _install(data_root, homes)  # re-install -> whole-line matching
    new = agents_md.read_text(encoding="utf-8")
    assert "User note: my docs mention" in new  # spoof line preserved
    assert ins._marker_line_count(new, ins._AGENTS_BEGIN) == 1  # only the real block
    assert "be blunt" in new  # the managed block is still present/refreshed


def test_agents_marker_spoof_strip_leaves_user_bytes(data_root, homes):
    """Stripping the managed block with an embedded-marker prose line present leaves
    the user prose intact and removes only the real block."""
    _install(data_root, homes)
    agents_md = ins.codex_agents_md(homes["home"])
    real = agents_md.read_text(encoding="utf-8")
    assert "be blunt" in real
    spoof = f"User note: docs mention {ins._AGENTS_BEGIN} inline.\n"
    agents_md.write_text(spoof + "\n" + real, encoding="utf-8")
    assert ins._strip_agents_block(agents_md) is True
    left = agents_md.read_text(encoding="utf-8")
    assert "User note: docs mention" in left  # user prose kept
    assert ins._AGENTS_BEGIN in left           # the inline mention survives
    assert "be blunt" not in left              # real managed block stripped


def test_mcp_marker_spoof_strip_leaves_user_bytes(homes):
    """config.toml strip is whole-line anchored: a comment mentioning the MCP BEGIN
    marker as a substring survives; only the real marked TOML table is removed."""
    home = _mkhome(homes)
    cfg = ins.codex_config_toml(homes["home"])
    ins.ensure_chrome_devtools_mcp(home)
    real = cfg.read_text(encoding="utf-8")
    spoof = f"# doc mentions {ins._MCP_BEGIN} inline\n"
    cfg.write_text(spoof + "\n" + real, encoding="utf-8")
    assert ins._strip_mcp_block(cfg) is True
    left = cfg.read_text(encoding="utf-8")
    assert "# doc mentions" in left                       # user comment kept
    assert ins._MCP_BEGIN in left                          # inline mention survives
    assert "[mcp_servers.chrome-devtools]" not in left     # real table removed


def test_mcp_duplicate_marker_lines_skipped(homes):
    """Two whole-line BEGIN markers (an unexpected count) make ensure_* skip rather
    than risk clobbering: the file is left untouched and the status is
    skipped-unparseable."""
    home = _mkhome(homes)
    cfg = ins.codex_config_toml(homes["home"])
    doubled = f"{ins._MCP_BEGIN}\n{ins._MCP_BEGIN}\n{ins._MCP_END}\n"
    cfg.write_bytes(doubled.encode())
    status = ins.ensure_chrome_devtools_mcp(home)
    assert status == "skipped-unparseable"
    assert cfg.read_bytes() == doubled.encode()  # untouched


# FIX 8c: manifest-supplied names that aren't safe path components are rejected.

def test_unsafe_manifest_names_not_honored_on_prune(data_root, homes):
    """A prior manifest carrying a traversal-y skill name (../evil) must never drive
    a delete outside the target dir during the install prune pass."""
    skills_base = ins.agents_skills_dir(homes["agents_home"])
    outsider = skills_base.parent / "evil"
    outsider.mkdir(parents=True)
    (outsider / "keep.txt").write_text("must survive\n")
    ins._write_manifest(
        homes["home"], "0.9",
        {"../evil": "sha256:whatever"}, {}, {}, False, False, "before",
    )
    _install(data_root, homes)  # prune iterates prior skills incl. the bad name
    assert (outsider / "keep.txt").read_text() == "must survive\n"  # untouched


# FIX 8 LOW test gaps

def test_command_skill_no_frontmatter_description_fallback(data_root, homes):
    """A command with no frontmatter at all: description falls back to the first
    non-empty body line and the body passes through verbatim."""
    (data_root / "commands" / "plain.md").write_text(
        "Just a plain command body.\nMore lines.\n", encoding="utf-8"
    )
    _install(data_root, homes)
    text = (_skill_dir(homes, "plain") / "SKILL.md").read_text(encoding="utf-8")
    meta = _frontmatter(text)
    assert meta["name"] == "plain"
    assert meta["description"] == "Just a plain command body."
    assert _body_after_frontmatter(text) == "Just a plain command body.\nMore lines.\n"


def test_corrupt_prior_manifest_does_not_crash_and_prune_noops(data_root, homes):
    """A corrupt (unparseable) prior manifest is treated as empty: install does not
    crash, prunes nothing, and writes a fresh valid manifest."""
    mp = ins.manifest_path(homes["home"])
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("{not valid json,,", encoding="utf-8")
    summ = _install(data_root, homes)  # must not raise
    assert summ.removed == []          # nothing to prune from a corrupt prior
    assert "demo-skill" in summ.skills  # normal install still happens
    assert _manifest(homes)["skills"]   # fresh manifest written


def test_empty_data_root_installs_nothing_cleanly(homes, tmp_path):
    """An empty data root yields no skills/prompts/agents and no rules block, with
    an empty (but valid) manifest, and does not raise."""
    empty = tmp_path / "empty-data"
    empty.mkdir()
    summ = ins.install_codex(
        empty, home=homes["home"], agents_home=homes["agents_home"],
        version="1.0", now_iso="now",
    )
    assert summ.skills == [] and summ.prompts == [] and summ.agents == []
    assert summ.rules is False
    manifest = _manifest(homes)
    assert manifest["skills"] == {}
    assert manifest["prompts"] == {}
    assert manifest["agents"] == {}
