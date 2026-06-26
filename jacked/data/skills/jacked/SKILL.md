---
name: jacked
description: Use when the user references past work — "how did I do/fix X before", "where did I debug Y", "what was that command/approach", "what did I work on yesterday/this week", "search my history", mentions a past project like "configurator", asks to continue/resume previous work, references past sessions, or starts work on a feature that may have been done before. Searches and loads context from past Claude Code sessions.
---

# Jacked

Search and load context from past Claude Code sessions using semantic search.

## Prerequisites

This skill requires the search extra (`jacked[search]`) and a configured Qdrant instance. **Before doing anything else**, check if search is available:

```bash
jacked search "test" --limit 1
```

If this returns an error like `'search' requires the search extra` or fails to connect to Qdrant, **STOP** and tell the user:

> "The /jacked session search feature isn't set up yet. It requires `uv tool install "claude-jacked[search]"` and a Qdrant instance. For now, I can check git history or local session files instead."

Do NOT attempt to install the search extra automatically.

## Usage

```
/jacked <description of what you want to work on>
```

Example:
```
/jacked implement overnight OB time handling
```

## How It Works

1. Takes your description and searches for similar past sessions
2. Shows matching sessions with relevance scores and content indicators
3. You pick which session(s) to load context from
4. Uses **smart mode** by default - loads plan files, agent summaries, and key user messages (NOT the full transcript which would blow up context)
5. If the session exists locally, suggests native Claude resume as an option

## Instructions for Claude

When the user runs `/jacked <description>`, follow these steps:

### Step 0: Classify the Query

Before searching, decide which kind of recall this is — it changes the path:

- **Topic** ("how did we fix the auth bug", "where did I debug the K8s OOM", "what was that migration approach") → the default flow below (Steps 1–5).
- **Temporal / digest** ("what did I do yesterday", "what was I working on this week", "give me standup notes") → jump to the **Temporal / Digest Queries** section; it lists recent sessions by age instead of forcing a topic match.
- **Hybrid** ("yesterday's auth work") → run the default topic flow but bias toward the most recent matches, reading the Age column.

### Step 1: Search for Similar Sessions

**Freshness first (avoid silent misses):** the index is updated by a Stop hook *after* each response, so the current in-flight session and very-recently-finished work may not be searchable yet. If the user is asking about something from this session or the last few minutes, index the current session before searching:

```bash
jacked index   # indexes the current session (uses CLAUDE_SESSION_ID); no-op if already current
```

Then run the search:

```bash
jacked search "<user's description>" --limit 5
```

If a just-discussed item still doesn't appear in the results, say so and fall back to the local transcript or `git log`/`git diff` for the current repo rather than reporting "nothing found" — a miss here usually means "not yet indexed," not "never happened."

The output includes:
- Relevance score (percentage)
- User (YOU or @username for teammates)
- Age (relative time like "24 days ago")
- Repository name
- Content indicators: 📋 = has plan file, 🤖 = has agent summaries
- Preview of matched content

**Drop /jacked recall sessions from the results.** Because every session is indexed by the Stop hook, prior `/jacked` runs get indexed too — so a search can surface your *own past recall sessions* instead of real work. Before presenting results, discard (or, if nothing else matches, visibly de-prioritize and label) any hit whose preview is dominated by `jacked search` / `jacked retrieve` output, "CONTEXT FROM PREVIOUS SESSION" injection blocks, or otherwise reads as a session that was itself searching history rather than doing the work. The picker should show real work, not echoes of earlier recalls.

### Step 2: Present Results Using AskUserQuestion

Use the AskUserQuestion tool with multiSelect=true to let the user pick which sessions to load:

```json
{
  "questions": [{
    "question": "Which sessions would you like to load context from?",
    "header": "Sessions",
    "multiSelect": true,
    "options": [
      {
        "label": "1. YOU - 24d ago 📋🤖",
        "description": "hank-coder: Implementing overnight time calculation..."
      },
      {
        "label": "2. @bob - 3mo ago 🤖",
        "description": "hank-coder: Time handling refactor for multiple..."
      },
      {
        "label": "3. YOU - 2d ago",
        "description": "krac-llm: Staff time merging edge cases..."
      },
      {
        "label": "None - skip",
        "description": "Don't load any previous context"
      }
    ]
  }]
}
```

Note: AskUserQuestion supports max 4 options, so if there are 5+ results, show top 3 + "None" option.

### Step 3: Retrieve Selected Sessions

When the user selects one or more sessions:

```bash
# Default: smart mode (recommended) - plan + summaries + labels + user msgs
jacked retrieve <session_id> --mode smart

# For full transcript (use sparingly - can be 50K+ chars)
jacked retrieve <session_id> --mode full
```

Smart mode retrieval output includes:
- Session metadata with relative age
- Token budget accounting
- Plan file content (if exists)
- Subagent summaries (exploration/planning results)
- Summary labels (chapter titles)
- First few user messages

**Surface "files touched" and "key commands run" as a compact recall block.** These are the cheapest, highest-signal artifacts for recreating a past solution without re-reading the whole transcript — the core "no more re-solving" payoff. From the retrieved content, extract the files Read/Write/Edit'd and the key Bash commands run (in smart mode they're referenced in the plan, summaries, and user messages; if the user explicitly wants to reproduce a fix and they aren't visible, escalate that one session to `--mode full` and pull the actual tool calls from the transcript). When present, **lead your post-injection summary with them**:

```
Files touched: src/api/auth.py, tests/test_auth.py, migrations/004_add_token.sql
Key commands: alembic upgrade head · pytest tests/test_auth.py -k token · ruff check src/api
```

Then follow with the narrative summary (what the session covered, key decisions).

### Step 4: Handle Based on Session Location

**If session is local:**
Tell the user:
```
This session exists locally on your machine!
To resume it natively (with full Claude memory), run in a new terminal:

claude --resume <session_id>

Or I can inject the smart context into our current conversation.
Would you like me to inject it here? (yes/no)
```

**If session is remote only:**
Tell the user:
```
This session is from another machine (<machine_name>).
I'll inject the context into our conversation.
```

### Step 5: Context Injection with Staleness Warning

The retrieve output already includes proper formatting with staleness warnings.

**For context older than 7 days, include the staleness warning** that appears in the output:
- 7-30 days: Mild warning - "Code may have changed"
- 30-90 days: Medium warning - "Use as starting point for WHERE to look"
- 90+ days: Strong warning - "Treat as historical reference only"

After injection, summarize:
1. What the previous session covered
2. Key decisions or implementations found
3. Ask what the user wants to work on now

## Temporal / Digest Queries

When the user asks what they *did* in a time window rather than about a topic ("what did I do yesterday", "what was I working on this week", "give me standup notes"), don't force a topic match — produce a recency-ordered digest instead.

1. Run a broad listing scoped to the user (and the current repo when the question is repo-specific):

   ```bash
   jacked search "<broad term, e.g. the repo or current focus area>" --mine --limit 15
   ```

   The result table already carries the Age column ("today", "yesterday", "3d ago", "2w ago"). Use it to keep only sessions inside the asked-for window, and drop /jacked recall sessions (see Step 1) so the digest is real work.

2. Order the survivors newest-first and summarize each as one compact line:

   ```
   • yesterday — hank-coder (feat/auth) — files: auth.py, test_auth.py — added refresh-token rotation, fixed 401 retry
   • yesterday — krac-llm (main) — files: scheduler.py — debugged overnight OB time merge
   ```

   Pull repo, branch (when known), files touched, and the key decision from each session's plan/summaries — `jacked retrieve <id> --mode labels` or `--mode plan` is cheap when you need a one-line gist per session. Don't dump full transcripts for a digest.

3. Close by asking whether they want to dive into any one of those sessions (then fall through to Step 3 retrieval for the chosen one).

## Retrieval Modes

| Mode | What's Included | When to Use |
|------|-----------------|-------------|
| smart | Plan + agent summaries + labels + user msgs | Default - best balance |
| plan | Just the plan file | Quick strategic overview |
| labels | Just summary labels (tiny) | Quick topic check |
| agents | All subagent summaries | Deep dive into exploration results |
| full | Everything including transcript | Need full details (use sparingly) |

## Error Handling

- If search returns no results: "No matching sessions found. Try a different description or run `jacked backfill` to index your sessions."
- If retrieve fails: "Session not found in index. It may have been deleted or the session ID is invalid."
- If jacked command not found: "jacked not installed or not on PATH. Run `uv tool install claude-jacked` to install."

## Notes

- Sessions are indexed automatically via a Stop hook (after each Claude response)
- Indexes plan files, subagent summaries, and summary labels for smarter retrieval
- Smart mode prevents context explosion by returning ~5-10K tokens instead of 50-200K
- The index is stored in Qdrant Cloud, accessible from any machine
- Local sessions can be resumed natively with `claude --resume` for the best experience
- Remote sessions are retrieved and injected as context (works but Claude won't have internal memory state)
- Use `jacked cleardb` to wipe your data before re-indexing with a new schema
- **Skip sub-agent sidecar transcripts.** If you ever enumerate local session files directly (the git/local fallback above, or a `recover`-style scan), ignore files named `agent-*.jsonl` — those are sub-agent sidecars, not standalone sessions, and their content is already folded into the parent session's summaries. Surfacing them as their own results is noise. (`backfill` already excludes them from the index.)

## Artifact Format Preference

When you write any artifact during a `/jacked`-driven follow-up — a continuation plan, a research note distilled from past sessions, an exported summary — write it as **HTML**, not Markdown. The file is for the user to open in a browser, scan diagrams, and re-read later. Copy `~/.claude/jacked-templates/plan-template.html` as a starting point.

Markdown is reserved for the explicit exceptions: GitHub-rendered files (`README.md`, `CONTRIBUTING.md`, `CHANGELOG.md`, `LICENSE.md`, files under `_wiki/`) and Claude-instruction files Claude reads at session boot (`CLAUDE.md`, `AGENTS.md`, `lessons.md`, `MEMORY.md`). See `~/.claude/jacked-reference.md` § Artifact Format Preference for the canonical rule.
