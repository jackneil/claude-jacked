# Session Indexing Improvements - Design Document

## Status: Design Complete, Ready for Implementation

**Last Updated**: 2026-01-19

---

## Migration Strategy

**IMPORTANT**: When implementing this new schema, DO NOT try to migrate existing data in-place.

**Simple approach:**
1. Run `jacked cleardb` to wipe YOUR data (only your user_name, not teammates')
2. Run `jacked backfill` to re-index everything with the new schema

**Why this is fine:**
- cleardb only deletes data for the current user_name
- Teammates' data is untouched
- backfill re-reads from local session files (still exist on disk)
- Clean slate avoids schema mismatch headaches

**Point ID change:**
- Old: UUID5 hash of `{session_id}_intent_{i}`
- New: UUID5 hash of `{session_id}:{content_type}:{i}`
- These are incompatible - backfill handles this by deleting old points first

---

## Security Considerations

### Config File Permissions

The config file at `~/.claude/jacked.json` contains sensitive data (API keys).

**File permissions (Unix/Mac):**
```bash
chmod 600 ~/.claude/jacked.json  # Owner read/write only
```

**Windows:**
Config file inherits user directory permissions which is typically fine.

**Best practices:**
1. Never commit `.claude/` directory to git
2. Add `.claude/` to global gitignore: `git config --global core.excludesfile ~/.gitignore`
3. Consider using environment variables for CI/CD instead of config file

**On install, show warning:**
```
⚠️  Config file contains your API key. Ensure ~/.claude/jacked.json
    is not accessible by other users (chmod 600 on Unix).
```

### Input Validation

All search queries are passed to Qdrant's text embedding endpoint. Sanitization:
- Max query length: 2000 chars (truncate silently)
- Strip control characters
- Qdrant handles the embedding - no SQL injection risk

---

## Problem Statement

Current jacked indexing has two major issues:
1. **Context window explosion** - Retrieving full transcripts (50K-200K chars) blows up context
2. **Missing gold data** - We're ignoring Claude's compaction summaries which are perfect for search

## Discovery: Claude Code Session JSONL Structure

### Message Types Found

| Type | Description | Currently Indexed | Should Index |
|------|-------------|-------------------|--------------|
| `user` | User messages | ✅ Yes | ✅ Yes (for intent) |
| `assistant` | Claude responses | ✅ Yes (full) | ⚠️ Maybe trim |
| `summary` | **Compaction summaries** | ❌ NO | ✅ **YES - PRIMARY** |
| `file-history-snapshot` | File backup tracking | ❌ No | ❌ No |
| `system` | Metrics (turn_duration, etc) | ❌ No | ❌ No |
| `progress` | Hook progress events | ❌ No | ❌ No |

### The Summary Labels (Not Full Summaries!)

Claude Code stores compaction **labels** in session JSONL files:

```json
{"type":"summary","summary":"Backfill Command Deduplication Logic Check","leafUuid":"aa638c9a-..."}
{"type":"summary","summary":"Smart install prompt with Windows env var handling","leafUuid":"cf910ea9-..."}
{"type":"summary","summary":"Anesthesia Billing: Units & Conversion Factors","leafUuid":"56037c44-..."}
```

**IMPORTANT DISCOVERY**: These are just **chapter titles**, NOT the full compacted context!

Each summary label:
- Is 3-10 words (tiny - ~50 bytes)
- Acts as a "chapter title" for a chunk of work
- Links to a specific message via `leafUuid`
- Does NOT contain the full context Claude retains after compaction

**What's NOT stored:**
- The full summary text Claude generates during `/compact`
- The actual context Claude uses after compaction
- Any detailed description of what was accomplished

The full compacted summary that Claude uses internally is NOT persisted to disk - it's only in Claude's context window. Feature request exists (GitHub #6907) but not implemented.

A single session can have 20-50+ of these labels representing different work chunks.

### Real Examples from Production Sessions

**From claude-jacked project:**
```
"Backfill Command Deduplication Logic Check"
"Backfill deduplication and Windows executable path issues"
"Smart install prompt with Windows env var handling"
"Improve Install Prompt & Handle Windows Git Bash Env Vars"
```

**From other projects:**
```
"Avid Asset Purchase and Sale Deal Summary"
"Anesthesia Billing: Units & Conversion Factors"
"JSON to CSV Extraction Tool Implementation"
```

## Current Architecture Problems

### 1. Indexing (what we store)
- **Current**: Chunks full transcript at 4KB, stores ~25-50 chunks per session
- **Problem**: Lots of noise, tool outputs, XML tags, etc.
- **Better**: Store summaries as primary index (1 vector per summary)

### 2. Retrieval (what we return)
- **Current**: `jacked retrieve` returns ENTIRE transcript
- **Problem**: 50K-200K chars = 12K-50K tokens = context explosion
- **Better**: Return summaries, optionally expand specific sections

### 3. Search (how we find)
- **Current**: Semantic search on user messages (intent_text)
- **Problem**: User messages can be terse, miss context
- **Better**: Search summaries - they're already distilled semantic descriptions

## Major Discovery: Subagent Summaries Are Gold!

While the main session only has tiny summary labels, **agent outputs are stored separately** and contain rich, detailed summaries!

### Where Agent Results Live

```
~/.claude/projects/{repo}/
├── {session-id}.jsonl           # Main session (contains "slug" field)
└── {session-id}/
    └── subagents/
        ├── agent-a4e75d5.jsonl  # Explore agent
        ├── agent-ab8625e.jsonl  # Plan agent
        └── ...
```

## Major Discovery #2: Plan Files Are Gold Too!

Plan files contain the full implementation strategy - problems, solutions, files to modify, steps, verification.

### Where Plan Files Live

```
~/.claude/plans/
├── {slug}.md                    # Main session's plan
├── {slug}-agent-{id}.md         # Subagent plans
└── ...

Example:
├── hidden-finding-goose.md                # Main plan
├── hidden-finding-goose-agent-ab3ea81.md  # Agent's plan
```

### How to Link Plans to Sessions

The session JSONL contains a `slug` field that matches the plan filename:

```json
// In session JSONL:
{"type":"user","slug":"hidden-finding-goose",...}
```

```python
def extract_session_slug(session_path: Path) -> str | None:
    """Extract the slug from a session JSONL file."""
    with open(session_path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                data = json.loads(line)
                if "slug" in data:
                    return data["slug"]
            except json.JSONDecodeError:
                continue
    return None

def find_plan_file(slug: str) -> Path | None:
    """Find the plan file for a session slug."""
    plans_dir = Path.home() / ".claude" / "plans"
    plan_path = plans_dir / f"{slug}.md"
    if plan_path.exists():
        return plan_path
    return None
```

### What Plan Files Contain

```markdown
# Feature Name: Problem + Solution

## Problems Being Fixed
1. **Issue 1** - Description
2. **Issue 2** - Description

## Solution
### Approach 1
- Before/after diagrams
- Code examples

## Files to Modify
- file1.py - what changes
- file2.py - what changes

## Implementation Steps
1. Step one
2. Step two
...

## Verification
How to verify it works
```

These are:
- **Structured** - Clear sections for problem/solution/steps
- **Detailed** - Full reasoning and approach
- **Linkable** - Via slug to session
- **Standalone** - Don't need session context to understand

### What Agent Summaries Contain

Example from an Explore agent's final output:

```markdown
## Summary Report: Claude-Jacked Codebase Exploration

Based on my thorough exploration of the repository, here's what I found:

### 1. **What is "Jacked"?**
Claude-Jacked is a sophisticated cross-machine semantic search system...

### 2. **Recent Changes (Modified Files)**
Three files have been modified in the current branch:
a) jacked/cli.py (major change) - Added sysconfig import...
```

These are:
- **Detailed and comprehensive** (500-2000+ words)
- **Structured** with clear sections
- **Generated by Claude** (semantically rich)
- **Already stored** - no extra API calls needed!

## New Architecture: Type-Tagged Indexing

Index different content types with metadata for filtered search/retrieval:

### Content Types to Index

| Type | Source | Size | Use Case |
|------|--------|------|----------|
| `plan` | `~/.claude/plans/{slug}.md` | 500-5000 chars | **GOLD**: Full implementation strategy, why + how |
| `subagent_summary` | `subagents/*.jsonl` final text | 500-2000 chars | **GOLD**: What was explored/done |
| `summary_label` | Main session `type:summary` | 20-100 chars | Quick topic matching |
| `user_message` | Main session `type:user` | Variable | Intent matching, what user asked for |
| `assistant_response` | Main session `type:assistant` | Variable | What Claude said/did (optional) |

### Metadata Schema

Each vector point stores:
```python
{
    "session_id": "abc123",
    "repo_name": "claude-jacked",
    "repo_id": "hash",
    "machine": "DESKTOP-ABC",
    "user_name": "jack",
    "timestamp": "2026-01-19T12:00:00Z",

    # NEW: Content type for filtering
    "content_type": "subagent_summary",  # or "summary_label", "user_message", etc.

    # NEW: For subagents
    "agent_type": "Explore",  # or "Plan", "code-review", etc.
    "agent_id": "a4e75d5",

    # Existing
    "chunk_index": 0,
    "text": "..."
}
```

### Search Strategy

```python
# High-level "what was done" search
results = search(query, filter={"content_type": "subagent_summary"})

# Intent-focused search
results = search(query, filter={"content_type": "user_message"})

# Comprehensive search (default)
results = search(query, filter={"content_type": ["subagent_summary", "summary_label", "user_message"]})

# Agent-specific search
results = search(query, filter={"content_type": "subagent_summary", "agent_type": "Plan"})
```

### Retrieval Strategy

Smart retrieval based on content types:

```python
def retrieve_context(session_id: str, mode: str = "smart") -> str:
    """
    Modes:
    - "smart": plan + subagent summaries + labels + first 3 user messages (default)
    - "plan": just the plan file (if exists)
    - "labels": just summary labels (tiny)
    - "agents": all subagent summaries
    - "full": everything including transcript
    """
```

**Default "smart" mode returns:**
1. **Plan file** (if exists) - the implementation strategy (~500-5000 chars)
2. All subagent summaries for the session (~1-2K chars each)
3. Summary labels (chapter titles)
4. First 3 user messages (intent)

**Result: ~5-10K chars instead of 50-200K chars!**

### Token Budgeting & Smart Exclusion

Never truncate content mid-text. Instead, calculate token costs and exclude lower-priority items to stay under budget.

**Config file location:** `~/.claude/jacked.json`

```json
{
  "max_context_tokens": 15000,
  "default_retrieve_mode": "smart",
  "staleness_warning_days": 7,
  "qdrant_endpoint": "https://your-cluster.qdrant.io",
  "qdrant_api_key": "your-api-key",
  "user_name": "jack",
  "teammate_weight": 0.8,
  "other_repo_weight": 0.7,
  "time_decay_halflife_weeks": 35
}
```

**Interactive config command:** `jacked config`

Uses AskUserQuestion to interactively set config values:

```python
@main.command()
def config():
    """Interactively configure jacked settings."""

    # Load existing config or defaults
    config_path = Path.home() / ".claude" / "jacked.json"
    current = load_config(config_path)

    # The SKILL.md tells Claude to use AskUserQuestion like this:
    # When user runs `jacked config`, present options:

    questions = [
        {
            "question": "What's your max token budget for context injection?",
            "header": "Tokens",
            "multiSelect": False,
            "options": [
                {"label": "15,000 (default)", "description": "Good for most cases, ~3 sessions"},
                {"label": "30,000", "description": "Larger context, ~6 sessions"},
                {"label": "50,000", "description": "Very large, may slow responses"},
                {"label": "Custom", "description": "Enter a custom value"}
            ]
        },
        {
            "question": "Default retrieval mode?",
            "header": "Mode",
            "multiSelect": False,
            "options": [
                {"label": "smart (recommended)", "description": "Plan + summaries + labels + user msgs"},
                {"label": "plan", "description": "Just the plan file"},
                {"label": "agents", "description": "All subagent summaries"},
                {"label": "full", "description": "Everything (use sparingly)"}
            ]
        },
        {
            "question": "Show staleness warnings after how many days?",
            "header": "Staleness",
            "multiSelect": False,
            "options": [
                {"label": "7 days (default)", "description": "Warn for week-old+ context"},
                {"label": "14 days", "description": "More lenient"},
                {"label": "30 days", "description": "Only warn for month-old+"},
                {"label": "Never", "description": "No staleness warnings"}
            ]
        }
    ]

    # Save to ~/.claude/jacked.json
    save_config(config_path, answers)
```

**Config loading priority:**
```python
def load_config() -> JackedConfig:
    """Load config with priority: CLI flags > config file > env vars > defaults.

    Also migrates env vars to config file on first use.
    """
    config_path = Path.home() / ".claude" / "jacked.json"

    # 1. Start with defaults
    config = DEFAULT_CONFIG.copy()

    # 2. Load config file if exists
    file_config = {}
    if config_path.exists():
        with open(config_path) as f:
            file_config = json.load(f)
            config.update(file_config)

    # 3. Check env vars - migrate to config file if not already there
    migrated = False

    # Required: Qdrant credentials
    if "qdrant_endpoint" not in file_config:
        env_endpoint = os.getenv("QDRANT_CLAUDE_SESSIONS_ENDPOINT")
        if env_endpoint:
            config["qdrant_endpoint"] = env_endpoint
            file_config["qdrant_endpoint"] = env_endpoint
            migrated = True

    if "qdrant_api_key" not in file_config:
        env_key = os.getenv("QDRANT_CLAUDE_SESSIONS_API_KEY")
        if env_key:
            config["qdrant_api_key"] = env_key
            file_config["qdrant_api_key"] = env_key
            migrated = True

    # Optional settings - also migrate if in env but not in file
    env_mappings = {
        "JACKED_USER_NAME": "user_name",
        "JACKED_MAX_CONTEXT_TOKENS": ("max_context_tokens", int),
        "JACKED_TEAMMATE_WEIGHT": ("teammate_weight", float),
        "JACKED_OTHER_REPO_WEIGHT": ("other_repo_weight", float),
        "JACKED_TIME_DECAY_HALFLIFE_WEEKS": ("time_decay_halflife_weeks", int),
    }

    for env_var, mapping in env_mappings.items():
        if isinstance(mapping, tuple):
            key, converter = mapping
        else:
            key, converter = mapping, str

        if key not in file_config:
            env_val = os.getenv(env_var)
            if env_val:
                config[key] = converter(env_val)
                file_config[key] = converter(env_val)
                migrated = True

    # 4. Save migrated config
    if migrated:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w") as f:
            json.dump(file_config, f, indent=2)
        logger.info(f"Migrated env vars to {config_path}")

    # 5. CLI flags override at runtime (not persisted)
    return config
```

**Migration flow:**
```
First run with env vars:
┌─────────────────────────────────────────────────────────────────┐
│  ENV: QDRANT_CLAUDE_SESSIONS_ENDPOINT=https://xxx.qdrant.io     │
│  ENV: QDRANT_CLAUDE_SESSIONS_API_KEY=my-secret-key              │
│  ENV: JACKED_USER_NAME=jack                                     │
│                           │                                     │
│                           ▼                                     │
│  ~/.claude/jacked.json doesn't exist or missing these keys      │
│                           │                                     │
│                           ▼                                     │
│  Creates/updates ~/.claude/jacked.json:                         │
│  {                                                              │
│    "qdrant_endpoint": "https://xxx.qdrant.io",                  │
│    "qdrant_api_key": "my-secret-key",                           │
│    "user_name": "jack"                                          │
│  }                                                              │
│                           │                                     │
│                           ▼                                     │
│  Next run: loads from config file, env vars ignored             │
└─────────────────────────────────────────────────────────────────┘
```

**Benefits:**
- Backwards compatible with existing env var users
- Automatically migrates to config file
- Config file is the source of truth going forward
- User can delete env vars after migration if they want

**Show current config:** `jacked config --show`
**Reset to defaults:** `jacked config --reset`

**Token estimation:**
```python
def estimate_tokens(text: str) -> int:
    """Estimate token count. ~4 chars per token for English."""
    return len(text) // 4

def estimate_session_tokens(session_content: dict) -> dict:
    """Estimate tokens for each content type in a session.

    Returns:
        {
            "plan": 1200,
            "subagent_summaries": [800, 600, 450],
            "summary_labels": 150,
            "user_messages": 200,
            "total": 3400
        }
    """
```

**Priority order (highest to lowest):**
```
1. Plan file              - THE WHY (strategic, irreplaceable)
2. Subagent summaries     - THE WHAT (exploration results)
3. First 3 user messages  - THE ASK (original intent)
4. Summary labels         - THE TOPICS (can reconstruct from above)
5. Additional user msgs   - More context (usually redundant)
```

**Smart exclusion algorithm:**
```python
def fit_to_budget(
    sessions: list[SessionContent],
    max_tokens: int = 15000
) -> list[SessionContent]:
    """Fit multiple sessions into token budget without truncating text.

    Strategy:
    1. Always include plan + first subagent summary for each session
    2. Add remaining content by priority until budget hit
    3. If still over, reduce number of sessions (warn user)
    """

    # Phase 1: Calculate "must have" for each session
    must_have_per_session = []
    for session in sessions:
        must_have = {
            "plan": session.plan,  # Full plan, never truncate
            "subagent_summaries": session.subagent_summaries[:1],  # At least first
            "user_messages": session.user_messages[:1],  # At least first
        }
        must_have_per_session.append(must_have)

    # Phase 2: Calculate must-have total
    must_have_tokens = sum(estimate_tokens(s) for s in must_have_per_session)

    # Phase 3: If must-haves exceed budget, reduce session count
    if must_have_tokens > max_tokens:
        # Keep sessions in order (user selected them in priority order)
        while must_have_tokens > max_tokens and len(sessions) > 1:
            sessions = sessions[:-1]  # Drop last session
            must_have_per_session = must_have_per_session[:-1]
            must_have_tokens = sum(estimate_tokens(s) for s in must_have_per_session)

        warn(f"Reduced to {len(sessions)} session(s) to fit token budget")

    # Phase 4: Add optional content by priority until budget
    remaining_budget = max_tokens - must_have_tokens

    # Try to add: more subagent summaries, more user messages, labels
    for session in sessions:
        # Add remaining subagent summaries
        for summary in session.subagent_summaries[1:]:
            tokens = estimate_tokens(summary)
            if tokens <= remaining_budget:
                session.included_summaries.append(summary)
                remaining_budget -= tokens

        # Add summary labels (small, usually fit)
        labels_tokens = estimate_tokens(session.summary_labels)
        if labels_tokens <= remaining_budget:
            session.include_labels = True
            remaining_budget -= labels_tokens

        # Add more user messages
        for msg in session.user_messages[1:3]:
            tokens = estimate_tokens(msg)
            if tokens <= remaining_budget:
                session.included_user_msgs.append(msg)
                remaining_budget -= tokens

    return sessions
```

**Output includes token accounting:**
```markdown
=== CONTEXT FROM PREVIOUS SESSIONS ===
Token budget: 15,000 | Used: 12,450 | Remaining: 2,550

--- Session 1: abc123 (8,200 tokens) ---
Age: 24 days ago | Repo: hank-coder

⚠️ STALENESS NOTICE: ...

[PLAN - 3,100 tokens]
# OB Time Handling Implementation
...full plan text, never truncated...

[SUBAGENT SUMMARIES - 4,200 tokens]
## Summary Report: OB Time Implementation
...full summary, never truncated...

[SUMMARY LABELS - 150 tokens]
• "OB overnight time handling"
• "Time zone edge case fixes"

[USER MESSAGES - 750 tokens]
USER: "Help me implement overnight OB time handling..."

--- Session 2: def456 (4,250 tokens) ---
...

========================================
```

**CLI flag for budget:**
```bash
# Use default (15K tokens)
jacked retrieve abc123 --mode smart

# Override budget
jacked retrieve abc123 --mode smart --max-tokens 30000

# See what would be included without retrieving
jacked retrieve abc123 --mode smart --dry-run
```

### Multi-Session Selection

Users can select none, one, multiple, or all sessions from search results:

```
Search results:
  1. [85%] YOU - hank-coder - 24 days ago
  2. [72%] @bob - hank-coder - 3 months ago
  3. [68%] YOU - krac-llm - 2 days ago

Select sessions (e.g., "1", "1,3", "1-3", "all", or "skip"): 1,3
```

CLI supports:
- Single: `jacked retrieve abc123`
- Multiple: `jacked retrieve abc123 def456 ghi789`
- The skill handles parsing user input like "1,3" or "1-3" or "all"

### Relative Timestamps & Staleness Warnings

**Display relative time in search results:**
```python
def format_relative_time(timestamp: datetime) -> str:
    """Format timestamp as relative time.

    Examples:
        2 hours ago, 3 days ago, 2 weeks ago, 3 months ago
    """
    delta = datetime.now(timezone.utc) - timestamp

    if delta.days == 0:
        hours = delta.seconds // 3600
        if hours == 0:
            return "just now"
        return f"{hours} hour{'s' if hours != 1 else ''} ago"
    elif delta.days < 7:
        return f"{delta.days} day{'s' if delta.days != 1 else ''} ago"
    elif delta.days < 30:
        weeks = delta.days // 7
        return f"{weeks} week{'s' if weeks != 1 else ''} ago"
    elif delta.days < 365:
        months = delta.days // 30
        return f"{months} month{'s' if months != 1 else ''} ago"
    else:
        years = delta.days // 365
        return f"{years} year{'s' if years != 1 else ''} ago"
```

**Context injection with staleness warning:**

```markdown
=== CONTEXT FROM PREVIOUS SESSION ===
Session: abc123
Repository: hank-coder
Machine: DESKTOP-JACK
Age: 24 days ago

⚠️ STALENESS NOTICE: This context is from 24 days ago. Code, APIs, or
project structure may have changed since then. If you encounter
discrepancies, re-explore the relevant areas rather than assuming
this context is still accurate. Use this as a starting point for
WHERE to look, not necessarily WHAT is there now.
========================================

[context content here]

========================================
=== END PREVIOUS SESSION CONTEXT ===
```

**Staleness thresholds:**

| Age | Warning Level |
|-----|---------------|
| < 7 days | None - "Recent context" |
| 7-30 days | Mild - "This context is X days old, verify current state" |
| 30-90 days | Medium - "Context may be stale, re-explore if needed" |
| > 90 days | Strong - "Context is quite old, likely needs re-verification" |

```python
def get_staleness_warning(age_days: int) -> str:
    """Generate appropriate staleness warning based on age."""
    if age_days < 7:
        return ""  # No warning needed
    elif age_days < 30:
        return (
            f"ℹ️ This context is {age_days} days old. Code may have "
            "changed - verify current state if anything seems off."
        )
    elif age_days < 90:
        return (
            f"⚠️ STALENESS NOTICE: This context is {age_days} days old. "
            "Code, APIs, or project structure may have changed. Use this "
            "as a starting point for WHERE to look, not necessarily WHAT "
            "is there now."
        )
    else:
        return (
            f"🚨 OLD CONTEXT WARNING: This context is {age_days} days old "
            f"(~{age_days // 30} months). Significant changes are likely. "
            "Treat this as historical reference only - re-explore the "
            "codebase to understand current state before making changes."
        )
```

## Proposed Architecture

### Phase 1: Extract Subagent Summaries

#### Subagent JSONL Structure Analysis

Each subagent file contains these message types:
- `type: "assistant"` - Claude's responses (text blocks and tool_use blocks)
- `type: "user"` - Tool results returned to Claude
- `type: "progress"` - Hook progress events (ignore)

**Key finding**: The final summary is ALWAYS in the **last** `type: "assistant"` message, in `message.content[]` blocks where `type == "text"`.

#### Real Example (from agent-a4e75d5.jsonl, line 111):
```json
{
  "type": "assistant",
  "message": {
    "role": "assistant",
    "content": [
      {
        "type": "text",
        "text": "## Summary Report: Claude-Jacked Codebase Exploration\n\nBased on my thorough exploration...\n\n### 1. **What is \"Jacked\"?**\n..."
      }
    ]
  }
}
```

#### Extraction Algorithm

```python
# In transcript.py - new functions
def find_subagent_files(session_path: Path) -> list[Path]:
    """Find all subagent JSONL files for a session.

    Args:
        session_path: Path to the main session JSONL file

    Returns:
        List of paths to subagent JSONL files
    """
    session_dir = session_path.parent / session_path.stem  # e.g., {session-id}/
    subagents_dir = session_dir / "subagents"
    if subagents_dir.exists():
        return list(subagents_dir.glob("agent-*.jsonl"))
    return []


def extract_agent_summary(agent_file: Path) -> dict | None:
    """Extract the final summary text from an agent's session.

    The summary is the text content from the LAST assistant message.

    Args:
        agent_file: Path to an agent-*.jsonl file

    Returns:
        dict with keys: agent_id, agent_type, summary_text, timestamp
        or None if no summary found
    """
    # Extract agent_id from filename: agent-a4e75d5.jsonl -> a4e75d5
    agent_id = agent_file.stem.replace("agent-", "")

    last_assistant_msg = None
    agent_type = None
    timestamp = None

    with open(agent_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "assistant":
                last_assistant_msg = data
                timestamp = data.get("timestamp")

            # Try to detect agent type from the prompt or model
            # The parent session's Task tool call contains the subagent_type
            # but that's not in this file - may need to infer or skip

    if not last_assistant_msg:
        return None

    # Extract text content from the message
    message = last_assistant_msg.get("message", {})
    content = message.get("content", [])

    text_parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text", "")
            if text:
                text_parts.append(text)

    summary_text = "\n".join(text_parts).strip()

    # Skip if too short (likely not a real summary)
    if len(summary_text) < 200:
        return None

    return {
        "agent_id": agent_id,
        "agent_type": agent_type,  # May be None - hard to detect
        "summary_text": summary_text,
        "timestamp": timestamp,
    }
```

#### Agent Type Detection (Best Effort)

The agent type (Explore, Plan, code-review, etc.) is stored in the **parent session's** Task tool call, not in the subagent file itself. Options:

1. **Parse parent session** - Find the Task tool call with matching `agent_id`
2. **Infer from content** - Look for patterns like "## Summary Report", "## Plan:", etc.
3. **Skip it** - Store as `agent_type: null`, still useful for search

Recommendation: Option 3 for MVP. Agent type filtering is nice-to-have.

#### Summary Label Extraction (from main session)

Summary labels are stored directly in the main session JSONL:

```json
{"type":"summary","summary":"Backfill Command Deduplication Logic Check","leafUuid":"aa638c9a-..."}
```

```python
def extract_summary_labels(session_path: Path) -> list[dict]:
    """Extract summary labels from a session JSONL file.

    These are the tiny "chapter titles" from compaction events.

    Args:
        session_path: Path to the main session JSONL file

    Returns:
        List of dicts with keys: label, leafUuid, timestamp (if available)
    """
    labels = []

    with open(session_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            if data.get("type") == "summary":
                labels.append({
                    "label": data.get("summary", ""),
                    "leafUuid": data.get("leafUuid"),
                    "timestamp": data.get("timestamp"),
                })

    return labels
```

### Phase 2: Update Indexer for Content Types

```python
# In indexer.py - index with content_type metadata
def index_session(session_path: Path, repo_path: str):
    # 1. Index summary labels (content_type="summary_label")
    # 2. Index user messages (content_type="user_message")
    # 3. Index subagent summaries (content_type="subagent_summary")

    for point in points:
        point.payload["content_type"] = content_type
        point.payload["agent_type"] = agent_type  # if applicable
```

### Phase 3: Update Search with Filters

```python
# In searcher.py - add content_type filter
def search(
    query: str,
    content_types: list[str] = ["subagent_summary", "summary_label", "user_message"],
    agent_types: list[str] = None,  # Optional: filter by agent type
    ...
):
    filter_conditions = {
        "content_type": {"$in": content_types}
    }
    if agent_types:
        filter_conditions["agent_type"] = {"$in": agent_types}
```

### Phase 4: Smart Retrieval Modes

```python
# In retriever.py
def retrieve_context(session_id: str, mode: str = "smart") -> str:
    if mode == "smart":
        # Best balance of context vs size
        return subagent_summaries + first_3_user_messages + labels
    elif mode == "labels":
        # Tiny, just chapter titles
        return labels_only
    elif mode == "agents":
        # All agent outputs
        return all_subagent_summaries
    elif mode == "full":
        # Everything (use sparingly!)
        return full_transcript
```

### Phase 5: CLI Updates

```bash
# Search with filters
jacked search "auth implementation" --type subagent_summary
jacked search "auth" --type user_message,summary_label
jacked search "planning" --agent-type Plan

# Retrieve with modes
jacked retrieve <id> --mode smart    # Default
jacked retrieve <id> --mode labels   # Tiny
jacked retrieve <id> --mode agents   # Agent summaries only
jacked retrieve <id> --mode full     # Everything
```

### Phase 6: Update SKILL.md Instructions

Update the skill instructions to tell Claude:

**Search presentation:**
1. Show results with relative timestamps ("24 days ago" not "2025-12-26")
2. Allow flexible selection: "1", "1,3", "1-3", "all", or "skip"
3. Respect user's choice - if they say "skip", don't push

**Retrieval behavior:**
1. Use `--mode smart` by default (subagent summaries + labels + first 3 user msgs)
2. Only use `--mode full` if user specifically asks for full transcript
3. Can retrieve multiple sessions: `jacked retrieve id1 id2 id3`

**Context injection:**
1. Include staleness warning appropriate to age (see thresholds above)
2. Frame old context as "where to look" not "what is there"
3. After injection, summarize what was found and ask what user wants to work on

**Example updated SKILL.md flow:**
```markdown
### Step 2: Present Results Using AskUserQuestion

IMPORTANT: Use the AskUserQuestion tool with multiSelect=true to let the user
pick which sessions to load. This provides a better UX than free-text input.

Example AskUserQuestion call:
```json
{
  "questions": [{
    "question": "Which sessions would you like to load context from?",
    "header": "Sessions",
    "multiSelect": true,
    "options": [
      {
        "label": "1. YOU - 24 days ago",
        "description": "hank-coder: Implementing overnight time calculation..."
      },
      {
        "label": "2. @bob - 3 months ago",
        "description": "hank-coder: Time handling refactor for multiple..."
      },
      {
        "label": "3. YOU - 2 days ago",
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

Note: AskUserQuestion supports max 4 options, so if there are 5+ results,
show top 3 + "None" option. User can always ask for more results.

### Step 4: Inject with Staleness Warning

When injecting context older than 7 days, include appropriate warning:

=== CONTEXT FROM PREVIOUS SESSION ===
Session: abc123 | Repo: hank-coder | Age: 24 days ago

⚠️ STALENESS NOTICE: This context is from 24 days ago. Code may have
changed. Use this as a starting point for WHERE to look, not WHAT is there.
========================================

[smart mode content: subagent summaries + labels + first user messages]

========================================
```

## Context Window Math

| Approach | Size per Session | 5 Sessions |
|----------|------------------|------------|
| Full transcript | 50K-200K chars | 250K-1M chars (OVERFLOW) |
| Labels only | 200-500 chars | 1K-2.5K chars (GREAT) |
| Labels + first 3 user msgs | 500-2000 chars | 2.5K-10K chars (FINE) |
| Hybrid (recommended) | ~1500 chars | ~7.5K chars (GOOD) |

## Edge Cases and Error Handling

### Subagent Extraction Edge Cases

| Case | How to Handle |
|------|---------------|
| No subagents directory | Return empty list from `find_subagent_files()` |
| Agent file exists but empty | Return `None` from `extract_agent_summary()` |
| Agent ends with tool_use (no text) | Skip - last message has no text content |
| Summary < 200 chars | Skip - likely not a real summary, just a brief note |
| Agent interrupted mid-task | Use whatever text is in the last assistant message |
| Unicode/encoding issues | Use `encoding="utf-8"` and skip malformed lines |
| Agent file corrupted | Catch `json.JSONDecodeError`, log warning, skip file |

### Session Indexing Edge Cases

| Case | How to Handle |
|------|---------------|
| Session never compacted | No summary labels - fall back to user messages only |
| Session has 0 user messages | Skip session (probably a system/meta session) |
| Session has 50+ summary labels | Index all of them - they're tiny (~100 bytes each) |
| Session from old Claude version | May not have subagents dir - just index main session |
| Duplicate indexing | Use `session_id + content_type + chunk_index` as unique point ID |

### Plan File Linking Edge Cases

| Case | How to Handle |
|------|---------------|
| No `slug` field in session | Plan linking skipped - index session without plan |
| Slug exists but no plan file | Plan linking skipped - index session without plan |
| Plan file empty or < 50 chars | Skip plan - too small to be useful |
| Plan file > 100KB | Skip plan - likely not a real plan file, log warning |
| Multiple sessions same slug | Each gets same plan - that's correct (same feature) |
| Plan file renamed/moved | Won't be found - user can re-create if needed |

### Error Recovery

```python
def index_session_safe(session_path: Path, repo_path: str) -> bool:
    """Index a session with error recovery.

    Returns True if successful, False if skipped due to errors.
    """
    try:
        # Main session indexing
        transcript = parse_jsonl_file(session_path)
        ...
    except FileNotFoundError:
        logger.warning(f"Session file not found: {session_path}")
        return False
    except json.JSONDecodeError as e:
        logger.warning(f"Invalid JSON in session: {session_path}: {e}")
        return False

    # Subagent indexing - errors here shouldn't fail the whole session
    for agent_file in find_subagent_files(session_path):
        try:
            summary = extract_agent_summary(agent_file)
            if summary:
                index_subagent_summary(summary, session_id, repo_path)
        except Exception as e:
            logger.warning(f"Failed to extract agent summary: {agent_file}: {e}")
            # Continue with other agents

    return True
```

## Hooks Investigation

### PreCompact Hook
- **Exists**: Yes, fires before compaction
- **Provides**: `transcript_path`, `trigger` (manual/auto), `session_id`
- **Use case**: Could save full transcript before compaction

### PostCompact Hook
- **Exists**: NO (requested in GitHub issue #17237)
- **Impact**: Can't capture summary at generation time
- **Workaround**: Summaries are written to JSONL anyway, we can read them

## Files to Modify

### Core Changes

| File | Changes |
|------|---------|
| `jacked/transcript.py` | Add `find_subagent_files()`, `extract_agent_summary()`, `extract_summary_labels()` |
| `jacked/indexer.py` | Add `content_type` metadata to all points, index subagent summaries |
| `jacked/searcher.py` | Add `content_types` filter param, update `_build_filter()` |
| `jacked/retriever.py` | Add `mode` param ("smart", "labels", "agents", "full") |
| `jacked/cli.py` | Add `--type` to search, `--mode` to retrieve |
| `skills/jacked/SKILL.md` | Update instructions to use `--mode smart` by default |

### New Functions

**transcript.py:**
```python
def find_subagent_files(session_path: Path) -> list[Path]
def extract_agent_summary(agent_file: Path) -> dict | None
def extract_summary_labels(session_path: Path) -> list[dict]  # NEW
```

**indexer.py:**
```python
def _create_subagent_points(session_id, summaries, base_payload) -> list[PointStruct]
def _create_summary_label_points(session_id, labels, base_payload) -> list[PointStruct]
```

**retriever.py:**
```python
def retrieve_smart(session_id) -> str  # subagent summaries + labels + first 3 user msgs
def retrieve_labels(session_id) -> str  # summary labels only
def retrieve_agents(session_id) -> str  # all subagent summaries
```

### Schema Changes

New metadata fields on all Qdrant points:
```python
{
    "content_type": "subagent_summary" | "summary_label" | "user_message" | "chunk",
    "agent_id": "a4e75d5" | None,  # only for subagent_summary
    "agent_type": "Explore" | "Plan" | None,  # if detectable
}
```

Point ID scheme: `{session_id}:{content_type}:{index}` to prevent duplicates.

## Open Questions

1. ~~Should we still index full transcripts for detailed retrieval, or just summaries?~~
   **Answer**: Index labels + first user messages. Store full transcript, but only load on request.

2. How to handle sessions with no labels (never compacted)?
   **Answer**: Fall back to first 3 user messages for intent. These sessions are usually shorter anyway.

3. ~~Should we generate our own summaries for non-compacted sessions?~~
   **Answer**: Not worth the API cost. User messages + labels are sufficient.

4. ~~What's the right balance of summary vs transcript in retrieved context?~~
   **Answer**: Default to labels + first 3 user messages. Full transcript only on explicit request.

5. **NEW**: Should we add a PreCompact hook to capture rich summaries going forward?
   **Answer**: Yes, but as Phase 3 - nice to have, not blocking.

6. ~~How do we handle the `leafUuid` linkage in labels?~~
   **Answer**: DEFERRED - not needed for MVP. Could link labels to specific conversation chunks in future.

7. **Token estimation accuracy?**
   **Answer**: `len(text) // 4` is good enough for English. Can add tiktoken later if needed.

8. **AskUserQuestion 4-option limit?**
   **Answer**: Already documented in SKILL.md update section - show top 3 + "None" option.

## Next Steps

### Completed
- [x] Document findings (this doc)
- [x] Fix install issues (v0.2.5 shipped)
- [x] Document subagent JSONL structure
- [x] Define extraction algorithm
- [x] Define edge cases and error handling

### Phase 1: transcript.py (Next)
- [ ] Add `find_subagent_files(session_path)` function
- [ ] Add `extract_agent_summary(agent_file)` function
- [ ] Add `extract_summary_labels(session_path)` function to parse `type: "summary"` messages
- [ ] Add tests for all new functions

### Phase 2: indexer.py
- [ ] Add `content_type` to all existing point payloads (backwards compatible)
- [ ] Add `_create_subagent_points()` helper
- [ ] Add `_create_summary_label_points()` helper
- [ ] Update `index_session()` to call new helpers
- [ ] Update point ID scheme to include content_type

### Phase 3: searcher.py
- [ ] Add `content_types: list[str]` param to `search()`
- [ ] Update `_build_filter()` to support `content_type` filter
- [ ] Default to `["subagent_summary", "summary_label", "user_message"]`

### Phase 4: retriever.py
- [ ] Add `mode` param: "smart", "labels", "agents", "full"
- [ ] Implement `retrieve_smart()` - subagent summaries + labels + first 3 user msgs
- [ ] Implement `retrieve_labels()` - just summary labels
- [ ] Implement `retrieve_agents()` - all subagent summaries

### Phase 5: CLI
- [ ] Add `--type` / `-t` flag to `search` command
- [ ] Add `--mode` / `-m` flag to `retrieve` command
- [ ] Update help text

### Phase 6: SKILL.md
- [ ] Update instructions to use `--mode smart` by default
- [ ] Add guidance on when to use `--mode full`

### Phase 7: Backfill & Migration
- [ ] Run `jacked backfill` to re-index all sessions with new schema
- [ ] Verify old points still work (backwards compatible)

### Future (Nice to Have)
- [ ] Add PreCompact hook to capture rich summaries going forward
- [ ] Add agent type detection from parent session
- [ ] Add `--agent-type` filter to search

## References

- Claude Code Hooks: https://code.claude.com/docs/en/hooks.md
- PostCompact feature request: https://github.com/anthropics/claude-code/issues/17237
- Session JSONL location: `~/.claude/projects/{encoded-repo-path}/{session-id}.jsonl`
