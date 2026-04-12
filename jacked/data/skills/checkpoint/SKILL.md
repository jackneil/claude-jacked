---
name: checkpoint
description: Save and resume working state checkpoints. Captures git state, decisions, research, session context, and remaining work so a new session can pick up exactly where you left off. Use when asked to "checkpoint", "save progress", "where was I", "resume", "what was I working on", or "pick up where I left off".
---

# Checkpoint

Save and resume working state. Captures git state, decisions made, research conducted,
session context, and remaining work so a new session picks up exactly where this one
left off — with full knowledge of what was learned, decided, and attempted.

## Commands

```
/checkpoint              — save current session state
/checkpoint resume       — load most recent in-progress checkpoint, auto-load all referenced files
/checkpoint resume {slug} — resume a specific checkpoint (slug from /checkpoint list)
/checkpoint complete     — mark the most recent in-progress checkpoint (current branch) as completed
/checkpoint list         — show all checkpoints for this project
```

## Save Flow

When the user runs `/checkpoint`:

### Step 1: Gather state

```bash
echo "=== BRANCH ==="
git rev-parse --abbrev-ref HEAD 2>/dev/null
echo "=== STATUS ==="
git status --short 2>/dev/null
echo "=== RECENT LOG ==="
git log --oneline -10 2>/dev/null
```

### Step 2: Check for existing in-progress checkpoints on this branch

```bash
CHECKPOINT_DIR=".claude/checkpoints"
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
```

Read frontmatter of any existing checkpoint files. If an in-progress checkpoint exists on the same branch, ask:

> "Mark previous checkpoint **{title}** as completed? (Y/n)"

If yes, update its `status:` line to `completed`. If no, proceed (multiple in-progress checkpoints are allowed).

### Step 3: Summarize from conversation context

Using gathered state PLUS your conversation history, produce:

1. **What We're Working On** — the high-level goal (1-3 sentences)
2. **Accomplished This Session** — commits made, features shipped, releases cut
3. **Decisions Made** — architectural choices, trade-offs, approaches chosen and WHY
4. **Session Context** — non-obvious knowledge from conversation: user intent, constraints, domain facts shared verbally, "build it like X" references, things tried and failed. This is the most important section — it preserves interactive knowledge that would otherwise die with the context window.
5. **Research & References** — summary of any web fetches, API docs, or reference material gathered. Sources cited. For small topics, inline here. For substantial research (multiple sources, detailed analysis), write separate research files (see Step 4).
6. **Remaining Work** — concrete next steps in priority order. Reference plan files.
7. **Current State** — where exactly we stopped (mid-plan? mid-DCR? waiting for user input?)
8. **Gotchas & Notes** — things tried and didn't work, open questions, known issues
9. **Key Files** — most important files to read to get up to speed. Format each line as: `- path/to/file — brief description`

Also determine:
- **Active lenses** — which specialist lenses (by filename stem, e.g., `accessibility`) were relevant during this session. Populated from: lenses selected by DCR, lenses whose triggers matched files modified, or lenses explicitly referenced in conversation.

If the user provided a title, use it. Otherwise infer one from the work.

### Step 4: Write research files (if needed)

For research topics with substantial findings (multiple sources, detailed analysis), create separate files:

```bash
mkdir -p .claude/research
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
```

Write to `.claude/research/{TIMESTAMP}-{topic-slug}.md`:

```markdown
---
topic: {topic title}
sources:
  - {url1}
  - {url2}
date: {YYYY-MM-DD}
checkpoint: {checkpoint-slug}
---

# {Topic Title}

## Findings

{Distilled research — key patterns, comparisons, recommendations.
Not a copy of the web page, but the actionable knowledge extracted.}

## Source Notes

{Per-source: what was useful, what wasn't, key quotes or data points.}
```

The `checkpoint` field is for human reference only — not consumed programmatically.

### Step 5: Write checkpoint file (atomic)

```bash
CHECKPOINT_DIR=".claude/checkpoints"
mkdir -p "$CHECKPOINT_DIR"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
TMPFILE=$(mktemp "${CHECKPOINT_DIR}/.tmp.XXXXXX")
```

Write to the temp file, then rename:

```markdown
---
status: in-progress
branch: {branch}
timestamp: {ISO-8601}
releases: [{list of versions released this session, if any}]
plans_in_progress:
  - {path to active plan file, if any}
research_files:
  - {path to research file, if any}
active_lenses: [{lens stems, if any}]
---

# Checkpoint: {title}

## What We're Working On
{content}

## Accomplished This Session
{content}

## Decisions Made
{content}

## Session Context
{content}

## Research & References
{content}

## Remaining Work
{content}

## Current State
{content}

## Gotchas & Notes
{content}

## Key Files
{content — each line: `- path/to/file — description`}
```

Rename temp file to final path:
```bash
mv "$TMPFILE" "${CHECKPOINT_DIR}/${TIMESTAMP}-{slug}.md"
```

### Step 6: Display confirmation

```
CHECKPOINT SAVED
════════════════════════════════
Title:    {title}
Branch:   {branch}
File:     {path}
Research: {N files written, or "inline"}
════════════════════════════════
```

## Resume Flow

When the user runs `/checkpoint resume` or `/checkpoint resume {slug}`:

### Step 1: Find checkpoint

```bash
CHECKPOINT_DIR=".claude/checkpoints"
if [ -d "$CHECKPOINT_DIR" ]; then
  ls -1t "$CHECKPOINT_DIR"/*.md 2>/dev/null | head -10
else
  echo "NO_CHECKPOINTS"
fi
```

If no slug specified, find the most recent file with `status: in-progress` in frontmatter. If a slug is given, find the file matching `*-{slug}.md`.

### Step 2: Branch check

```bash
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
```

If the checkpoint's `branch:` field differs from the current branch, warn:

> "Checkpoint was created on branch **{branch}** but you are on **{current_branch}**. Context may not apply. Continue anyway?"

Do NOT auto-switch branches.

### Step 3: Auto-load referenced files with budget

Read files in priority order. Track total lines loaded.

1. Files from `plans_in_progress` frontmatter (highest priority — defines remaining work)
2. Files from `research_files` frontmatter (decision context)
3. Files from Key Files section body (each line: `- path/to/file — description`, extract path before em-dash)

For each file:
- If it doesn't exist: warn "Referenced file {path} no longer exists (may have been renamed/deleted since checkpoint)" and continue
- If total lines loaded would exceed ~3000: stop loading. Present remaining as "Also referenced (not loaded): {list}" so the user can request specific ones.

### Step 4: Present checkpoint

```
RESUMING CHECKPOINT
════════════════════════════════
Title:    {title}
Branch:   {branch}
Saved:    {timestamp, human-readable}
Status:   {status}
Loaded:   {N} referenced files ({total_lines} lines)
════════════════════════════════

{Checkpoint summary — What We're Working On + Remaining Work + Current State + Gotchas}
```

### Step 5: Continue

Begin working on the first remaining work item.

## Complete Flow

When the user runs `/checkpoint complete`:

```bash
CHECKPOINT_DIR=".claude/checkpoints"
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
```

Find the most recent in-progress checkpoint on the current branch. If none found on current branch, show all in-progress checkpoints and ask which to complete.

Update the checkpoint file's frontmatter: change `status: in-progress` to `status: completed`.

```
CHECKPOINT COMPLETED
════════════════════════════════
Title:    {title}
Branch:   {branch}
════════════════════════════════
```

## List Flow

When the user runs `/checkpoint list`:

```bash
CHECKPOINT_DIR=".claude/checkpoints"
ls -1t "$CHECKPOINT_DIR"/*.md 2>/dev/null | while read f; do
  echo "$(basename "$f")"
done
```

Read frontmatter from each to show a table:

```
CHECKPOINTS
════════════════════════════════
#  Date        Branch     Title                Status
─  ──────────  ─────────  ───────────────────  ───────────
1  2026-04-12  master     usage-analytics      in-progress
2  2026-04-06  master     token-resilience     completed
3  2026-04-05  master     auto-swap-system     completed
════════════════════════════════
```

## Frontmatter Field Semantics

- `plans_in_progress`, `research_files` — **file paths** resolved against project root
- `active_lenses` — **lens filename stems** (e.g., `accessibility`, `api-ergonomics`). NOT frontmatter name field, NOT file paths.
- All list fields are optional. Missing fields = empty lists (backward compatible with older checkpoints).
- Older checkpoints may have deprecated fields (e.g., `files_modified`). Ignore on read.

## Rules

- **Never modify code** during checkpoint save — only read state and write checkpoint/research files.
- **Infer, don't interrogate** — use git state and conversation context. Only ask for a title if it genuinely can't be inferred.
- **Checkpoint files are append-only** — each save creates a new file. Never overwrite (except status changes via /checkpoint complete).
- **Atomic writes** — always write to temp file first, then rename.
- **Single-writer** — one Claude session per project at a time. No file locking needed.
- **Research file order** — write checkpoint FIRST (references research files speculatively), then write research files. If research write fails, checkpoint's missing-file handling covers it on resume.
