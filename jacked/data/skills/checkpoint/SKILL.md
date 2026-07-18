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
/checkpoint resume {slug} --goal "<next task>" — goal-directed resume: extract only the context that
                           matters for that next task and propose a focused starting plan (see Resume Step 6)
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

**Secrets hygiene (do this before writing anything in Step 4 or 5):** scan the drafted **Session Context**, **Gotchas & Notes**, and **Key Files** content — the free-form sections most likely to have slurped up a pasted value — for likely credentials: API keys (`sk-…`, `ghp_…`, `AKIA…`, `xox[bap]-…`), bearer/access tokens, passwords, `.env` values, and DB/connection strings (`postgres://user:pass@…`). Redact each to a descriptive placeholder, e.g. `{REDACTED: openai key}`, `{REDACTED: postgres connection string}`. Keep the surrounding prose so the context still reads — redact the secret, not the sentence. **Never write a credential or secret into a checkpoint or research file** (these HTML files may be committed to git — see the git-hygiene note in Step 5).

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
<meta name="jacked:head_sha" content="{output of `git rev-parse HEAD`}">
<meta name="jacked:dirty_files" content="{number of lines in `git status --short`, i.e. count of uncommitted/untracked files}">
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
>
> **Git hygiene**: `.claude/checkpoints/` can be **committed** to share session handoffs with teammates, or **gitignored** for local-only use — your call. If you commit it, the Step 3 secrets-hygiene scan is **mandatory**, not optional: a checkpoint with a leaked key becomes a leaked key in git history.

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

### Step 7: Record to the memory vault (guarded)

If the memory vault is enabled (`jacked memory status --quiet` exits 0; if it exits nonzero, skip this step silently), record a short progress note so the checkpoint is searchable across sessions and repos:

```bash
jacked memory add --type progress --title "Checkpoint: {title}" --body "{one-paragraph summary: current state + top remaining work}"
```

Apply the Step 3 secrets-hygiene scan to the summary before it goes into the vault too: never write a credential into a note. This note is a pointer to the full checkpoint file, not a copy of it. If the vault is off, do nothing here.

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

### Step 3: Freshness check

The checkpoint captured a snapshot of repo state at save time. If the repo moved on since, the **Current State** section is stale and should not be trusted verbatim. Compare the saved fingerprint to now:

```bash
SAVED_SHA="{value of <meta name=\"jacked:head_sha\"> — empty for legacy checkpoints}"
CURRENT_SHA=$(git rev-parse HEAD 2>/dev/null)
if [ -n "$SAVED_SHA" ] && [ "$SAVED_SHA" != "$CURRENT_SHA" ]; then
  git log --oneline "$SAVED_SHA..HEAD" 2>/dev/null | wc -l   # new commits since save
fi
git status --short 2>/dev/null | wc -l                       # current dirty-file count
```

If `head_sha` is present and differs, or the current dirty-file count differs from the saved `dirty_files` meta, surface a one-line warning before presenting the checkpoint:

> "⚠ State has moved since this checkpoint: **{N} new commits**, **{M} files changed** — treat *Current State* / *Gotchas* as possibly stale and re-verify against the live tree."

If `head_sha` is missing (legacy checkpoint), skip silently — there's nothing to compare. Never block resume on this; it's a heads-up, not a gate.

### Step 4: Auto-load referenced files with budget

Read files in priority order. Track total lines loaded.

1. Files from `plans_in_progress` (HTML: `<meta name="jacked:plans_in_progress" content="path1;path2">`; legacy MD: YAML `plans_in_progress` frontmatter) — highest priority, defines remaining work
2. Files from `research_files` (HTML: `<meta name="jacked:research_files">`; legacy MD: YAML `research_files` frontmatter) — decision context
3. Files from the Key Files section body (each row: `<code>path/to/file</code> — description` in HTML, or `- path/to/file — description` in MD)

For each file:
- If it doesn't exist: warn "Referenced file {path} no longer exists (may have been renamed/deleted since checkpoint)" and continue
- If total lines loaded would exceed ~3000: stop loading. Present remaining as "Also referenced (not loaded): {list}" so the user can request specific ones.

**Goal-directed loading** (when `--goal "<next task>"` was passed): rank the candidate files by relevance to the stated goal *before* applying the budget — load the plan/research/key files that bear on that task first, and let the lower-relevance ones spill into "Also referenced (not loaded)". The goal narrows what's worth pulling into context; don't burn the budget on files unrelated to the next task.

### Step 5: Present checkpoint

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

If a **project journal** exists (`.claude/PROJECT-NOTES.md` — see Complete Flow), read it and fold its durable learnings into how you resume. It outlives any single checkpoint, so it's authoritative for recurring gotchas, user preferences, and non-obvious patterns.

### Step 6: Continue

**Plain resume** (no `--goal`): begin working on the first remaining work item.

**Goal-directed resume** (`--goal "<next task>"` was passed, or the user supplied a free-text goal on resume): don't replay the full snapshot — launch the *next focused task* with exactly the context it needs. Synthesize a **next-task brief**:

1. **Goal** — restate the stated next task in one line.
2. **Relevant context** — only the decisions, session-context facts, and gotchas from the checkpoint that bear on this goal (drop the rest).
3. **Relevant files** — the subset of loaded files (ranked in Step 4) that matter for this goal.
4. **Proposed first steps** — the Remaining-Work items that advance this goal, in order.

Present the brief as a **proposed starting plan the user can edit before you begin** — do not start executing until they confirm or amend it. (This mirrors Amp's handoff: a focused, reviewable launch beats a meandering full-context replay.)

## Complete Flow

When the user runs `/checkpoint complete`:

```bash
CHECKPOINT_DIR=".claude/checkpoints"
BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null)
```

Find the most recent in-progress checkpoint on the current branch (HTML or legacy MD). If none found on current branch, show all in-progress checkpoints and ask which to complete.

Update the checkpoint file's status. For HTML: change `<meta name="jacked:status" content="in-progress">` to `content="completed"`. For legacy MD: change the YAML `status: in-progress` to `status: completed`.

**Promote durable learnings to the project journal.** A completed checkpoint's hard-won project knowledge would otherwise die with it. Before finishing, scan this checkpoint's **Decisions**, **Session Context**, and **Gotchas** for learnings that are *durable and checkpoint-independent* — recurring gotchas, user preferences, non-obvious patterns that will matter on the next task too (not one-off state like "stopped mid-DCR"). If any exist, offer:

> "Promote N durable learning(s) to the project journal so they survive this checkpoint? (Y/n)"

If yes, append them as terse bullets to `.claude/PROJECT-NOTES.md` (create from a plain `# Project Notes` heading if absent). Keep it **small and curated** — merge into or sharpen an existing bullet rather than duplicating, and skip anything already captured in `CLAUDE.md`. This journal is read on every resume (Resume Step 5). The same secrets-hygiene scan as Save Step 3 applies — never write a credential into it.

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
- `head_sha`, `dirty_files` — repo fingerprint at save time, consumed by the Resume Step 3 freshness check. Optional; absent on legacy checkpoints (freshness check skips silently when missing).
- All list fields are optional. Missing fields = empty lists (backward compatible with older checkpoints).
- Older checkpoints may have deprecated fields (e.g., `files_modified`). Ignore on read.

## Rules

- **Never modify code** during checkpoint save — only read state and write checkpoint/research files.
- **Never write credentials or secrets** into a checkpoint, research file, or the project journal. Run the Save Step 3 redaction scan (API keys, tokens, passwords, `.env` values, connection strings → `{REDACTED: …}` placeholders) before any write. These files may be committed to git, so a leaked key is a permanent leak.
- **Infer, don't interrogate** — use git state and conversation context. Only ask for a title if it genuinely can't be inferred.
- **Checkpoint files are append-only** — each save creates a new file. Never overwrite (except status changes via /checkpoint complete).
- **Atomic writes** — always write to temp file first, then rename.
- **Single-writer** — one Claude session per project at a time. No file locking needed.
- **Research file order** — write checkpoint FIRST (references research files speculatively), then write research files. If research write fails, checkpoint's missing-file handling covers it on resume.
