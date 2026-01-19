# Jacked

Search and load context from past Claude Code sessions using semantic search.

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
2. Shows matching sessions with relevance scores
3. You pick which session to load context from
4. If the session exists locally → suggests native Claude resume
5. If remote only → retrieves and injects the transcript as context

## Instructions for Claude

When the user runs `/jacked <description>`, follow these steps:

### Step 1: Search for Similar Sessions

Run the search command:

```bash
jacked search "<user's description>" --limit 5
```

Parse the output to get the list of matching sessions.

### Step 2: Present Results to User

Show the user the matching sessions in a table format:
- Number (for selection)
- Relevance score (percentage)
- Date
- Repository name
- Preview of the conversation

Ask the user to select a session by number, or 'skip' to cancel.

### Step 3: Retrieve Selected Session

When the user selects a session:

```bash
jacked retrieve <session_id>
```

This will output:
- Session metadata (repo, machine, local status)
- If local: the `claude --resume` command
- The full transcript

### Step 4: Handle Based on Session Location

**If session is local:**
Tell the user:
```
This session exists locally on your machine!
To resume it natively (with full Claude memory), run in a new terminal:

claude --resume <session_id>

Or I can inject the transcript into our current conversation instead.
Would you like me to inject it here? (yes/no)
```

**If session is remote only:**
Tell the user:
```
This session is from another machine (<machine_name>).
I'll inject the transcript into our conversation so I have the context.
```

Then inject the formatted transcript.

### Step 5: Context Injection

When injecting context, format it clearly:

```
=== CONTEXT FROM PREVIOUS SESSION ===
Session: <session_id>
Repository: <repo_name>
Machine: <machine_name>
========================================

<transcript content>

========================================
=== END PREVIOUS SESSION CONTEXT ===
```

After injection, summarize what the previous session covered and ask what the user wants to work on.

## Error Handling

- If search returns no results: "No matching sessions found. Try a different description or run `jacked backfill` to index your sessions."
- If retrieve fails: "Session not found in index. It may have been deleted or the session ID is invalid."
- If jacked command not found: "jacked CLI not installed. Run `pipx install jacked` to install."

## Notes

- Sessions are indexed automatically via a Stop hook (after each Claude response)
- The index is stored in Qdrant Cloud, accessible from any machine
- Local sessions can be resumed natively with `claude --resume` for the best experience
- Remote sessions are retrieved and injected as context (works but Claude won't have internal memory state)
