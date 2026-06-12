---
description: Use when a branch has code changes that may have made documentation stale, after completing a feature, or before creating a PR.
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

If there are no changes (empty diff), **do not stop** — proceed to Step 2.5 in case stale docs need auditing. Otherwise, categorize changes below.

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

## Step 2.5: Stale Doc Detection (mandatory)

Documentation rots silently when PRs forget to run `/docs-sync`. Catch that drift here. Every doc file whose last git-tracked modification is **older than 30 days** gets a full fresh audit, regardless of whether the current branch touches it.

Find stale docs using git's last-commit timestamp (filesystem mtime is unreliable — it changes on checkout):

```bash
THIRTY_DAYS_AGO=$(( $(date +%s) - 30*24*60*60 ))

# Collect candidate doc files (root markdown, docs/, _wiki/, CLAUDE.md hierarchy)
DOC_CANDIDATES=$(
  {
    ls *.md 2>/dev/null
    find docs -name "*.md" 2>/dev/null
    find _wiki -name "*.md" 2>/dev/null
    find . -maxdepth 4 -name "CLAUDE.md" 2>/dev/null
  } | sort -u
)

STALE_DOCS=()
for doc in $DOC_CANDIDATES; do
  [ -f "$doc" ] || continue
  LAST_COMMIT_TS=$(git log -1 --format=%ct -- "$doc" 2>/dev/null)
  # If file is untracked or has no history, fall back to filesystem mtime
  [ -z "$LAST_COMMIT_TS" ] && LAST_COMMIT_TS=$(stat -f %m "$doc" 2>/dev/null || stat -c %Y "$doc" 2>/dev/null)
  [ -z "$LAST_COMMIT_TS" ] && continue
  if [ "$LAST_COMMIT_TS" -lt "$THIRTY_DAYS_AGO" ]; then
    DAYS_OLD=$(( ($(date +%s) - LAST_COMMIT_TS) / 86400 ))
    echo "STALE: $doc (${DAYS_OLD}d since last edit)"
    STALE_DOCS+=("$doc")
  fi
done
```

Build a **stale audit queue** from this output. These docs go through the full audit pipeline in Step 4 even if the current branch doesn't touch their related code.

If `STALE_DOCS` is empty AND no branch changes exist, say: "No branch changes and no stale docs. Nothing to sync." Stop.

If `STALE_DOCS` is empty but branch changes exist, proceed to Step 3 with branch-driven mapping only.

If `STALE_DOCS` has entries, they will be merged into the agent dispatch in Step 4 with a `fresh-audit` flag (no diff context — agents must verify the entire doc against current code).

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

Filter to only doc files that actually exist. Merge with `STALE_DOCS` from Step 2.5.

If the combined set (branch-affected ∪ stale) is empty, say: "Changes don't affect any docs and no stale docs found." Stop.

## Step 4: Spawn Audit Agents (Multi-Pass Verification)

Spawn one agent per doc file in the combined set. Agents run in parallel — send all Agent tool calls in a single message.

**Every agent runs a 3-pass verification protocol** (described inside the agent prompt). This is non-negotiable. Single-pass updates produce confident-sounding-but-wrong docs that mislead future readers.

**Dispatch modes:**
- `branch-driven` — doc is in the branch-change map. Agent gets diff context.
- `fresh-audit` — doc is in `STALE_DOCS`. Agent gets NO diff (might be stale in ways nobody flagged). Must audit the entire file against current code from scratch.
- `both` — doc is in both. Agent does fresh-audit AND incorporates branch changes.

**Use the appropriate template below.** Each template embeds the 3-pass protocol verbatim — do not summarize or shorten it when constructing the agent prompt.

### The 3-Pass Verification Protocol (embedded in every agent prompt)

```
You will run THREE passes over this doc. Do not skip passes. Do not collapse them. Each pass has a different lens — running them separately catches different failure modes.

PASS 1 — UPDATE
1. Read the entire target doc file from line 1 to EOF. Do not skim. Do not jump to "the relevant section" — you do not yet know what is relevant.
2. Read every source file referenced in your Context section, fully. If the doc mentions a file, function, env var, CLI flag, or directory, locate it in the codebase and read it. Do not trust the doc's description over the code.
3. Make edits. Update factual claims that are wrong. Update examples that no longer run. Update file paths that have moved. Update version numbers, env var names, and command syntax that have drifted. Do not rewrite voice, structure, or tone.

PASS 2 — CROSS-CHECK (the "lying detector" pass)
After Pass 1, re-read the doc you just edited. For EVERY factual claim that remains in the doc — including ones you didn't touch — verify it against the current code:
- Every file path mentioned: does the file exist at that path right now? Run ls or Read to confirm.
- Every function/class/method named: does it exist with that signature? Grep to confirm.
- Every env var: is it actually read by the code? Grep for it.
- Every CLI flag, subcommand, or argument: does the current CLI accept it? Read the argparse/click definitions.
- Every code example or snippet: would it actually run today? Trace it mentally against the current code.
- Every cross-reference to another doc or section: does the target still exist?
For each claim that fails verification, fix it. If you cannot verify a claim with reasonable effort, mark it: insert an HTML comment <!-- docs-sync: unable to verify --> next to it and note it in your final report. Do not silently leave unverifiable claims.

PASS 3 — MISDIRECTION AUDIT (the "fresh reader" pass)
Re-read the doc one more time, this time as if you were a developer encountering this codebase for the first time. For each section ask:
- If I followed these instructions literally, would I succeed, or would I hit an error?
- Does this section accurately describe what the code does, or does it describe what it USED to do, or what someone WANTED it to do?
- Are there critical setup steps, dependencies, or gotchas not mentioned that the code clearly requires?
- Is anything dangerously misleading — wrong defaults, wrong order of operations, wrong assumptions about state?
Fix anything that would misdirect a fresh reader. Misdirection is worse than absence — silence is honest, wrong instructions are not.

OUTPUT
At the end, report:
- Sections updated and why
- Verification failures discovered in Pass 2 (and how you fixed them)
- Misdirection issues caught in Pass 3 (and how you fixed them)
- Any claims left marked as unverifiable and why
```

---

**README Agent** — branch-driven or fresh-audit:

```
You are a README auditor and updater. Your job is to make README.md perfectly accurate against the current codebase. Future readers will rely on this doc — every wrong claim is a trap.

## Mode
<branch-driven | fresh-audit | both>

## Context (branch-driven or both)
Base branch: <base>
Changes summary:
<paste git diff --stat output>

Changed files:
<paste git diff --name-only output>

Commit messages:
<paste git log --oneline output>

## Context (fresh-audit or both)
This doc has not been edited in the git history for over 30 days. The codebase has likely drifted. Audit the entire README against the current code. Do not assume any claim is correct just because it has been there for a while.

## Style constraints
- Match existing voice, formatting, and structure
- Do not add emojis unless the existing README uses them
- Do not rewrite sections that are still accurate, even if you would phrase them differently
- If a new feature exists with no README coverage, add a minimal section in the appropriate location

## Verification protocol
<paste the entire 3-Pass Verification Protocol above, verbatim>
```

**Wiki Agent** — branch-driven or fresh-audit:

```
You are a wiki page auditor and updater for pages under _wiki/. Your job is to make these pages perfectly accurate against the current codebase.

## Mode
<branch-driven | fresh-audit | both>

## Target pages
<list specific pages — one agent per page if pages are large, otherwise group related pages>

## Context (branch-driven or both)
Base branch: <base>
Changes summary:
<paste git diff --stat output>

## Context (fresh-audit or both)
This page has not been edited in the git history for over 30 days. Audit the entire page against the current code from scratch.

## Style constraints
- Match existing wiki formatting and structure
- If _wiki/_Sidebar.md exists and you added/removed pages, update the sidebar
- Cross-link to related wiki pages when natural; check those links resolve

## Verification protocol
<paste the entire 3-Pass Verification Protocol above, verbatim>
```

**CLAUDE.md Agent** — branch-driven or fresh-audit:

```
You are a CLAUDE.md auditor and updater. Your job is to make CLAUDE.md project instructions factually accurate against the current codebase.

## Mode
<branch-driven | fresh-audit | both>

## Context (branch-driven or both)
Base branch: <base>
Changes summary:
<paste git diff --stat output>

## Context (fresh-audit or both)
This CLAUDE.md has not been edited in the git history for over 30 days. Audit the entire file against the current codebase.

## Scope constraints
- Update factual descriptions: architecture, testing commands, env vars, file paths, build steps
- Do NOT add new behavioral rules or instructions on your own
- Do NOT remove existing rules unless they unambiguously reference deleted code
- If a rule references a removed file/function, update it to reference the replacement (if there is one) or flag it for human review with an HTML comment

## Verification protocol
<paste the entire 3-Pass Verification Protocol above, verbatim>
```

**Generic Doc Agent** — for any other markdown file in the stale audit queue (e.g., `docs/architecture.md`, `CONTRIBUTING.md`):

```
You are a documentation auditor for <doc path>. Your job is to make this doc perfectly accurate against the current codebase.

## Mode
fresh-audit (file unedited for over 30 days in git history)

## Style constraints
- Match existing voice, formatting, and structure
- Preserve intentional formatting choices (callouts, tables, code blocks)

## Verification protocol
<paste the entire 3-Pass Verification Protocol above, verbatim>
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

Aggregate every agent's verification report. Show a structured summary:

```
## docs-sync complete

**Branch-driven updates:**
- README.md: updated <sections>
- _wiki/page.md: updated <sections>

**Stale-doc audits (>30d unedited):**
- docs/architecture.md (87d): <fixes applied>
- CONTRIBUTING.md (412d): <fixes applied>

**Verification findings (Pass 2 cross-check):**
- <claim that was wrong> → <how it was fixed>
- <claim left unverifiable> → flagged with <!-- docs-sync: unable to verify -->

**Misdirection caught (Pass 3 fresh-reader audit):**
- <misleading instruction> → <correction>

**Suggestions:**
- <any new doc suggestions from Step 5>
```

If any agent left `<!-- docs-sync: unable to verify -->` markers, list them explicitly so the human can resolve them before merging.

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
