# Skill Description Improvements Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix all jacked skill and command descriptions to follow Anthropic's writing-skills standard: trigger-first format ("Use when..."), no workflow summaries in descriptions, third-person voice.

**Architecture:** Descriptions-only changes to frontmatter YAML in 9 skill SKILL.md files and 21 command .md files. No body content changes — the skill bodies are solid. The key insight from Anthropic's standard: descriptions that summarize workflow cause Claude to follow the description as a shortcut instead of reading the full skill. Leading with triggers fixes this.

**YAML quoting rule:** Use unquoted YAML scalars (the existing pattern in this codebase). Unquoted scalars freely allow `"` characters without escaping. Do NOT wrap in double quotes with `\"` escaping — that's fragile.

**Tech Stack:** Markdown (YAML frontmatter)

---

### Task 1: Fix all 9 skill descriptions

**Files:** All `jacked/data/skills/*/SKILL.md`

Each edit replaces only the `description:` line in frontmatter. Body content is untouched.

- [ ] **Step 1: Fix qa (critical — zero trigger conditions)**

File: `jacked/data/skills/qa/SKILL.md`

Old:
```
description: Browser-based QA testing of UI changes — returns a detailed issue list for the caller to plan fixes.
```

New:
```
description: Use when testing UI changes for visual correctness, interactions, console errors, and edge cases. Performs browser-based QA and returns a detailed issue list for planning fixes.
```

- [ ] **Step 2: Fix dcr**

File: `jacked/data/skills/dcr/SKILL.md`

Old:
```
description: "Parallel recursive review — selects relevant lenses, spawns focused reviewers per wave until all selected lenses pass clean. Use after implementing a feature, fixing a bug, or completing any non-trivial code change."
```

New:
```
description: Use after implementing a feature, fixing a bug, or completing any non-trivial code change. Recursive multi-lens review that continues until all selected lenses pass clean.
```

- [ ] **Step 3: Fix docs-sync**

File: `jacked/data/skills/docs-sync/SKILL.md`

Old:
```
description: "Sync docs with code changes — diffs branch against base, maps changes to affected docs, spawns parallel update agents. Use when a branch has code changes that may have made documentation stale, after completing a feature, or before creating a PR."
```

New:
```
description: Use when a branch has code changes that may have made documentation stale, after completing a feature, or before creating a PR. Diffs branch against base and maps changes to affected docs.
```

- [ ] **Step 4: Fix swarm-research**

File: `jacked/data/skills/swarm-research/SKILL.md`

Old:
```
description: "Divergent research — spawns independent agents from different angles, synthesizes proposals, then verifies + attacks with devil's advocate. Use when approaching non-trivial planning: architectural decisions, major features, system design, or any decision with multiple viable approaches."
```

New:
```
description: Use when approaching non-trivial planning — architectural decisions, major features, system design, or any decision with multiple viable approaches.
```

- [ ] **Step 5: Fix ux**

File: `jacked/data/skills/ux/SKILL.md`

Old:
```
description: "Parallel browser-based UX checks — spawns focused agents to test different UX aspects on affected pages simultaneously. Use when: UI changes span multiple components or pages, layout or navigation structure changed, a new user-facing feature was added, or comprehensive UX validation is needed. For targeted single-component or bug-fix verification, use the qa skill instead."
```

New:
```
description: Use when UI changes span multiple components or pages, layout or navigation changed, a new user-facing feature was added, or comprehensive UX validation is needed. For single-component or bug-fix checks, use the qa skill instead.
```

- [ ] **Step 6: Fix claude-md-optimizer**

File: `jacked/data/skills/claude-md-optimizer/SKILL.md`

Old:
```
description: Audit and optimize CLAUDE.md for both quality AND token efficiency. Scores content quality, identifies bloat, extracts reference material to sub-docs, builds doc chains with strong pointers, and enforces a token budget. Use when CLAUDE.md feels too long, when starting a new project, or when you want to ensure Claude Code gets maximum signal per token.
```

New:
```
description: Use when CLAUDE.md feels bloated or too long, when starting a new project, or when optimizing documentation for token efficiency. Audits content quality, extracts reference material to sub-docs, and enforces a token budget.
```

- [ ] **Step 7: Fix whats-next**

File: `jacked/data/skills/whats-next/SKILL.md`

Old:
```
description: Roadmap advisor for any repo — reads plans, issues, commits, and lifecycle stage to recommend the highest-yield next work items. Use when the user asks "what should I work on", "what's next", "what are our priorities", "help me prioritize", "what should we build next", "I'm not sure what to do next", or "where should I start". Run `/jacked-setup whats-next` for faster repeat runs.
```

New:
```
description: Use when the user asks "what should I work on", "what's next", "what are our priorities", "help me prioritize", "what should we build next", "I'm not sure what to do next", or "where should I start". Analyzes plans, issues, commits, and lifecycle stage to recommend highest-yield next work items.
```

- [ ] **Step 8: Fix jack-it-up**

File: `jacked/data/skills/jack-it-up/SKILL.md`

Old:
```
description: Use when starting any significant feature, enhancement, or non-trivial task that deserves a thorough iterative development cycle — brainstorming through implementation with continuous refinement. Triggers on "jack it up", "do this right", "full cycle", "build this properly", or when the user wants quality-first development rather than just getting something done.
```

New:
```
description: Use when starting any significant feature or non-trivial task that deserves a thorough development cycle. Triggers on "jack it up", "do this right", "full cycle", "build this properly", or when the user wants quality-first development over speed.
```

- [ ] **Step 9: Fix jacked**

File: `jacked/data/skills/jacked/SKILL.md`

Old:
```
description: Search and load context from past Claude Code sessions. Use when: user mentions a past project like "configurator" or other previous work, asks to continue/resume previous work, says "how did I do X before", references past sessions, or starts work on a feature that may have been done before.
```

New:
```
description: Use when the user mentions a past project like "configurator", asks to continue/resume previous work, says "how did I do X before", references past sessions, or starts work on a feature that may have been done before. Searches and loads context from past Claude Code sessions.
```

- [ ] **Step 10: Commit skills**

```bash
git add jacked/data/skills/
git commit -m "fix: flip all skill descriptions to trigger-first format

Anthropic's writing-skills standard: descriptions that summarize
workflow cause Claude to follow the description shortcut instead of
reading the full skill. Leading with 'Use when...' trigger conditions
fixes this. No body content changes."
```

---

### Task 2: Fix all 21 command descriptions

**Files:** All `jacked/data/commands/*.md`

Same pattern: replace `description:` line in frontmatter only. Body untouched. Use unquoted YAML scalars.

- [ ] **Step 1: Fix dc.md**

Old:
```
description: "Trigger comprehensive double-check review - auto-detects planning/implementation/post-implementation phase and spawns appropriate review threads"
```

New:
```
description: Use after completing a plan, implementation, or any non-trivial code change. Auto-detects phase and spawns appropriate review threads with pre-mortem analysis.
```

- [ ] **Step 2: Fix dcr.md**

Old:
```
description: "Parallel recursive review — selects relevant lenses, spawns focused reviewers per wave until all selected lenses pass clean"
```

New:
```
description: Use after implementing a feature, fixing a bug, or completing any non-trivial code change. Recursive multi-lens review that continues until all selected lenses pass clean.
```

- [ ] **Step 3: Fix docs-sync.md**

Old:
```
description: "Docs sync — diffs branch against base, maps code changes to affected docs, spawns parallel agents to update them"
```

New:
```
description: Use when a branch has code changes that may have made documentation stale, after completing a feature, or before creating a PR.
```

- [ ] **Step 4: Fix qa.md**

Old:
```
description: "Browser-based QA testing of UI changes from the current session. Pass a URL as argument, or let it auto-detect."
```

New:
```
description: Use when testing UI changes for visual correctness, interactions, console errors, and edge cases. Pass a URL as argument, or let it auto-detect.
```

- [ ] **Step 5: Fix swarm-research.md**

Old:
```
description: "Divergent research — spawns independent agents from different angles, synthesizes proposals, then verifies + attacks with devil's advocate"
```

New:
```
description: Use when approaching non-trivial planning — architectural decisions, major features, system design, or any decision with multiple viable approaches.
```

- [ ] **Step 6: Fix ux.md**

Old:
```
description: "Parallel browser-based UX checks — spawns focused agents to test different UX aspects on affected pages simultaneously"
```

New:
```
description: Use when UI changes span multiple components or pages, layout or navigation changed, a new user-facing feature was added, or comprehensive UX validation is needed.
```

- [ ] **Step 7: Fix whats-next.md**

Old:
```
description: "Roadmap advisor — analyzes plans, issues, commits, and lifecycle stage to recommend the highest-yield next work items."
```

New:
```
description: Use when the user asks "what should I work on", "what's next", "what are our priorities", or "where should I start". Recommends highest-yield next work items.
```

- [ ] **Step 8: Fix audit-rules.md**

Old:
```
description: "Audit your CLAUDE.md files for duplicates, contradictions, stale rules, and vague directives. Companion to /learn."
```

New:
```
description: Use after adding several rules to CLAUDE.md, or when rules feel contradictory or stale. Audits for duplicates, contradictions, and vague directives. Companion to /learn.
```

- [ ] **Step 9: Fix benchmark.md**

Old:
```
description: "Performance regression detection — captures web performance metrics, compares against baselines, flags regressions"
```

New:
```
description: Use before and after performance-sensitive changes. Captures web performance metrics, compares against baselines, flags regressions.
```

- [ ] **Step 10: Fix browser-reset.md**

Old:
```
description: "Diagnose and fix stuck browser MCP connections — kills stale processes, tests connectivity, reports status"
```

New:
```
description: Use when browser MCP tools are failing, stuck, or unresponsive. Diagnoses connection issues, kills stale processes, and tests connectivity.
```

- [ ] **Step 11: Fix canary.md**

Old:
```
description: "Post-deploy monitoring — takes periodic screenshots, checks console errors, measures performance, compares against baselines"
```

New:
```
description: Use after a production deploy to monitor for regressions. Takes periodic screenshots, checks console errors, and compares performance against baselines.
```

- [ ] **Step 12: Fix cso.md**

Old:
```
description: "Security audit — systematic OWASP Top 10 + STRIDE threat model analysis with confidence-gated findings"
```

New:
```
description: Use after implementing security-sensitive changes — auth, RBAC, multi-tenancy, billing, credential handling. Systematic OWASP Top 10 + STRIDE threat model analysis.
```

- [ ] **Step 13: Fix freeze.md**

Old:
```
description: "Restrict file edits to a single directory — prevents accidental changes outside the target area"
```

New:
```
description: Use when debugging in a focused area or working on sensitive code to prevent accidental edits outside the target directory.
```

- [ ] **Step 14: Fix land-and-deploy.md**

Old:
```
description: "Post-merge deploy verification — merges PR, waits for CI, waits for deploy, runs canary checks, offers revert on failure"
```

New:
```
description: Use after a PR is approved and CI passes. Merges, waits for deploy, runs canary checks, and offers revert on failure.
```

- [ ] **Step 15: Fix learn.md**

Old:
```
description: "Distill a lesson from this conversation into a CLAUDE.md rule. Use after corrections, mistakes, or when you want to codify a preference."
```

New:
```
description: Use after corrections, mistakes, or when the user wants to codify a preference into a permanent CLAUDE.md rule.
```

- [ ] **Step 16: Fix pr.md**

Old:
```
description: Check PR status and manage pull request workflow for current branch
```

New:
```
description: Use when checking PR status, creating a PR, or managing the pull request workflow for the current branch.
```

- [ ] **Step 17: Fix redo.md**

Old:
```
description: "Scrap the current approach and re-implement from scratch with full hindsight. Creates a safety branch, stashes your work, and forces structured reflection before rewriting."
```

New:
```
description: Use when an approach has gone sideways and patching patches. Scraps the current approach and re-implements from scratch with full hindsight after structured reflection.
```

- [ ] **Step 18: Fix release.md**

Old:
```
description: "Release workflow — bump version, commit, push, verify CI, create GitHub Release for PyPI publishing"
```

New:
```
description: Use when ready to cut a release. Bumps version, commits, pushes, verifies CI, creates GitHub Release for PyPI publishing.
```

- [ ] **Step 19: Fix retro.md**

Old:
```
description: "Engineering retrospective — analyzes git history for contributor metrics, test health, velocity trends, and team patterns"
```

New:
```
description: Use periodically during long sessions or weekly. Analyzes git history for contributor metrics, test health, velocity trends, and team patterns.
```

- [ ] **Step 20: Fix swarm.md**

Old:
```
description: "Launch a coordinated agent swarm using Claude Code's experimental agent teams to implement the current plan or work in parallel."
```

New:
```
description: Use when implementing a plan with multiple independent tasks that can be worked in parallel using Claude Code's experimental agent teams.
```

- [ ] **Step 21: Fix techdebt.md**

Old:
```
description: "Run a tech debt audit on your project. Finds TODOs, oversized files, missing tests, linter issues, and dead code. Pass a path to focus on a specific area."
```

New:
```
description: Use periodically during long sessions to scan for accumulating debt. Finds TODOs, oversized files, missing tests, linter issues, and dead code. Pass a path to focus on a specific area.
```

- [ ] **Step 22: Fix unfreeze.md**

Old:
```
description: "Remove the edit restriction set by /freeze — allows edits anywhere again"
```

New:
```
description: Use when done working in a frozen directory and ready to allow edits anywhere again. Removes the restriction set by /freeze.
```

- [ ] **Step 23: Fix jacked-setup.md**

Old:
```
description: "Analyze this repo and generate faster, customized versions of jacked commands (/whats-next, /qa, /ux, /dcr, /docs-sync)"
```

New:
```
description: Use when setting up jacked in a new repo or after significant codebase changes. Analyzes the repo and generates faster, customized versions of jacked commands.
```

- [ ] **Step 24: Commit commands**

```bash
git add jacked/data/commands/
git commit -m "fix: flip all command descriptions to trigger-first format

Matches the skill description fixes. All 21 commands now lead with
'Use when...' trigger conditions per Anthropic's writing-skills standard."
```

---

### Task 3: Verify all frontmatter

- [ ] **Step 1: Verify all skills and commands have valid trigger-first descriptions**

```bash
uv run python -c "
import yaml
from pathlib import Path

errors = []

# Check skills
for f in sorted(Path('jacked/data/skills').rglob('SKILL.md')):
    text = f.read_text()
    parts = text.split('---')
    if len(parts) < 3:
        errors.append(f'{f}: no frontmatter')
        continue
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        errors.append(f'{f}: YAML parse error: {e}')
        continue
    desc = data.get('description', '')
    if not desc.startswith('Use when') and not desc.startswith('Use after') and not desc.startswith('Use periodically'):
        errors.append(f'{f}: description does not start with trigger ({desc[:50]}...)')
    else:
        print(f'  OK {f.parent.name} ({len(desc)} chars)')

# Check commands
for f in sorted(Path('jacked/data/commands').glob('*.md')):
    text = f.read_text()
    parts = text.split('---')
    if len(parts) < 3:
        errors.append(f'{f}: no frontmatter')
        continue
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        errors.append(f'{f}: YAML parse error: {e}')
        continue
    desc = data.get('description', '')
    if not desc.startswith('Use when') and not desc.startswith('Use after') and not desc.startswith('Use periodically'):
        errors.append(f'{f}: description does not start with trigger ({desc[:50]}...)')
    else:
        print(f'  OK {f.name} ({len(desc)} chars)')

if errors:
    print(f'\nERRORS ({len(errors)}):')
    for e in errors:
        print(f'  FAIL {e}')
else:
    print(f'\nAll descriptions are trigger-first.')
"
```

- [ ] **Step 2: Reinstall**

```bash
jacked install --force
```
