---
description: "Docs sync — diffs branch against base, maps code changes to affected docs, spawns parallel agents to update them"
---

> **Note:** If `.claude/commands/docs-sync.md` exists in the current repo, that version has pre-filled repo config from `/jacked-setup docs-sync` — use it instead of this global file. If it doesn't exist, continue here.

## Config Override

If this command was invoked via a local config wrapper (you see a `## Repo Config` section earlier in the prompt), use that config to accelerate sync:
- **Base Branch** specified? → Use it instead of auto-detecting in Step 1
- **Doc Inventory** listed? → Skip doc discovery in Step 1, use the listed files (validate with `ls` first, skip missing)
- **Change-to-Doc Map** specified? → Use it in Step 3 instead of the default mapping

If the config overlay date is more than 90 days old, mention: "Your `/docs-sync` config is over 90 days old — consider running `/jacked-setup docs-sync` to refresh it."

If no `## Repo Config` section is present, run all discovery steps normally.

## Step 1: Discover Repo Context

> Skip this step entirely if `## Repo Config` was found above.

Run these commands to gather context (all are gatekeeper-safe):

```bash
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
echo "REPO_ROOT=$REPO_ROOT"
echo "REPO_NAME=$(basename "$REPO_ROOT")"

# Detect base branch
git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main"

# Doc files at repo root
ls README.md CONTRIBUTING.md CHANGELOG.md LICENSE.md 2>/dev/null

# Wiki structure
ls -d _wiki 2>/dev/null
ls _wiki/*.md 2>/dev/null | head -30

# CLAUDE.md sections
grep -n "^#" CLAUDE.md 2>/dev/null | head -20

# docs/ directory
find docs -name "*.md" -maxdepth 2 2>/dev/null | head -20

# Other root-level markdown
ls *.md 2>/dev/null
```

Build a mental doc inventory from the results. Note which doc files exist and what sections they cover.

## Step 2: Diff Analysis

Diff the current branch against the base branch:

```bash
# Get base branch (from Repo Config or detected above)
BASE_BRANCH=$(git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main")

# Summary of changes
git diff ${BASE_BRANCH}...HEAD --stat 2>/dev/null || git diff ${BASE_BRANCH}..HEAD --stat
git diff ${BASE_BRANCH}...HEAD --name-only 2>/dev/null || git diff ${BASE_BRANCH}..HEAD --name-only
git log ${BASE_BRANCH}..HEAD --oneline 2>/dev/null
```

If there are no changes (empty diff), say: "No changes detected against `<base>`. Nothing to sync." Stop.

Categorize all changes into these buckets:

| Category | Signals |
|----------|---------|
| **Pipeline/Architecture** | New entry points, changed main flows, new modules, renamed directories |
| **Configuration** | New env vars, changed settings, new config files |
| **Commands/CLI** | New flags, changed usage, new subcommands, changed arguments |
| **Models/Schemas** | New models, changed fields, migration files |
| **Tests** | New test patterns, changed test commands, new test utilities |
| **Dependencies** | New requirements, changed versions, new package managers |
| **UI/Frontend** | New pages, changed components, route changes |

## Step 3: Map Changes to Docs

**If `## Change-to-Doc Map` exists in Repo Config:** Use the table to determine which doc files are affected. Validate each target path still exists before including it — skip missing ones silently.

**If no map exists (runtime discovery):** Use this default mapping:

| Change Category | Likely Affected Docs |
|----------------|---------------------|
| Pipeline/Architecture | README.md (architecture section), CLAUDE.md |
| Configuration | README.md (env vars / config section) |
| Commands/CLI | README.md (usage section) |
| Dependencies | README.md (install / requirements section) |
| UI/Frontend | README.md (features section) |
| Models/Schemas | _wiki/ pages if they exist, CLAUDE.md |
| Tests | README.md (testing section) |

Filter to only doc files that actually exist. If no doc files are affected, say: "Changes don't appear to affect any existing docs." Stop.

## Step 4: Spawn Parallel Update Agents

Based on which docs are affected, spawn up to 3 agents using the Agent tool. Each agent is **read-write** — it reads the current doc, reads the changed source files, and edits the doc directly.

**Agent dispatch rules:**
- Only spawn agents for doc files that exist AND are affected by the changes
- Each agent gets a focused scope — one doc type, not all docs
- Agents run in parallel (send all Agent tool calls in one message)

**README Agent** — spawn if README.md is affected:

```
You are a README updater. Your job is to update README.md to reflect recent code changes.

## Context
Base branch: <base>
Changes summary:
<paste git diff --stat output>

Changed files:
<paste git diff --name-only output>

Commit messages:
<paste git log --oneline output>

## Instructions
1. Read README.md fully
2. Read the key changed source files to understand what actually changed
3. Update ONLY the sections of README.md that are affected by these changes
4. Do NOT rewrite sections unrelated to the changes
5. Do NOT add emojis unless the existing README uses them
6. Match the existing voice, formatting, and structure
7. If a new feature was added with no corresponding README section, add a minimal section in the appropriate location
```

**Wiki Agent** — spawn if `_wiki/` pages are affected:

```
You are a wiki documentation updater. Your job is to update wiki pages under _wiki/ to reflect recent code changes.

## Context
Base branch: <base>
Changes summary:
<paste git diff --stat output>

Affected wiki pages:
<list specific pages from the Change-to-Doc Map>

## Instructions
1. Read each affected wiki page
2. Read the key changed source files
3. Update ONLY the affected sections
4. If _wiki/_Sidebar.md exists and you added a new page, update the sidebar
5. Match existing wiki formatting and structure
```

**CLAUDE.md Agent** — spawn if architecture/behavioral changes detected:

```
You are a CLAUDE.md updater. Your job is to update CLAUDE.md project instructions to reflect recent code changes.

## Context
Base branch: <base>
Changes summary:
<paste git diff --stat output>

## Instructions
1. Read CLAUDE.md fully
2. Read the key changed source files
3. Update ONLY sections that are stale due to the changes (architecture descriptions, testing commands, env vars, file paths)
4. Do NOT add new rules or behavioral instructions — only update factual descriptions
5. Do NOT remove existing rules unless they reference deleted code
```

## Step 5: Suggest New Docs

After agents complete, check if the changes introduced anything with no existing doc coverage:

- New env vars with no env var table in README
- New CLI flags with no usage section
- New major features with no feature description
- New dependencies with no install instructions

For each gap found, suggest (but do NOT auto-create):
```
Suggestion: <description> — consider adding a section to <doc file>
```

## Step 6: Stage and Report

Show a summary of all changes made:

```
## docs-sync complete

**Docs updated:**
- README.md: updated <sections>
- _wiki/page.md: updated <sections>
- CLAUDE.md: updated <sections>

**Suggestions:**
- <any new doc suggestions from Step 5>
```

Stage the changed doc files:
```bash
git add README.md CLAUDE.md _wiki/ docs/ 2>/dev/null
git status --short
```

Say: "Doc changes staged. Review the diffs and commit when ready."

## What NOT to Update

- Internal-only refactors with no user-facing impact
- Auto-generated docs (check for generation scripts first — e.g., `docs/api/` with a Makefile)
- Scratch/debug scripts
- Unfinished/WIP features unless explicitly asked
- Test-only changes (unless they change how to run tests)
