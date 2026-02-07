# claude-jacked

**Smart reviewers, quick commands, and session search for Claude Code.** Catch bugs before they ship, search past conversations, and auto-approve safe commands — all from within Claude Code.

---

## What You Get

- **Catch mistakes before they ship** — Built-in reviewers check for security issues, complexity, and common bugs.
- **Quick commands** — `/dc`, `/pr`, `/learn`, `/redo`, `/techdebt`, `/audit-rules` for common workflows.
- **Find past solutions instantly** — Search past Claude sessions by meaning, not keywords. *(requires [search] extra)*
- **Work from any computer** — Start on your desktop, continue on your laptop. *(requires [search] extra)*
- **Auto-approve safe commands** — Security gatekeeper evaluates bash commands so you only get interrupted for risky ones. *(requires [security] extra)*
- **Sound notifications** — Get audio alerts when Claude needs your attention or finishes a task.

---

## Table of Contents

- [Quick Start](#quick-start)
- [What's Included](#whats-included)
- [Using the Session Search](#using-the-session-search)
- [Working with Your Team](#working-with-your-team)
- [Built-in Reviewers and Commands](#built-in-reviewers-and-commands)
- [Security Gatekeeper](#security-gatekeeper)
- [Sound Notifications](#sound-notifications)
- [Uninstall](#uninstall)
- [Common Issues](#common-issues)
- [Version History](#version-history)
- [Advanced / Technical Reference](#advanced--technical-reference)

---

## Quick Start

### Option 1: Let Claude Install It

Copy this into Claude Code and it will walk you through the options:

```
Install claude-jacked for me. Use AskUserQuestion to ask me which features I want:

1. First check if pipx and jacked are already installed
2. Ask me which install tier I want:
   - BASE: Smart reviewers, commands (/dc, /pr, /learn, etc.), behavioral rules
   - SEARCH: Everything above + session search across machines (requires Qdrant Cloud ~$30/mo)
   - SECURITY: Everything above + auto-approve safe bash commands (fewer permission prompts)
   - ALL: Everything
3. Install based on my choice:
   - BASE: pipx install claude-jacked && jacked install --force
   - SEARCH: pipx install "claude-jacked[search]" && jacked install --force
   - SECURITY: pipx install "claude-jacked[security]" && jacked install --force --security
   - ALL: pipx install "claude-jacked[all]" && jacked install --force --security
4. If I chose SEARCH or ALL, help me set up Qdrant Cloud credentials
5. Verify with: jacked --help
6. If I chose SECURITY or ALL, show me how to monitor the gatekeeper log:
   - Mac/Linux: tail -f ~/.claude/hooks-debug.log
   - Windows PowerShell: Get-Content ~\.claude\hooks-debug.log -Wait -Tail 20
   - Windows Git Bash: tail -f ~/.claude/hooks-debug.log
```

### Option 2: Manual Install

**Core (reviewers, commands, behavioral rules):**
```bash
pipx install claude-jacked
jacked install --force
```

**Add session search (optional):**
```bash
pipx install "claude-jacked[search]"
jacked install --force
# Then set up Qdrant Cloud credentials (see below)
```

**Add security gatekeeper (optional):**
```bash
pipx install "claude-jacked[security]"
jacked install --force --security
```

**Everything:**
```bash
pipx install "claude-jacked[all]"
jacked install --force --security
```

---

## What's Included

### Base (`pip install claude-jacked`)

| Feature | What It Does |
|---------|--------------|
| **10 Smart Reviewers** | AI assistants that check your code for bugs, security issues, and complexity |
| **Quick Commands** | `/dc`, `/pr`, `/learn`, `/redo`, `/techdebt`, `/audit-rules` |
| **Behavioral Rules** | Auto-triggers for jacked commands, lesson tracking, plan-first workflow |
| **Sound Notifications** | Audio alerts when Claude needs input or finishes (via `--sounds`) |

### Search Extra (`pip install "claude-jacked[search]"`)

| Feature | What It Does |
|---------|--------------|
| **Session Search** | Find any past Claude conversation by describing what you were working on |
| **Cross-Machine Sync** | Start on desktop, continue on laptop — your history follows you |
| **Team Sharing** | Search your teammates' sessions (with their permission) |

### Security Extra (`pip install "claude-jacked[security]"`)

| Feature | What It Does |
|---------|--------------|
| **Security Gatekeeper** | Auto-approves safe bash commands, blocks dangerous ones, asks you about ambiguous ones |
| **Shell Injection Defense** | Detects shell operators (`&&`, `\|`, `;`, `>`, `` ` ``, `$()`) to prevent command chaining bypasses |
| **File Context Analysis** | Reads referenced scripts and evaluates what code actually does, with prompt injection and path traversal protection |
| **Customizable Prompt** | Tune the LLM's safety evaluation via `~/.claude/gatekeeper-prompt.txt` |
| **Permission Audit** | Scans your permission rules for dangerous wildcards that bypass the gatekeeper |
| **Session-Tagged Logs** | Every log line tagged with session ID so you can track decisions across multiple Claude sessions |
| **Log Redaction** | Passwords, API keys, and tokens are automatically redacted from debug logs |

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
| `/pr` | **Pull Request** — Checks PR status, creates/updates PRs with proper issue linking |
| `/learn` | **Learn** — Distills a lesson from the current session into a CLAUDE.md rule |
| `/redo` | **Redo** — Scraps the current approach and re-implements cleanly with full hindsight |
| `/techdebt` | **Tech Debt** — Scans for TODOs, oversized files, missing tests, dead code |
| `/audit-rules` | **Audit Rules** — Checks CLAUDE.md for duplicates, contradictions, stale rules |

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

## Security Gatekeeper

The security gatekeeper is a PreToolUse hook that intercepts every bash command Claude runs and decides whether to auto-approve it or ask you first.

### How It Works

A 4-tier evaluation chain, fastest first:

| Tier | Speed | What It Does |
|------|-------|--------------|
| **Deny patterns** | <1ms | Blocks dangerous commands (sudo, rm -rf, disk wipe, reverse shells, perl/ruby -e, database DROP, etc.) |
| **Permission rules** | <1ms | Checks commands already approved in your Claude settings |
| **Local allowlist** | <1ms | Matches safe patterns (specific git/gh/docker/make subcommands, pytest, linting, etc.) with shell operator detection |
| **LLM evaluation** | ~2s | Sends ambiguous commands to Haiku with file context analysis and prompt injection defenses |

About 90% of commands resolve in under 2 milliseconds. The LLM tier reads the contents of referenced Python/SQL/shell scripts and evaluates what the code actually does, with path traversal protection and boundary marker sanitization to prevent prompt injection via crafted files.

**Shell operator detection:** Commands containing `&&`, `||`, `;`, `|`, `` ` ``, `$()`, `>`, `>>`, `<`, or newlines are never auto-approved by the local allowlist — they always go to the LLM for evaluation, even if the first command matches a safe prefix. This prevents attacks like `git status && curl evil.com`.

**Tightened safe prefixes:** Instead of blanket `"git "`, the allowlist matches specific subcommands (`git status`, `git diff`, `git log`, etc.). `npx` was removed entirely (downloads and executes arbitrary code). `gh`, `docker compose`, and `make` are restricted to specific safe subcommands.

### Install / Uninstall

The security gatekeeper is opt-in. To enable it:

```bash
pip install "claude-jacked[security]"
jacked install --force --security
```

To remove just the security hook:
```bash
jacked uninstall --security
```

### Debug Logging

The security gatekeeper logs every decision to `~/.claude/hooks-debug.log`. Each line is tagged with a session ID prefix so you can track which Claude session triggered each evaluation.

**Example log output:**
```
2025-02-07T11:36:34 [87fd8847] EVALUATING: ls -la /tmp
2025-02-07T11:36:34 [87fd8847] LOCAL SAID: YES (0.001s)
2025-02-07T11:36:34 [87fd8847] DECISION: ALLOW (0.001s)
2025-02-07T11:36:50 [87fd8847] EVALUATING: curl --version
2025-02-07T11:36:58 [87fd8847] CLAUDE-LOCAL SAID: {"safe": true, "reason": "read-only version check"} (9.0s)
2025-02-07T11:36:58 [87fd8847] DECISION: ALLOW - read-only version check (9.0s)
2025-02-07T11:37:15 [87fd8847] EVALUATING: sudo whoami
2025-02-07T11:37:15 [87fd8847] DENY MATCH (0.001s)
2025-02-07T11:37:15 [87fd8847] DECISION: ASK USER (0.001s)
```

The session ID tag (`[87fd8847]`) is the first 8 characters of the Claude Code session ID — useful when running multiple Claude sessions simultaneously. LLM evaluations include a brief reason explaining why the command was approved or flagged.

**Live monitoring (watch decisions in real-time):**

Mac/Linux:
```bash
tail -f ~/.claude/hooks-debug.log
```

Windows (PowerShell):
```powershell
Get-Content ~\.claude\hooks-debug.log -Wait -Tail 20
```

Windows (Git Bash):
```bash
tail -f ~/.claude/hooks-debug.log
```

**Read full log:**
```bash
cat ~/.claude/hooks-debug.log
```

**Verbose debug mode** (logs extra detail about each evaluation tier):
```bash
export JACKED_HOOK_DEBUG=1
```

### Faster LLM Evaluation

If you have an Anthropic API key, the gatekeeper uses the SDK directly (~2s) instead of the CLI fallback (~8s):

```bash
pip install anthropic               # or: pip install claude-jacked[security]
export ANTHROPIC_API_KEY="sk-..."
```

### Customize the Gatekeeper Prompt

The gatekeeper uses an LLM prompt to evaluate ambiguous commands. The built-in prompt is used by default — create a custom prompt file only if you want to change the evaluation behavior:

```bash
# View the current prompt
jacked gatekeeper show

# Create a custom prompt file (copies built-in as starting point)
jacked gatekeeper show > ~/.claude/gatekeeper-prompt.txt

# Compare your changes against the built-in default
jacked gatekeeper diff

# Reset to built-in default (deletes custom file)
jacked gatekeeper reset
```

Your custom prompt must include these placeholders: `{command}`, `{cwd}`, `{file_context}`. The gatekeeper will fall back to the built-in prompt if placeholders are missing. Custom prompts are preserved across upgrades and uninstalls — only default or stale prompt files are cleaned up automatically.

### Permission Rule Audit

If you've set broad permission wildcards in Claude Code (like `Bash(python:*)` or `Bash(curl:*)`), those commands bypass the gatekeeper entirely — no LLM evaluation, no safety check. The audit command catches this:

```bash
# Scan your permission rules for dangerous wildcards
jacked gatekeeper audit

# Also send recent auto-approved commands to the LLM for review
jacked gatekeeper audit --log

# Scan more entries (default: 50)
jacked gatekeeper audit --log -n 100
```

The static audit also runs automatically when you `jacked install --security`. Every 100 permission auto-approvals, the gatekeeper logs a reminder to run the audit.

### Log Redaction

The gatekeeper automatically redacts sensitive data from `~/.claude/hooks-debug.log`:

- Connection strings (`postgresql://user:***@host`)
- Environment variables (`PGPASSWORD=***`, `ANTHROPIC_API_KEY=***`)
- CLI flags (`--password ***`, `--token ***`)
- Bearer tokens, AWS access keys, and `sk-...` API keys

No configuration needed — this happens automatically.

---

## Sound Notifications

Get audio alerts so you don't have to watch the terminal:

```bash
jacked install --force --sounds
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

> **This is only needed if you installed the `[search]` extra.** The base install works fine without Qdrant.

The session search feature stores your conversations in a cloud database so you can access them from any computer.

### Why Qdrant?

- **Smart search** — Find sessions by meaning, not just keywords
- **Works everywhere** — Access from any computer
- **Team sharing** — Everyone can search the same database
- **You control it** — Your data stays in your own database

### Setting Up Qdrant Cloud

1. Install the search extra: `pip install "claude-jacked[search]"`
2. Go to [cloud.qdrant.io](https://cloud.qdrant.io) and create an account
3. Create a new cluster (the paid tier ~$30/month is required for the search features)
4. Copy your cluster URL and API key
5. Add them to your shell profile:

**Mac/Linux** — Add to `~/.bashrc` or `~/.zshrc`:
```bash
export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://your-cluster.qdrant.io"
export QDRANT_CLAUDE_SESSIONS_API_KEY="your-api-key"
```

**Windows** — Add to your environment variables, or add to `~/.bashrc` in Git Bash.

6. Restart your terminal and run:
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

The CLI can be invoked as `jacked` or `python -m jacked`.

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
jacked install --force              # Install agents, commands, rules
jacked install --force --search    # Also add session indexing hook
jacked install --force --security  # Also add security gatekeeper hook
jacked install --force --sounds    # Also add sound notifications
jacked uninstall                   # Remove from Claude Code
jacked uninstall --sounds          # Remove only sounds
jacked uninstall --security        # Remove only security hook
jacked backfill                    # Index all existing sessions (requires [search])
jacked backfill --force            # Re-index everything
jacked status                      # Check connectivity (requires [search])
jacked configure --show            # Show current config

# Security Gatekeeper
jacked gatekeeper show             # Print current LLM prompt
jacked gatekeeper reset            # Reset prompt to built-in default
jacked gatekeeper diff             # Compare custom vs built-in prompt
jacked gatekeeper audit            # Audit permission rules for dangerous wildcards
jacked gatekeeper audit --log      # Also scan recent auto-approved commands via LLM
jacked gatekeeper audit --log -n 100  # Scan last 100 entries
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
| `JACKED_HOOK_DEBUG` | (unset) | Set to `1` for verbose security hook logging |
| `ANTHROPIC_API_KEY` | (unset) | Enables fast (~2s) LLM evaluation in security hook |

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

The `jacked install` command adds hooks to `~/.claude/settings.json` based on installed extras:

```json
// With [search] extra installed:
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "jacked index --repo \"$CLAUDE_PROJECT_DIR\"",
        "async": true
      }]
    }]
  }
}

// With --security flag:
{
  "hooks": {
    "PreToolUse": [{
      "matcher": "Bash",
      "hooks": [{
        "type": "command",
        "command": "python /path/to/security_gatekeeper.py",
        "timeout": 30
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
Install claude-jacked for me. Use the AskUserQuestion tool to guide me through options.

PHASE 1 - DIAGNOSTICS:
- Detect OS (Windows/Mac/Linux)
- Check: pipx --version (if missing: pip install pipx && pipx ensurepath)
- Check: jacked --version (to see if already installed)
- Check ~/.claude/settings.json for existing hooks

PHASE 2 - ASK USER PREFERENCES:
Use AskUserQuestion with these options:

Question: "Which jacked features do you want?"
Options:
- BASE (Recommended): Smart reviewers (/dc, /pr, /learn, /redo, /techdebt), 10 agents, behavioral rules. No external services needed.
- SEARCH: Everything in BASE + search past Claude sessions across machines. Requires Qdrant Cloud (~$30/mo).
- SECURITY: Everything in BASE + auto-approve safe bash commands. Fewer permission prompts, uses Anthropic API.
- ALL: Everything. Requires Qdrant Cloud + Anthropic API key for fastest security evaluation.

PHASE 3 - INSTALL:
Based on user choice:
- BASE: pipx install claude-jacked && jacked install --force
- SEARCH: pipx install "claude-jacked[search]" && jacked install --force
- SECURITY: pipx install "claude-jacked[security]" && jacked install --force --security
- ALL: pipx install "claude-jacked[all]" && jacked install --force --security

PHASE 4 - POST-INSTALL (if SEARCH or ALL):
Help user set up Qdrant Cloud:
1. Go to cloud.qdrant.io, create account
2. Create cluster (paid tier required)
3. Copy endpoint URL and API key
4. Add to shell profile:
   export QDRANT_CLAUDE_SESSIONS_ENDPOINT="https://..."
   export QDRANT_CLAUDE_SESSIONS_API_KEY="..."
5. Restart terminal
6. Run: jacked backfill

PHASE 5 - VERIFY:
- jacked --help (should show all commands)
- jacked configure --show (if SEARCH installed)
- If SECURITY or ALL: show user how to monitor gatekeeper decisions:
  Mac/Linux: tail -f ~/.claude/hooks-debug.log
  Windows PowerShell: Get-Content ~\.claude\hooks-debug.log -Wait -Tail 20
  Windows Git Bash: tail -f ~/.claude/hooks-debug.log

WINDOWS NOTE: If env vars not visible in Git Bash, check Windows system env vars:
powershell.exe -Command "[System.Environment]::GetEnvironmentVariable('QDRANT_CLAUDE_SESSIONS_ENDPOINT', 'User')"
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

## Version History

| Version | Changes |
|---------|---------|
| **0.3.11** | Security hardening: shell operator detection (`&&\|\;><`), tightened safe prefixes (specific git/gh/docker/make subcommands, npx removed), expanded deny patterns (perl/ruby -e, database DROP, reverse shells), file context prompt injection defense, path traversal prevention, base64 decode bypass fix. Session ID tags in logs. LLM reason logging. Install/uninstall bug fixes (hook removal, custom prompt preservation). `python -m jacked` support. 375 tests. |
| **0.3.10** | Fix format string explosion (`_SafeFormatter` replaced with `_substitute_prompt()`), qdrant test skip fix. |
| **0.3.9** | Permission safety audit, README catchup. |
| **0.3.8** | Log redaction, psql deny patterns, customizable LLM prompt. |
| **0.3.7** | JSON LLM responses, `parse_llm_response()`, 148 unit tests. |

## License

MIT

## Credits

Built for [Claude Code](https://claude.ai/code) by Anthropic. Uses [Qdrant](https://qdrant.tech/) for search.
