"""Pure content generation for Codex artifacts (skills, agent TOML, rules body)."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Mapping, Optional

from ._fsutil import _sha_dir

logger = logging.getLogger(__name__)


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
- File references to ~/.claude/skills/<name>/SKILL.md (or the older ~/.claude/commands/<name>.md): the same content ships at ~/.agents/skills/<name>/SKILL.md.
- "Plan mode" exists here too (the `plan` permission mode) - use it where the behaviors call for it.
- Where a rule or skill says CLAUDE.md, your instruction file is AGENTS.md (~/.codex/AGENTS.md globally, the repo's AGENTS.md per project).
"""


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
    if "\r\n" in text:
        # An autocrlf=true checkout would miss the "---\n" fence and every
        # consumer (prompts, command-derived skills, agent TOML) would emit
        # garbage; normalize here so all three see the same LF text.
        text = text.replace("\r\n", "\n")
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


# A skill whose SKILL.md carries this as a whole line absorbed the body of a
# former data/commands/<name>.md: everything after it is the engine that used to
# be the command file, everything before it is Claude-side repo-dispatch prose.
_ENGINE_MARKER = "<!-- ENGINE -->"


def _skill_engine_prompt(skill_md: Path) -> Optional[str]:
    """Rebuild the ~/.codex/prompts/<name>.md content for a migrated skill.

    Returns the SKILL.md's own frontmatter block verbatim (so Codex still shows a
    description for `/prompts:<name>`) followed by everything after the
    `<!-- ENGINE -->` line - i.e. the old command file, minus the repo-dispatch
    preamble and positioning prose that only Claude Code acts on. Returns None
    when the skill carries no marker: an ordinary skill ships as a skill only.
    The frontmatter block is recovered as the text `_split_command_frontmatter`
    consumed, so there is one parser for both halves."""
    text = skill_md.read_text(encoding="utf-8")
    if "\r\n" in text:
        # An autocrlf=true checkout would fail the "---\n" fence match and turn
        # every prompt into a silent prune; normalize instead (LF prompts are
        # what wheel installs emit anyway).
        text = text.replace("\r\n", "\n")
    meta, body = _split_command_frontmatter(text)
    if not meta:
        # Unparseable/missing frontmatter would emit a nameless prompt; refuse
        # instead — the exact-marker-set contract test then fails loudly in CI.
        return None
    lines = body.splitlines(keepends=True)
    for i, line in enumerate(lines):
        if line.strip() == _ENGINE_MARKER:
            return text[:len(text) - len(body)] + "".join(lines[i + 1:])
    return None


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


def _is_jacked_owned(
    name: str, prior_manifest: Mapping, this_run: Optional[Mapping] = None,
    target: Optional[Path] = None, src_dir: Optional[Path] = None,
) -> bool:
    """True iff overwriting `name` is replacing jacked's own copy, not the user's.

    Ownership is decided by CONTENT, not by the name alone:
      * written by an earlier pass of the CURRENT run (`this_run`, the in-progress
        skills dict) - step 1 writes a pointer-wrapper skill dir that step 2
        (command-derived skill) overwrites within the same install, and jacked's
        own step-1 output must not be backed up as spurious user content;
      * recorded in the PRIOR manifest AND `target` still hashes to the recorded
        value (jacked wrote it and nobody replaced it since);
      * recorded in the PRIOR manifest AND every file in `target` still matches
        the same relative path in `src_dir` - the dir is jacked's, the SOURCE
        just moved under it (a dev/editable checkout, a version bump).

    A name recorded in the manifest is NOT enough: a user who creates their own
    ~/.agents/skills/<name> after an install would otherwise have it silently
    overwritten. `target=None` keeps the old name-only answer for callers with no
    dir in hand."""
    from jacked.install_manifest import is_source_subset, skill_content_hash

    if this_run is not None and name in this_run:
        return True
    recorded = (prior_manifest.get("skills") or {}).get(name)
    if not isinstance(recorded, str):
        return False
    if target is None:
        return True
    if target.is_symlink() or not target.is_dir():
        return False
    # Strict hash first (matches how the manifest was recorded), then the
    # droppings-tolerant hash: a .DS_Store Finder dropped into jacked's own dir
    # must not flip ownership to "user's" and trigger a spurious backup. The
    # tolerant hash equals _sha_dir on a clean dir, so recorded values from
    # existing manifests match without migration.
    if _sha_dir(target) == recorded or skill_content_hash(target) == recorded:
        return True
    return src_dir is not None and is_source_subset(target, src_dir)


def _preserve_user_skill_dir(
    target: Path, expected_hash: str, name: str,
    prior_manifest: Mapping, preserved: list,
    this_run: Optional[Mapping] = None,
    src_dir: Optional[Path] = None,
) -> None:
    """Never destroy a user's OWN ~/.agents/skills/<name> on a name collision.

    ~/.agents/skills is a shared surface; a user may own a dir whose name collides
    with a jacked skill/command stem (pr, release, dcr, ...) - including a dir they
    create AFTER an install already recorded that name. Before jacked overwrites
    `target`, if the dir exists, is NOT already jacked-owned (by content: manifest
    hash, consistency with `src_dir`, or written earlier this run), and is not
    byte-identical to what we'd install, move it aside to
    ``~/.agents/jacked-backups/skills/<name>-<UTC timestamp>`` so the user's copy
    survives. The backup lives OUTSIDE ~/.agents/skills on purpose: a copy left
    beside the live skills is a discoverable duplicate skill, and those pile up
    forever. Records ``skills/<name>`` in `preserved`. The caller then writes
    jacked's copy into the now-vacant path."""
    from jacked.install_manifest import backup_dir_for, skill_content_hash

    if not (target.exists() or target.is_symlink()):
        return
    if _is_jacked_owned(name, prior_manifest, this_run, target, src_dir):
        return
    if (target.is_dir() and not target.is_symlink()
            and (_sha_dir(target) == expected_hash
                 or skill_content_hash(target) == expected_hash)):
        return  # already what we'd install (droppings aside) -> no clobber, no backup
    # Never clobber a backup that already exists (it may be the user's own, or a
    # prior preservation): backup_dir_for picks the first free timestamped path
    # so no earlier preserved copy is silently destroyed.
    backup = backup_dir_for(target, name)
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(target), str(backup))
    logger.warning("preserved your existing ~/.agents/skills/%s as %s", name, backup)
    preserved.append(f"skills/{name}")
