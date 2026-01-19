# claude-jacked

A collection of agents, commands, skills, and tools to supercharge your Claude Code workflow.

```bash
pipx install claude-jacked
```

## What's In Here

| Component | Description |
|-----------|-------------|
| **jacked CLI** | Cross-machine semantic search for Claude Code sessions via Qdrant |
| **10 Agents** | Double-check reviewer, PR workflow, test coverage, code simplicity, and more |
| **2 Commands** | `/dc` (double-check), `/pr` (PR workflow) |
| **1 Skill** | `/jacked` for searching past sessions from within Claude |

## Why This Exists

Claude Code has a context problem:

1. **Sessions don't sync across machines** - Work on your desktop, can't resume on laptop
2. **Auto-compact destroys context** - Hit the limit and your carefully built context gets summarized into oblivion
3. **Finding past work is painful** - "How did I solve that auth bug last week?" means grep-ing through JSONL files

This repo addresses these problems:

- **jacked** indexes all your sessions to Qdrant Cloud for semantic search from anywhere
- **Agents** like `double-check-reviewer` catch mistakes before they ship
- **Commands** like `/dc` trigger comprehensive reviews at the right moments

The goal: never lose useful context, never repeat solved problems, catch issues early.

---

## Quick Start

### Install the CLI

**Use pipx** (recommended - installs globally, always on PATH):

```bash
pipx install claude-jacked
```

Don't have pipx? `pip install pipx && pipx ensurepath`

**Why not regular pip?** If you `pip install` into a conda env or virtualenv, the `jacked` command only works when that env is active. Claude Code hooks run in a fresh shell without your env activated → `jacked: command not found`. pipx avoids this by installing to an isolated global location that's always on PATH.

### Install Everything

```bash
jacked install
```

This copies all agents, commands, and skills to `~/.claude/` and adds the auto-index hook. Restart Claude Code after running this.

### (Optional) Set Up Cross-Machine Session Search

If you want semantic search across sessions from any machine, you need Qdrant Cloud:

1. Sign up at [cloud.qdrant.io](https://cloud.qdrant.io) (requires paid tier ~$30/mo for server-side embedding)
2. Create a cluster and get your URL + API key
3. Add to your shell profile:

```bash
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="your-api-key"
```

4. Index existing sessions:

```bash
jacked backfill
```

5. Verify:

```bash
jacked status
jacked search "something you worked on"
```

**Note:** The agents and commands work without Qdrant. Only the `/jacked` skill and CLI search features need it.

---

## Agents

Installed automatically by `jacked install` to `~/.claude/agents/`.

| Agent | What It Does |
|-------|--------------|
| `double-check-reviewer` | CTO/CSO-level review for security, auth gaps, data leaks |
| `code-simplicity-reviewer` | Reviews for over-engineering and unnecessary complexity |
| `defensive-error-handler` | Audits error handling and adds defensive patterns |
| `git-pr-workflow-manager` | Manages branches, commits, and PR organization |
| `pr-workflow-checker` | Checks PR status and handles PR lifecycle |
| `issue-pr-coordinator` | Scans issues, groups related ones, manages PR workflows |
| `test-coverage-engineer` | Analyzes and improves test coverage |
| `test-coverage-improver` | Adds doctests and test files systematically |
| `readme-maintainer` | Keeps README in sync with code changes |
| `wiki-documentation-architect` | Creates/maintains GitHub Wiki documentation |

### Usage

Claude automatically uses these agents when appropriate. You can also invoke them explicitly:

```
Use the double-check-reviewer agent to review what we just built
```

---

## Commands

Installed automatically by `jacked install` to `~/.claude/commands/`.

| Command | What It Does |
|---------|--------------|
| `/dc` | Triggers comprehensive double-check review (auto-detects if planning vs implementation) |
| `/pr` | Checks PR status, manages workflow for current branch |

### Usage

```
/dc          # Review current work
/pr          # Check PR status
```

---

## Skills

Installed automatically by `jacked install` to `~/.claude/skills/`.

| Skill | What It Does |
|-------|--------------|
| `/jacked` | Search past sessions and load context |

### Usage

```
/jacked implement user authentication
```

Claude searches your indexed sessions, shows matches, and lets you load relevant context.

---

## Jacked CLI Reference

### Commands

```bash
jacked search "query"              # Semantic search across sessions
jacked search "query" --repo path  # Filter by repo

jacked list                        # List indexed sessions
jacked list --repo myproject       # Filter by repo name

jacked retrieve <session_id>       # Get full transcript
jacked retrieve <id> --summary     # Get summary only

jacked index /path/to/session.jsonl --repo /path  # Index specific session
jacked backfill                    # Index all existing sessions
jacked backfill --force            # Re-index everything

jacked status                      # Check Qdrant connectivity
jacked delete <session_id>         # Remove session from index
jacked install                     # Install hook + skill
jacked configure                   # Show config help
```

### How It Works

```
┌─────────────────────────────────────────────────────────────┐
│  YOUR MACHINE                                               │
│                                                             │
│  Claude Code                                                │
│  ├── Stop hook → jacked index (after every response)        │
│  └── /jacked skill → search + load context                  │
│                                                             │
│  ~/.claude/projects/                                        │
│  └── {repo}/                                                │
│      └── {session}.jsonl  ←── parsed and indexed            │
└─────────────────────────────────────────────────────────────┘
                            │
                            │ HTTPS
                            ▼
┌─────────────────────────────────────────────────────────────┐
│  QDRANT CLOUD                                               │
│                                                             │
│  • Server-side embedding (no local ML needed)               │
│  • Vectors + full transcripts stored                        │
│  • Accessible from any machine                              │
└─────────────────────────────────────────────────────────────┘
```

---

## Configuration

### Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `QDRANT_CLAUDE_SESSIONS_ENDPOINT` | Yes | Qdrant Cloud cluster URL |
| `QDRANT_CLAUDE_SESSIONS_API_KEY` | Yes | Qdrant API key |
| `QDRANT_CLAUDE_SESSIONS_COLLECTION` | No | Collection name (default: `claude_sessions`) |
| `CLAUDE_PROJECTS_DIR` | No | Override Claude projects dir (default: `~/.claude/projects`) |
| `SMART_FORK_MACHINE_NAME` | No | Override machine name |

### Hook Configuration

The `jacked install` command adds this to `~/.claude/settings.json`:

```json
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "jacked index --repo \"$CLAUDE_PROJECT_DIR\""
      }]
    }]
  }
}
```

---

## Security Warning

**Jacked sends session data to Qdrant Cloud.** This includes:

- Full conversation transcripts
- Repo paths and machine names
- Anything you paste into sessions (including secrets)

Recommendations:
- Don't paste API keys/passwords in Claude sessions
- Keep your Qdrant API key secure
- Consider self-hosting Qdrant for sensitive work

---

## Troubleshooting

### "QDRANT_CLAUDE_SESSIONS_ENDPOINT not set"

Add to your shell profile (`~/.bashrc`, `~/.zshrc`, or PowerShell profile):

```bash
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="your-key"
```

### "No matching sessions found"

```bash
jacked backfill  # Index existing sessions first
jacked status    # Verify connectivity
```

### "jacked: command not found"

You probably installed with `pip` into a virtualenv/conda env that isn't active. Fix:

```bash
pipx install claude-jacked
```

This installs globally so the hook can find it regardless of which env is active.

### Agents not loading

Make sure files are in the right place:
- Global: `~/.claude/agents/`
- Project: `.claude/agents/` in your repo root

---

## Development

```bash
git clone https://github.com/jackneil/claude-jacked
cd claude-jacked
pip install -e ".[dev]"
pytest
```

---

## License

MIT

## Credits

Built for [Claude Code](https://claude.ai/code) by Anthropic.

Uses [Qdrant](https://qdrant.tech/) for vector search.
