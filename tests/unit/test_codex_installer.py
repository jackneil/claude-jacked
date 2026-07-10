"""M7: install jacked's artifacts into Codex (+ fix the Claude skill sidecar drop).

Covers install_codex (skills with sidecars -> ~/.agents/skills, commands ->
~/.codex/prompts, rules -> AGENTS.md block), idempotency + prune via its own
manifest, legacy gatekeeper hook pruning that preserves user hooks, uninstall,
and the Claude-side fix that now copies skill sidecar files.
"""

import json
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
    # gatekeeper hooks are no longer installed (retired in 0.70.0)
    assert not ins.codex_hooks_json(homes["home"]).exists() or \
        "security_gatekeeper" not in ins.codex_hooks_json(homes["home"]).read_text()
    # commands now ALSO ship as Codex skills (see below), so the command dcr.md
    # yields a `dcr` skill alongside the ordinary demo-skill.
    assert summ.skills == ["demo-skill", "dcr"] and summ.prompts == ["dcr.md"]
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
        {"recover": "sha256:stale"}, {"swarm.md": "sha256:stale"}, {},
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
