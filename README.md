# claude-jacked

**Never lose your Claude Code work again.** Search past conversations, share solutions with your team, and get AI-powered code reviews—all from within Claude Code.

---

## What You Get

- **Find past solutions instantly** — "How did I fix that login bug last month?" Just ask, and get the answer.
- **Work from any computer** — Start on your desktop, continue on your laptop. Your history follows you.
- **Share knowledge with your team** — Your teammate already solved this problem. Find their solution in seconds.
- **Catch mistakes before they ship** — Built-in reviewers check for security issues, complexity, and common bugs.
- **Sound notifications** — Get audio alerts when Claude needs your attention or finishes a task.

---

## Table of Contents

- [Quick Start](#quick-start)
- [What's Included](#whats-included)
- [Using the Session Search](#using-the-session-search)
- [Working with Your Team](#working-with-your-team)
- [Built-in Reviewers and Commands](#built-in-reviewers-and-commands)
- [Sound Notifications](#sound-notifications)
- [Uninstall](#uninstall)
- [Common Issues](#common-issues)
- [Advanced / Technical Reference](#advanced--technical-reference)

---

## Quick Start

### Option 1: Let Claude Install It For You

Copy this into Claude Code and it will handle everything:

```
Install claude-jacked for me. Walk me through each step and help me set up Qdrant Cloud.
```

### Option 2: One-Line Install

**Mac/Linux:**
```bash
curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/install.sh | bash
```

**Windows (in Git Bash):**
```bash
curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/install.sh | bash
```

After installing, you'll need to set up a free cloud database (Qdrant) to store your session history. The installer will guide you through this, or ask Claude to help: `"Help me set up Qdrant Cloud for jacked"`

### Option 3: Manual Install

```bash
pipx install claude-jacked
jacked install
```

Then follow the [cloud database setup](#cloud-database-setup-qdrant) instructions below.

---

## What's Included

When you run `jacked install`, you get:

| Feature | What It Does |
|---------|--------------|
| **Session Search** | Find any past Claude conversation by describing what you were working on |
| **10 Smart Reviewers** | AI assistants that check your code for bugs, security issues, and complexity |
| **Quick Commands** | `/dc` for code review, `/pr` for pull request help |
| **Team Sharing** | Search your teammates' sessions (with their permission) |

---

## Using the Session Search

Once installed, you can search your past Claude sessions right from within Claude Code.

### Example: Finding Past Work

You're working on user authentication and remember solving something similar before:

```
/jacked user authentication login
```

Claude will show you matching sessions:

```
Search Results:
#  Score  User  Age      Repo           Preview
1  92%    YOU   3d ago   my-app         Implementing JWT auth with refresh tokens...
2  85%    YOU   2w ago   api-server     Adding password reset flow...
3  78%    @sam  1w ago   shared-lib     OAuth2 integration with Google...
```

Pick one to load that context into your current session.

### Example: Resuming Work from Another Computer

You started building a feature on your desktop. Now you're on your laptop:

```
/jacked that shopping cart feature I was building
```

Claude finds it and you can continue right where you left off.

### Example: Learning from Teammates

Your teammate Sam already built something similar:

```
/jacked how did Sam implement the payment system
```

You can see Sam's approach without bothering them.

---

## Working with Your Team

Share knowledge across your team by using the same cloud database.

### Setting Up Team Sharing

1. **One person** creates a Qdrant Cloud account and shares the credentials
2. **Everyone** adds the same credentials to their computer
3. **Each person** sets their name so sessions are attributed correctly

```bash
# Everyone uses the same database
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://team-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="team-api-key"

# Each person sets their own name
export JACKED_USER_NAME="sarah"
```

### Searching Team Sessions

```
/jacked payment processing           # Shows your work first, then teammates
/jacked payment processing --mine    # Only your sessions
/jacked payment processing --user sam   # Only Sam's sessions
```

---

## Built-in Reviewers and Commands

### Quick Commands

Type these directly in Claude Code:

| Command | What It Does |
|---------|--------------|
| `/dc` | **Double-check** — Reviews your recent work for bugs, security issues, and problems |
| `/pr` | **Pull Request** — Helps organize your changes and create a clean PR |

### Smart Reviewers

These work automatically when Claude thinks they'd help, or you can ask for them:

| Reviewer | What It Catches |
|----------|-----------------|
| **Double-check** | Security holes, authentication gaps, data leaks |
| **Code Simplicity** | Over-complicated code, unnecessary abstractions |
| **Error Handler** | Missing error handling, potential crashes |
| **Test Coverage** | Untested code, missing edge cases |

**Example:** After building a new feature:
```
Use the double-check reviewer to review what we just built
```

---

## Sound Notifications

Get audio alerts so you don't have to watch the terminal:

```bash
jacked install --sounds
```

- **Notification sound** — Plays when Claude needs your input
- **Completion sound** — Plays when Claude finishes a task

Works on Windows, Mac, and Linux. To remove sounds later:
```bash
jacked uninstall --sounds
```

---

## Uninstall

**Remove everything:**
```bash
jacked uninstall && pipx uninstall claude-jacked
```

**Or one-liner:**
```bash
curl -sSL https://raw.githubusercontent.com/jackneil/claude-jacked/master/uninstall.sh | bash
```

Your cloud database stays intact, so you won't lose your history if you reinstall later.

---

## Common Issues

### "I installed it but search isn't working"

You need to set up the cloud database first. Ask Claude:
```
Help me set up Qdrant Cloud for jacked
```

### "It says 'jacked: command not found'"

The install didn't add jacked to your PATH. Try:
```bash
pipx ensurepath
```
Then restart your terminal.

### "My sessions aren't showing up in search"

Run this to index your existing sessions:
```bash
jacked backfill
```

### "I'm on Windows and getting weird errors"

Claude Code on Windows uses Git Bash, which can have path issues. Ask Claude:
```
Help me fix jacked path issues on Windows
```

---

## Cloud Database Setup (Qdrant)

The session search feature stores your conversations in a cloud database so you can access them from any computer.

### Why Qdrant?

- **Smart search** — Find sessions by meaning, not just keywords
- **Works everywhere** — Access from any computer
- **Team sharing** — Everyone can search the same database
- **You control it** — Your data stays in your own database

### Setting Up Qdrant Cloud

1. Go to [cloud.qdrant.io](https://cloud.qdrant.io) and create an account
2. Create a new cluster (the paid tier ~$30/month is required for the search features)
3. Copy your cluster URL and API key
4. Add them to your shell profile:

**Mac/Linux** — Add to `~/.bashrc` or `~/.zshrc`:
```bash
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="your-api-key"
```

**Windows** — Add to your environment variables, or add to `~/.bashrc` in Git Bash.

5. Restart your terminal and run:
```bash
jacked backfill    # Index your existing sessions
jacked status      # Verify it's working
```

---

## Security Note

**Your conversations are sent to Qdrant Cloud.** This includes:
- Everything you and Claude discuss
- Code snippets you share
- File paths on your computer

**Recommendations:**
- Don't paste passwords or API keys in Claude sessions
- Keep your Qdrant API key private
- For sensitive work, consider self-hosting Qdrant

---

## Advanced / Technical Reference

<details>
<summary><strong>CLI Command Reference</strong></summary>

```bash
# Search
jacked search "query"              # Search all sessions
jacked search "query" --mine       # Only your sessions
jacked search "query" --user name  # Specific teammate
jacked search "query" --repo path  # Boost specific repo

# Session Management
jacked sessions                    # List indexed sessions
jacked retrieve <session_id>       # Get session content
jacked retrieve <id> --mode full   # Get full transcript
jacked delete <session_id>         # Remove from index
jacked cleardb                     # Delete all your data

# Setup
jacked install                     # Install hooks, agents, commands
jacked install --sounds            # Also add sound notifications
jacked uninstall                   # Remove from Claude Code
jacked uninstall --sounds          # Remove only sounds
jacked backfill                    # Index all existing sessions
jacked backfill --force            # Re-index everything
jacked status                      # Check connectivity
jacked configure --show            # Show current config
```

</details>

<details>
<summary><strong>Environment Variables</strong></summary>

**Required:**
| Variable | Description |
|----------|-------------|
| `QDRANT_CLAUDE_SESSIONS_ENDPOINT` | Your Qdrant Cloud URL |
| `QDRANT_CLAUDE_SESSIONS_API_KEY` | Your Qdrant API key |

**Optional:**
| Variable | Default | Description |
|----------|---------|-------------|
| `JACKED_USER_NAME` | git username | Your name for team attribution |
| `JACKED_TEAMMATE_WEIGHT` | 0.8 | How much to weight teammate results |
| `JACKED_OTHER_REPO_WEIGHT` | 0.7 | How much to weight other repos |
| `JACKED_TIME_DECAY_HALFLIFE_WEEKS` | 35 | How fast old sessions lose relevance |

</details>

<details>
<summary><strong>How It Works (Technical)</strong></summary>

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
│  • Vectors + transcripts stored                             │
│  • Accessible from any machine                              │
└─────────────────────────────────────────────────────────────┘
```

**Indexing:** After each Claude response, a hook automatically indexes the session to Qdrant. The indexer extracts:
- Plan files (implementation strategies)
- Agent summaries (exploration results)
- Summary labels (chapter titles from auto-compaction)
- User messages (for intent matching)

**Retrieval modes:**
- `smart` (default): Plan + summaries + labels (~5-10K tokens)
- `full`: Complete transcript (50-200K tokens)
- `plan`: Just the plan file
- `agents`: Just agent summaries
- `labels`: Just summary labels (tiny)

</details>

<details>
<summary><strong>All Agents</strong></summary>

| Agent | Description |
|-------|-------------|
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

</details>

<details>
<summary><strong>Hook Configuration</strong></summary>

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

</details>

<details>
<summary><strong>Guided Install Prompt (for Claude)</strong></summary>

Copy this into Claude Code for a guided installation:

```
Install claude-jacked for me. First check what's already set up, then help me with anything missing:

DIAGNOSTIC PHASE (run these first to see current state):
- Detect my operating system
- Check if pipx is installed: pipx --version
- Check if jacked CLI is installed: jacked --version
- Check if Qdrant credentials are set: echo $QDRANT_CLAUDE_SESSIONS_ENDPOINT
- Check if hook is installed: look in ~/.claude/settings.json for "jacked index"

WINDOWS EXTRA CHECK (Git Bash doesn't inherit Windows System Environment):
- If env vars NOT visible in bash, check Windows:
  powershell.exe -Command "[System.Environment]::GetEnvironmentVariable('QDRANT_CLAUDE_SESSIONS_ENDPOINT', 'User')"

SETUP PHASE (only do steps that are missing):
1. If no pipx: pip install pipx && pipx ensurepath
2. If jacked not installed: pipx install claude-jacked && jacked install
3. If no Qdrant credentials: walk me through cloud.qdrant.io setup
4. If no indexed sessions: jacked backfill

VERIFY: jacked status && jacked configure --show
```

</details>

<details>
<summary><strong>Windows Troubleshooting</strong></summary>

Claude Code uses Git Bash on Windows, which can cause path issues.

**Where jacked is installed:**
```
C:\Users\<username>\pipx\venvs\claude-jacked\Scripts\jacked.exe
```

**If "jacked" isn't found:**
```bash
# Find it
where jacked

# Or add to PATH
pipx ensurepath
```

**If paths are getting mangled:**
Use forward slashes in Git Bash:
```bash
/c/Users/jack/pipx/venvs/claude-jacked/Scripts/jacked.exe status
```

</details>

---

## License

MIT

## Credits

Built for [Claude Code](https://claude.ai/code) by Anthropic. Uses [Qdrant](https://qdrant.tech/) for search.
