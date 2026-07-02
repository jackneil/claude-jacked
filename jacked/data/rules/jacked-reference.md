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
| `~/.claude/commands/*.md` | 6 quick commands (/dc, /pr, /learn, /redo, /techdebt, /audit-rules) |
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
jacked install [--sounds] [--force]                 # Install skills, agents, commands, hooks
jacked uninstall [--sounds]                         # Remove from Claude Code
jacked permissions audit [--fix] [--yes]            # Audit permission rules for dangerous wildcards
jacked check-version                                # Check for newer PyPI version
jacked webux                                        # Launch the web dashboard
jacked service start                                # Start the tray service (menu-bar pill on macOS)
jacked init [--repo PATH] [--language LANG]          # Set up guardrails + lint hook in project
jacked guardrails init [--repo PATH] [--force]       # Create JACKED_GUARDRAILS.md from templates
jacked lint-hook init [--repo PATH] [--force]        # Install pre-push lint hook in .git/hooks/
python -m jacked                                    # Alternative invocation
```

Retired in 0.70.0: the security gatekeeper (`jacked gatekeeper *`, superseded by
Claude Code's native auto permission mode) and Qdrant session search
(`jacked search/backfill/status/configure`, the `/jacked` skill, the `[search]`
extra). `jacked install` prunes their hooks from settings.json automatically.

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
