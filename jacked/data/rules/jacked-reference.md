# Jacked Reference (for Claude Code)

This file gives you deep knowledge about the jacked toolkit installed on this system.
Read this when the user asks about jacked features, installation, gatekeeper, logs, or troubleshooting.

## What Jacked Is

- Toolkit for Claude Code: smart reviewers, quick commands, session search, security gatekeeper
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
| `~/.claude/skills/jacked/SKILL.md` | /jacked session search skill |
| `~/.claude/hooks-debug.log` | Security gatekeeper decision log |
| `~/.claude/gatekeeper-prompt.txt` | Custom gatekeeper LLM prompt (optional, user-created) |
| `~/.claude/jacked-guardrails/*.md` | Guardrails templates (base + 4 languages) |
| `~/.claude/jacked-hooks/*.sh` | Git hook templates (installed extensionless) |
| `<project>/JACKED_GUARDRAILS.md` | Per-project coding standards (created by `jacked guardrails init`) |

## CLI Commands

```
jacked install [--security] [--sounds] [--force]   # Install agents, commands, hooks
jacked uninstall [--security] [--sounds]            # Remove from Claude Code
jacked gatekeeper show                              # Print current LLM prompt
jacked gatekeeper diff                              # Compare custom vs built-in prompt
jacked gatekeeper reset                             # Reset prompt to built-in default
jacked gatekeeper audit [--log] [-n COUNT]          # Audit permission rules + recent approvals
jacked search "query" [--mine] [--user NAME]        # Search past sessions (requires [search])
jacked backfill [--force]                           # Index existing sessions (requires [search])
jacked status                                       # Check Qdrant connectivity (requires [search])
jacked check-version                              # Check for newer PyPI version
jacked configure --show                             # Show current configuration
jacked init [--repo PATH] [--language LANG]          # Set up guardrails + lint hook in project
jacked guardrails init [--repo PATH] [--force]       # Create JACKED_GUARDRAILS.md from templates
jacked lint-hook init [--repo PATH] [--force]        # Install pre-push lint hook in .git/hooks/
python -m jacked                                    # Alternative invocation
```

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
- Projects with gatekeeper activity but no JACKED_GUARDRAILS.md show "No Guardrails" badge
- Projects without our pre-push hook show "No Lint Hook" badge
- One-click setup from dashboard creates guardrails and/or installs hooks

## Security Gatekeeper (requires [security] extra)

4-tier evaluation chain for every Bash command Claude runs:

1. **Deny patterns** (<1ms) -- Blocks: sudo, rm -rf, disk wipe, reverse shells (bash/sh/zsh -i /dev/tcp), perl/ruby -e, psql/mysql/mongo DROP/TRUNCATE, base64 decode, chmod 777, kill -9 1, crontab manipulation, sensitive file access (.ssh, .aws, .kube, .gnupg, /etc/passwd|shadow|sudoers)
2. **Permission rules** (<1ms) -- Checks commands already approved in settings.json permission patterns
3. **Local allowlist** (<1ms) -- Matches specific safe subcommands (24 git subcommands, specific gh/docker/make targets, pytest, linting tools, etc.) with shell operator detection
4. **LLM evaluation** (~2-10s) -- Sends ambiguous commands to Haiku with file context analysis, returns JSON with reason

**Shell operator detection:** Commands containing `&`, `;`, `|`, `` ` ``, `$()`, `>`, `<`, or newlines are flagged as compound. For `&&` and `||` specifically: if ALL sub-commands are individually safe (match safe prefixes/patterns), the compound is auto-approved locally. If any sub-command is ambiguous or denied, the whole thing goes to LLM evaluation. Pipes, semicolons, backticks, and lone `&` always go to LLM.

**File context analysis:** When a command references a Python, SQL, or shell script, the gatekeeper reads the file contents and includes them in the LLM prompt. Defenses include:
- Path traversal prevention (files must be within the working directory)
- Boundary marker sanitization (prevents prompt injection via crafted files)
- Untrusted data warning in the LLM prompt

**Session ID tagging:** Every log line is prefixed with the first 8 chars of the Claude session ID (e.g., `[a1b2c3d4]`) so you can track which session triggered which decision when running multiple Claude instances.

## Log Interpretation

The gatekeeper logs to `~/.claude/hooks-debug.log`. Key log patterns:

| Log Entry | Meaning |
|-----------|---------|
| `LOCAL SAID: YES` | Matched safe prefix, auto-approved instantly (<1ms) |
| `PERMS MATCH` | Matched a permission rule in settings.json |
| `DENY MATCH` | Hit a deny pattern, Claude will ask the user |
| `CLAUDE-API SAID: {...}` | LLM evaluated via Anthropic API (~2s) |
| `CLAUDE-LOCAL SAID: {...}` | LLM evaluated via Claude CLI fallback (~8s) |
| `DECISION: ALLOW - reason` | Auto-approved with LLM's reasoning |
| `DECISION: ASK USER - reason` | Flagged with LLM's reasoning, user prompted |

**Example log session:**
```
2025-02-07T11:36:34 [87fd8847] EVALUATING: ls -la /tmp
2025-02-07T11:36:34 [87fd8847] LOCAL SAID: YES (0.001s)
2025-02-07T11:36:34 [87fd8847] DECISION: ALLOW (0.001s)
2025-02-07T11:37:00 [87fd8847] EVALUATING: rm c:/Github/project/old_file.py
2025-02-07T11:37:10 [87fd8847] CLAUDE-LOCAL SAID: {"safe": false, "reason": "rm in project directory"} (10.3s)
2025-02-07T11:37:10 [87fd8847] DECISION: ASK USER - rm in project directory (10.3s)
```

## Install / Uninstall Details

- `jacked install --security` adds a PreToolUse hook to `~/.claude/settings.json` pointing to the gatekeeper script
- The hook command is: `{python_exe} {path_to_security_gatekeeper.py}` with a 30-second timeout
- `jacked uninstall --security` removes the hook entry and cleans up stale/default prompt files
- Custom gatekeeper prompts (genuinely modified by the user) are preserved across uninstall and upgrade
- Custom prompts must contain `{command}`, `{cwd}`, and `{file_context}` placeholders or they will be treated as stale

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "WARNING: Custom prompt missing required placeholders" | Stale prompt file. Run `jacked gatekeeper reset` |
| No DECISION logged after WARNING | Old gatekeeper version. Run `jacked uninstall --security && jacked install --security` |
| Hook not running your code changes | Check `~/.claude/settings.json` hook path -- may point to stale uv/pip install instead of current env |
| Commands taking 8-10s instead of 2s | Set `ANTHROPIC_API_KEY` for direct API access instead of CLI fallback |
| "jacked: command not found" | Run `uv tool update-shell` and restart terminal |
| Too many permission prompts | Safe commands should be auto-approved. Check gatekeeper log for what's hitting LLM tier |
| Permission wildcards bypassing gatekeeper | Run `jacked gatekeeper audit --log` to find dangerous patterns |

## Environment Variables

| Variable | Default | Purpose |
|----------|---------|---------|
| `JACKED_HOOK_DEBUG` | (unset) | Set to `1` for verbose gatekeeper logging |
| `ANTHROPIC_API_KEY` | (unset) | Enables fast (~2s) API-based LLM evaluation |
| `QDRANT_CLAUDE_SESSIONS_ENDPOINT` | (required for search) | Qdrant Cloud URL |
| `QDRANT_CLAUDE_SESSIONS_API_KEY` | (required for search) | Qdrant Cloud API key |
| `JACKED_USER_NAME` | git username | Your name for team session attribution |
| `JACKED_TEAMMATE_WEIGHT` | 0.8 | Relevance weight for teammate search results |
| `JACKED_OTHER_REPO_WEIGHT` | 0.7 | Relevance weight for other-repo results |
| `JACKED_TIME_DECAY_HALFLIFE_WEEKS` | 35 | How fast old sessions lose search relevance |

## Quick Commands

| Command | What It Does |
|---------|-------------|
| `/dc` | Double-check reviewer -- auto-detects phase (planning/implementation/post-implementation) |
| `/pr` | Pull request workflow -- checks status, creates/updates PRs |
| `/learn` | Distills a lesson from the current session into a CLAUDE.md rule |
| `/redo` | Scraps current approach, preserves work, re-implements with hindsight |
| `/techdebt` | Scans for TODOs, oversized files, missing tests, dead code |
| `/audit-rules` | Audits CLAUDE.md for duplicates, contradictions, stale rules |
| `/jacked <query>` | Searches past Claude sessions by semantic similarity |

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
