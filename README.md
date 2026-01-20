# claude-jacked

Supercharge your Claude Code workflow with cross-machine session search, review agents, and workflow commands.

## Quick Install

**One-liner (Linux/macOS/Git Bash):**
```bash
curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/install.sh | bash
```

**Or manual two-step:**
```bash
pipx install claude-jacked && jacked install
```

Then set up Qdrant credentials (see [Qdrant Setup](#set-up-qdrant-cloud)) and run `jacked backfill`.

---

## Guided Install (Copy Into Claude Code)

> 📋 [View on GitHub](https://github.com/jackneil/claude-jacked#guided-install-copy-into-claude-code) for copy button

```
Install claude-jacked for me. First check what's already set up, then help me with anything missing:

DIAGNOSTIC PHASE (run these first to see current state):
- Detect my operating system
- Check if pipx is installed: pipx --version (or: python -m pipx --version)
- Check if jacked CLI is installed: jacked --version (or on Windows: where jacked)
- Check if Qdrant credentials are set in current shell: echo $QDRANT_CLAUDE_SESSIONS_ENDPOINT
- Check if hook is installed: look in ~/.claude/settings.json for "jacked index"
- If jacked exists and env vars visible: jacked status && jacked configure --show

WINDOWS EXTRA CHECK (Git Bash doesn't inherit Windows System Environment):
- If env vars NOT visible in bash, check Windows System Environment:
  powershell.exe -Command "[System.Environment]::GetEnvironmentVariable('QDRANT_CLAUDE_SESSIONS_ENDPOINT', 'Machine')"
  powershell.exe -Command "[System.Environment]::GetEnvironmentVariable('QDRANT_CLAUDE_SESSIONS_ENDPOINT', 'User')"
- If vars exist in Windows but not bash: they need to be added to ~/.bashrc

REPORT what's already configured vs what's missing before proceeding.

SETUP PHASE (only do steps that are missing):
1. If no Python 3.11+: help install miniconda
2. If no pipx: pip install pipx && pipx ensurepath
3. If jacked not installed: pipx install claude-jacked && jacked install
4. If no Qdrant credentials anywhere: walk me through cloud.qdrant.io setup
5. If env vars in Windows but not bash: add export lines to ~/.bashrc, then source it
6. If env vars missing entirely: help add to shell profile
7. If no indexed sessions: jacked backfill

VERIFY: jacked status && jacked configure --show

Ask if this is personal use or team setup.
If team: explain that everyone needs the same Qdrant cluster credentials.

WINDOWS NOTES:
- Claude Code uses Git Bash, which does NOT inherit Windows System Environment variables
- If you set env vars in Windows Settings, you ALSO need them in ~/.bashrc for Git Bash
- pipx installs jacked to: C:\Users\<user>\pipx\venvs\claude-jacked\Scripts\jacked.exe
- If "jacked" isn't found, find it with: where jacked OR ls /c/Users/$USER/pipx/venvs/claude-jacked/Scripts/
- In Git Bash, backslash paths get mangled. Use forward slashes: /c/Users/...
```

---

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

## Manual Install

### Step 1: Install the CLI

**Use pipx** (recommended - installs globally, always on PATH):

```bash
pipx install claude-jacked
```

Don't have pipx? `pip install pipx && pipx ensurepath`

**Why not regular pip?** If you `pip install` into a conda env or virtualenv, the `jacked` command only works when that env is active. Claude Code hooks run in a fresh shell without your env activated → `jacked: command not found`. pipx avoids this by installing to an isolated global location that's always on PATH.

### Step 2: Install Claude Code Integration

```bash
jacked install
```

This installs:
- All agents to `~/.claude/agents/`
- All commands to `~/.claude/commands/`
- The `/jacked` skill to `~/.claude/skills/`
- Auto-index hook (indexes sessions after every Claude response)

Restart Claude Code after running this.

### Step 3: Set Up Qdrant Cloud

The session search features require Qdrant Cloud for vector storage and embedding:

1. Sign up at [cloud.qdrant.io](https://cloud.qdrant.io) (requires paid tier ~$30/mo for server-side embedding)
2. Create a cluster and get your URL + API key
3. Add to your shell profile:

```bash
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="your-api-key"
```

### Step 4: Index Your Sessions

```bash
jacked backfill        # Index all existing sessions
jacked status          # Verify it's working
jacked search "something you worked on before"
```

**Note:** If you only want the agents and commands (not the session search), you can manually copy just those files from the repo without setting up Qdrant. But the main `jacked` functionality requires it.

---

## Uninstall

**One-liner:**
```bash
curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/uninstall.sh | bash
```

**Or manual two-step:**
```bash
jacked uninstall && pipx uninstall claude-jacked
```

This removes hooks, skill, agents, and commands from Claude Code. Your Qdrant index is preserved if you reinstall later.

---

## Team Setup

Share knowledge across your team by using the same Qdrant cluster.

### How It Works

1. **Everyone on the team** uses the same `QDRANT_CLAUDE_SESSIONS_ENDPOINT` and `QDRANT_CLAUDE_SESSIONS_API_KEY`
2. **Each person sets** their `JACKED_USER_NAME` to identify their sessions
3. **Search results** show who created each session (YOU vs @teammate)
4. **Ranking prioritizes** your own sessions, then teammates, with recency boost

### Team Environment Setup

```bash
# Everyone uses the same cluster
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://team-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="team-api-key"

# Each person sets their name
export JACKED_USER_NAME="sarah"  # or "mike", "jack", etc.
```

### Search Examples

```bash
jacked search "auth implementation"     # Ranked: your stuff first, then team
jacked search "auth" --mine             # Only your sessions
jacked search "auth" --user sarah       # Only Sarah's sessions
```

### Multi-Factor Ranking

Results are ranked by:
| Factor | Weight | Description |
|--------|--------|-------------|
| Semantic | Core | How well the query matches the session content |
| Ownership | 1.0 / 0.8 | Your sessions weighted higher than teammates |
| Repository | 1.0 / 0.7 | Current repo weighted higher than others |
| Recency | Decay | Recent sessions weighted higher (35-week half-life) |

Customize weights via environment variables:
```bash
export JACKED_TEAMMATE_WEIGHT=0.8        # Teammate session multiplier
export JACKED_OTHER_REPO_WEIGHT=0.7      # Other repo multiplier
export JACKED_TIME_DECAY_HALFLIFE_WEEKS=35  # Weeks until half relevance
```

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

**Note:** Searches all indexed sessions (yours + teammates if team setup). Results are ranked by: semantic match × ownership (yours first) × repo (current first) × recency. Use `--mine` for only your sessions.

---

## Jacked CLI Reference

### Commands

```bash
jacked search "query"              # Semantic search with multi-factor ranking
jacked search "query" --mine       # Only your sessions
jacked search "query" --user sarah # Only this teammate's sessions
jacked search "query" --repo path  # Boost results from this repo
jacked search "query" --type chunk # Search full transcript chunks only

jacked sessions                        # List indexed sessions
jacked sessions --repo myproject       # Filter by repo name

jacked retrieve <session_id>       # Smart mode: plan + summaries + labels
jacked retrieve <id> --mode full   # Get full transcript (huge)
jacked retrieve <id> --mode plan   # Just the plan file
jacked retrieve <id> --mode agents # Just subagent summaries
jacked retrieve <id> --mode labels # Just summary labels (tiny)

jacked index /path/to/session.jsonl --repo /path  # Index specific session
jacked backfill                    # Index all existing sessions
jacked backfill --force            # Re-index everything

jacked status                      # Check Qdrant connectivity
jacked delete <session_id>         # Remove session from index
jacked cleardb                     # Delete all YOUR indexed data (requires confirmation)
jacked install                     # Install hook + skill + agents + commands
jacked install --sounds            # Also install sound notification hooks
jacked uninstall                   # Remove hook + skill + agents + commands
jacked uninstall --sounds          # Remove only sound hooks
jacked configure                   # Show config help
jacked configure --show            # Show current config values
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

### Smart Retrieval (v0.2.6+)

Sessions are indexed with multiple content types for efficient retrieval:

| Content Type | What It Contains | Token Cost |
|--------------|------------------|------------|
| `plan` | Full implementation strategy from plan files | ~500-2K |
| `subagent_summary` | Rich summaries from exploration/planning agents | ~200-500 each |
| `summary_label` | Tiny chapter titles from auto-compaction | ~10-20 each |
| `user_message` | First 5 user messages for intent matching | ~100-500 each |
| `chunk` | Full transcript chunks (legacy) | ~2K each |

**Retrieval Modes:**

| Mode | What's Included | When to Use |
|------|-----------------|-------------|
| `smart` | Plan + agent summaries + labels + user msgs | Default - best balance (~5-10K tokens) |
| `plan` | Just the plan file | Quick strategic overview |
| `labels` | Just summary labels | Quick topic check (tiny) |
| `agents` | All subagent summaries | Deep dive into exploration results |
| `full` | Everything including transcript | Need full details (50-200K tokens - use sparingly!) |

**Why smart mode?** Full transcripts can be 50-200K tokens, which blows up your context window. Smart mode returns the highest-value content (~5-10K tokens) so you get the key decisions and plans without the bloat.

**Staleness warnings:** When loading old context, you'll see warnings based on age:
- 7-30 days: "Code may have changed since this session"
- 30-90 days: "Treat as starting point for WHERE to look, not WHAT to do"
- 90+ days: "Historical reference only - verify everything"

### Re-indexing After Upgrade

If you upgraded from a version before v0.2.6, your existing sessions are indexed as full transcript chunks only. To get smart retrieval:

```bash
jacked cleardb   # Wipes YOUR data (not teammates), requires typing "DELETE MY DATA"
jacked backfill  # Re-index with new content types
```

---

## Configuration

### Environment Variables

**Required:**
| Variable | Description |
|----------|-------------|
| `QDRANT_CLAUDE_SESSIONS_ENDPOINT` | Qdrant Cloud cluster URL |
| `QDRANT_CLAUDE_SESSIONS_API_KEY` | Qdrant API key |

**Identity (for team sharing):**
| Variable | Default | Description |
|----------|---------|-------------|
| `JACKED_USER_NAME` | git user.name | Your name for session attribution |
| `SMART_FORK_MACHINE_NAME` | hostname | Override machine name |

**Ranking weights:**
| Variable | Default | Description |
|----------|---------|-------------|
| `JACKED_TEAMMATE_WEIGHT` | 0.8 | Multiplier for teammate sessions |
| `JACKED_OTHER_REPO_WEIGHT` | 0.7 | Multiplier for other repos |
| `JACKED_TIME_DECAY_HALFLIFE_WEEKS` | 35 | Weeks until session relevance halves |

**Other:**
| Variable | Default | Description |
|----------|---------|-------------|
| `QDRANT_CLAUDE_SESSIONS_COLLECTION` | `claude_sessions` | Collection name |
| `CLAUDE_PROJECTS_DIR` | `~/.claude/projects` | Claude projects directory |

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

### Sound Notifications

Add audio feedback when Claude needs attention or completes a task:

```bash
jacked install --sounds
```

This adds hooks that play:
- **Notification sound** when Claude requests user input
- **Completion sound** when Claude finishes a task

Works on Windows (PowerShell), macOS (afplay), Linux (paplay), and WSL. Falls back to terminal bell on unsupported systems.

To remove only sound hooks (keep everything else):
```bash
jacked uninstall --sounds
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

**Data isolation:** The `cleardb` command only deletes data belonging to the current user (based on `JACKED_USER_NAME`). Teammates' data is unaffected.

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

### Windows: Path Issues in Git Bash

Claude Code uses Git Bash on Windows, which mangles backslash paths. When you run `C:\Users\jack\.local\bin\jacked.exe`, bash turns it into garbage like `C:Usersjack.localbinjacked.exe`.

**Where pipx installs jacked on Windows:**
```
C:\Users\<your-username>\pipx\venvs\claude-jacked\Scripts\jacked.exe
```

**Solutions:**

1. **Use cmd.exe wrapper** (most reliable in Git Bash):
   ```bash
   cmd.exe /c "C:\Users\jack\pipx\venvs\claude-jacked\Scripts\jacked.exe status"
   ```

2. **Add Scripts folder to PATH** (one-time fix):
   Add `C:\Users\<you>\pipx\venvs\claude-jacked\Scripts` to your Windows PATH environment variable.

3. **Use forward slashes** (sometimes works):
   ```bash
   /c/Users/jack/pipx/venvs/claude-jacked/Scripts/jacked.exe status
   ```

**Finding where jacked is installed:**
```cmd
where jacked
```
or
```cmd
dir C:\Users\%USERNAME%\pipx\venvs\claude-jacked\Scripts\jacked.exe
```

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
