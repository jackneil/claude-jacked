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

If CLAUDE.md exceeds the max, it MUST be restructured — not trimmed. Trimming loses information. Restructuring moves it to sub-docs.

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

For CLAUDE.md and every referenced doc, measure:

```
File: ./CLAUDE.md
Lines: XXX
Chars: XXX (~XXX tokens at 1 token/4 chars)
Referenced docs: X files
```

Calculate total per-turn token cost (CLAUDE.md only — sub-docs don't count since they load on-demand).

### Phase 3: Content Audit

Score CLAUDE.md on two dimensions: **Quality** and **Efficiency**.

#### Quality Score (50 points)

| Criterion | Points | What to check |
|---|---|---|
| **Commands present** | 10 | Are build/test/dev/lint commands documented and copy-paste ready? |
| **Critical rules present** | 15 | Are security rules, workflow rules, and "never do X" guardrails in the main file? |
| **Gotchas & non-obvious patterns** | 10 | Are project-specific quirks captured (not generic advice)? |
| **Currency** | 10 | Do commands work? Do file references exist? Is tech stack current? |
| **Actionability** | 5 | Are instructions concrete with real paths, not vague? |

#### Efficiency Score (50 points)

| Criterion | Points | What to check |
|---|---|---|
| **Within token budget** | 15 | Is CLAUDE.md under the target line count for project size? |
| **No reference material inlined** | 10 | Are detailed setup steps, architecture trees, env var lists, test org trees in sub-docs instead of CLAUDE.md? |
| **Strong pointer language** | 10 | Do cross-references use MANDATORY/REQUIRED language with trigger conditions? |
| **No duplication across docs** | 10 | Is the same content repeated in multiple files? |
| **Clean doc chain** | 5 | Is there a clear hierarchy with no circular or orphaned references? |

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
| **EXTRACT** | Only needed for specific task types | Move to sub-doc, add strong pointer |
| **DEDUPE** | Same content exists in another doc | Remove from CLAUDE.md, ensure pointer exists |
| **DELETE** | Stale, empty, or discoverable by tools | Remove entirely |

**What belongs in KEEP (always-needed):**
- Project identity (1-3 lines: what is this, tech stack)
- Reference doc pointers (the "read these" section)
- Critical commands (dev server start/stop/health — NOT full install procedures)
- Security rules and hard constraints ("never push to master", "always filter by org_id")
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

**What belongs in DELETE:**
- Empty sections ("Known Issues: None")
- Changelog entries older than 1-2 months
- Content that duplicates what's in referenced docs
- Generic advice not specific to this project

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

1. **Create sub-docs** — Extract content into well-organized reference files
2. **Rewrite CLAUDE.md** — Keep only KEEP-classified content + pointer section
3. **Add pointer section** — Centralized, strongly-worded reference section near the top
4. **Fix cross-references** — Update all docs to point to correct locations
5. **Deduplicate** — Remove any content that now lives in two places

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

### Phase 7: Verify

After restructuring:

1. **Line count check** — Is CLAUDE.md within budget?
2. **Pointer check** — Does every referenced file exist?
3. **Chain check** — No circular references, no orphaned docs?
4. **Content check** — Was any content lost (not in CLAUDE.md or any sub-doc)?
5. **Strength check** — Does every pointer use MANDATORY/REQUIRED language?
6. **Duplication check** — Is any content repeated across files?

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

## What Makes an A-Grade CLAUDE.md

- Under token budget for project size
- Every line is a rule, a command, or a strong pointer — no reference material
- Centralized pointer section with MANDATORY/REQUIRED language
- Sub-docs organized by trigger condition (always, when UI, when task-specific)
- No duplication across files
- All commands copy-paste ready
- All pointers resolve to existing files
- Clean hierarchy: main file -> coding rules -> domain guides -> component references
