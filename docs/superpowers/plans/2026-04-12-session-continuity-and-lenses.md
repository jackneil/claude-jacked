# Session Continuity & Specialist Lenses Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship checkpoint skill, 4 specialist lenses, and skill integration updates so that cross-session development is seamless and reviews are context-aware.

**Architecture:** Checkpoint is a Claude Code skill (prompt-only, no Python) at `jacked/data/skills/checkpoint/SKILL.md` deployed to `~/.claude/skills/checkpoint/SKILL.md`. Lenses are markdown files at `jacked/data/lenses/*.md` deployed to `~/.claude/lenses/*.md`. Installer gains a shared `_install_asset_dir()` helper and lens support. Existing commands (dcr, qa, ux, whats-next, jack-it-up, techdebt) get small additions for lens/checkpoint awareness.

**Tech Stack:** Markdown (skills/lenses/commands), Python (cli.py installer)

**Spec:** `docs/superpowers/specs/2026-04-12-session-continuity-and-lenses-design.md`

---

## File Map

### New files
- `jacked/data/skills/checkpoint/SKILL.md` — checkpoint skill (save/resume/complete/list)
- `jacked/data/lenses/accessibility.md` — accessibility lens
- `jacked/data/lenses/api-ergonomics.md` — API design lens
- `jacked/data/lenses/database-design.md` — schema/migration lens
- `jacked/data/lenses/error-handling.md` — error handling lens

### Modified files
- `jacked/cli.py` — extract `_install_asset_dir()`, add lens install/uninstall
- `jacked/data/commands/dcr.md` — add specialist lens awareness to lens selection
- `jacked/data/commands/whats-next.md` — add checkpoint awareness + lens gap detection
- `jacked/data/commands/qa.md` — add accessibility lens checklist
- `jacked/data/commands/ux.md` — add accessibility lens checklist
- `jacked/data/skills/jack-it-up/SKILL.md` — add lens awareness to brainstorm phase
- `jacked/data/commands/techdebt.md` — add lens-based anti-pattern scanning
- Global `~/.claude/CLAUDE.md` — add session-start checkpoint detection rule

---

## Task 1: Extract `_install_asset_dir()` helper

**Files:**
- Modify: `jacked/cli.py:2046-2133` (agents + commands install loops)

- [ ] **Step 1: Write the test**

```python
# tests/unit/test_install_asset_dir.py
import json
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def tmp_asset_dirs(tmp_path):
    """Create source and dest directories with test markdown files."""
    src = tmp_path / "src"
    src.mkdir()
    dst = tmp_path / "dst"
    # Don't create dst — helper should create it
    (src / "one.md").write_text("# One\ncontent one")
    (src / "two.md").write_text("# Two\ncontent two")
    return src, dst


def test_install_asset_dir_creates_dst_and_copies(tmp_asset_dirs):
    from jacked.cli import _install_asset_dir

    src, dst = tmp_asset_dirs
    with patch("jacked.cli._is_editable_install", return_value=False):
        installed, skipped, method = _install_asset_dir(
            src, dst, "test-assets", glob_pattern="*.md", force=False
        )
    assert installed == 2
    assert skipped == 0
    assert (dst / "one.md").read_text() == "# One\ncontent one"
    assert (dst / "two.md").read_text() == "# Two\ncontent two"


def test_install_asset_dir_skips_unchanged(tmp_asset_dirs):
    from jacked.cli import _install_asset_dir

    src, dst = tmp_asset_dirs
    dst.mkdir()
    # Pre-populate with identical content
    (dst / "one.md").write_text("# One\ncontent one")
    (dst / "two.md").write_text("# Two\ncontent two")
    with patch("jacked.cli._is_editable_install", return_value=False):
        installed, skipped, method = _install_asset_dir(
            src, dst, "test-assets", glob_pattern="*.md", force=False
        )
    assert installed == 0
    assert skipped == 2


def test_install_asset_dir_force_overwrites(tmp_asset_dirs):
    from jacked.cli import _install_asset_dir

    src, dst = tmp_asset_dirs
    dst.mkdir()
    (dst / "one.md").write_text("old content that differs")
    with patch("jacked.cli._is_editable_install", return_value=False):
        installed, skipped, method = _install_asset_dir(
            src, dst, "test-assets", glob_pattern="*.md", force=True
        )
    assert installed == 2
    assert (dst / "one.md").read_text() == "# One\ncontent one"


def test_install_asset_dir_missing_src(tmp_path):
    from jacked.cli import _install_asset_dir

    src = tmp_path / "nonexistent"
    dst = tmp_path / "dst"
    installed, skipped, method = _install_asset_dir(
        src, dst, "test-assets", glob_pattern="*.md", force=False
    )
    assert installed == 0
    assert skipped == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run python -m pytest tests/unit/test_install_asset_dir.py -v`
Expected: FAIL with "cannot import name '_install_asset_dir'"

- [ ] **Step 3: Implement `_install_asset_dir()` helper**

Add this function to `jacked/cli.py` after the existing `_link_or_copy` function (after line ~940):

```python
def _install_asset_dir(
    src_dir: Path,
    dst_dir: Path,
    asset_label: str,
    *,
    glob_pattern: str = "*.md",
    force: bool = False,
) -> tuple[int, int, str | None]:
    """Install assets from src_dir to dst_dir with conflict handling.

    Handles: symlink detection, hardlink detection, content comparison,
    force overwrite, and interactive conflict prompts.

    Returns (installed_count, skipped_count, link_method).
    """
    if not src_dir.exists():
        return 0, 0, None

    dst_dir.mkdir(parents=True, exist_ok=True)
    installed = 0
    skipped = 0
    link_method = None

    for src_file in sorted(src_dir.glob(glob_pattern)):
        dst_file = dst_dir / src_file.name

        # Already a correct symlink — always skip
        if dst_file.is_symlink() and dst_file.resolve() == src_file.resolve():
            skipped += 1
            continue

        # Already a hardlink to same inode — always skip
        if not dst_file.is_symlink() and dst_file.exists():
            try:
                if dst_file.stat().st_ino == src_file.stat().st_ino:
                    skipped += 1
                    continue
            except OSError:
                pass

        # Existing file with same content — skip unless --force
        if not force and not dst_file.is_symlink() and dst_file.exists():
            if src_file.read_text(encoding="utf-8") == dst_file.read_text(
                encoding="utf-8"
            ):
                skipped += 1
                continue
            if sys.stdin.isatty() and not click.confirm(
                f"{asset_label.title()} '{src_file.name}' exists with different content. Overwrite?"
            ):
                skipped += 1
                continue

        link_method = _link_or_copy(src_file, dst_file)
        installed += 1

    return installed, skipped, link_method
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run python -m pytest tests/unit/test_install_asset_dir.py -v`
Expected: All 4 tests PASS

- [ ] **Step 5: Refactor agents install to use `_install_asset_dir()`**

Replace `jacked/cli.py` lines 2046-2089 (the agents install block) with:

```python
    # Install agents (symlink for editable, copy otherwise)
    editable = _is_editable_install()
    agents_src = pkg_root / "agents"
    agents_dst = home / ".claude" / "agents"
    agent_count, agent_skipped, agent_method = _install_asset_dir(
        agents_src, agents_dst, "agent", glob_pattern="*.md", force=force
    )
    if agents_src.exists():
        method_label = f" ({agent_method})" if agent_method and editable else ""
        msg = f"[green][OK][/green] Installed {agent_count} agents{method_label}"
        if agent_skipped:
            msg += f" ({agent_skipped} unchanged)"
        console.print(msg)
    else:
        console.print("[yellow][-][/yellow] Agents directory not found")
```

- [ ] **Step 6: Refactor commands install to use `_install_asset_dir()`**

Replace `jacked/cli.py` lines 2091-2133 (the commands install block) with:

```python
    # Install commands (symlink for editable, copy otherwise)
    commands_src = pkg_root / "commands"
    commands_dst = home / ".claude" / "commands"
    cmd_count, cmd_skipped, cmd_method = _install_asset_dir(
        commands_src, commands_dst, "command", glob_pattern="*.md", force=force
    )
    if commands_src.exists():
        method_label = f" ({cmd_method})" if cmd_method and editable else ""
        msg = f"[green][OK][/green] Installed {cmd_count} commands{method_label}"
        if cmd_skipped:
            msg += f" ({cmd_skipped} unchanged)"
        console.print(msg)
    else:
        console.print("[yellow][-][/yellow] Commands directory not found")
```

- [ ] **Step 7: Add lens install block**

Add after the commands install block:

```python
    # Install lenses (symlink for editable, copy otherwise)
    lenses_src = pkg_root / "lenses"
    lenses_dst = home / ".claude" / "lenses"
    lens_count, lens_skipped, lens_method = _install_asset_dir(
        lenses_src, lenses_dst, "lens", glob_pattern="*.md", force=force
    )
    if lenses_src.exists():
        method_label = f" ({lens_method})" if lens_method and editable else ""
        msg = f"[green][OK][/green] Installed {lens_count} lenses{method_label}"
        if lens_skipped:
            msg += f" ({lens_skipped} unchanged)"
        console.print(msg)
    else:
        console.print("[dim][-][/dim] No lenses found to install")
```

- [ ] **Step 8: Add lens uninstall block**

Add to the `uninstall()` function after the commands removal block (after current line ~2514):

```python
    # Remove only jacked-installed lenses (not the whole directory!)
    lenses_src = pkg_root / "lenses"
    lenses_dst = home / ".claude" / "lenses"
    if lenses_src.exists() and lenses_dst.exists():
        lens_count = 0
        for lens_file in lenses_src.glob("*.md"):
            dst_file = lenses_dst / lens_file.name
            if dst_file.exists() or dst_file.is_symlink():
                dst_file.unlink()
                lens_count += 1
        if lens_count > 0:
            console.print(f"[green][OK][/green] Removed {lens_count} lenses")
        else:
            console.print("[yellow][-][/yellow] No jacked lenses found")
    else:
        console.print("[yellow][-][/yellow] Lenses directory not found")
```

- [ ] **Step 9: Run all tests**

Run: `uv run python -m pytest tests/ -v --tb=short`
Expected: All tests PASS (existing + new)

- [ ] **Step 10: Commit**

```bash
git add jacked/cli.py tests/unit/test_install_asset_dir.py
git commit -m "refactor: extract _install_asset_dir() helper, add lens install/uninstall"
```

---

## Task 2: Create checkpoint skill

**Files:**
- Create: `jacked/data/skills/checkpoint/SKILL.md`

- [ ] **Step 1: Create the checkpoint skill file**

Create `jacked/data/skills/checkpoint/SKILL.md` with the full checkpoint skill content. Use the existing `~/.claude/skills/checkpoint/SKILL.md` as a starting point, enhanced per the spec.

```markdown
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
```

- [ ] **Step 2: Verify the skill file is well-formed**

Run: `head -5 jacked/data/skills/checkpoint/SKILL.md`
Expected: YAML frontmatter with `name: checkpoint` and `description:` fields

- [ ] **Step 3: Commit**

```bash
git add jacked/data/skills/checkpoint/SKILL.md
git commit -m "feat: add checkpoint skill to jacked — save/resume session state"
```

---

## Task 3: Create 4 specialist lens files

**Files:**
- Create: `jacked/data/lenses/accessibility.md`
- Create: `jacked/data/lenses/api-ergonomics.md`
- Create: `jacked/data/lenses/database-design.md`
- Create: `jacked/data/lenses/error-handling.md`

- [ ] **Step 1: Create accessibility lens**

```markdown
---
name: Accessibility
description: WCAG 2.2 compliance, keyboard nav, screen readers, color contrast
triggers: [ui, frontend, css, html, component, page, form, button, input, modal, dialog]
---

# Accessibility Lens

## What to check

- Color contrast ratios meet WCAG AA (4.5:1 normal text, 3:1 large text)
- All interactive elements are keyboard-accessible (tab order, focus indicators)
- Form inputs have associated labels (not just placeholder text)
- Images have alt text, decorative images have empty alt=""
- ARIA roles used correctly — not sprinkled on arbitrarily
- Error messages are announced to screen readers
- No information conveyed by color alone
- Focus management after dynamic content changes (modals, route changes)
- Skip navigation link for keyboard users
- Touch targets are at least 44x44px on mobile
- Language attribute set on html element
- Page title is descriptive and unique per page
- Heading hierarchy is logical (no skipped levels)

## Common anti-patterns

- Using div/span as buttons instead of semantic button/a elements
- Hiding focus outlines with outline:none without providing alternative
- Auto-playing media without controls
- Using tabindex > 0 (disrupts natural tab order)
- Relying on hover states for essential information
- Placeholder text as the only label for inputs
- Modal dialogs that don't trap focus
- Custom dropdown/select that isn't keyboard-navigable
- Toast notifications that disappear before screen reader announces them

## When to apply

Any change that touches user-facing HTML, components, or styling.
Especially important for: forms, modals/dialogs, navigation, data tables,
error states, and any interactive widget.
```

- [ ] **Step 2: Create API ergonomics lens**

```markdown
---
name: API Ergonomics
description: Consumer-friendly API design — naming, error contracts, discoverability, consistency
triggers: [api, route, endpoint, handler, rest, graphql, controller, resource]
---

# API Ergonomics Lens

## What to check

- Resource naming is consistent (plural nouns, kebab-case or snake_case — pick one)
- HTTP methods match semantics (GET reads, POST creates, PUT replaces, PATCH updates, DELETE removes)
- Error responses use consistent structure with machine-readable codes and human-readable messages
- Pagination is consistent across all list endpoints (cursor-based or offset-based — pick one)
- Filtering and sorting parameters follow a uniform convention
- Partial responses / field selection available for large resources
- Versioning strategy is explicit (URL path, header, or query param)
- Authentication errors (401) vs authorization errors (403) are distinct
- Rate limiting headers are present (X-RateLimit-Limit, X-RateLimit-Remaining)
- Request/response schemas are documented or self-describing

## Common anti-patterns

- Inconsistent naming across endpoints (users vs user vs getUsers)
- Returning 200 with an error body instead of proper HTTP status codes
- Nested URLs deeper than 2 levels (/orgs/123/teams/456/members/789/roles)
- Requiring clients to make multiple calls for data that naturally belongs together
- Breaking changes without version bump
- Different error formats from different endpoints
- Exposing internal IDs or implementation details in URLs
- Missing or incorrect Content-Type headers
- Accepting GET requests with side effects

## When to apply

Any change that adds, modifies, or extends API endpoints — REST routes,
GraphQL resolvers, RPC handlers. Especially important for public-facing APIs
or APIs consumed by external teams.
```

- [ ] **Step 3: Create database design lens**

```markdown
---
name: Database Design
description: Schema normalization, index strategy, migration safety, data integrity
triggers: [schema, migration, model, sql, database, orm, table, column, index, query]
---

# Database Design Lens

## What to check

- New columns have appropriate NOT NULL constraints (nullable only when semantically correct)
- Foreign keys have ON DELETE behavior specified (CASCADE, SET NULL, RESTRICT)
- Indexes exist for columns used in WHERE, JOIN, and ORDER BY clauses
- Composite indexes have columns in selectivity order (most selective first)
- Migrations are backward-compatible (can roll back without data loss)
- Large table migrations avoid locking (use batched updates, not ALTER TABLE on hot tables)
- Enum types use string representations, not magic integers
- Timestamps use timezone-aware types (timestamptz, not timestamp)
- Default values are specified for new non-nullable columns in migrations
- Unique constraints exist where business logic requires uniqueness

## Common anti-patterns

- Adding NOT NULL column without default to existing table (breaks migration on non-empty tables)
- Missing indexes on foreign key columns (causes slow joins)
- Using LIKE '%term%' on unindexed text columns
- N+1 queries from ORM lazy loading
- Storing JSON blobs instead of normalized columns for structured data
- Using FLOAT for money (use DECIMAL or integer cents)
- Missing created_at/updated_at columns on mutable tables
- Cascading deletes that could wipe large amounts of data unexpectedly
- Schema migrations that are not idempotent

## When to apply

Any change involving database schemas, migrations, model definitions, or
complex queries. Especially important for: new tables, column additions to
large tables, index changes, and multi-table transactions.
```

- [ ] **Step 4: Create error handling lens**

```markdown
---
name: Error Handling
description: Exception strategy, error propagation, failure recovery, user-facing error messages
triggers: [error, exception, catch, try, handler, middleware, fault, failure, retry]
---

# Error Handling Lens

## What to check

- Catch blocks handle specific exception types, not bare except/catch-all
- Error context is preserved when re-raising (use `raise ... from e` or equivalent)
- User-facing error messages are helpful without leaking internals
- Transient failures have retry logic with exponential backoff and jitter
- Resource cleanup happens in finally blocks or context managers
- Error boundaries exist at system boundaries (API handlers, message consumers, job runners)
- Validation errors are collected and returned together, not one at a time
- Expected errors (user input, network) are handled differently from unexpected errors (bugs)
- Async operations have timeout and cancellation handling
- Error responses include enough context to debug (correlation ID, timestamp, error code)

## Common anti-patterns

- Swallowing exceptions silently (empty catch blocks)
- Logging the error but returning success to the caller
- Using exceptions for flow control (try/catch instead of if/else)
- Retrying non-idempotent operations on failure
- Catch-all at the top level that hides the real error
- String-matching on error messages instead of using typed errors
- Missing timeout on external calls (HTTP, database, file I/O)
- Returning generic "Something went wrong" to users for all errors
- Nested try/catch that makes control flow unreadable

## When to apply

Any change that adds error handling, modifies exception flow, or touches
code that calls external services. Especially important for: API handlers,
background jobs, database operations, and any multi-step workflows.
```

- [ ] **Step 5: Verify all lens files exist and have frontmatter**

Run: `ls -la jacked/data/lenses/ && head -4 jacked/data/lenses/*.md`
Expected: 4 files, each starting with `---` YAML frontmatter

- [ ] **Step 6: Commit**

```bash
git add jacked/data/lenses/
git commit -m "feat: add 4 specialist lenses — accessibility, api-ergonomics, database-design, error-handling"
```

---

## Task 4: Add lens awareness to `/dcr`

**Files:**
- Modify: `jacked/data/commands/dcr.md`

- [ ] **Step 1: Read the current dcr.md to find insertion point**

Read `jacked/data/commands/dcr.md` and locate the LENS SELECTION section (step 3d). The specialist lens awareness goes right after "3d. **Select lenses for this review.**"

- [ ] **Step 2: Add specialist lens discovery section**

Insert a new section `### SPECIALIST LENS DISCOVERY` between `### LENS SELECTION` (step 3d) and `### WAVE 1` (step 4). Add this block right before the line that says `3e. **Announce selected lenses with reasoning:**`:

```markdown
### SPECIALIST LENS DISCOVERY

3d-ii. **Check for installed specialist lenses.**

After selecting built-in lenses, check for specialist lens files:

1. Glob `~/.claude/lenses/*.md` and `.claude/lenses/*.md`. If neither directory exists, skip (lenses are optional).
2. Parse frontmatter of each file (name, description, triggers).
3. If both global and project-local have the same filename, project-local wins. Note: "Project lens `{name}.md` overrides global lens."
4. Match each lens's `triggers` against the domains identified from changed files (the same heuristic used to select built-in lenses above).
5. If an active checkpoint exists in `.claude/checkpoints/` with `active_lenses` in frontmatter, include those lenses regardless of trigger matching.
6. **Cap:** include at most 4 specialist lenses. If more match, take the top 4 by trigger specificity (most tags matched). Tiebreaker: alphabetical by filename. List remaining as "also relevant" in the announcement.

Each matched specialist lens is added to the selected lens pool alongside the built-in lenses. When pairing lenses for reviewers, specialist lenses can be paired with built-in lenses or with each other.

Each specialist lens becomes a reviewer instruction: "Additionally review through the **{lens.name}** lens. Use the following checklist and anti-patterns as your guide:\n{full lens file content}"
```

- [ ] **Step 3: Update the announcement format**

In the existing announcement block (step 3e), add a line showing specialist lenses:

After the existing `⊘` lines for skipped built-in lenses, add:

```markdown
    **Specialist lenses:**
      ✓ Accessibility (specialist) — frontend files changed
      ⊘ API Ergonomics — no API routes in diff
```

- [ ] **Step 4: Commit**

```bash
git add jacked/data/commands/dcr.md
git commit -m "feat: add specialist lens awareness to /dcr"
```

---

## Task 5: Add checkpoint awareness to `/whats-next`

**Files:**
- Modify: `jacked/data/commands/whats-next.md`

- [ ] **Step 1: Read the current whats-next.md**

Read the full file to find the right insertion points.

- [ ] **Step 2: Add checkpoint check to Step 1 (Orient)**

At the very beginning of Step 1 (Orient), before the git commands, add:

```markdown
**Check for active checkpoint first:**

```bash
CHECKPOINT_DIR=".claude/checkpoints"
if [ -d "$CHECKPOINT_DIR" ]; then
  ls -1t "$CHECKPOINT_DIR"/*.md 2>/dev/null | head -5
fi
```

If any checkpoint files exist, read their frontmatter. If one or more have `status: in-progress`, note the most recent one — it becomes **Option 0** in the recommendations (Step 6).

If multiple in-progress checkpoints exist, note the count for the recommendation display.
```

- [ ] **Step 3: Add Option 0 to Step 6 (Present Recommendations)**

In Step 6, before `### Option 1:`, add:

```markdown
### Option 0: Resume Active Checkpoint (if applicable)

If an in-progress checkpoint was found in Step 1:

```
### Option 0: Resume — {checkpoint title}
- **Tier**: 0 — Active work in progress
- **Impact**: 5 — continuing existing momentum
- **Effort**: S (context already captured)
- **What to do**: Run `/checkpoint resume` to load full context and continue
- **Branch**: {checkpoint branch}
- **Last saved**: {checkpoint date, human-readable}
- **Remaining**: {first 3 items from checkpoint's Remaining Work section}
- **Evidence**: checkpoint file at {path}
```

If multiple in-progress checkpoints: "**+{N} older checkpoints** — run `/checkpoint list` to see all."

Always present other options below Option 0 — the user may want to switch focus.
```

- [ ] **Step 4: Add lens gap detection to Step 6**

After the `## Quick Wins` section in Step 6, add:

```markdown
## Lens Coverage Gaps

Check for installed specialist lenses (`~/.claude/lenses/*.md` and `.claude/lenses/*.md`). If lenses exist, cross-reference their `triggers` against recent git activity:

```bash
git log --name-only --pretty=format: --since="14 days ago" 2>/dev/null | sort -u | head -50
```

If recently modified files match a lens's triggers but no recent commit messages or DCR output reference that lens domain, suggest:

> "Recent work has touched {domain} files but no {lens.name} review has been done — consider including it in the next `/dcr`."

Only mention gaps for lenses whose triggers match actual recent file changes. Don't suggest lenses for domains the project hasn't touched.
```

- [ ] **Step 5: Commit**

```bash
git add jacked/data/commands/whats-next.md
git commit -m "feat: add checkpoint awareness and lens gap detection to /whats-next"
```

---

## Task 6: Add accessibility lens to `/qa` and `/ux`

**Files:**
- Modify: `jacked/data/commands/qa.md`
- Modify: `jacked/data/commands/ux.md`

- [ ] **Step 1: Read qa.md to find the checklist section**

Read the full `jacked/data/commands/qa.md` to find where the testing checklist is defined.

- [ ] **Step 2: Add lens checklist to /qa**

Find the section where QA generates its testing checklist. Add this block after it:

```markdown
### Accessibility Lens (if available)

Check if an accessibility specialist lens is installed:

```bash
ls ~/.claude/lenses/accessibility.md .claude/lenses/accessibility.md 2>/dev/null | head -1
```

If found, read it and incorporate its "What to check" items into your testing checklist. These are **additive** — they don't replace your existing QA checks. Focus on items that can be verified visually or via browser DevTools:

- Color contrast (use DevTools accessibility panel or Lighthouse)
- Keyboard navigation (tab through the page, verify focus indicators)
- Semantic HTML (inspect elements — buttons should be `<button>`, not `<div>`)
- Form labels (each input has a visible, associated `<label>`)
- Focus management after interactions (modal open/close, route changes)

Skip items that require specialized tooling (screen reader testing, automated WCAG scanners) unless the user specifically requests them.
```

- [ ] **Step 3: Add lens checklist to /ux**

Read `jacked/data/commands/ux.md` and find the equivalent checklist section. Add the same accessibility lens block as in Step 2 (repeated, not referenced — the engineer may read tasks out of order).

- [ ] **Step 4: Commit**

```bash
git add jacked/data/commands/qa.md jacked/data/commands/ux.md
git commit -m "feat: add accessibility lens awareness to /qa and /ux"
```

---

## Task 7: Add lens awareness to `/jack-it-up` and `/techdebt`

**Files:**
- Modify: `jacked/data/skills/jack-it-up/SKILL.md`
- Modify: `jacked/data/commands/techdebt.md`

- [ ] **Step 1: Read jack-it-up/SKILL.md and techdebt.md**

Read both files to find the insertion points.

- [ ] **Step 2: Add lens awareness to /jack-it-up brainstorm phase**

In `jacked/data/skills/jack-it-up/SKILL.md`, find `### Phase 1: Brainstorm`. Add after the existing content of that section:

```markdown
**Lens awareness:** Before presenting the design, check for installed specialist lenses:

```bash
ls ~/.claude/lenses/*.md .claude/lenses/*.md 2>/dev/null
```

If lenses exist, read their frontmatter (name, description, triggers). If any lens triggers match the feature being brainstormed (e.g., building UI → accessibility lens, building API → api-ergonomics lens), surface relevant design considerations:

> "The **{lens.name}** lens suggests considering: {2-3 key items from the lens's 'What to check' section relevant to this feature}"

This is informational only — it doesn't block or change the brainstorm flow. It ensures specialist concerns are raised during design rather than caught late in review.
```

- [ ] **Step 3: Read techdebt.md**

Read `jacked/data/commands/techdebt.md` to find where debt scanning is defined.

- [ ] **Step 4: Add lens-based anti-pattern scanning to /techdebt**

Find the section where techdebt scans for issues. Add this block:

```markdown
### Specialist Lens Anti-Patterns

Check for installed specialist lenses:

```bash
ls ~/.claude/lenses/*.md .claude/lenses/*.md 2>/dev/null
```

If lenses exist, read their frontmatter to find lenses whose `triggers` match the codebase (check file extensions and directory names in the project). For each matched lens, read its "Common anti-patterns" section.

Use these anti-patterns as **review guidance** — look for them in the codebase alongside the standard TODO/FIXME/HACK scanning. These are interpreted by you (the LLM reviewer), not grepped as literal patterns. For example, if the accessibility lens mentions "Using div/span as buttons instead of semantic button/a elements," look for that pattern in any frontend code you're reviewing.

Report lens-based findings in the same format as other techdebt findings, tagged with the lens name:

```
[Accessibility] Using <div onClick> as buttons in src/components/NavItem.tsx:23
  → Should use <button> for keyboard accessibility and screen reader support
```
```

- [ ] **Step 5: Commit**

```bash
git add jacked/data/skills/jack-it-up/SKILL.md jacked/data/commands/techdebt.md
git commit -m "feat: add lens awareness to /jack-it-up brainstorm and /techdebt scanning"
```

---

## Task 8: Add session-start checkpoint detection to CLAUDE.md

**Files:**
- Modify: global `~/.claude/CLAUDE.md` (jacked-behaviors section)

- [ ] **Step 1: Read the current jacked-behaviors section**

Read `~/.claude/CLAUDE.md` and locate the `# jacked-behaviors-v2` section.

- [ ] **Step 2: Add checkpoint detection rule**

Add this rule after the existing "At the start of a session, read `lessons.md`" rule and before the "After any correction" rule:

```markdown
- At the start of a session, check `.claude/checkpoints/` for any files with `status: in-progress` in their YAML frontmatter. If found, mention the most recent one: "Found an active checkpoint: **{title}** ({date}). Run `/checkpoint resume` to pick up where you left off." If multiple in-progress checkpoints exist, add "(+N older)". One line only, not pushy.
```

- [ ] **Step 3: Commit**

This is a user-level config file, not in the repo. Document the rule addition but do NOT commit `~/.claude/CLAUDE.md` to this repo. Instead, verify the rule was added:

```bash
grep -c "checkpoint" ~/.claude/CLAUDE.md
```

Expected: At least 1 match.

---

## Task 9: Run full test suite and verify installation

- [ ] **Step 1: Run all tests**

Run: `uv run python -m pytest tests/ -v --tb=short`
Expected: All tests PASS

- [ ] **Step 2: Test the install flow**

Run: `jacked install --force`
Expected output should include:
- `[OK] Installed 9 skills` (now includes checkpoint)
- `[OK] Installed 4 lenses`
- `[OK] Installed 23 commands`

- [ ] **Step 3: Verify installed files**

```bash
ls ~/.claude/skills/checkpoint/SKILL.md
ls ~/.claude/lenses/accessibility.md ~/.claude/lenses/api-ergonomics.md ~/.claude/lenses/database-design.md ~/.claude/lenses/error-handling.md
```

Expected: All files exist.

- [ ] **Step 4: Test uninstall**

Run: `jacked uninstall --yes` then `ls ~/.claude/lenses/`
Expected: Lenses directory is empty or removed.

- [ ] **Step 5: Reinstall**

Run: `jacked install --force`
Expected: Everything reinstalled cleanly.

- [ ] **Step 6: Commit final state**

```bash
git add -A
git commit -m "chore: verify full install/uninstall cycle with lenses and checkpoint"
```

Only commit if there are actual changes (e.g., test files or minor fixes discovered during verification).

---

## Out of scope

- **`/coverage-matrix` integration** — the spec mentions it but `/coverage-matrix` is a superpowers plugin skill, not a jacked command. Integration would happen in the superpowers plugin repo, not here. Deferred.

## Task Groups (for parallel execution)

**Group A (independent, run in parallel):**
- Task 1: Extract `_install_asset_dir()` helper
- Task 2: Create checkpoint skill
- Task 3: Create 4 specialist lens files

**Group B (depends on Group A completion):**
- Task 4: Add lens awareness to `/dcr`
- Task 5: Add checkpoint awareness to `/whats-next`
- Task 6: Add accessibility lens to `/qa` and `/ux`
- Task 7: Add lens awareness to `/jack-it-up` and `/techdebt`

Tasks 4-7 are independent of each other and can run in parallel.

**Group C (depends on Group B):**
- Task 8: Add session-start checkpoint detection to CLAUDE.md
- Task 9: Run full test suite and verify installation
