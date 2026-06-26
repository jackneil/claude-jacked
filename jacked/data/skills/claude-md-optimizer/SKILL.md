---
name: claude-md-optimizer
description: Use when CLAUDE.md feels bloated or too long, when starting a new project, or when optimizing documentation for token efficiency. Audits content quality, extracts reference material to sub-docs, and enforces a token budget.
---

# CLAUDE.md Optimizer

Audit, restructure, and optimize CLAUDE.md files for maximum signal-to-token ratio. Combines content quality assessment with token efficiency — because a CLAUDE.md that's complete but bloated wastes thousands of tokens every turn.

## Core Principle

**CLAUDE.md gets re-injected into every conversation turn.** Every line costs tokens on every single API call. Content that's only needed for specific tasks should live in sub-docs with strong pointers, not in the main file.

```
CLAUDE.md = rules for EVERY task + strong pointers to task-specific docs
Sub-docs  = detailed reference loaded on-demand when relevant
```

## Token Budget

| Project size | CLAUDE.md target | Max |
|---|---|---|
| Small (< 20 files) | < 80 lines | 120 lines |
| Medium (20-100 files) | < 150 lines | 200 lines |
| Large (100+ files) | < 200 lines | 250 lines |

Lines are a proxy — **tokens are the real per-turn cost.** Measure them directly with `/context` (run it in the target repo). Anthropic's official guidance is **< 200 lines**; lean teams run ~60 lines / **< 500 tokens** (a verified 3,847 → 312-token cut = 91.9% reduction with no quality loss). Aim under the line budget above *and* as far under ~500 tokens as the project allows.

If CLAUDE.md exceeds the max, it MUST be restructured — not trimmed. Trimming loses information. Restructuring moves it to sub-docs, path-scoped rules, or Skills.

## Workflow

### Phase 1: Discovery

Find all CLAUDE.md files and related documentation:

```bash
# Find CLAUDE.md files
find . -name "CLAUDE.md" -o -name ".claude.md" -o -name ".claude.local.md" 2>/dev/null | head -20

# Find referenced docs (design guides, guardrails, workflows, etc.)
grep -roh '\[.*\](.*\.md)' ./CLAUDE.md 2>/dev/null | sort -u
```

Read every file found. Map the full reference chain — which docs point to which.

### Phase 2: Measure

Quantify in **tokens**, not just lines. Inside the target repo, run the built-in commands that expose the real cost:

```
/context   # per-turn context breakdown — shows what CLAUDE.md + rules actually cost
/memory    # which memory/CLAUDE.md files are loaded and their sizes
/usage     # token usage to date — confirms the per-turn overhead is real spend
```

For CLAUDE.md and every referenced doc, measure:

```
File: ./CLAUDE.md
Lines: XXX
Tokens: XXX (from /context if available; else ~chars/4)
Referenced docs: X files
```

Calculate total per-turn token cost. This counts CLAUDE.md **plus any `.claude/rules/*.md` without `paths:` frontmatter** (those load every turn too) — sub-docs and path-scoped rules don't count, since they load on-demand.

### Phase 3: Content Audit

Score CLAUDE.md on two dimensions: **Quality** and **Efficiency**.

#### Quality Score (50 points)

| Criterion | Points | What to check |
|---|---|---|
| **Commands present (with flags)** | 10 | Are build/test/dev/lint commands documented as exact copy-paste invocations *with their flags* (`uv run python -m pytest -q`, not "run the tests")? |
| **Critical rules present** | 10 | Are security rules, workflow rules, and "never do X" guardrails in the main file? |
| **Boundaries (Always / Ask-first / Never)** | 10 | Is there an explicit three-tier boundary block — Always-do, Ask-first, Never-do (never commit secrets, never edit vendor/generated dirs, never force-push) — plus escalation-when-blocked guidance? |
| **Gotchas & non-obvious patterns** | 8 | Are project-specific quirks captured (not generic advice)? |
| **Currency** | 7 | Do commands work? Do file references exist? Is the stack named with versions ("React 18 + TS + Vite", not "React project")? |
| **Actionability & verifiability** | 5 | Are instructions concrete (real paths, no vague directives like "be careful"/"gracefully")? Do high-stakes rules carry a verification command or Definition of Done? |

#### Efficiency Score (50 points)

| Criterion | Points | What to check |
|---|---|---|
| **Within token budget** | 15 | Is CLAUDE.md under the target line count for project size? |
| **No reference material inlined** | 10 | Are detailed setup steps, architecture trees, env var lists, test org trees in sub-docs instead of CLAUDE.md? |
| **Strong pointer language** | 10 | Do cross-references use MANDATORY/REQUIRED language with trigger conditions? |
| **No duplication across docs** | 10 | Is the same content repeated in multiple files? |
| **Clean doc chain** | 5 | Is there a clear hierarchy with no circular or orphaned references? |

#### Content-Quality Lint (weak rules)

Vagueness — not length — is the #1 reason instruction files get ignored (studies across 2,500+ repos and 10+-run behavior tests). Flag every rule that trips these and dock the Quality score; they're the rule-level analog of the weak-pointer anti-patterns:

- **Prose without a command** — a paragraph describing a workflow with no copy-paste invocation. One real command beats three paragraphs.
- **Ambiguous directives** — "be careful", "gracefully", "where possible", "use good judgment". They mean nothing to an agent. Replace with a concrete, checkable action.
- **Contradictory priorities without ordering** — competing rules ("move fast" vs "always add tests") with no explicit numbered precedence.
- **Style rule with no enforcement command** — a style guide not backed by a formatter/linter invocation. Instructions without a verification command are suggestions, not rules.
- **Command without flags** — "run the linter" instead of the exact `ruff check .` an agent can paste.

#### Grades

| Score | Grade | Meaning |
|---|---|---|
| 90-100 | A | Lean, complete, well-structured doc chain |
| 70-89 | B | Good but has efficiency or quality gaps |
| 50-69 | C | Functional but bloated or missing key content |
| 30-49 | D | Needs significant restructuring |
| 0-29 | F | Missing, severely bloated, or broken references |

### Phase 4: Classify Every Section

Go through CLAUDE.md section by section and classify each:

| Classification | Meaning | Action |
|---|---|---|
| **KEEP** | Needed on every turn regardless of task | Leave in CLAUDE.md |
| **SCOPE** | A real rule, but only applies to files under a path | Move to `.claude/rules/<area>.md` with `paths:` frontmatter — auto-loads only when a matching file is touched |
| **EXTRACT** | Only needed for specific task types | Move to sub-doc, add strong pointer |
| **DEDUPE** | Same content exists in another doc | Remove from CLAUDE.md, ensure pointer exists |
| **DELETE** | Stale, empty, or discoverable by tools | Remove entirely |

**What belongs in KEEP (always-needed):**
- Project identity (1-3 lines: what is this, tech stack)
- Reference doc pointers (the "read these" section)
- Critical commands (dev server start/stop/health — NOT full install procedures)
- Security rules and hard constraints ("never push to master", "always filter by org_id")
- Boundaries — explicit three-tier **Always-do / Ask-first / Never-do** (never commit secrets, never edit vendor/generated dirs, never force-push) + escalation-when-blocked guidance (what to do instead of inventing a destructive workaround)
- Business logic rules that affect every code change
- Pre-commit / CI commands
- Workflow rules (git flow, PR process)

**What belongs in EXTRACT (task-specific):**
- Environment setup / installation steps
- Full test user tables and test organization trees
- Project structure / directory trees (Claude can glob)
- Environment variable details
- Database schema descriptions
- Deployment procedures
- Backup/restore procedures
- Browser testing checklists
- Authentication flow details
- CI/CD pipeline details

**What belongs in SCOPE (path-specific rules → `.claude/rules/<area>.md`):**
- Rules that only matter when editing one area ("in `src/api/**`, every endpoint must filter by `org_id`")
- Per-language / style rules tied to a file glob (test-file conventions under `tests/**`, component conventions under a package)

A rules file **without** `paths:` frontmatter loads every session like a second CLAUDE.md — no saving. **With** `paths:` (e.g. `paths: ["src/api/**/*.ts"]`) it loads only when a matching file is touched (documented ~41% always-loaded-overhead reduction). This is strictly better than a pointer for path-specific rules: it auto-loads instead of relying on Claude choosing to read the doc. For extracted *multi-step procedures*, prefer a **Skill** (progressive disclosure — 30-100 tokens at startup vs. loading the full doc).

**What belongs in DELETE:**
- Empty sections ("Known Issues: None")
- Changelog entries older than 1-2 months
- Content that duplicates what's in referenced docs
- Generic advice not specific to this project
- **Inference test:** anything Claude can infer from reading the codebase — or a senior dev could in ~20 min — cut it (framework-default conventions, generic stack facts, globbable directory structure)

### Phase 5: Quality Report

**ALWAYS output the report BEFORE making changes.**

```markdown
## CLAUDE.md Optimization Report

### Current State
- CLAUDE.md: XXX lines (~XXX tokens/turn)
- Referenced docs: X files
- Token budget: XXX lines (for project size)
- Over budget by: XXX lines

### Scores
| Dimension | Score | Notes |
|---|---|---|
| Quality | XX/50 | ... |
| Efficiency | XX/50 | ... |
| **Total** | **XX/100 (Grade: X)** | |

### Section-by-Section Classification

| Section | Lines | Classification | Reason |
|---|---|---|---|
| Repository Overview | 8 | KEEP | Project identity |
| Environment Setup | 45 | EXTRACT -> docs/DEV_SETUP.md | Only needed when setting up |
| Project Structure | 55 | DELETE | Discoverable by globbing |
| ... | ... | ... | ... |

### Proposed Structure

After optimization:
- CLAUDE.md: ~XXX lines (~XXX tokens/turn)
- New sub-docs: X files
- Token savings: ~XXX tokens/turn (XX% reduction)

### Proposed Doc Chain

CLAUDE.md (XXX lines, loaded every turn)
+-- [always] guardrails.md (XXX lines, read when coding)
|   +-- ux-guide.md (XXX lines, read when UI work)
|   +-- form-styling.md (XXX lines, read when forms)
+-- [task] dev-setup.md (XXX lines, read when setting up)
+-- [task] testing.md (XXX lines, read when testing)
+-- [task] deploy.md (XXX lines, read when deploying)
```

### Phase 6: Restructure

After user approval, execute the restructure:

1. **Create sub-docs** — Extract EXTRACT content into well-organized reference files
2. **Create path-scoped rules** — Move SCOPE rules into `.claude/rules/<area>.md`, each with `paths:` frontmatter so it auto-loads only for matching files; move multi-step procedures into a Skill
3. **Rewrite CLAUDE.md** — Keep only KEEP-classified content + pointer section
4. **Add pointer section** — Centralized, strongly-worded reference section near the top
5. **Fix cross-references** — Update all docs to point to correct locations
6. **Deduplicate** — Remove any content that now lives in two places

#### Pointer Language Patterns

Pointers MUST be strong enough that Claude actually reads the sub-doc. Weak language gets ignored.

**For mandatory docs (security, coding rules):**
```markdown
**[doc.md](doc.md)** — [1-line summary]. **Violations break [consequence]. Non-negotiable.**
```

**For task-triggered docs:**
```markdown
**[doc.md](doc.md)** — [1-line summary]. **Read before [doing X].**
```

**For the pointer section header:**
```markdown
## Reference Docs — READ THESE

These docs contain detailed rules and procedures. **You MUST read the relevant doc
before doing that type of work. Do NOT guess or improvise — the answers are in these files.**

### Always (read before writing or modifying any code)
- ...

### When touching [area]
- ...

### When task-specific
- ...
```

**Anti-patterns (too weak — Claude will skip these):**
```markdown
# BAD — Claude ignores these:
See `docs/setup.md` for more details.
For more information, refer to the testing guide.
Check the deployment docs if needed.
```

#### Sub-Doc Organization

Place extracted docs where they make sense:

```
project/
+-- CLAUDE.md              # Lean command center (< 200 lines)
+-- DESIGN_GUARDRAILS.md   # Coding rules (if security-critical, keep at root)
+-- docs/
|   +-- DEV_SETUP.md       # Environment, installation, auth
|   +-- TESTING_GUIDE.md   # Test strategy, organization, browser testing
|   +-- UX_DESIGN_GUIDE.md # Component patterns, design direction
|   +-- FORM_STYLING.md    # Theme classes, form patterns
|   +-- [other refs]       # Deployment, backup, workflows
+-- FEEDBACK_WORKFLOW.md   # Operational procedures
```

#### Mechanics — free wins and one myth

- **HTML comments are stripped before injection.** `<!-- note to teammates -->` costs zero runtime tokens — use it for human-only notes inside CLAUDE.md.
- **`@path` imports are organizational only.** Splitting CLAUDE.md into `@docs/foo.md` imports does **not** save tokens — every imported file still loads at session start. Use imports for editing ergonomics, not budget. To actually defer load, use path-scoped rules (`.claude/rules/`) or Skills.
- **`CLAUDE.local.md` (gitignored)** holds personal / machine-specific prefs so they never bloat the shared, committed CLAUDE.md.

### Phase 7: Verify

After restructuring:

1. **Line count check** — Is CLAUDE.md within budget?
2. **Token delta** — Re-run `/context` and report before/after **tokens** (not just lines) and the % reduction. Confirm no `.claude/rules/*.md` lost its `paths:` frontmatter (or it's now always-loaded).
3. **Pointer check** — Does every referenced file exist?
4. **Chain check** — No circular references, no orphaned docs?
5. **Content check** — Was any content lost (not in CLAUDE.md, a sub-doc, a rules file, or a Skill)?
6. **Strength check** — Does every pointer use MANDATORY/REQUIRED language?
7. **Duplication check** — Is any content repeated across files?
8. **Recite test** — In a fresh context, ask Claude to reproduce the build/test commands and the top 3 constraints verbatim. If it can't, the file is still too verbose or too vague — iterate.

```bash
# Verify all referenced docs exist
grep -oP '\[.*?\]\((.*?\.md)\)' CLAUDE.md | grep -oP '\(.*?\)' | tr -d '()' | while read f; do
  test -f "$f" && echo "OK: $f" || echo "MISSING: $f"
done

# Check for weak pointer language
grep -n 'See\|refer to\|check\|for more' CLAUDE.md | grep -iv 'MUST\|MANDATORY\|REQUIRED'
```

## Common Restructure Patterns

### The "Everything In One File" Problem
**Symptom:** CLAUDE.md is 500+ lines with install steps, architecture trees, test tables.
**Fix:** Extract to DEV_SETUP.md, TESTING_GUIDE.md, etc. Keep rules + pointers.

### The "Weak Pointers" Problem
**Symptom:** Sub-docs exist but Claude never reads them.
**Fix:** Replace "See X for details" with "MANDATORY: Read X before doing Y."

### The "Duplicated Tables" Problem
**Symptom:** Same test user table, env var list, or command reference appears in 2+ files.
**Fix:** Keep one canonical copy, replace others with pointer + 1-line summary.

### The "Stale Reference" Problem
**Symptom:** CLAUDE.md references files that don't exist or commands that don't work.
**Fix:** Verify every reference and command. Delete or update stale ones.

### The "No Doc Chain" Problem
**Symptom:** Multiple docs exist but CLAUDE.md doesn't point to them.
**Fix:** Add the centralized pointer section. Organize by trigger condition.

## AGENTS.md Interop

If the repo or team also uses other agent tools (Codex, Cursor, Copilot, Windsurf, Amp), don't hand-maintain a second instruction file — it silently drifts from CLAUDE.md, which is exactly the duplication this skill exists to kill. `AGENTS.md` is a Linux-Foundation standard read by 30+ tools. Treat it as the **canonical source** for shared sections and have CLAUDE.md mirror or `@AGENTS.md`-import them, so there's one source of truth instead of two diverging copies.

## What Makes an A-Grade CLAUDE.md

- Under token budget for project size — verified in **tokens** via `/context`, not just line count
- Every line is a rule, a command, or a strong pointer — no reference material
- Explicit three-tier **boundaries** (Always / Ask-first / Never) + escalation-when-blocked
- All commands copy-paste ready, **with their flags**
- Path-specific rules live in `.claude/rules/*.md` with `paths:` frontmatter (auto-load), not inlined; multi-step procedures live in Skills
- No vague directives — every high-stakes rule has a verification command / Definition of Done
- Centralized pointer section with MANDATORY/REQUIRED language
- Sub-docs organized by trigger condition (always, when UI, when task-specific)
- No duplication across files
- All pointers resolve to existing files
- Clean hierarchy: main file -> coding rules -> domain guides -> component references
