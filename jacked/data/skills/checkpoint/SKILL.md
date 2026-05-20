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

For research topics with substantial findings (multiple sources, detailed analysis), create separate files. **Use HTML** so they open cleanly in a browser, render diagrams, and look like documentation instead of raw text:

```bash
mkdir -p .claude/research
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
cp ~/.claude/jacked-templates/plan-template.html ".claude/research/${TIMESTAMP}-{topic-slug}.html"
```

Then edit the copy. Fill the `<meta>` tags and replace `{{PLACEHOLDERS}}`:

```html
<title>{Topic Title}</title>
<meta name="jacked:type" content="research">
<meta name="jacked:status" content="complete">
<meta name="jacked:date" content="{YYYY-MM-DD}">
<meta name="jacked:checkpoint" content="{checkpoint-slug}">

<h1>{Topic Title}</h1>

<div class="meta">
  <div class="kv"><span class="k">Sources:</span><span class="v">{url1}, {url2}</span></div>
  <div class="kv"><span class="k">Checkpoint:</span><span class="v">{checkpoint-slug}</span></div>
</div>

<h2>Findings</h2>
<p>{Distilled research — key patterns, comparisons, recommendations.
Not a copy of the web page, but the actionable knowledge extracted.}</p>

<h2>Source Notes</h2>
<table>
  <thead><tr><th>Source</th><th>What was useful</th><th>Key quotes / data</th></tr></thead>
  <tbody>
    <tr><td>{url}</td><td>{notes}</td><td>{excerpts}</td></tr>
  </tbody>
</table>
```

The `jacked:checkpoint` meta tag is for human reference only — not consumed programmatically.

### Step 5: Write checkpoint file (atomic, HTML)

Checkpoints are written as HTML so a future you can open them in a browser, see diagrams of the in-progress branch state, and skim the rendered TOC. They're still small enough that Claude can read them when resuming.

```bash
CHECKPOINT_DIR=".claude/checkpoints"
mkdir -p "$CHECKPOINT_DIR"
TIMESTAMP=$(date +%Y%m%d-%H%M%S)
TMPFILE=$(mktemp "${CHECKPOINT_DIR}/.tmp.XXXXXX.html")
cp ~/.claude/jacked-templates/plan-template.html "$TMPFILE"
```

Then edit the temp file. The metadata that used to live in YAML frontmatter goes into HTML `<meta>` tags so it stays machine-introspectable; everything else becomes proper HTML sections:

```html
<title>Checkpoint: {title}</title>
<meta name="jacked:type" content="checkpoint">
<meta name="jacked:status" content="in-progress">
<meta name="jacked:branch" content="{branch}">
<meta name="jacked:timestamp" content="{ISO-8601}">
<meta name="jacked:releases" content="{comma-separated versions released this session, if any}">
<meta name="jacked:plans_in_progress" content="{semicolon-separated paths to active plan files}">
<meta name="jacked:research_files" content="{semicolon-separated paths to research files}">
<meta name="jacked:active_lenses" content="{comma-separated lens stems}">

<h1>Checkpoint: {title}</h1>

<h2 id="working-on">What We're Working On</h2>
<p>{content}</p>

<h2 id="accomplished">Accomplished This Session</h2>
<ul><li>{content}</li></ul>

<h2 id="decisions">Decisions Made</h2>
<ul><li>{content — include the WHY for each decision}</li></ul>

<h2 id="context">Session Context</h2>
<p>{content}</p>

<h2 id="research">Research &amp; References</h2>
<ul><li><a href="{relative path to .html research file}">{topic}</a></li></ul>

<h2 id="remaining">Remaining Work</h2>
<ul class="tasks">
  <li><input type="checkbox" disabled> {task}</li>
</ul>

<h2 id="state">Current State</h2>
<p>{content}</p>

<h2 id="gotchas">Gotchas &amp; Notes</h2>
<aside class="callout warn">{content}</aside>

<h2 id="key-files">Key Files</h2>
<table>
  <thead><tr><th>File</th><th>Description</th></tr></thead>
  <tbody>
    <tr><td><code>path/to/file</code></td><td>{description}</td></tr>
  </tbody>
</table>
```

Rename temp file to final path:
```bash
mv "$TMPFILE" "${CHECKPOINT_DIR}/${TIMESTAMP}-{slug}.html"
```

> **Migration note**: existing `.md` checkpoints from before 0.43.2 still load fine on `/checkpoint resume`. Leave them as-is; only new checkpoints are HTML.

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
  # Glob both .html (current) and .md (pre-0.43.2 legacy) so old checkpoints still resume.
  ls -1t "$CHECKPOINT_DIR"/*.html "$CHECKPOINT_DIR"/*.md 2>/dev/null | head -10
else
  echo "NO_CHECKPOINTS"
fi
```

If no slug specified, find the most recent file with `in-progress` status. For HTML checkpoints, status lives in `<meta name="jacked:status" content="in-progress">`. For legacy Markdown checkpoints, it's in the YAML `status:` field. If a slug is given, match the file `*-{slug}.html` first, then `*-{slug}.md`.

### Step 2: Branch check

```bash
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
```

Read the branch from the checkpoint — for HTML checkpoints, `<meta name="jacked:branch" content="...">`; for legacy Markdown checkpoints, the YAML `branch:` field. If it differs from the current branch, warn:

> "Checkpoint was created on branch **{branch}** but you are on **{current_branch}**. Context may not apply. Continue anyway?"

Do NOT auto-switch branches.

### Step 3: Auto-load referenced files with budget

Read files in priority order. Track total lines loaded.

1. Files from `plans_in_progress` (HTML: `<meta name="jacked:plans_in_progress" content="path1;path2">`; legacy MD: YAML `plans_in_progress` frontmatter) — highest priority, defines remaining work
2. Files from `research_files` (HTML: `<meta name="jacked:research_files">`; legacy MD: YAML `research_files` frontmatter) — decision context
3. Files from the Key Files section body (each row: `<code>path/to/file</code> — description` in HTML, or `- path/to/file — description` in MD)

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

Find the most recent in-progress checkpoint on the current branch (HTML or legacy MD). If none found on current branch, show all in-progress checkpoints and ask which to complete.

Update the checkpoint file's status. For HTML: change `<meta name="jacked:status" content="in-progress">` to `content="completed"`. For legacy MD: change the YAML `status: in-progress` to `status: completed`.

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
ls -1t "$CHECKPOINT_DIR"/*.html "$CHECKPOINT_DIR"/*.md 2>/dev/null | while read f; do
  echo "$(basename "$f")"
done
```

Read metadata from each to show a table. For `.html`, parse the `<meta name="jacked:*">` tags. For legacy `.md`, parse the YAML frontmatter block:

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
