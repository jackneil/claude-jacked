# Session Continuity & Specialist Lenses

Three interconnected features that make cross-session development seamless and reviews context-aware.

1. **Checkpoint skill** — save/resume session state with full knowledge preservation
2. **Specialist lenses** — curated review perspectives that extend DCR's existing lens system
3. **Skill integration** — existing skills become checkpoint-aware and lens-aware

## 1. Checkpoint Skill

### What it does

Captures everything a new session needs to continue where the last one left off: progress state, design decisions, research findings, user-provided context, and references to all relevant files. On resume, auto-loads everything referenced so the new session has full context immediately.

### Storage

- Checkpoint files: `.claude/checkpoints/{timestamp}-{slug}.md`
- Research summaries (when substantial): `.claude/research/{timestamp}-{topic}.md`
- Both are project-local, git-trackable

### File format — Checkpoint

```markdown
---
status: in-progress | completed
branch: feature-branch
timestamp: 2026-04-12T14:30:00-04:00
releases: [v1.2.0, v1.2.1]
plans_in_progress:
  - docs/superpowers/plans/2026-04-12-feature.md
research_files:
  - .claude/research/20260412-143000-api-comparison.md
active_lenses: [security, api-ergonomics]
---

# Checkpoint: {title}

## What We're Working On

1-3 sentences: the high-level goal.

## Accomplished This Session

Bulleted list — commits, features, releases.

## Decisions Made

Bulleted list with reasoning — WHY each choice was made.

## Session Context

Non-obvious knowledge that only existed in conversation: user intent, constraints,
domain facts shared verbally, "build it like X" references, things tried and failed.
This section preserves the interactive knowledge that would otherwise die with the
context window.

## Research & References

Summary of web research, API docs, reference material gathered.
Sources cited. For large research topics, full details in separate files
listed in research_files frontmatter. For small topics, inline here is fine.

## Remaining Work

Numbered list of concrete next steps, in priority order.
References plan files where applicable.

## Current State

Where exactly we stopped — mid-implementation? waiting for DCR? etc.

## Gotchas & Notes

Failed approaches, known issues, edge cases discovered.

## Key Files

The most important files to read to get up to speed.
```

**Frontmatter field semantics:**
- `plans_in_progress`, `research_files` — **file paths** (resolved against project root)
- `active_lenses` — **lens names** (matched against lens file `name` field, NOT file paths)
- All list fields are optional. Missing fields treated as empty lists for backward compatibility with older checkpoints.

### File format — Research summary

Used when research is substantial enough to warrant a separate file. For quick lookups, inline in the checkpoint's "Research & References" section instead.

```markdown
---
topic: API rate limiting patterns
sources:
  - https://stripe.com/docs/rate-limiting
  - https://cloud.google.com/apis/design/errors
date: 2026-04-12
checkpoint: 20260412-143000-auth-system
---

# API Rate Limiting Patterns

## Findings

{Distilled research — key patterns, comparisons, recommendations.
Not a copy of the web page, but the actionable knowledge extracted.}

## Source Notes

{Per-source: what was useful, what wasn't, key quotes or data points.}
```

### Commands

```
/checkpoint              — save current session state
/checkpoint resume       — load most recent in-progress checkpoint, auto-load all referenced files
/checkpoint complete     — mark the most recent in-progress checkpoint as completed
/checkpoint list         — show all checkpoints with status
```

### Save flow

1. Gather git state (branch, status, recent log)
2. Summarize from conversation context:
   - Progress, decisions, remaining work
   - **Session Context** — constraints, user intent, verbal domain knowledge
   - **Research & References** — summarize any web fetches or API docs consulted
3. For research topics with substantial findings (multiple sources, detailed analysis), write a `.claude/research/{YYYYMMDD-HHMMSS}-{topic}.md` file. For quick lookups, inline in the checkpoint.
4. Write checkpoint file:
   - Write to a temp file first, then rename to final path (atomic write prevents partial checkpoint on Ctrl+C)
   - If another in-progress checkpoint exists on the same branch, prompt: "Mark previous checkpoint **{title}** as completed?" (default yes)
   - Frontmatter references research files, active plan, and which lenses were active
5. Display confirmation with title, branch, file path

### Resume flow

1. Find most recent in-progress checkpoint (or user-specified one)
2. Read the checkpoint file
3. **Branch check:** If current branch differs from checkpoint's branch, warn: "Checkpoint was created on branch **{branch}** but you are on **{current_branch}**. Context may not apply. Continue anyway?" Do not auto-switch branches.
4. **Auto-load referenced files with budget:**
   - Read files in priority order: `plans_in_progress` → `research_files` → files mentioned in Key Files section
   - For each file: if it doesn't exist, warn "Referenced file {path} no longer exists (may have been renamed/deleted since checkpoint)" and continue
   - Budget: stop loading after ~3000 total lines across all files. Present remaining as "Also referenced (not loaded): {list}" so the user can request specific ones.
5. Present the checkpoint summary
6. Continue with the first remaining work item

### Checkpoint lifecycle

- **Creating:** `/checkpoint` writes a new file. If another in-progress checkpoint exists on the same branch, offer to mark it completed.
- **Completing:** `/checkpoint complete` marks the most recent in-progress checkpoint as `completed`. Also happens implicitly when creating a new checkpoint on the same branch (with user confirmation).
- **Multiple in-progress:** Can happen across branches or if the user skips the completion prompt. Session-start shows the most recent one only, with a count of others: "Found active checkpoint: **{title}** ({date}) (+2 older). Run `/checkpoint list` to see all."
- **Backward compatibility:** Existing checkpoint files that lack `research_files` or `active_lenses` fields work fine — all list fields default to empty.

### Session-start behavior

CLAUDE.md rule: on session start, check `.claude/checkpoints/` for any `status: in-progress` files. If found, mention the most recent:

> "Found an active checkpoint: **{title}** ({date}). Run `/checkpoint resume` to pick up where you left off."

One line, not pushy. User decides. If the user runs `/whats-next` instead, that command also surfaces the checkpoint (see Skill Integration section) — this is intentional redundancy, not duplication, since the user may not see the session-start message.

### Checkpoint vs. /jacked

These are complementary, not competing:
- **`/checkpoint`** — structured local files for the current project. Fast, offline, git-tracked. Best for: picking up where you left off in this repo.
- **`/jacked`** — Qdrant-backed cross-repo session search. Best for: "how did I solve X in a different project?" or searching teammates' sessions.

If both are available: `/checkpoint resume` for continuing current work, `/jacked` for pulling in context from other projects or past sessions beyond the most recent.

### Trigger

Explicit only. User runs `/checkpoint`. No auto-suggest, no auto-save.

## 2. Specialist Lenses

### What they are

Lightweight review perspectives that **extend DCR's existing lens pool** — not a parallel system. DCR already has 11 built-in lenses (Security, Performance, Testing, etc.). Specialist lenses add new domains DCR doesn't cover (accessibility, API ergonomics, database design) and are also consumed by other skills (/qa, /coverage-matrix, etc.) for domain-specific checklists and patterns.

### Relationship to DCR's built-in lenses

DCR's 11 built-in lenses are defined inline in the `/dcr` command. Specialist lenses are file-based and user-extensible. They do NOT duplicate DCR's built-in domains:

| DCR built-in | Specialist lens | Relationship |
|---|---|---|
| Security (#2) | `security.md` | NOT shipped — DCR covers this |
| Performance (#6) | `performance.md` | NOT shipped — DCR covers this |
| Testing (#7) | `testing-strategy.md` | NOT shipped — DCR covers this |
| Observability (#10) | `observability.md` | NOT shipped — DCR covers this |
| — | `accessibility.md` | NEW — DCR has no accessibility lens |
| — | `api-ergonomics.md` | NEW — DCR has no API design lens |
| — | `database-design.md` | NEW — DCR has no schema/migration lens |
| — | `error-handling.md` | NEW — DCR has no dedicated error handling lens |

When DCR selects lenses, it checks both its built-in set AND any installed specialist lens files. Specialist lenses are additional review angles, never redundant with built-ins.

### Storage & discovery

- **Global (installed by jacked):** `~/.claude/lenses/*.md`
- **Project-local (user-created):** `.claude/lenses/*.md`
- **Resolution:** project-local overrides global on **filename** collision (not frontmatter `name`). A project-local `accessibility.md` fully replaces the global one.
- **Source in repo:** `jacked/data/lenses/*.md`

### File format

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

## Common anti-patterns

- Using div/span as buttons instead of semantic button/a elements
- Hiding focus outlines with outline:none without providing alternative
- Auto-playing media without controls
- Using tabindex > 0 (disrupts natural tab order)
- Relying on hover states for essential information

## When to apply

Any change that touches user-facing HTML, components, or styling.
Especially important for: forms, modals/dialogs, navigation, data tables,
error states, and any interactive widget.
```

### Lens discovery pattern (shared across all consumers)

All skills that consume lenses use the same pattern:

1. Glob `~/.claude/lenses/*.md` and `.claude/lenses/*.md`
2. Parse frontmatter only (name, description, triggers)
3. Project-local overrides global on filename collision
4. Match triggers against context (changed files, directory names, checkpoint domain)
5. Return matched lens list

This pattern is described once here and referenced by each skill integration. Implementation should extract this into a reusable section in the checkpoint/lens skill or a shared instruction block — not reimplemented per skill.

### Trigger matching

Triggers are simple tags. Matching is done by DCR's existing heuristic (which already analyzes changed file types and maps them to relevant lenses). The `triggers` frontmatter provides hints to that heuristic:

- DCR checks changed files → identifies domains (API, UI, database, etc.)
- DCR checks installed lenses → matches their `triggers` against identified domains
- If active checkpoint has `active_lenses`, those are always included regardless of trigger matching
- **Cap:** If more than 4 specialist lenses match, include the top 4 by specificity (most trigger tags matched). List remaining as "also relevant" in the announcement.

No separate trigger matching engine. DCR's lens selection step (which already exists) simply gains awareness of external lens files.

### Initial lens set (shipped with jacked)

| Lens | Triggers | Domain |
|---|---|---|
| `accessibility.md` | ui, frontend, css, html, component, form, modal | Quality |
| `api-ergonomics.md` | api, route, endpoint, handler, rest, graphql | Design |
| `database-design.md` | schema, migration, model, sql, database, orm | Design |
| `error-handling.md` | error, exception, catch, try, handler, middleware | Quality |

Four lenses that cover domains DCR's built-in lenses don't. Users add project-specific ones to `.claude/lenses/`.

### Installation

Same mechanism as agents/commands in `cli.py install()`. **Implementation note:** the existing agents and commands install loops are near-identical 40-line blocks. Before adding lenses, extract the shared pattern into a helper: `_install_asset_dir(src_dir, dst_dir, asset_type_label, force)`. Use it for agents, commands, AND lenses. Conflict handling follows the agents/commands pattern: prompt on content mismatch, skip on same content, `--force` to overwrite.

## 3. Skill Integration Updates

Small, targeted additions to existing skills. Each consumes lenses via the shared discovery pattern defined above — no per-skill reimplementation.

### `/dcr` — Primary lens consumer

**Where:** During lens selection (step 3d), before spawning reviewers.

**Addition:**
1. Run the shared lens discovery pattern against files in the diff
2. If active checkpoint exists, include its `active_lenses` regardless of trigger matching
3. Matched specialist lenses become additional review angles alongside DCR's built-in lens selection
4. Each specialist lens becomes a reviewer prompt: "Additionally review through the {lens.name} lens: {lens content}"
5. **Cap:** max 4 specialist lenses per DCR wave (on top of built-in selections). If more match, prioritize by trigger specificity.
6. **Announcement:** include specialist lenses in the lens selection announcement: `✓ Accessibility (specialist lens) — frontend files changed`

### `/coverage-matrix` — Lenses as completeness dimensions

**Where:** When building the coverage matrix dimensions.

**Addition:**
1. Run the shared lens discovery pattern to find all available lenses (global + project-local)
2. Each specialist lens becomes a completeness dimension: "Has this project addressed {lens.name} concerns?"
3. Score based on: relevant tests exist, recent reviews covered it, known gaps

### `/qa` — Accessibility and performance checklists

**Where:** During browser testing checklist generation.

**Addition:**
1. Check if `accessibility` lens file exists (glob, not trigger matching)
2. If found, include its "What to check" items during visual QA
3. Items are additive to QA's existing checks, not replacing

### `/ux` — Same as QA

**Where:** During multi-component UX validation.

**Addition:** Same as `/qa` — pull accessibility lens for UX review.

### `/jack-it-up` — Brainstorm-phase lens awareness

**Where:** During Phase 1 (brainstorming), after understanding what's being built.

**Addition:**
1. Run the shared lens discovery pattern for the feature domain
2. Surface as design considerations: "The accessibility lens suggests considering keyboard nav for this component"
3. Informational only — doesn't block or change the brainstorm flow

### `/whats-next` — Checkpoint awareness + lens gap detection

**Where:** At the start of analysis (checkpoint) and after synthesizing recommendations (lenses).

**Addition — checkpoint:**
1. Before running any discovery, check `.claude/checkpoints/` for `status: in-progress` files
2. If found, the active checkpoint is the **top recommendation**: "Resume active checkpoint: **{title}** — run `/checkpoint resume` to load full context"
3. If multiple in-progress checkpoints, show most recent + count of others
4. Still present other options below it, but the checkpoint is Option 0

**Addition — lenses:**
1. Check git log for which lens domains have been reviewed recently
2. If a lens relevant to recent work hasn't been applied, suggest it as a quick win: "You've been shipping UI but haven't run an accessibility review — consider adding it to the next /dcr"

### `/techdebt` — Domain-specific debt patterns

**Where:** When scanning for debt beyond TODO/FIXME markers.

**Addition:**
1. Run the shared lens discovery pattern to find lenses relevant to the codebase
2. Each matched lens's "Common anti-patterns" section provides review guidance (interpreted by the LLM reviewer, not grepped as literal patterns)
3. Report alongside existing techdebt findings

### `/checkpoint` — Records lens context

**Where:** During checkpoint save.

**Addition:**
1. `active_lenses` frontmatter field records which lenses were relevant during the session (by name)
2. Next session's `/dcr` includes these lenses regardless of trigger matching

### Session-start CLAUDE.md rule

**Current:** Read `lessons.md`, check version.

**Addition:** After version check:
1. Check `.claude/checkpoints/` for `status: in-progress` files
2. If found, display one-line mention with title, date, and count of older checkpoints
3. User decides whether to resume or start fresh

## Installation changes

### `jacked install` additions

1. **Lenses:** via the new `_install_asset_dir()` helper — `jacked/data/lenses/*.md` → `~/.claude/lenses/`. Symlink for editable, copy otherwise, prompt on conflict, `--force` to overwrite.
2. **Checkpoint skill:** `jacked/data/skills/checkpoint/SKILL.md` → `~/.claude/skills/checkpoint/SKILL.md`. Already handled by existing skill install loop.
3. **CLAUDE.md rule:** The session-start checkpoint detection is a CLAUDE.md instruction, not a hook. Documented for manual addition (or added by `jacked-setup`).

### `jacked install` output

```
[OK] Installed 9 skills (checkpoint is new)
[OK] Installed 4 lenses
[OK] Installed 23 commands
...
```

## What this does NOT do

- No Qdrant dependency. Everything is project-local files. (`/jacked` remains the cross-repo search tool if Qdrant is configured.)
- No auto-save checkpoints. Explicit `/checkpoint` only.
- No auto-suggest checkpoints. User triggers when they want.
- No separate lens invocation commands. Lenses are consumed by existing skills, not invoked directly. Users inspect installed lenses by reading `~/.claude/lenses/` or `.claude/lenses/`.
- No full web page archival. Research summaries are distilled, with source URLs for reference.
- No duplication of DCR's built-in lens domains. Specialist lenses cover only new domains.

## Implementation order

1. **Installer refactor** — extract `_install_asset_dir()` helper from agents/commands install loops
2. **Checkpoint skill** — move existing `~/.claude/skills/checkpoint/SKILL.md` into `jacked/data/skills/checkpoint/SKILL.md`, enhance with: research capture, session context, completion lifecycle, branch check, atomic write, context budget on resume, backward-compatible frontmatter
3. **Lenses** — create `jacked/data/lenses/` with 4 initial lens files (accessibility, api-ergonomics, database-design, error-handling)
4. **Installer update** — add lens installation via `_install_asset_dir()`
5. **CLAUDE.md rule** — add session-start checkpoint detection
6. **Skill updates** — add lens/checkpoint awareness to /dcr, /coverage-matrix, /qa, /ux, /jack-it-up, /whats-next, /techdebt (each uses shared lens discovery pattern)
