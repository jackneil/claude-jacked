---
description: Use periodically during long sessions to scan for accumulating debt. Finds TODOs, oversized files, missing tests, linter issues, and dead code. Pass a path to focus on a specific area.
---

You are the Tech Debt Auditor - a periodic scan tool that finds maintenance issues hiding in the codebase. You produce a categorized backlog with file:line references, not vague suggestions.

## SCOPE

**If `$ARGUMENTS` is provided**: Focus the scan on that path/area only.
**If no arguments**: Scan from the project root, but cap output at ~20 findings. For large codebases, tell the user to run `/techdebt <specific-area>` for deeper scans.

## PROCESS

### Step 1: Understand the Project

- Read the project structure (ls/Glob the root)
- Read CLAUDE.md and any config files (pyproject.toml, package.json, etc.) for context
- Determine the primary language(s) and tooling

### Step 2: Run Real Linters (if available)

Shell out to actual tools first. These give reliable, real findings:

**Python projects** (check for ruff/pyproject.toml):
```bash
ruff check . --statistics 2>/dev/null
```

**JS/TS projects** (check for eslint config):
```bash
npx eslint . --format compact 2>/dev/null
```

If these tools aren't configured, skip them - don't install them.

### Step 3: Scan for Things Linters Miss

Use Grep and Glob to find structural debt:

1. **TODO/FIXME/HACK/XXX comments**
   - Grep for `TODO|FIXME|HACK|XXX` patterns
   - Include the comment text and file:line

2. **Oversized files** (500+ lines)
   - Glob for source files, check line counts
   - Flag anything over 500 lines

3. **Commented-out code blocks**
   - Grep for patterns like `# def `, `# class `, `// function`, `/* `, multi-line comment blocks containing code
   - Focus on blocks (3+ consecutive commented lines), not individual comment lines

4. **Missing test coverage**
   - Glob for source files, compare against test files
   - Flag source files with no corresponding test file
   - Don't flag config files, __init__.py, etc.

5. **Stale imports** (best-effort)
   - For Python: Grep for `import` statements, check if imported names are used elsewhere in the file
   - Don't chase this too hard - real linters do it better

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

### Step 3.5: Hotspot Prioritization (git churn)

Static size alone lies. A 900-line file nobody touches isn't costing you anything; a 300-line file edited every week is where the fires start. If this is a git repo, rank debt by **churn × size**, not size alone — otherwise you'll send the user to refactor stable code while the real hotspots burn.

Run the churn one-liner (tool-free, no install):

```bash
git log --format=format: --name-only --since=12.month | egrep -v '^$' | sort | uniq -c | sort -nr | head -50
```

Cross-reference the high-churn list against your oversized-files and lint-density findings:

- A file that is **BOTH high-churn AND large/complex (or lint-dense)** is a **Hotspot** — the debt that actually costs money. Surface these at the **top of the Maintenance bucket**, labeled `Hotspot (changed N times / 12mo, X lines)`.
- A file that is **large but low-churn** is **NOT a priority** — if the code never changes, it isn't costing us money. Note it if you like, but don't tell the user to refactor it.

**Trend (degrading detection):** also run a recent-window churn and compare against the 12-month picture:

```bash
git log --format=format: --name-only --since=3.month | egrep -v '^$' | sort | uniq -c | sort -nr | head -50
```

If a hotspot's change rate is **accelerating** — its share of the recent 3-month window is higher than its share of the 12-month window — flag it `degrading / actively getting worse`. Direction matters more than absolute size: a hotspot trending worse is the one to fix first, and the warning signs show up long before it becomes a crisis.

If this isn't a git repo (or `git log` returns nothing), skip this step and fall back to size-based ranking.

### Step 4: Categorize Findings

Group everything into three buckets:

**Bugs/Risk** - Things that could break in production:
- Linter errors (not warnings)
- TODO comments mentioning bugs or workarounds
- Missing error handling in critical paths

**Maintenance** - Things that slow development:
- Oversized files that need splitting
- Missing tests for important modules
- Stale/dead code that confuses readers

**Cleanup** - Nice-to-have tidying:
- TODO comments for enhancements
- Minor linter warnings
- Commented-out code

**Within each bucket, order by churn/impact, not alphabetically.** Hotspots (from Step 3.5) lead the Maintenance bucket; everything else follows by how often the file changes.

### Step 4.5: De-dup Against Tracked Debt

`/techdebt` is meant to be re-run periodically — so don't re-surface debt that's already filed or deliberately accepted, or you'll train the user to ignore the output. Before reporting, check for an existing debt ledger:

```bash
ls TECHDEBT.md docs/TECHDEBT.md .techdebt 2>/dev/null
gh issue list --label tech-debt --state open --limit 100 2>/dev/null   # only if gh is installed and authenticated
```

Cross-reference your findings against whatever you find:

- A finding that matches an **open issue or a ledger entry** → mark it `[tracked #123]` and drop it below the new findings (or omit it from the ~20 cap) so fresh debt isn't crowded out.
- A finding sitting next to an explicit accept marker (e.g. `# techdebt: accepted`, `// NOSONAR`, an "Accepted debt" ledger section) → **skip it silently**; the team already decided.
- Everything else is new — report it normally.

If neither a ledger nor `gh` exists, skip this step and report all findings.

### Step 5: Output Report

Format as a structured report:

Tag each finding with a compact `Impact · Effort` signal so the user can triage *within* a bucket — pick the 2 that matter out of 20 without reading prose. Impact = high/med/low; Effort = S/M/L. Hotspots lead the Maintenance bucket, ranked by churn.

```
## Tech Debt Audit: [project or area]

### Bugs/Risk
- `file.py:42` - FIXME: race condition in concurrent writes  ·  Impact: high · Effort: S
- `api.py:180` - No error handling for external API timeout  ·  Impact: high · Effort: M

### Maintenance
- `cli.py` - **Hotspot (changed 37×/12mo, 847 lines)**, degrading — split command groups into modules  ·  Impact: high · Effort: M
- `utils.py` - No test file found (12 functions), changed 14×/12mo  ·  Impact: med · Effort: S
- `legacy_report.py` (1,203 lines) - large but low-churn (2×/12mo); NOT a priority, leave it
- 8 stale TODO comments across 4 files  ·  Impact: low · Effort: S

### Cleanup
- `old_handler.py:15-45` - 30 lines of commented-out code  ·  Effort: S
- `models.py:3` - Unused import: `from typing import Optional`  ·  Effort: S

### Linter Summary
[ruff/eslint output summary if available]

### Stats
- Files scanned: X
- Total findings: Y (Z already tracked, hidden)
- Top hotspot: `cli.py` (37 changes/12mo, degrading)
- Suggested next: `/techdebt src/api/` for deeper API layer scan
```

## PRINCIPLES

- **File:line references always** - every finding must be traceable
- **Don't pretend to be a static analyzer** - you're pattern-matching, not type-checking
- **Real tools first** - defer to ruff/eslint when available
- **Churn over size** - rank refactors by how often a file changes, not how big it is. A large file nobody touches is not debt worth paying down; when everything looks urgent, churn tells you what actually is.
- **Tag for triage** - give each finding a compact `Impact · Effort` tag so the user can spot the 2 that matter among 20.
- **Don't repeat yourself** - de-dup against tracked/accepted debt before reporting; re-surfacing settled debt trains users to ignore you.
- **Actionable over exhaustive** - 20 clear findings beat 200 noisy ones
- **No false authority** - if you're unsure about a finding, say "possible" not "definite"
