# Session Continuity & Specialist Lenses

Three interconnected features that make cross-session development seamless and reviews context-aware.

1. **Checkpoint skill** — save/resume session state with full knowledge preservation
2. **Specialist lenses** — curated review perspectives installed with jacked
3. **Skill integration** — existing skills become checkpoint-aware and lens-aware

## 1. Checkpoint Skill

### What it does

Captures everything a new session needs to continue where the last one left off: progress state, design decisions, research findings, user-provided context, and references to all relevant files. On resume, auto-loads everything referenced so the new session has full context immediately.

### Storage

- Checkpoint files: `.claude/checkpoints/{timestamp}-{slug}.md`
- Research summaries: `.claude/research/{date}-{topic}.md`
- Both are project-local, git-trackable

### File format — Checkpoint

```markdown
---
status: in-progress | completed
branch: feature-branch
timestamp: 2026-04-12T14:30:00-04:00
releases: [v1.2.0, v1.2.1]
files_modified:
  - path/to/file.py
plans_in_progress:
  - docs/superpowers/plans/2026-04-12-feature.md
research_files:
  - .claude/research/2026-04-12-api-comparison.md
key_files:
  - src/auth/middleware.py
  - src/auth/token.py
lessons_added:
  - "Integration tests must hit real DB, not mocks"
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
Sources cited. Full details in research_files listed in frontmatter.

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

### File format — Research summary

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
/checkpoint list         — show all checkpoints with status
```

### Save flow

1. Gather git state (branch, status, recent log)
2. Summarize from conversation context:
   - Progress, decisions, remaining work (already captured today)
   - **Session Context** — constraints, user intent, verbal domain knowledge
   - **Research & References** — summarize any web fetches or API docs consulted
3. For each research topic with substantial findings, write a `.claude/research/{date}-{topic}.md` file with source URLs and distilled findings
4. Write checkpoint file with frontmatter referencing research files, active plan, lessons added, and which lenses were active
5. Display confirmation with title, branch, file path

### Resume flow

1. Find most recent in-progress checkpoint (or user-specified one)
2. Read the checkpoint file
3. **Auto-load all referenced files:**
   - Every file in `plans_in_progress` (frontmatter — structured YAML list)
   - Every file in `research_files` (frontmatter — structured YAML list)
   - Every file in `key_files` (frontmatter — structured YAML list, added alongside the markdown section for parseability)
   - Cap: skip any file over 500 lines, warn instead ("Skipped {path} — {N} lines. Read manually if needed.")
4. Present the checkpoint summary
5. Continue with the first remaining work item

### Session-start behavior

CLAUDE.md rule: on session start, check `.claude/checkpoints/` for any `status: in-progress` files. If found, mention it:

> "Found an active checkpoint: **{title}** ({date}). Run `/checkpoint resume` to pick up where you left off, or `/whats-next` for fresh recommendations."

One line, not pushy. User decides.

### Trigger

Explicit only. User runs `/checkpoint`. No auto-suggest, no auto-save.

## 2. Specialist Lenses

### What they are

Lightweight review perspectives — focused prompt templates that plug into existing review workflows. Not full skills with multi-step orchestration, just expert knowledge distilled into "look at this from X perspective."

### Storage & discovery

- **Global (installed by jacked):** `~/.claude/lenses/*.md`
- **Project-local (user-created):** `.claude/lenses/*.md`
- **Resolution:** project-local overrides global on name collision
- **Source in repo:** `jacked/data/lenses/*.md`

### File format

```markdown
---
name: Accessibility
description: WCAG 2.2 compliance, keyboard nav, screen readers, color contrast
triggers: [ui, frontend, css, html, component, page, form, button, input, modal, dialog]
tier: quality
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

### Trigger matching

Skills that consume lenses match by:
1. **File extensions** of changed files against trigger tags using this mapping:
   - `.css`, `.scss`, `.less` → `css`
   - `.html`, `.htm` → `html`
   - `.tsx`, `.jsx`, `.vue`, `.svelte` → `frontend`, `ui`, `component`
   - `.ts`, `.js` in `components/`, `pages/`, `views/` → `frontend`, `ui`, `component`
   - `.ts`, `.js` in `api/`, `routes/`, `handlers/` → `api`, `route`, `endpoint`, `handler`
   - `.sql`, `.py` in `migrations/`, `models/` → `schema`, `migration`, `model`, `database`
   - `.test.*`, `.spec.*` → `test`, `spec`
   - `.py`, `.ts`, `.js` (generic) → match against trigger tags by content/directory context
2. **Directory names** against trigger tags (`components/` → `component`, `api/` → `api`)
3. **Checkpoint domain** — if active checkpoint says "working on auth," the `security` lens gets boosted

No fuzzy matching, no ML. Simple tag intersection. When in doubt, include the lens — a slightly broad match is better than missing a relevant perspective.

### Tier field

Used by `/coverage-matrix` to categorize:
- `security` — data protection, auth, injection prevention
- `quality` — accessibility, performance, observability
- `design` — API ergonomics, schema design, naming
- `compliance` — HIPAA, SOC2, GDPR (project-local typically)

### Initial lens set (shipped with jacked)

| Lens | Triggers | Tier |
|---|---|---|
| `accessibility.md` | ui, frontend, css, html, component, form, modal | quality |
| `api-ergonomics.md` | api, route, endpoint, handler, rest, graphql | design |
| `database-design.md` | schema, migration, model, sql, database, orm | design |
| `error-handling.md` | error, exception, catch, try, handler, middleware | quality |
| `observability.md` | log, metric, trace, monitor, alert, health | quality |
| `performance.md` | query, cache, loop, render, bundle, load | quality |
| `security.md` | auth, token, password, session, cookie, cors, csp | security |
| `testing-strategy.md` | test, spec, mock, fixture, factory, coverage | quality |

Eight lenses to start. Users add project-specific ones to `.claude/lenses/`.

### Installation

Same mechanism as skills/commands in `cli.py install()`:
- Glob `jacked/data/lenses/*.md`
- Copy to `~/.claude/lenses/`
- Symlink for editable installs
- `--force` overwrites

## 3. Skill Integration Updates

Small, targeted additions to existing skills. No rewrites — just awareness.

### `/dcr` — Primary lens consumer

**Where:** After selecting review angles, before spawning reviewers.

**Addition:**
1. Glob `~/.claude/lenses/*.md` and `.claude/lenses/*.md`
2. Parse frontmatter of each (name, triggers, description only)
3. Match trigger tags against files in the diff
4. If active checkpoint exists, boost lenses matching the checkpoint domain
5. Include matched lenses as additional review angles alongside DCR's existing random angles
6. Each lens becomes a reviewer prompt: "Review this change through the {lens.name} lens: {lens content}"

### `/coverage-matrix` — Lenses as completeness dimensions

**Where:** When building the coverage matrix dimensions.

**Addition:**
1. Scan all available lenses (global + project-local)
2. Each lens tier becomes a matrix category
3. Each lens becomes a row: "Has this project addressed {lens.name} concerns?"
4. Score based on: relevant tests exist, recent reviews covered it, known gaps

### `/qa` — Accessibility and performance checklists

**Where:** During browser testing checklist generation.

**Addition:**
1. Check if `accessibility` lens exists → include its checklist items during visual QA
2. Check if `performance` lens exists → include its client-side performance items
3. Items are additive to QA's existing checks, not replacing

### `/ux` — Same as QA

**Where:** During multi-component UX validation.

**Addition:** Same as `/qa` — pull accessibility lens for UX review.

### `/jack-it-up` — Brainstorm-phase lens awareness

**Where:** During Phase 1 (brainstorming), after understanding what's being built.

**Addition:**
1. Scan lenses for ones relevant to the feature domain
2. Surface as design considerations: "The accessibility lens suggests considering keyboard nav for this component"
3. Informational only — doesn't block or change the brainstorm flow

### `/whats-next` — Checkpoint awareness + lens gap detection

**Where:** At the start of analysis (checkpoint) and after synthesizing recommendations (lenses).

**Addition — checkpoint:**
1. Before running any discovery, check `.claude/checkpoints/` for `status: in-progress` files
2. If found, the active checkpoint is the **top recommendation**: "Resume active checkpoint: **{title}** — run `/checkpoint resume` to load full context"
3. Still present other options below it, but the checkpoint is Option 0

**Addition — lenses:**
1. Check git log for which lens domains have been reviewed recently
2. If a lens relevant to recent work hasn't been applied, suggest it as a quick win: "You've been shipping UI but haven't run an accessibility review — consider adding it to the next /dcr"

### `/techdebt` — Domain-specific debt patterns

**Where:** When scanning for debt beyond TODO/FIXME markers.

**Addition:**
1. Each lens's "common anti-patterns" section defines domain-specific debt
2. Scan for anti-patterns from lenses whose triggers match the codebase
3. Report alongside existing techdebt findings

### `/checkpoint` — Records lens context

**Where:** During checkpoint save.

**Addition:**
1. `active_lenses` frontmatter field records which lenses were relevant during the session
2. Next session's `/dcr` knows which lenses to prioritize from the checkpoint

### Session-start CLAUDE.md rule

**Current:** Read `lessons.md`, check version.

**Addition:** After version check:
1. Check `.claude/checkpoints/` for `status: in-progress` files
2. If found, display one-line mention with title and date
3. User decides whether to resume or start fresh

## Installation changes

### `jacked install` additions

1. **Lenses:** Glob `jacked/data/lenses/*.md` → copy/symlink to `~/.claude/lenses/`. Same pattern as skills.
2. **Checkpoint skill:** `jacked/data/skills/checkpoint/SKILL.md` → `~/.claude/skills/checkpoint/SKILL.md`. Already handled by existing skill install loop.
3. **CLAUDE.md rule:** The session-start checkpoint detection is a CLAUDE.md instruction, not a hook. Documented for manual addition (or added by `jacked-setup`).

### `jacked install` output

```
[OK] Installed 9 skills (checkpoint is new)
[OK] Installed 8 lenses
[OK] Installed 23 commands
...
```

## What this does NOT do

- No Qdrant dependency. Everything is project-local files.
- No auto-save checkpoints. Explicit `/checkpoint` only.
- No auto-suggest checkpoints. User triggers when they want.
- No separate lens commands. Lenses are consumed by existing skills, not invoked directly.
- No full web page archival. Research summaries are distilled, with source URLs for reference.
- No changes to the checkpoint file format beyond adding `research_files`, `lessons_added`, `active_lenses`, and the `Session Context` / `Research & References` sections.

## Implementation order

1. **Checkpoint skill** — move existing `~/.claude/skills/checkpoint/SKILL.md` into jacked source, enhance with research capture, session context, and auto-load resume
2. **Lenses** — create `jacked/data/lenses/` with 8 initial lens files
3. **Installer update** — add lens installation to `cli.py install()`
4. **CLAUDE.md rule** — add session-start checkpoint detection
5. **Skill updates** — add lens/checkpoint awareness to /dcr, /coverage-matrix, /qa, /ux, /jack-it-up, /whats-next, /techdebt (small diffs each)
