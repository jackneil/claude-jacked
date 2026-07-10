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
- rules    -> a managed block in ~/.codex/AGENTS.md (Codex's CLAUDE.md analog)
- legacy gatekeeper hooks.json entries are PRUNED on install (the
             gatekeeper was retired in 0.70.0)

A separate manifest (~/.codex/jacked-codex-manifest.json) makes install
idempotent and uninstall/prune precise; it never touches the Claude manifest.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .credentials import codex_home

logger = logging.getLogger(__name__)

_AGENTS_BEGIN = "<!-- BEGIN jacked behaviors (managed by `jacked install`) -->"
_AGENTS_END = "<!-- END jacked behaviors (managed by `jacked install`) -->"

# A jacked-managed hook entry is identified by this marker in its command, so
# install/uninstall can find and replace exactly its own entries.
_HOOK_MARKERS = ("_hook security_gatekeeper",)

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
# Detection + paths
# ---------------------------------------------------------------------------

def codex_present(env: Optional[Mapping[str, str]] = None) -> bool:
    """True if Codex looks installed (binary on PATH, or a CODEX_HOME exists)."""
    return shutil.which("codex") is not None or codex_home(env).exists()


def agents_skills_dir(agents_home: Optional[Path] = None) -> Path:
    return (agents_home or Path.home() / ".agents") / "skills"


def codex_prompts_dir(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "prompts"


def codex_agents_md(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "AGENTS.md"


def codex_hooks_json(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "hooks.json"


def manifest_path(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "jacked-codex-manifest.json"


# ---------------------------------------------------------------------------
# Hashing + copy helpers
# ---------------------------------------------------------------------------

def _sha_file(f: Path) -> str:
    return "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()


def _sha_dir(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode())
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def _copy_tree(src: Path, dst: Path) -> None:
    """Replace dst with an exact copy of src (no stale sidecars left behind)."""
    if dst.exists() or dst.is_symlink():
        if dst.is_dir() and not dst.is_symlink():
            shutil.rmtree(dst)
        else:
            dst.unlink()
    shutil.copytree(src, dst)


def _write_solo_skill(skill_dir: Path, content: str) -> None:
    """Replace skill_dir with a single-file skill (only SKILL.md).

    Mirrors _copy_tree's replace semantics so a prior pointer-wrapper copy (and
    any of its sidecars) is wiped, leaving nothing stale behind."""
    if skill_dir.exists() or skill_dir.is_symlink():
        if skill_dir.is_dir() and not skill_dir.is_symlink():
            shutil.rmtree(skill_dir)
        else:
            skill_dir.unlink()
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(content)


# ---------------------------------------------------------------------------
# Command -> Codex skill (OpenAI deprecated ~/.codex/prompts on 2026-01-22 in
# favor of the agentskills.io skills surface, so every non-excluded command is
# also emitted as a skill)
# ---------------------------------------------------------------------------

def _split_command_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading `---`-delimited frontmatter block off a command file.

    Returns (meta, body): meta maps each flat `key: value` line to its raw value
    (right of the first colon, stripped); body is everything after the closing
    `---` (the whole file verbatim when there is no frontmatter). Line-based on
    purpose - jacked command frontmatter is flat key/value pairs and PyYAML is
    not a runtime dependency."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta: dict = {}
    for line in text[4:end].splitlines():
        if ":" in line and not line.lstrip().startswith("#"):
            key, _, val = line.partition(":")
            meta[key.strip()] = val.strip()
    return meta, text[end + len("\n---\n"):]


def _first_nonempty_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ""


def _command_skill_md(cmd: Path) -> str:
    """Build the SKILL.md content that ships a command as a Codex skill.

    Generated frontmatter carries `name` (the command's stem), `description`
    (the command's own frontmatter description, else its first non-empty body
    line, trimmed and quoted via json.dumps so colons/quotes stay a valid YAML
    scalar), and passes through `argument-hint` when the command declares one.
    The body below is the command's content verbatim after its own frontmatter
    (the whole file when it has none)."""
    meta, body = _split_command_frontmatter(cmd.read_text())
    desc = (meta.get("description") or _first_nonempty_line(body)).strip()
    lines = [f"name: {cmd.stem}", f"description: {json.dumps(desc)}"]
    hint = meta.get("argument-hint")
    if hint is not None:
        lines.append(f"argument-hint: {hint}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


# ---------------------------------------------------------------------------
# AGENTS.md block (idempotent)
# ---------------------------------------------------------------------------

def _install_agents_block(path: Path, body: str) -> None:
    block = f"{_AGENTS_BEGIN}\n{body.strip()}\n{_AGENTS_END}\n"
    existing = path.read_text() if path.exists() else ""
    if _AGENTS_BEGIN in existing and _AGENTS_END in existing:
        pre = existing.split(_AGENTS_BEGIN)[0].rstrip("\n")
        post = existing.split(_AGENTS_END, 1)[1].lstrip("\n")
        parts = [p for p in (pre, block.rstrip("\n"), post) if p]
        new = "\n\n".join(parts).rstrip("\n") + "\n"
    else:
        new = (existing.rstrip("\n") + "\n\n" + block) if existing.strip() else block
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new)


def _strip_agents_block(path: Path) -> bool:
    if not path.exists():
        return False
    existing = path.read_text()
    if _AGENTS_BEGIN not in existing or _AGENTS_END not in existing:
        return False
    pre = existing.split(_AGENTS_BEGIN)[0].rstrip("\n")
    post = existing.split(_AGENTS_END, 1)[1].lstrip("\n")
    parts = [p for p in (pre, post) if p]
    new = ("\n\n".join(parts).rstrip("\n") + "\n") if parts else ""
    path.write_text(new)
    return True


# ---------------------------------------------------------------------------
# hooks.json (merge, jacked-owned entries only)
# ---------------------------------------------------------------------------

def _is_jacked_hook_group(group: dict) -> bool:
    for h in group.get("hooks", []):
        cmd = h.get("command", "")
        if any(m in cmd for m in _HOOK_MARKERS):
            return True
    return False


def _remove_codex_hooks(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return False
    hooks = data.get("hooks", {})
    changed = False
    for event in list(hooks.keys()):
        kept = [g for g in hooks[event] if not _is_jacked_hook_group(g)]
        if len(kept) != len(hooks[event]):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if not hooks:
        data.pop("hooks", None)
    # If the file is now just an empty object jacked created, remove it.
    if not data:
        path.unlink()
    else:
        path.write_text(json.dumps(data, indent=2) + "\n")
    return changed


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

def _load_manifest(home: Path) -> Optional[dict]:
    p = manifest_path(home)
    try:
        return json.loads(p.read_text())
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _write_manifest(home: Path, version: str, skills: dict, prompts: dict,
                    rules: bool, hooks: bool, now_iso: str) -> None:
    p = manifest_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "version": version,
        "written_at": now_iso,
        "skills": skills,
        "prompts": prompts,
        "rules": rules,
        "hooks": hooks,
    }, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Install / uninstall
# ---------------------------------------------------------------------------

@dataclass
class CodexInstallSummary:
    skills: list = field(default_factory=list)
    prompts: list = field(default_factory=list)
    rules: bool = False
    hooks: bool = False
    removed: list = field(default_factory=list)
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

    prior = _load_manifest(home) or {}

    # 1. Skills: full dir copy (sidecars included). Claude-only skills are
    #    skipped so they never land in Codex. This runs FIRST so any pointer-
    #    wrapper skill dir is in place before step 2 overwrites the ones that
    #    have a same-name command (precedence rule: command content wins).
    skills: dict = {}
    for skill_md in sorted((data_root / "skills").glob("*/SKILL.md")):
        name = skill_md.parent.name
        if name in _CLAUDE_ONLY_SKILLS:
            continue
        _copy_tree(skill_md.parent, skills_base / name)
        skills[name] = _sha_dir(skill_md.parent)

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
            shutil.copy(cmd, prompts_dst / cmd.name)
            prompts[cmd.name] = _sha_file(cmd)
            skill_dir = skills_base / cmd.stem
            _write_solo_skill(skill_dir, _command_skill_md(cmd))
            skills[cmd.stem] = _sha_dir(skill_dir)

    # 3. Rules -> AGENTS.md block.
    rules_done = False
    rules_src = data_root / "rules" / "jacked_behaviors.md"
    if rules_src.exists():
        _install_agents_block(codex_agents_md(home), rules_src.read_text())
        rules_done = True

    # 4. Prune legacy gatekeeper entries from hooks.json (retired in 0.70.0).
    _remove_codex_hooks(codex_hooks_json(home))
    hooks_done = False

    # Prune artifacts shipped before but not now.
    removed = []
    for name in (prior.get("skills") or {}):
        if name not in skills:
            d = skills_base / name
            if d.is_dir():
                shutil.rmtree(d, ignore_errors=True)
                removed.append(f"skills/{name}")
    for name in (prior.get("prompts") or {}):
        if name not in prompts:
            f = prompts_dst / name
            if f.exists():
                f.unlink()
                removed.append(f"prompts/{name}")

    changed = (
        skills != (prior.get("skills") or {})
        or prompts != (prior.get("prompts") or {})
        or bool(removed)
    )

    _write_manifest(home, version, skills, prompts, rules_done, hooks_done, now_iso)
    return CodexInstallSummary(
        skills=list(skills), prompts=list(prompts), rules=rules_done,
        hooks=hooks_done, removed=removed, changed=changed,
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
    manifest = _load_manifest(home) or {}
    removed: list = []

    for name in (manifest.get("skills") or {}):
        d = skills_base / name
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            removed.append(f"skills/{name}")
    for name in (manifest.get("prompts") or {}):
        f = prompts_dst / name
        if f.exists():
            f.unlink()
            removed.append(f"prompts/{name}")
    if _strip_agents_block(codex_agents_md(home)):
        removed.append("AGENTS.md block")
    if _remove_codex_hooks(codex_hooks_json(home)):
        removed.append("hooks.json gatekeeper")

    mp = manifest_path(home)
    if mp.exists():
        mp.unlink()
    return {"removed": removed}
