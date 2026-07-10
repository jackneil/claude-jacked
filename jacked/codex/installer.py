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

import hashlib
import json
import logging
import os
import shutil
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

from .credentials import codex_home

logger = logging.getLogger(__name__)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write `data` to `path` via a sibling temp file + os.replace.

    Mirrors cli._write_settings_atomic: write to a temp file, flush + os.fsync,
    then os.replace onto the target so a process killed mid-write can never leave
    a half-written Codex file (AGENTS.md, config.toml, hooks.json, manifest, or
    a restore-to-original). Cleans up the temp file if anything fails.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write `text` to `path` as UTF-8 (see `_atomic_write_bytes`)."""
    _atomic_write_bytes(path, text.encode("utf-8"))


def _is_safe_name(name: str) -> bool:
    """True iff `name` is a single, safe path component (no separators, no
    traversal). Manifest-supplied artifact names are joined onto real dirs during
    prune/uninstall; a name like ``../foo`` or ``a/b`` must never be honored."""
    if not isinstance(name, str) or name in ("", ".", ".."):
        return False
    if "/" in name or "\\" in name:
        return False
    return Path(name).name == name


def _marker_line_count(text: str, marker: str) -> int:
    """How many lines of `text` are EXACTLY `marker` (stripped). Whole-line
    matching so user prose that merely embeds the marker substring never counts."""
    return sum(1 for line in text.splitlines() if line.strip() == marker)


def _extract_block(
    text: str, begin: str, end: str
) -> Optional[tuple[str, str, str]]:
    """Split `text` around jacked's whole-line-delimited ``begin``..``end`` block.

    Returns ``(pre, block, post)`` where `block` is the marker-to-marker text
    (markers inclusive, verbatim with line endings) and `pre`/`post` are the text
    before/after it. Returns None when `begin`/`end` are not each present EXACTLY
    once as their own lines, or `end` precedes `begin` - the caller then warns and
    skips, so a marker embedded in user prose (or a duplicated/half marker) can
    never trigger an edit that clobbers user content."""
    if _marker_line_count(text, begin) != 1 or _marker_line_count(text, end) != 1:
        return None
    lines = text.splitlines(keepends=True)
    bi = next(i for i, ln in enumerate(lines) if ln.strip() == begin)
    ei = next(i for i, ln in enumerate(lines) if ln.strip() == end)
    if ei < bi:
        return None
    return "".join(lines[:bi]), "".join(lines[bi:ei + 1]), "".join(lines[ei + 1:])

_AGENTS_BEGIN = "<!-- BEGIN jacked behaviors (managed by `jacked install`) -->"
_AGENTS_END = "<!-- END jacked behaviors (managed by `jacked install`) -->"

# chrome-devtools MCP block markers + body. The block is a marker-wrapped
# `[mcp_servers.chrome-devtools]` TOML table appended to ~/.codex/config.toml. Its
# command/args MIRROR the Claude side (`_install_chrome_devtools_mcp` in
# jacked/cli.py) so the same server backs both CLIs and Codex skills referencing
# `mcp__chrome-devtools__*` resolve. The markers delimit exactly jacked's own entry
# so install can replace it and uninstall can strip it without touching a user's
# own chrome-devtools table.
_MCP_BEGIN = "# BEGIN jacked chrome-devtools MCP (managed by `jacked install`)"
_MCP_END = "# END jacked chrome-devtools MCP"


def _mcp_block_body() -> str:
    """The `[mcp_servers.chrome-devtools]` TOML table body, built from the SAME
    npx package + autoConnect args the Claude side registers (cli.py's
    ``CHROME_DEVTOOLS_NPX_PACKAGE`` / ``CHROME_DEVTOOLS_MODES["autoConnect"]``), so
    the two CLIs never drift on the version/flags. Imported lazily to keep the
    click CLI out of installer-module import time (like `_codex_qa_hook_command`)."""
    from jacked.cli import CHROME_DEVTOOLS_MODES, CHROME_DEVTOOLS_NPX_PACKAGE

    args = [CHROME_DEVTOOLS_NPX_PACKAGE, *CHROME_DEVTOOLS_MODES["autoConnect"]]
    return (
        "[mcp_servers.chrome-devtools]\n"
        'command = "npx"\n'
        f"args = {json.dumps(args)}"
    )

# A jacked-managed hook entry is identified by a marker substring in its command
# (present in both the `"jacked" _hook <name>` shim and the `-m jacked _hook
# <name>` fallback forms _build_hook_command emits), so install/uninstall can
# find and replace exactly its own entries and never a user's.
#   - _LEGACY_HOOK_MARKERS: the retired gatekeeper (removed in 0.70.0). Install
#     prunes with THESE ONLY so it never clobbers the qa entry it just wrote.
#   - _QA_HOOK_MARKERS: jacked's Codex QA-suggestion Stop hook.
#   - _HOOK_MARKERS: both, the default for _remove_codex_hooks (uninstall strips
#     everything jacked ever wrote into hooks.json).
_LEGACY_HOOK_MARKERS = ("_hook security_gatekeeper",)
_QA_HOOK_MARKER = "_hook qa_suggest"
_QA_HOOK_MARKERS = (_QA_HOOK_MARKER,)
_HOOK_MARKERS = _LEGACY_HOOK_MARKERS + _QA_HOOK_MARKERS

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

# Appended (verbatim, reviewed production copy) to the rules body when it lands
# in Codex's AGENTS.md. The behaviors + shipped skills speak Claude Code
# vocabulary; this section maps that vocabulary to Codex's native equivalents at
# runtime. It deliberately keeps one "CLAUDE.md" (the final mapping bullet), so
# the CLAUDE.md->AGENTS.md rename in `_codex_rules_body` runs on the SOURCE body
# only, before this adapter is appended.
_CODEX_ADAPTER = """\
## Codex runtime adapter (managed by `jacked install`)

The behaviors above and the jacked skills in ~/.agents/skills were authored for Claude Code. Running under Codex, map Claude vocabulary to your native equivalents:

- Slash commands are skills here: a reference to `/dcr`, `/qa`, `/pr`, etc. means the same-name skill in ~/.agents/skills - invoke it as `$dcr`, `$qa`, ... (or via `/skills`).
- "Task tool" / "Agent tool" / `subagent_type: "..."` means your subagent mechanism: spawn parallel subagents natively. Custom agent definitions live in ~/.codex/agents/*.toml. Where a named Claude agent (e.g. double-check-reviewer) is unavailable, inline its described role into the subagent prompt.
- Claude model dispatch (`model: "opus"`, `"fable"`, `"sonnet"`, `"haiku"`) does not apply: ignore Anthropic model names and pick your own model/reasoning effort per task - cheap and fast for mechanical sweeps, strongest for judgment and review.
- Browser tooling: `mcp__plugin_playwright_playwright__*` tools and Claude-in-Chrome do not exist here. Use the MCP servers from your own config (~/.codex/config.toml); `mcp__chrome-devtools__*` names resolve once a `chrome-devtools` MCP server is registered. Where instructions say `claude mcp add ...`, use `codex mcp add ...`.
- File references to ~/.claude/commands/<name>.md: the same content ships at ~/.agents/skills/<name>/SKILL.md.
- "Plan mode" exists here too (the `plan` permission mode) - use it where the behaviors call for it.
- Where a rule or skill says CLAUDE.md, your instruction file is AGENTS.md (~/.codex/AGENTS.md globally, the repo's AGENTS.md per project).
"""


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


def codex_agents_dir(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "agents"


def codex_agents_md(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "AGENTS.md"


def codex_hooks_json(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "hooks.json"


def codex_config_toml(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "config.toml"


def manifest_path(home: Optional[Path] = None, env=None) -> Path:
    return (home or codex_home(env)) / "jacked-codex-manifest.json"


# ---------------------------------------------------------------------------
# Hashing + copy helpers
# ---------------------------------------------------------------------------

def _sha_file(f: Path) -> str:
    return "sha256:" + hashlib.sha256(f.read_bytes()).hexdigest()


def _sha_text(s: str) -> str:
    return "sha256:" + hashlib.sha256(s.encode()).hexdigest()


def _sha_dir(d: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p for p in d.rglob("*") if p.is_file()):
        h.update(f.relative_to(d).as_posix().encode())
        h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def _sha_solo_skill(content: str) -> str:
    """The `_sha_dir` value a solo skill dir (only ``SKILL.md`` holding `content`)
    will hash to once written, computed without touching disk. Mirrors `_sha_dir`
    for a single UTF-8 ``SKILL.md`` so a pre-write byte-identity check is exact."""
    h = hashlib.sha256()
    h.update(b"SKILL.md")
    h.update(content.encode("utf-8"))
    return "sha256:" + h.hexdigest()


def _is_jacked_owned(
    name: str, prior_manifest: Mapping, this_run: Optional[Mapping] = None
) -> bool:
    """True iff overwriting `name` is replacing jacked's own copy, not the user's.

    Jacked-owned means recorded as a jacked skill in the PRIOR manifest, OR
    already written by an earlier pass of the CURRENT run (`this_run` is the
    in-progress skills dict). The second case matters because step 1 writes a
    pointer-wrapper skill dir that step 2 (command-derived skill) then overwrites
    within the same install: without it, step 2 would mistake jacked's own
    step-1 output for user content and back it up as a spurious `.pre-jacked`."""
    if name in (prior_manifest.get("skills") or {}):
        return True
    return this_run is not None and name in this_run


def _preserve_user_skill_dir(
    target: Path, expected_hash: str, name: str,
    prior_manifest: Mapping, preserved: list,
    this_run: Optional[Mapping] = None,
) -> None:
    """Never destroy a user's OWN ~/.agents/skills/<name> on a name collision.

    ~/.agents/skills is a shared surface; a user may own a dir whose name collides
    with a jacked skill/command stem (pr, release, dcr, ...). Before jacked
    overwrites `target`, if the dir exists, is NOT already jacked-owned (per the
    prior manifest or written earlier this run via `this_run`), and is not already
    byte-identical to what we'd install, move it aside to ``<target>.pre-jacked``
    (replacing any stale backup first) so the user's copy survives. Records
    ``skills/<name>`` in `preserved`. The caller then writes jacked's copy into
    the now-vacant path."""
    if not (target.exists() or target.is_symlink()):
        return
    if _is_jacked_owned(name, prior_manifest, this_run):
        return
    if (target.is_dir() and not target.is_symlink()
            and _sha_dir(target) == expected_hash):
        return  # already exactly what we'd install -> no clobber, no backup
    # Never clobber a backup that already exists (it may be the user's own, or a
    # prior preservation): pick the first free `.pre-jacked[-N]` suffix so no
    # earlier preserved copy is silently destroyed.
    backup = target.with_name(target.name + ".pre-jacked")
    n = 2
    while backup.exists() or backup.is_symlink():
        backup = target.with_name(f"{target.name}.pre-jacked-{n}")
        n += 1
    shutil.move(str(target), str(backup))
    logger.warning(
        "preserved your existing ~/.agents/skills/%s as %s", name, backup.name
    )
    preserved.append(f"skills/{name}")


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
    (skill_dir / "SKILL.md").write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Command -> Codex skill (OpenAI deprecated ~/.codex/prompts on 2026-01-22 in
# favor of the agentskills.io skills surface, so every non-excluded command is
# also emitted as a skill)
# ---------------------------------------------------------------------------

def _split_command_frontmatter(text: str) -> tuple[dict, str]:
    """Split a leading `---`-delimited frontmatter block off a command file.

    Returns (meta, body): meta maps each flat `key: value` entry to its value;
    body is everything after the closing `---` (the whole file verbatim when
    there is no frontmatter). Line-based on purpose - jacked frontmatter is
    flat key/value pairs and PyYAML is not a runtime dependency. Values in
    YAML double-quoted scalars may span lines (e.g. double-check-reviewer's
    description): continuation lines fold to spaces per YAML semantics and the
    surrounding quote pair is stripped, so consumers see the full clean text
    instead of a truncated fragment with a stray quote."""
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    meta: dict = {}
    lines = text[4:end].splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        i += 1
        if ":" not in line or line.lstrip().startswith("#"):
            continue
        if line[0] in " \t":
            # Indented continuation of a scalar we didn't consume below (never
            # a real key in jacked frontmatter) - skip rather than misparse.
            continue
        key, _, val = line.partition(":")
        val = val.strip()
        # Multi-line double-quoted scalar: consume until the closing quote.
        # Our frontmatter never uses escaped \" so endswith('"') is safe.
        if val.startswith('"') and not (len(val) > 1 and val.endswith('"')):
            parts = [val]
            while i < len(lines) and not parts[-1].rstrip().endswith('"'):
                parts.append(lines[i].strip())
                i += 1
            val = " ".join(p for p in parts if p)
        if len(val) > 1 and val[0] == val[-1] == '"':
            val = val[1:-1]
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
    (the whole file when it has none).

    ``ensure_ascii=False`` is REQUIRED on every json.dumps here (same reason as
    `_agent_toml`): the default escapes astral-plane emoji as UTF-16 surrogate
    pairs (``\\uD83D\\uDE00``), which strict YAML rejects as a lone surrogate.
    Emitting the chars literally as UTF-8 keeps the double-quoted YAML scalar
    valid. `name` is quoted too, for symmetry and to survive stems YAML would
    otherwise choke on."""
    meta, body = _split_command_frontmatter(cmd.read_text(encoding="utf-8"))
    desc = (meta.get("description") or _first_nonempty_line(body)).strip()
    lines = [
        f"name: {json.dumps(cmd.stem, ensure_ascii=False)}",
        f"description: {json.dumps(desc, ensure_ascii=False)}",
    ]
    hint = meta.get("argument-hint")
    if hint is not None:
        # Re-quote: the parser returns clean unquoted values, and a bare
        # "[--flag]" would parse as a YAML flow sequence, not a string.
        lines.append(f"argument-hint: {json.dumps(hint, ensure_ascii=False)}")
    return "---\n" + "\n".join(lines) + "\n---\n" + body


# ---------------------------------------------------------------------------
# Agent -> Codex custom-agent TOML. jacked ships Claude Code subagent
# definitions (data/agents/*.md); Codex reads custom agents as TOML.
# ---------------------------------------------------------------------------

def _agent_toml(agent_md: Path) -> str:
    """Build the ~/.codex/agents/<stem>.toml content for one Claude subagent.

    Codex custom agents are TOML with required `name` (the agent's stem),
    `description`, and `developer_instructions` (the agent's system prompt = its
    markdown body). We split the agent file's `---`-delimited frontmatter (via
    the generic `_split_command_frontmatter`) from its body, then emit those
    three as TOML basic strings. json.dumps escaping (\\n, \\", \\\\, \\uXXXX for
    control chars) is valid TOML basic-string syntax, so it doubles as the TOML
    string quoter and the whole description/body ship VERBATIM (no truncation).
    `ensure_ascii=False` is REQUIRED: the default (ascii) escapes astral-plane
    characters (the emoji real agents use, e.g. 🎯🚀) as UTF-16 surrogate-pair
    `\\uD83D\\uDE00` escapes, which are valid JSON but NOT valid TOML (a TOML
    `\\uXXXX` must be a Unicode scalar, never a surrogate half); emitting those
    chars literally as UTF-8 is valid TOML. Description falls back to the first
    non-empty body line when the frontmatter omits it. Claude-only `tools:` /
    `model:` keys are deliberately NOT carried over: no model is pinned so Codex
    picks its own."""
    meta, body = _split_command_frontmatter(agent_md.read_text(encoding="utf-8"))
    desc = (meta.get("description") or _first_nonempty_line(body)).strip()
    return (
        f"name = {json.dumps(agent_md.stem, ensure_ascii=False)}\n"
        f"description = {json.dumps(desc, ensure_ascii=False)}\n"
        f"developer_instructions = {json.dumps(body, ensure_ascii=False)}\n"
    )


# ---------------------------------------------------------------------------
# AGENTS.md block (idempotent)
# ---------------------------------------------------------------------------

def _codex_rules_body(text: str) -> str:
    """Adapt the Claude-authored rules body for Codex before it lands in AGENTS.md.

    Rewrites every `CLAUDE.md` reference to `AGENTS.md` (Codex's instruction
    file), collapses the `AGENTS.md`, `AGENTS.md` duplicate the rename creates in
    the Markdown-exceptions filename enumeration (the source lists both names)
    back to a single `AGENTS.md` with the rest of that line intact, then appends
    the runtime-adapter section that maps the remaining Claude vocabulary in the
    behaviors + shipped skills to Codex's native equivalents. The rename runs on
    the source body only: the adapter's own single `CLAUDE.md` mention is
    intentional (it tells the agent that `CLAUDE.md` means `AGENTS.md`) and stays
    verbatim. Case-sensitive on purpose, so lowercase `~/.claude/...` paths are
    untouched."""
    body = text.replace("CLAUDE.md", "AGENTS.md")
    body = body.replace("`AGENTS.md`, `AGENTS.md`", "`AGENTS.md`")  # backticked dup (real data)
    body = body.replace("AGENTS.md, AGENTS.md", "AGENTS.md")        # bare dup, just in case
    return body.rstrip("\n") + "\n\n" + _CODEX_ADAPTER


def _install_agents_block(path: Path, body: str) -> None:
    block = f"{_AGENTS_BEGIN}\n{body.strip()}\n{_AGENTS_END}\n"
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    begin_ct = _marker_line_count(existing, _AGENTS_BEGIN)
    end_ct = _marker_line_count(existing, _AGENTS_END)
    if begin_ct == 0 and end_ct == 0:
        new = (existing.rstrip("\n") + "\n\n" + block) if existing.strip() else block
    else:
        extracted = _extract_block(existing, _AGENTS_BEGIN, _AGENTS_END)
        if extracted is None:
            logger.warning(
                "unexpected jacked marker layout in %s (begin=%d, end=%d); leaving "
                "it untouched rather than risk clobbering your content",
                path, begin_ct, end_ct,
            )
            return
        pre, _block, post = extracted
        pre = pre.rstrip("\n")
        post = post.lstrip("\n")
        parts = [p for p in (pre, block.rstrip("\n"), post) if p]
        new = "\n\n".join(parts).rstrip("\n") + "\n"
    _atomic_write_text(path, new)


def _strip_agents_block(path: Path) -> bool:
    if not path.exists():
        return False
    existing = path.read_text(encoding="utf-8")
    extracted = _extract_block(existing, _AGENTS_BEGIN, _AGENTS_END)
    if extracted is None:
        if _marker_line_count(existing, _AGENTS_BEGIN) or _marker_line_count(
            existing, _AGENTS_END
        ):
            logger.warning(
                "unexpected jacked marker layout in %s; leaving it untouched", path
            )
        return False
    pre, _block, post = extracted
    pre = pre.rstrip("\n")
    post = post.lstrip("\n")
    parts = [p for p in (pre, post) if p]
    new = ("\n\n".join(parts).rstrip("\n") + "\n") if parts else ""
    _atomic_write_text(path, new)
    return True


# ---------------------------------------------------------------------------
# chrome-devtools MCP block in config.toml (marker-wrapped TOML append)
# ---------------------------------------------------------------------------

def _mcp_block() -> str:
    """The full marker-wrapped block, marker-to-marker plus a trailing newline."""
    return f"{_MCP_BEGIN}\n{_mcp_block_body()}\n{_MCP_END}\n"


def _write_mcp_verified(cfg: Path, new_text: str, original: Optional[bytes],
                        status: str) -> str:
    """Atomically write `new_text`, then parse-check the result with tomllib. On
    failure, restore the file to `original` bytes exactly (or delete it when we
    created it, i.e. original is None) and return "skipped-unparseable" - never
    leave a broken config. On success return `status`. The atomic write and the
    post-write parse-check are complementary: the first prevents a torn file, the
    second guarantees the (whole) file we produced is valid TOML."""
    _atomic_write_text(cfg, new_text)
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        if original is None:
            cfg.unlink()
        else:
            _atomic_write_bytes(cfg, original)
        logger.warning(
            "chrome-devtools MCP write to %s produced unparseable TOML; "
            "restored the original and skipped registration", cfg,
        )
        return "skipped-unparseable"
    return status


def ensure_chrome_devtools_mcp(
    home: Optional[Path] = None, env: Optional[Mapping[str, str]] = None
) -> str:
    """Register jacked's chrome-devtools MCP server in Codex's config.toml.

    Deterministic, marker-wrapped TOML append (never `codex mcp add`). Returns one
    of:
      - "added"               config.toml was missing (created with just our block)
                              OR it parses, has no chrome-devtools entry, and our
                              block was appended at EOF.
      - "updated"             our marked block was present but its body drifted, so
                              it was replaced in place.
      - "unchanged"           our marked block was present and already current.
      - "preexisting"         the config parses and already has an mcp_servers.
                              chrome-devtools entry OUTSIDE our markers (the user's
                              own) - file left byte-untouched; we never fight it.
      - "skipped-unparseable" config.toml exists but tomllib can't parse it (left
                              untouched) OR a write produced broken TOML (restored).

    Existing content is preserved byte-for-byte before an appended block, and any
    write is parse-checked with the original bytes restored on failure.
    """
    home = home or codex_home(env)
    cfg = codex_config_toml(home)
    block = _mcp_block()
    desired_block = f"{_MCP_BEGIN}\n{_mcp_block_body()}\n{_MCP_END}"

    # Missing config.toml -> create it containing only our marked block.
    if not cfg.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        return _write_mcp_verified(cfg, block, None, "added")

    original = cfg.read_bytes()
    try:
        text = original.decode("utf-8")
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        logger.warning(
            "Codex config.toml at %s did not parse; leaving it untouched and "
            "skipping chrome-devtools MCP registration", cfg,
        )
        return "skipped-unparseable"

    # Our marked block already present (whole-line markers) -> replace in place iff
    # its body drifted. Whole-line matching so a marker embedded in a user string
    # can't be mistaken for our block. (A duplicate user chrome-devtools table
    # alongside ours would be a TOML duplicate-key error and never reach here.)
    begin_ct = _marker_line_count(text, _MCP_BEGIN)
    end_ct = _marker_line_count(text, _MCP_END)
    if begin_ct or end_ct:
        extracted = _extract_block(text, _MCP_BEGIN, _MCP_END)
        if extracted is None:
            logger.warning(
                "unexpected jacked chrome-devtools marker layout in %s (begin=%d, "
                "end=%d); leaving it untouched and skipping registration",
                cfg, begin_ct, end_ct,
            )
            return "skipped-unparseable"
        pre, current_block, post = extracted
        if current_block.rstrip("\n") == desired_block:
            return "unchanged"
        return _write_mcp_verified(
            cfg, pre + desired_block + "\n" + post, original, "updated"
        )

    # A user's OWN (unmarked) chrome-devtools entry -> never fight it.
    if "chrome-devtools" in (parsed.get("mcp_servers") or {}):
        return "preexisting"

    # Parses, no entry -> append our block at EOF, separated by a blank line,
    # preserving existing content byte-for-byte as the prefix (append only).
    new_text = block if not text else text + "\n\n" + block
    return _write_mcp_verified(cfg, new_text, original, "added")


def _strip_mcp_block(cfg: Path) -> bool:
    """Strip ONLY jacked's marked chrome-devtools block from config.toml, leaving
    the rest byte-identical (the exact inverse of the append in
    ``ensure_chrome_devtools_mcp``: drop the ``\\n\\n`` separator install inserted
    before the block and the block's own trailing newline). A user's own unmarked
    entry is never touched. Returns True if a block was removed. If nothing but our
    block remained, the (jacked-created) file is deleted."""
    if not cfg.exists():
        return False
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    extracted = _extract_block(text, _MCP_BEGIN, _MCP_END)
    if extracted is None:
        if _marker_line_count(text, _MCP_BEGIN) or _marker_line_count(text, _MCP_END):
            logger.warning(
                "unexpected jacked chrome-devtools marker layout in %s; leaving it "
                "untouched", cfg,
            )
        return False
    # `_extract_block` already consumed the block's own trailing newline into
    # `block`, so `post` is clean; drop the blank-line separator install inserted
    # before the block (which lives at the tail of `pre`).
    pre, _block, post = extracted
    if pre.endswith("\n\n"):
        pre = pre[:-2]
    new_text = pre + post
    if new_text:
        _atomic_write_text(cfg, new_text)
    else:
        cfg.unlink()                     # only our block existed -> jacked-created
    return True


# ---------------------------------------------------------------------------
# hooks.json (merge, jacked-owned entries only)
# ---------------------------------------------------------------------------

def _is_jacked_hook_group(group: dict, markers: tuple = _HOOK_MARKERS) -> bool:
    # A hand-malformed hooks.json can carry non-dict group entries (a bare
    # string in the list) or a non-list inner "hooks" value (null/scalar);
    # treat anything that isn't our shape as not-ours.
    if not isinstance(group, dict):
        return False
    inner = group.get("hooks")
    if not isinstance(inner, list):
        return False
    for h in inner:
        cmd = h.get("command", "") if isinstance(h, dict) else ""
        # A present-but-non-string `command` (null/int/bool) is not ours; `.get`
        # only defaults a MISSING key, so guard the value type before `in`.
        if isinstance(cmd, str) and any(m in cmd for m in markers):
            return True
    return False


def _remove_codex_hooks(path: Path, markers: tuple = _HOOK_MARKERS) -> bool:
    """Strip jacked-owned hook groups (matching `markers`) from hooks.json,
    leaving user entries and unknown top-level keys intact. Install passes
    _LEGACY_HOOK_MARKERS (gatekeeper only, so the just-written qa entry survives);
    uninstall uses the default (both). Returns True if anything was removed."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return False
    if not isinstance(data, dict):
        # A non-object root is not ours to rewrite; leave it byte-untouched.
        return False
    hooks = data.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event in list(hooks.keys()):
        groups = hooks[event]
        if not isinstance(groups, list):
            # A non-list event value isn't our shape; leave it byte-untouched.
            continue
        kept = [g for g in groups if not _is_jacked_hook_group(g, markers)]
        if len(kept) != len(groups):
            changed = True
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    # Nothing of ours was present: leave the file byte-identical (don't reformat
    # the user's JSON just for having looked at it).
    if not changed:
        return False
    if not hooks:
        data.pop("hooks", None)
    # If the file is now just an empty object jacked created, remove it.
    if not data:
        path.unlink()
    else:
        _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return changed


def _codex_qa_hook_command() -> str:
    """The Stop-hook command jacked writes into Codex's hooks.json.

    Reuses cli's `_build_hook_command` (the SAME upgrade-safe `"jacked" _hook
    <name>` shim / `"{python}" -m jacked _hook <name>` fallback the Claude side
    writes) and appends `--runtime codex` so the shared qa_suggest.py hook prints
    the Codex `$qa` skill invocation instead of Claude's `/qa`. Imported lazily to
    avoid importing the click CLI at installer-module import time and to keep the
    find_bin fallback logic in ONE place (no duplication)."""
    from jacked.cli import _build_hook_command

    return f"{_build_hook_command('qa_suggest')} --runtime codex"


def _install_codex_qa_hook(home: Optional[Path] = None) -> bool:
    """Idempotently ensure Codex's hooks.json Stop event carries jacked's
    QA-suggest entry.

    The entry is ``{"matcher": "", "hooks": [{"type": "command", "command":
    "<...> _hook qa_suggest --runtime codex"}]}``. OUR entry is marker-identified
    by ``_hook qa_suggest`` in its command (like ``_is_jacked_hook_group``): if
    present with a drifted command it's replaced in place (not duplicated); other
    entries and unknown top-level keys are never touched. Returns True when our
    entry is present after the call.

    If hooks.json exists but is unparseable (bad JSON - including a trailing
    comma - or a non-UTF-8 file) OR its root is not a JSON object, we DO NOT
    write: warn and return False, leaving the user's file byte-identical (mirrors
    ``ensure_chrome_devtools_mcp``'s skipped-unparseable contract). Clobbering
    every user hook to force ours in would be worse than skipping."""
    path = codex_hooks_json(home)
    command = _codex_qa_hook_command()

    data: dict = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
            logger.warning(
                "Codex hooks.json at %s did not parse (%s); leaving it untouched "
                "and skipping the QA hook", path, exc,
            )
            return False
        if not isinstance(loaded, dict):
            logger.warning(
                "Codex hooks.json at %s has a non-object root; leaving it untouched "
                "and skipping the QA hook", path,
            )
            return False
        data = loaded

    # A PRESENT-but-wrong-type "hooks"/"Stop" is a malformed structure we don't
    # own; leave it byte-untouched rather than replacing (and dropping) it. An
    # ABSENT key is fine to create.
    hooks = data.get("hooks")
    if hooks is None:
        hooks = {}
        data["hooks"] = hooks
    elif not isinstance(hooks, dict):
        logger.warning(
            "Codex hooks.json at %s has a non-object 'hooks' value; leaving it "
            "untouched and skipping the QA hook", path,
        )
        return False
    stop = hooks.get("Stop")
    if stop is None:
        stop = []
        hooks["Stop"] = stop
    elif not isinstance(stop, list):
        logger.warning(
            "Codex hooks.json at %s has a non-list 'Stop' value; leaving it "
            "untouched and skipping the QA hook", path,
        )
        return False

    entry_hooks = [{"type": "command", "command": command}]
    for group in stop:
        if isinstance(group, dict) and _is_jacked_hook_group(group, _QA_HOOK_MARKERS):
            group.setdefault("matcher", "")
            group["hooks"] = entry_hooks  # replace ours in place (command may drift)
            break
    else:
        stop.append({"matcher": "", "hooks": entry_hooks})

    _atomic_write_text(path, json.dumps(data, indent=2) + "\n")
    return True


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
        _preserve_user_skill_dir(
            skills_base / name, expected, name, prior, preserved
        )
        _copy_tree(skill_md.parent, skills_base / name)
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
            shutil.copy(cmd, prompts_dst / cmd.name)
            prompts[cmd.name] = _sha_file(cmd)
            skill_dir = skills_base / cmd.stem
            content = _command_skill_md(cmd)
            # this_run=skills: a wrapper dir step 1 wrote this run is jacked's own,
            # not user content, so overwriting it must not spawn a .pre-jacked.
            _preserve_user_skill_dir(
                skill_dir, _sha_solo_skill(content), cmd.stem, prior, preserved,
                this_run=skills,
            )
            _write_solo_skill(skill_dir, content)
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
        if isinstance(recorded, str) and _sha_dir(d) == recorded:
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
        if isinstance(recorded, str) and _sha_dir(d) == recorded:
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
