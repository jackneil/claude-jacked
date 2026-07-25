"""Install jacked's skills / commands / rules into Codex too.

`jacked install` deploys to ~/.claude for Claude Code; this adds a parallel
Codex pass when Codex is present, writing the native Codex installables:

- skills   -> ~/.agents/skills/<name>/   (FULL dir incl. sidecar files: the
             agentskills.io standard Codex discovers; jacked's SKILL.md already
             carries name+description frontmatter)
- commands -> BOTH ~/.codex/prompts/<name>.md (invoked /prompts:<name> in Codex)
             AND ~/.agents/skills/<stem>/SKILL.md. OpenAI deprecated the
             ~/.codex/prompts surface on 2026-01-22 in favor of skills, so each
             non-excluded command is also emitted as a command-derived skill; the
             prompts copy stays for back-compat during the deprecation window. A
             command-derived skill OVERWRITES any same-name pointer-wrapper skill
             dir from the skills pass (command content wins).
- rules    -> a managed block in ~/.codex/AGENTS.md (CLAUDE.md references
             rewritten for Codex + a Codex runtime-adapter section appended)
- agents   -> ~/.codex/agents/<name>.toml (jacked's Claude Code subagent
             definitions converted to Codex's native TOML custom-agent format:
             name/description/developer_instructions, with NO model pin so Codex
             chooses its own model)
- MCP      -> a marker-wrapped `[mcp_servers.chrome-devtools]` table appended to
             ~/.codex/config.toml (the SAME npx server the Claude side registers),
             so Codex skills referencing `mcp__chrome-devtools__*` resolve. Never
             fights a user's own chrome-devtools entry and never leaves a broken
             config (parse-checked, byte-restored on failure).
- hooks     -> a QA-suggestion Stop entry in ~/.codex/hooks.json invoking the
             SAME runtime-portable qa_suggest.py hook with `--runtime codex`
             (so the suggestion reads `$qa`, the Codex skill invocation, not
             Claude's `/qa`). Marker-identified by `_hook qa_suggest`; replaces
             ours in place if the command drifts and never touches user entries.
             Legacy gatekeeper entries are PRUNED on install (the gatekeeper was
             retired in 0.70.0); install prunes gatekeeper-only so it never
             clobbers the qa entry it just wrote. Codex requires a one-time
             /hooks trust for non-managed command hooks; the installer surfaces
             that step when the entry is newly added.

A separate manifest (~/.codex/jacked-codex-manifest.json) makes install
idempotent and uninstall/prune precise; it never touches the Claude manifest.
"""

from __future__ import annotations

import json
import logging
import shutil
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from jacked.install_manifest import skill_content_hash as _skill_content_hash

from .credentials import codex_home
from ._fsutil import (
    _atomic_write_bytes as _atomic_write_bytes,
    _atomic_write_text as _atomic_write_text,
    _copy_tree as _copy_tree,
    _extract_block as _extract_block,
    _is_safe_name as _is_safe_name,
    _marker_line_count as _marker_line_count,
    _sha_dir as _sha_dir,
    _sha_file as _sha_file,
    _sha_solo_skill as _sha_solo_skill,
    _sha_text as _sha_text,
    _write_solo_skill as _write_solo_skill,
    agents_skills_dir as agents_skills_dir,
    codex_agents_dir as codex_agents_dir,
    codex_agents_md as codex_agents_md,
    codex_config_toml as codex_config_toml,
    codex_hooks_json as codex_hooks_json,
    codex_present as codex_present,
    codex_prompts_dir as codex_prompts_dir,
    manifest_path as manifest_path,
)
from ._generate import (
    _CODEX_ADAPTER as _CODEX_ADAPTER,
    _agent_toml as _agent_toml,
    _codex_rules_body as _codex_rules_body,
    _command_skill_md as _command_skill_md,
    _first_nonempty_line as _first_nonempty_line,
    _is_jacked_owned as _is_jacked_owned,
    _preserve_user_skill_dir as _preserve_user_skill_dir,
    _split_command_frontmatter as _split_command_frontmatter,
)
from ._managed import (
    _AGENTS_BEGIN as _AGENTS_BEGIN,
    _AGENTS_END as _AGENTS_END,
    _HOOK_MARKERS as _HOOK_MARKERS,
    _LEGACY_HOOK_MARKERS as _LEGACY_HOOK_MARKERS,
    _MCP_BEGIN as _MCP_BEGIN,
    _MCP_END as _MCP_END,
    _QA_HOOK_MARKER as _QA_HOOK_MARKER,
    _QA_HOOK_MARKERS as _QA_HOOK_MARKERS,
    _codex_qa_hook_command as _codex_qa_hook_command,
    _install_agents_block as _install_agents_block,
    _install_codex_qa_hook as _install_codex_qa_hook,
    _is_jacked_hook_group as _is_jacked_hook_group,
    _mcp_block as _mcp_block,
    _mcp_block_body as _mcp_block_body,
    _remove_codex_hooks as _remove_codex_hooks,
    _strip_agents_block as _strip_agents_block,
    _strip_mcp_block as _strip_mcp_block,
    _write_mcp_verified as _write_mcp_verified,
    ensure_chrome_devtools_mcp as ensure_chrome_devtools_mcp,
)

logger = logging.getLogger(__name__)


# Skills that are Claude-only and must NOT be deployed to Codex. `chain-of-command`
# is a Claude Code model-dispatch policy (Fable plans, Opus codes); Codex has no
# equivalent multi-model dispatch, so shipping it there is dead weight. `recover`'s
# entire purpose is recovering crashed CLAUDE CODE sessions: it reads
# ~/.claude/projects transcripts via `jacked recover` and ends with `claude
# --resume`, so it's useless and misleading inside Codex. Excluded names never
# enter the Codex skills dict, so they're never written to ~/.agents/skills and
# never recorded in the Codex manifest.
_CLAUDE_ONLY_SKILLS = frozenset({"chain-of-command", "recover"})

# Commands that are Claude-only and must NOT be deployed to Codex prompts. Each is
# wired to Claude Code machinery Codex has no analog for:
#   swarm.md         - Claude Code's experimental agent teams (the Task/Agent tool +
#                      SendMessage, gated by CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS in
#                      settings.json).
#   goal-maker.md    - forges briefs for Claude Code's built-in /goal
#                      completion-condition engine.
#   browser-reset.md - diagnoses Claude Code's MCP plumbing (Claude log paths, the
#                      `claude mcp` CLI, plugin MCP servers).
#   jacked-setup.md  - generates a repo-local .claude/commands + .claude/skills
#                      layout that Codex never reads.
# Excluded names never enter the Codex prompts dict, so they're never written to
# ~/.codex/prompts and never recorded in the Codex manifest.
_CLAUDE_ONLY_COMMANDS = frozenset(
    {"swarm.md", "goal-maker.md", "browser-reset.md", "jacked-setup.md"}
)


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _load_manifest(home: Path) -> Optional[dict]:
    p = manifest_path(home)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def _write_manifest(home: Path, version: str, skills: dict, prompts: dict,
                    agents: dict, rules: bool, hooks: bool, now_iso: str,
                    mcp: str = "") -> None:
    _atomic_write_text(manifest_path(home), json.dumps({
        "version": version,
        "written_at": now_iso,
        "skills": skills,
        "prompts": prompts,
        "agents": agents,
        "rules": rules,
        "hooks": hooks,
        "mcp": mcp,
    }, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

@dataclass
class CodexInstallSummary:
    skills: list = field(default_factory=list)
    prompts: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    rules: bool = False
    hooks: bool = False
    hooks_added: bool = False
    mcp: str = ""
    removed: list = field(default_factory=list)
    preserved: list = field(default_factory=list)
    changed: bool = False


def install_codex(
    data_root,
    *,
    home: Optional[Path] = None,
    agents_home: Optional[Path] = None,
    version: str = "0",
    now_iso: str = "",
    env: Optional[Mapping[str, str]] = None,
) -> CodexInstallSummary:
    """Deploy jacked's artifacts into Codex. Idempotent; prunes artifacts that
    jacked previously shipped but no longer does."""
    data_root = Path(data_root)
    home = home or codex_home(env)
    skills_base = agents_skills_dir(agents_home)
    prompts_dst = codex_prompts_dir(home)
    agents_dst = codex_agents_dir(home)

    prior = _load_manifest(home) or {}
    preserved: list = []

    # 1. Skills: full dir copy (sidecars included). Claude-only skills are
    #    skipped so they never land in Codex. This runs FIRST so any pointer-
    #    wrapper skill dir is in place before step 2 overwrites the ones that
    #    have a same-name command (precedence rule: command content wins).
    #    Before overwriting a target, `_preserve_user_skill_dir` moves aside any
    #    NON-jacked dir that collides with a skill name (shared ~/.agents/skills),
    #    so a user's own dir is never silently destroyed.
    skills: dict = {}
    for skill_md in sorted((data_root / "skills").glob("*/SKILL.md")):
        name = skill_md.parent.name
        if name in _CLAUDE_ONLY_SKILLS:
            continue
        expected = _sha_dir(skill_md.parent)
        # One unwritable path must not abort the whole Codex pass.
        try:
            _preserve_user_skill_dir(
                skills_base / name, expected, name, prior, preserved,
                src_dir=skill_md.parent,
            )
            _copy_tree(skill_md.parent, skills_base / name)
        except OSError as e:
            logger.warning("skipping Codex skill %s: %s", name, e)
            continue
        skills[name] = expected

    # 2. Commands -> prompts AND command-derived skills. Claude-only commands are
    #    skipped so they never land in Codex (and the prune loop below deletes any
    #    prior copies). OpenAI deprecated ~/.codex/prompts on 2026-01-22 in favor
    #    of skills, so each non-excluded command is ALSO written as a skill; the
    #    prompts copy stays for back-compat during the deprecation window. The
    #    command-derived skill runs after step 1 and overwrites any same-name
    #    pointer-wrapper dir, leaving only the generated SKILL.md, and is recorded
    #    in the SAME `skills` manifest dict (keyed by stem) so a changed command
    #    changes the hash and a removed command is pruned like any other skill.
    prompts: dict = {}
    if (data_root / "commands").exists():
        prompts_dst.mkdir(parents=True, exist_ok=True)
        for cmd in sorted((data_root / "commands").glob("*.md")):
            if cmd.name in _CLAUDE_ONLY_COMMANDS:
                continue
            skill_dir = skills_base / cmd.stem
            content = _command_skill_md(cmd)
            try:
                shutil.copy(cmd, prompts_dst / cmd.name)
                prompts[cmd.name] = _sha_file(cmd)
                # this_run=skills: a wrapper dir step 1 wrote this run is jacked's
                # own, not user content, so overwriting it must not spawn a backup.
                # No src_dir: this skill's content is GENERATED from the command,
                # so the expected-hash identity check is the only source test.
                _preserve_user_skill_dir(
                    skill_dir, _sha_solo_skill(content), cmd.stem, prior, preserved,
                    this_run=skills,
                )
                _write_solo_skill(skill_dir, content)
            except OSError as e:
                logger.warning("skipping Codex command %s: %s", cmd.name, e)
                continue
            skills[cmd.stem] = _sha_dir(skill_dir)

    # 3. Rules -> AGENTS.md block. The body is authored for Claude Code, so it is
    #    adapted for Codex first (CLAUDE.md refs rewritten to AGENTS.md + a
    #    runtime-adapter section appended); block markers / idempotency unchanged.
    rules_done = False
    rules_src = data_root / "rules" / "jacked_behaviors.md"
    if rules_src.exists():
        _install_agents_block(
            codex_agents_md(home),
            _codex_rules_body(rules_src.read_text(encoding="utf-8")),
        )
        rules_done = True

    # 4. Agents -> ~/.codex/agents/<stem>.toml. jacked's Claude Code subagent
    #    definitions (data/agents/*.md: YAML frontmatter + a markdown-body system
    #    prompt) are converted to Codex's native TOML custom-agent format via
    #    _agent_toml (name/description/developer_instructions, NO model pin - Codex
    #    picks its own). Recorded in the `agents` manifest dict keyed by stem ->
    #    sha of the GENERATED TOML content, so a changed source OR a changed
    #    conversion re-hashes and re-writes (consistent with the file-sha keys
    #    used for skills/prompts, just hashing the produced content).
    agents: dict = {}
    agents_src_dir = data_root / "agents"
    if agents_src_dir.exists():
        agents_dst.mkdir(parents=True, exist_ok=True)
        for agent_md in sorted(agents_src_dir.glob("*.md")):
            content = _agent_toml(agent_md)
            # Parse-check before writing (mirrors the MCP verify): never persist a
            # TOML that Codex can't load. A stray control char in the source body
            # (e.g. U+007F, which json emits literally but TOML basic strings
            # forbid) would otherwise ship a broken agent file.
            try:
                tomllib.loads(content)
            except tomllib.TOMLDecodeError:
                logger.warning(
                    "generated Codex agent TOML for %s did not parse; skipping it",
                    agent_md.name,
                )
                continue
            _atomic_write_text(agents_dst / f"{agent_md.stem}.toml", content)
            agents[agent_md.stem] = _sha_text(content)

    # 5. chrome-devtools MCP -> a marker-wrapped [mcp_servers.chrome-devtools] table
    #    in config.toml, mirroring the Claude side's npx server so Codex skills that
    #    reference mcp__chrome-devtools__* resolve. Never fights a user's own entry
    #    and never leaves a broken config. The returned status ("added"/"updated"/
    #    "unchanged"/"preexisting"/"skipped-unparseable") is recorded in the manifest.
    mcp_status = ensure_chrome_devtools_mcp(home)

    # 6. hooks.json: prune the LEGACY gatekeeper entry (retired 0.70.0) with the
    #    gatekeeper-only markers, then install the QA-suggest Stop hook. Pruning
    #    with the legacy markers ONLY means the just-installed qa entry is never
    #    clobbered by the prune, so install and prune don't fight (uninstall
    #    strips both). hooks_changed folds a real file change from either step
    #    into `changed`; hooks_added (entry absent before, present after) drives
    #    the one-time /hooks trust notice cli.py prints.
    hooks_path = codex_hooks_json(home)
    _hooks_before = (
        hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else None
    )
    _qa_present_before = _hooks_before is not None and _QA_HOOK_MARKER in _hooks_before
    _remove_codex_hooks(hooks_path, markers=_LEGACY_HOOK_MARKERS)
    hooks_done = _install_codex_qa_hook(home)
    _hooks_after = (
        hooks_path.read_text(encoding="utf-8") if hooks_path.exists() else None
    )
    hooks_changed = _hooks_before != _hooks_after
    hooks_added = hooks_done and not _qa_present_before

    # Prune artifacts shipped before but not now. Manifest-supplied names are
    # validated as single safe path components before being joined onto real dirs
    # (a malformed name never drives a delete outside the target dir).
    removed = []
    for name, recorded in (prior.get("skills") or {}).items():
        if name in skills or not _is_safe_name(name):
            continue
        d = skills_base / name
        if not d.is_dir():
            continue
        # Same hash-gate as uninstall: only delete a dir whose content still
        # matches what jacked installed. A user who edited/recreated a now-
        # dropped skill keeps it (upgrade runs this automatically, so it's a
        # higher-exposure path than an explicit uninstall).
        if isinstance(recorded, str) and (
            _sha_dir(d) == recorded or _skill_content_hash(d) == recorded
        ):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(f"skills/{name}")
        else:
            logger.warning(
                "leaving Codex skill dir %s in place: it no longer matches what "
                "jacked installed (you likely modified or recreated it)", d,
            )
            preserved.append(f"skills/{name}")
    for name in (prior.get("prompts") or {}):
        if name not in prompts and _is_safe_name(name):
            f = prompts_dst / name
            if f.exists():
                f.unlink()
                removed.append(f"prompts/{name}")
    for name in (prior.get("agents") or {}):
        if name not in agents and _is_safe_name(name):
            f = agents_dst / f"{name}.toml"
            if f.exists():
                f.unlink()
                removed.append(f"agents/{name}")

    changed = (
        skills != (prior.get("skills") or {})
        or prompts != (prior.get("prompts") or {})
        or agents != (prior.get("agents") or {})
        or mcp_status in {"added", "updated"}
        or hooks_changed
        or bool(removed)
    )

    _write_manifest(home, version, skills, prompts, agents, rules_done, hooks_done,
                    now_iso, mcp_status)
    return CodexInstallSummary(
        skills=list(skills), prompts=list(prompts), agents=list(agents),
        rules=rules_done, hooks=hooks_done, hooks_added=hooks_added, mcp=mcp_status,
        removed=removed, preserved=preserved, changed=changed,
    )


def uninstall_codex(
    *,
    home: Optional[Path] = None,
    agents_home: Optional[Path] = None,
    env: Optional[Mapping[str, str]] = None,
) -> dict:
    """Remove everything jacked installed into Codex (per the manifest)."""
    home = home or codex_home(env)
    skills_base = agents_skills_dir(agents_home)
    prompts_dst = codex_prompts_dir(home)
    agents_dst = codex_agents_dir(home)
    manifest = _load_manifest(home) or {}
    removed: list = []
    skipped: list = []

    # Skills: only rmtree a dir whose CURRENT content still hashes to what the
    # manifest recorded (i.e. jacked wrote it and the user hasn't replaced it). If
    # the user recreated/edited it under the same name, LEAVE it and note it, so
    # uninstall never destroys a dir that is no longer jacked's.
    for name, recorded in (manifest.get("skills") or {}).items():
        if not _is_safe_name(name):
            continue
        d = skills_base / name
        if not d.is_dir():
            continue
        if isinstance(recorded, str) and (
            _sha_dir(d) == recorded or _skill_content_hash(d) == recorded
        ):
            shutil.rmtree(d, ignore_errors=True)
            removed.append(f"skills/{name}")
        else:
            logger.warning(
                "leaving Codex skill dir %s in place: its content no longer matches "
                "what jacked installed (you likely modified or recreated it)", d,
            )
            skipped.append(f"skills/{name}")
    for name in (manifest.get("prompts") or {}):
        if not _is_safe_name(name):
            continue
        f = prompts_dst / name
        if f.exists():
            f.unlink()
            removed.append(f"prompts/{name}")
    for name in (manifest.get("agents") or {}):
        if not _is_safe_name(name):
            continue
        f = agents_dst / f"{name}.toml"
        if f.exists():
            f.unlink()
            removed.append(f"agents/{name}")
    if _strip_agents_block(codex_agents_md(home)):
        removed.append("AGENTS.md block")
    if _strip_mcp_block(codex_config_toml(home)):
        removed.append("config.toml chrome-devtools MCP")
    # Strip BOTH the qa entry and any legacy gatekeeper entry (default markers),
    # never a user's own hooks.
    if _remove_codex_hooks(codex_hooks_json(home)):
        removed.append("hooks.json qa_suggest")

    mp = manifest_path(home)
    if mp.exists():
        mp.unlink()
    return {"removed": removed, "skipped": skipped}
