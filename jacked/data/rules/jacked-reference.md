# Jacked Reference (for Claude Code)

This file gives you deep knowledge about the jacked toolkit installed on this system.
Read this when the user asks about jacked features, installation, logs, or troubleshooting.

## What Jacked Is

- Multi-account manager + skills suite for Claude Code (and Codex): live usage tracking, auto account-switch, smart reviewers, quick commands, web dashboard
- Installed via `uv tool install`, configured via `jacked install`
- Source: https://github.com/jackneil/claude-jacked

## File Locations

| File | Purpose |
|------|---------|
| `~/.claude/settings.json` | Hook configuration (PreToolUse, Stop) |
| `~/.claude/CLAUDE.md` | Behavioral rules (between `# jacked-behaviors-v2` markers) |
| `~/.claude/jacked-reference.md` | This reference doc |
| `~/.claude/agents/*.md` | 10 specialized review/workflow agents |
| `~/.claude/commands/*.md` | Slash commands (20 total; the 7 quick ones detailed under Quick Commands below: /dc, /pr, /learn, /blindspot, /redo, /techdebt, /audit-rules) |
| `~/.claude/skills/*/SKILL.md` | Skills (28 total; every skill is slash-invocable as /name). Ten used to ship as commands: /dcr, /qa, /ux, /qa-video, /demo-video, /docs-sync, /swarm-research, /whats-next, /release, /lockdown. They carry their full engine body in SKILL.md now, so there is no command file for them |
| `~/.claude/jacked-guardrails/*.md` | Guardrails templates (base + 4 languages) |
| `~/.claude/jacked-hooks/*.sh` | Git hook templates (installed extensionless) |
| `~/.claude/jacked-templates/*.html` | HTML scaffolds for human-readable artifacts (plans, specs, research, checkpoints) |
| `<project>/JACKED_GUARDRAILS.md` | Per-project coding standards (created by `jacked guardrails init`) |

## Artifact Format Preference

When you write a file that a **human will open and read** (plans, specs, research summaries, checkpoints, design docs, internal knowledge artifacts), **prefer HTML over Markdown**. Markdown is only a great choice when something else renders it for the human — GitHub's web UI, a wiki engine, a docs site. When the user opens the file directly from disk, HTML wins on every axis:

| Need | Markdown (opened locally) | HTML |
|------|---------------------------|------|
| Headings, sections | Plain text, no styling | Typography, anchored TOC |
| Diagrams | Stays as `mermaid` source code | Renders via Mermaid.js |
| Tables | ASCII pipes | Styled, accessible |
| Code | Backticks | Monospace block with proper background |
| Dark mode | None | `prefers-color-scheme` adapts |
| Print / PDF export | Untyped page breaks | Print stylesheet with `break-inside: avoid` |

### Rule

- **HTML (`.html`)** for: `docs/plans/`, `docs/specs/`, `docs/design/`, `docs/superpowers/plans/`, `docs/superpowers/specs/`, `.claude/checkpoints/`, `.claude/research/`, and any other location holding a human-consumed artifact.
- **Markdown (`.md`)** for: `README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSE.md`, `_wiki/*.md` (GitHub-rendered), and `CLAUDE.md`, `AGENTS.md`, `lessons.md`, `MEMORY.md` (Claude reads these as instructions at session start — Markdown is the format Claude Code expects there).

### How to write an HTML artifact

Start from the bundled template. The canonical filename is **`plan-template.html`** — it covers all artifact types (plans, specs, research, checkpoints) via the `<meta name="jacked:type">` tag and adaptive sections:

```bash
cp ~/.claude/jacked-templates/plan-template.html docs/superpowers/plans/$(date +%Y-%m-%d)-{slug}.html
```

The template includes:
- Embedded CSS (no external stylesheet — works offline)
- Mermaid.js via CDN with **automatic fallback** that surfaces diagram source if the CDN is unreachable
- Dark mode via `prefers-color-scheme`
- Print stylesheet with sensible page breaks
- Metadata `<meta>` tags (`jacked:type`, `jacked:status`, `jacked:branch`, `jacked:date`) so artifacts are machine-introspectable
- Status badges, callouts (info/warn/danger/ok), task checklists, file-structure tables, anchored TOC

Replace `{{PLACEHOLDERS}}`, keep the sections you want, delete the rest. Pure HTML — no preprocessor.

### When to break the rule

You may keep Markdown for an internal artifact **only** when a downstream tool *requires* Markdown input — a static-site generator that ingests `.md`, a linter that scans Markdown for issues, a CI step expecting specific frontmatter. That's the only valid reason.

"It feels short," "no diagrams needed," "it's just notes," or "the user will probably never reopen it" are NOT valid overrides. The template's overhead is one `cp` command; the cost of getting it wrong is a file that's harder to read every time anyone opens it.

## CLI Commands

```
jacked install [--sounds] [--force] [--no-packs] [--packs NAME]  # Install suite (+ default skill packs; --no-packs to skip)
jacked uninstall [--sounds]                         # Remove from Claude Code (also removes enabled skill packs)
jacked packs list                                   # List skill packs + on/off/default status + install counts
jacked packs enable NAME / disable NAME             # Install / durably remove a pack via the npx skills CLI
jacked packs update [NAME]                          # Refresh enabled packs from their upstream repos
jacked permissions audit [--fix] [--yes]            # Audit permission rules for dangerous wildcards
jacked check-version                                # Check for newer PyPI version
jacked webux                                        # Launch the web dashboard
jacked service start                                # Start the tray service (menu-bar pill on macOS)
jacked service restart --host 0.0.0.0               # Expose dashboard beyond loopback (Tailscale/LAN)
jacked init [--repo PATH] [--language LANG]          # Set up guardrails + lint hook in project
jacked guardrails init [--repo PATH] [--force]       # Create JACKED_GUARDRAILS.md from templates
jacked lint-hook init [--repo PATH] [--force]        # Install pre-push lint hook in .git/hooks/
jacked dcr engine [--json]                          # Show the /dcr review engine config + Codex readiness
jacked dcr engine set codex --model M --effort E    # Route /dcr volume reviewers to the Codex CLI
jacked dcr engine clear                             # Back to Claude reviewers (the default)
python -m jacked                                    # Alternative invocation
```

DCR review engine (0.91.0+): `/dcr` can run its volume reviewers on the OpenAI
Codex CLI instead of Claude Task subagents, so review fan-out spends the user's
OpenAI subscription instead of their Anthropic plan. Config lives in
`~/.claude/jacked-dcr.json`, written by `jacked dcr engine set` or the dashboard
card (Settings > Features > Review Engine; GET/PUT /api/dcr-engine). At review
time /dcr runs one `jacked dcr engine --json` call; the output carries
engine/model/effort, the `keep_on_claude` lens list, a live Codex preflight
(installed + signed in = `usable`), and the packaged findings-schema path
(`jacked/data/schemas/dcr-review-output.schema.json`) that Codex jobs bind with
`--output-schema`. Judgment never moves: lens selection, finding validation, the
fix phase, and the verdict stay in the parent Claude session; `keep_on_claude`
lenses (default Security + Frontend Design) always spawn as Claude reviewers.
Every failure shape falls back to Claude - codex missing/unauthed, a corrupt
config file, a failed or hung Codex job (that one reviewer re-runs as a Claude
Task) - so a review never blocks on the engine and no lens is ever dropped.
Defaults when enabling: model `gpt-5.6-luna`, effort `xhigh`. Any Codex model
name is accepted; effort is one of none/minimal/low/medium/high/xhigh/max.

Chain-of-command auto-load (0.76.0+): `jacked install` registers a synchronous
SessionStart hook (`jacked _hook chain_of_command_context`) that injects the
chain-of-command dispatch policy (from ~/.claude/skills/chain-of-command/SKILL.md)
into every new Claude Code session, making the model-dispatch lanes binding from
the first turn with no /chain-of-command invocation. Disable: toggle the
chain-of-command skill off in the dashboard (hook goes silent when the skill
file is absent). `jacked uninstall` removes the hook entry.

Skill packs (0.82.0+): curated collections of third-party skills installed LIVE
from their upstream GitHub repos via the vercel-labs skills CLI (`npx skills`,
Node 18+ required); nothing is vendored. Registry ships in jacked/data/packs.json
(currently `marketing` = 28 curated skills from coreyhaines31/marketingskills,
`design-extras` = improve-animations from emilkowalski/skills). Packs marked
`default: true` install by DEFAULT on a plain `jacked install`; opt out with
`jacked install --no-packs` (per-run) or `jacked packs disable NAME` (durable).
State in ~/.claude/jacked-packs.json is a v2 three-state model
(`{"version":2,"packs":{name:{"state":"enabled"|"disabled","at":iso}}}`): an
explicit decision wins, and a pack with no entry follows the registry `default`.
Disable writes a durable "disabled" (never drops the entry) so a default-on pack
the user removed is not silently reinstalled next install. v1 files migrate on
load. Skills land canonical in ~/.agents/skills/ with symlinks in ~/.claude/skills/
tracked by the skills CLI lockfile (~/.agents/.skill-lock.json). Every
`jacked install` refreshes the effectively-enabled packs (defaults minus
disabled, plus explicit-enabled). Dashboard toggles: Settings > Features > Skill
Packs (GET/PUT /api/packs; GET exposes enabled/default/explicit per pack). Exit
codes from the skills CLI are untrusted (rc=0 even when nothing installs); jacked
verifies every operation on disk. Removal is lockfile-source-checked and install
refuses to overwrite a skill dir you already own (both from another source or
unmanaged) so a same-named user skill is never clobbered.

Remote dashboard access (0.76.0+): the dashboard binds 127.0.0.1 by default. Any
`--host` beyond loopback is hardened server-side (no CORS wildcard, same-origin
WebSocket gate, cross-site write rejection, Host-header validation against DNS
rebinding); there is still NO auth layer, so reachability must be restricted at
the network layer (Tailscale ACL scoped to the port, firewall). Escape hatches:
`JACKED_ALLOWED_ORIGINS` (extra cross-origin consumers) and `JACKED_ALLOWED_HOSTS`
(custom DNS names) env vars on the service process. Alternative that needs no
rebind: `tailscale serve --bg 8321`. See README "Remote Access (Tailscale)".

Retired in 0.70.0: the security gatekeeper (`jacked gatekeeper *`, superseded by
Claude Code's native auto permission mode) and Qdrant session search
(`jacked search/backfill/status/configure`, the `/jacked` skill, the `[search]`
extra). `jacked install` prunes their hooks from settings.json automatically.

## Codex Integration

When Codex is present (the `codex` binary is on PATH, or a `~/.codex` home exists),
`jacked install` runs a second pass that deploys jacked into Codex's native formats
alongside the Claude Code install. It is idempotent, tracked by its own manifest,
and fully reversed by `jacked uninstall`.

What ships to Codex:

| jacked artifact | Codex target | Notes |
|------|------|------|
| Skills | `~/.agents/skills/<name>/` | Full directory copy (sidecar files included), the agentskills.io format Codex discovers. |
| Commands | `~/.codex/prompts/<name>.md` AND `~/.agents/skills/<stem>/SKILL.md` | OpenAI deprecated `~/.codex/prompts` on 2026-01-22 in favor of skills, so each non-excluded command also ships as a command-derived skill; the prompts copy stays for back-compat during the deprecation window. This applies to the command files only. The ten migrated names (`dcr`, `qa`, `ux`, `qa-video`, `demo-video`, `docs-sync`, `swarm-research`, `whats-next`, `release`, `lockdown`) are skills now and ship their full content straight across, with their `$name` prompts generated from the skill. |
| Rules | managed block in `~/.codex/AGENTS.md` | The Claude-authored behaviors, adapted for Codex: `CLAUDE.md` references rewritten to `AGENTS.md`, plus a "Codex runtime adapter" section that maps Claude vocabulary to Codex equivalents (slash commands to `$skill` invocations, the Task/Agent tool to native subagents, Claude model lanes ignored, `mcp__chrome-devtools__*` names, plan mode). |
| Review agents | `~/.codex/agents/<name>.toml` | The 10 review agents converted to Codex custom-agent TOML (`name`, `description`, `developer_instructions`), with no model pin so Codex picks its own. |
| chrome-devtools MCP | jacked-managed marker block in `~/.codex/config.toml` | Registers the same npx server the Claude side uses, so Codex skills referencing `mcp__chrome-devtools__*` resolve. A user's own chrome-devtools entry is never touched, and a write that would break the TOML is rolled back. |
| QA-suggest hook | `~/.codex/hooks.json` (Stop event) | The runtime-portable `qa_suggest` hook with `--runtime codex`, so the suggestion reads `$qa` (the Codex skill invocation) instead of `/qa`. Codex requires a one-time trust for this hook: run `/hooks` inside Codex to approve it (the installer prints this reminder when the entry is newly added). |

Claude-only artifacts excluded from Codex: the `chain-of-command` and `recover`
skills, and the `swarm`, `goal-maker`, `browser-reset`, and `jacked-setup`
commands (each is wired to Claude Code machinery Codex has no analog for).
Upgrades prune any previously-shipped copies of these.

Manifest and uninstall: everything above is recorded in
`~/.codex/jacked-codex-manifest.json` (skills, prompts, agents, mcp, hooks), which
keeps the Codex install idempotent and lets `jacked uninstall` remove exactly what
jacked wrote, never a user's own entries. The Claude manifest
(`~/.claude/jacked-manifest.json`) is separate and untouched.

## Guardrails System

Language-specific coding standards enforced through templates and git hooks.

**Templates** (`~/.claude/jacked-guardrails/`):
- `base.md` — universal rules: size limits, structure, /dc before commits, lint before push
- `python.md`, `node.md`, `rust.md`, `go.md` — language-specific tooling and patterns

**Per-project setup** (`jacked init` or `jacked guardrails init`):
- Auto-detects language from pyproject.toml/package.json/Cargo.toml/go.mod
- Creates `JACKED_GUARDRAILS.md` in project root (base + language template)
- Claude follows these because global CLAUDE.md says "follow JACKED_GUARDRAILS.md or DESIGN_GUARDRAILS.md if they exist"

**Git pre-push hook** (`jacked lint-hook init`):
- Installs to `.git/hooks/pre-push` (extensionless, as git requires)
- Runs language-appropriate linter before allowing push
- Detects existing hook frameworks (husky, pre-commit, lefthook) and warns

**Dashboard warnings**:
- Projects with recorded activity but no JACKED_GUARDRAILS.md show "No Guardrails" badge
- Projects without our pre-push hook show "No Lint Hook" badge
- One-click setup from dashboard creates guardrails and/or installs hooks

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Hook not running your code changes | Check `~/.claude/settings.json` hook path -- may point to stale uv/pip install instead of current env |
| "jacked: command not found" | Run `uv tool update-shell` and restart terminal |
| Dangerous permission wildcards | Run `jacked permissions audit --fix` to find and prune them |
| Service will not start | Run `jacked service status`. It prints the last start failure with its reason. The statusline shows `jacked service down` while the service is not running. Correct the cause, then run `jacked service restart` |
| Upgrade left the service down | Read `~/.claude/jacked-update-failed.txt`. `jacked upgrade` is a transaction: it runs `jacked service preflight` before it replaces the old install, and it reinstalls the previous version if the new one cannot start. The file names both versions and the manual recovery commands |
| Is this build able to start? | Run `jacked service preflight`. Exit code 0 means the build can provision its service contract. Exit code 1 prints `[FAIL] <error type>: <reason>`. The command starts no process and changes no supervisor. Add `--json` for one machine-readable object |

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JACKED_HOST` / `JACKED_PORT` | 127.0.0.1 / 8321 | Dashboard/service bind address |

## Quick Commands

| Command | What It Does |
|---------|-------------|
| `/dc` | Double-check reviewer -- auto-detects phase (planning/implementation/post-implementation) |
| `/pr` | Pull request workflow -- checks status, creates/updates PRs |
| `/learn` | Distills a lesson from the current session into a CLAUDE.md rule |
| `/blindspot` | Pre-build discovery pass: surfaces unknown-unknowns, teaches you to prompt well, asks must-have questions |
| `/redo` | Scraps current approach, preserves work, re-implements with hindsight |
| `/techdebt` | Scans for TODOs, oversized files, missing tests, dead code |
| `/audit-rules` | Audits CLAUDE.md for duplicates, contradictions, stale rules |

## Smart Reviewers (10 Agents)

| Agent | Focus |
|-------|-------|
| double-check-reviewer | Security, auth, RBAC, org isolation, architecture |
| code-simplicity-reviewer | Over-engineering, unnecessary abstractions |
| defensive-error-handler | Missing error handling, potential crashes |
| test-coverage-engineer | Test gaps, coverage analysis |
| test-coverage-improver | Adds doctests and test files |
| git-pr-workflow-manager | Branch management, PR organization |
| pr-workflow-checker | PR status and lifecycle |
| issue-pr-coordinator | Issue grouping, PR-issue linking |
| readme-maintainer | README sync with code changes |
| wiki-documentation-architect | GitHub Wiki maintenance |
