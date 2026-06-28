---
description: Use when setting up jacked in a new repo or after significant codebase changes. Analyzes the repo and generates faster, customized versions of jacked commands.
---

You are a repo analyzer. Your job is to examine the current repo's structure, tech stack, and conventions, then generate fully standalone command files that embed the engine logic with repo-specific config pre-filled.

> **How it works:** Each generated file is fully standalone — it embeds the complete engine logic with repo-specific config pre-filled. No jacked installation required to USE the generated commands. Commit these files to your repo so collaborators can use them without installing jacked. To get engine updates, upgrade jacked and re-run `/jacked-setup`. The engine's `## Config Override` section detects the `## Repo Config` header and skips discovery automatically.
>
> **Living document, not a one-time setup:** an outdated config is worse than a lean one. Each generated file stamps a repo fingerprint (generation commit + manifest set) and carries a self-contained staleness check so it can flag when the codebase has drifted away from the cached config. Re-run `/jacked-setup` as the project evolves — see the "When to re-run" triggers in Step 6.
>
> **Note:** Generated command files are exempt from the 300/500-line code guardrail — they are markdown command documents, not code files.

## Step 1: Parse Argument

Check `$ARGUMENTS` for a target:

| Argument | Action |
|----------|--------|
| `whats-next` | Generate config for `/whats-next` |
| `qa` | Generate config for `/qa` and `/ux` (always paired — they share one analysis pass) |
| `ux` | Generate config for `/ux` and `/qa` (always paired — they share one analysis pass) |
| `dcr` | Generate config for `/dcr` |
| `docs-sync` | Generate config for `/docs-sync` |
| `all` | Generate all five sequentially (qa and ux share one analysis pass) |
| *(empty)* | Show the explanation below and ask which to generate |

**If no argument provided**, show this:
```
/jacked-setup generates repo-specific config files that make jacked commands faster and
work for repo cloners without jacked installed.

Available targets:
  whats-next  — Pre-configure lifecycle, planning doc paths, strategic emphasis
  qa          — Pre-configure browser tool, framework checks, component paths (also generates /ux)
  ux          — Pre-configure parallel UX checks (also generates /qa)
  dcr         — Pre-configure lens selection, context paths, domain-specific checks
  docs-sync   — Pre-configure doc inventory, change-to-doc mapping, base branch
  all         — Generate all five

Usage: /jacked-setup <target>
```
Then ask which target to generate.

If the argument doesn't match any of the above, say: "Unknown target. Valid options: `whats-next`, `qa`, `ux`, `dcr`, `docs-sync`, `all`."

## Step 2: Common Repo Analysis

Run these to gather baseline context (all are gatekeeper-safe):

```bash
REPO_ROOT=$(git rev-parse --show-toplevel 2>/dev/null || pwd)
echo "REPO_ROOT=$REPO_ROOT"
echo "REPO_NAME=$(basename "$REPO_ROOT")"
```

```bash
# Tech stack detection
ls package.json pyproject.toml go.mod Cargo.toml setup.py Gemfile pom.xml build.gradle composer.json mix.exs 2>/dev/null
```

```bash
# Project type inference (from directory structure)
ls -d src lib app cmd internal pages components routes api 2>/dev/null
```

```bash
# Language detection (bounded depth, exclude dependency dirs)
find . -maxdepth 3 -type f \
  \( -name "*.py" -o -name "*.js" -o -name "*.ts" -o -name "*.tsx" -o -name "*.go" -o -name "*.rs" -o -name "*.java" -o -name "*.rb" \) \
  -not -path "*/node_modules/*" -not -path "*/.venv/*" -not -path "*/vendor/*" -not -path "*/dist/*" -not -path "*/build/*" \
  2>/dev/null | head -50 | sed 's/.*\.//' | sort | uniq -c | sort -rn
```

```bash
# Git maturity
git rev-list --count HEAD 2>/dev/null || echo "0"
git log --oneline -10 2>/dev/null
```

```bash
# Generation fingerprint (stamp into ## Repo Config so generated files can self-check staleness)
git rev-parse HEAD 2>/dev/null || echo "no-commits"
```

```bash
# GitHub CLI availability
gh auth status 2>/dev/null && echo "GH_OK" || echo "GH_NOT_AUTH"
```

From these results, determine:
- **Project name**: from repo root directory name
- **Stack**: languages and frameworks detected
- **Type**: web-app, CLI, library, API, monorepo, or other (infer from directory structure + manifests)

**Floor check:** If ALL of the following are true — no manifest files found, zero source files detected, and zero git commits — this repo has no useful context to cache. Tell the user: "This repo doesn't have enough structure yet for `/jacked-setup` to generate useful config. Add some code and commit history first, then try again." Stop here.

## Step 3: Target-Specific Analysis

Run additional analysis based on the target(s) being generated.

### For `whats-next`:

```bash
# Planning artifacts
ls ROADMAP.md IMPLEMENTATION_STATUS.md TODO.md BACKLOG.md FEEDBACK_BACKLOG.md 2>/dev/null
ls -d docs docs/plans docs/specs design .claude/plans 2>/dev/null
find docs design .claude/plans \( -name "*.md" -o -name "*.html" \) -maxdepth 2 2>/dev/null | head -20
```

```bash
# Version detection
grep -r "version" pyproject.toml package.json Cargo.toml go.mod setup.py 2>/dev/null | grep -iE '^\s*version\s*[=:]' | head -5
grep -r "__version__" --include="*.py" -l 2>/dev/null | head -3
```

**Asana access probe** (whats-next only) — try three methods in order, stop at the first that succeeds. This decides the `## Asana Integration` config block below.

1. **MCP**: check whether an Asana MCP plugin is installed and its tools are reachable.
   ```bash
   ls -d ~/.claude/plugins/marketplaces/*/external_plugins/asana ~/.claude/plugins/*asana* 2>/dev/null && echo "ASANA_PLUGIN_PRESENT" || echo "ASANA_PLUGIN_ABSENT"
   ```
   If present, try a read-only Asana MCP tool from whichever namespace is connected (e.g. `mcp__claude_ai_Asana__get_me` or `mcp__plugin_asana_asana__*`). If a call succeeds, record `Access: mcp` (note the namespace) and proceed to discovery.
2. **CLI**: probe for a local Asana CLI binary.
   ```bash
   command -v asana >/dev/null 2>&1 && asana --version 2>/dev/null
   command -v asana-cli >/dev/null 2>&1 && asana-cli --version 2>/dev/null
   ```
   If either responds, record `Access: cli` (and the binary name) and proceed to discovery.
3. **REST + PAT**: probe for a personal access token in the environment.
   ```bash
   if [ -n "$ASANA_PERSONAL_ACCESS_TOKEN" ] || [ -n "$ASANA_TOKEN" ]; then
     TOKEN="${ASANA_PERSONAL_ACCESS_TOKEN:-$ASANA_TOKEN}"
     curl -s --connect-timeout 5 --max-time 10 -o /dev/null -w "%{http_code}" -H "Authorization: Bearer $TOKEN" https://app.asana.com/api/1.0/users/me
   else
     echo "NO_TOKEN"
   fi
   ```
   `200` → record `Access: rest-pat` and proceed to discovery. `401` → the token is set but rejected: record `Access: none` (runtime will skip cleanly), and surface a refresh nudge instead of the generic install hint — tell the user their `ASANA_PERSONAL_ACCESS_TOKEN` is rejected and to refresh it at https://app.asana.com/0/my-apps. Any other non-200/no response → fall through.
4. **None**: if all three fail, record `Access: none`, skip discovery, and write the install-hint variant of the `## Asana Integration` block (see the whats-next template below).

**Asana zero-touch discovery** (only if access succeeded), using whichever method won:
- `users/me` — cache the user GID and friendly name.
- List the user's workspaces; report the count (`Found N workspace(s)`).
- List the projects the user belongs to per workspace; report the count (`Found M project(s) across N workspace(s)`).
- Ask one question: *"Track tasks across all M projects, or pick specific projects? [all/pick]"* (default `all` on empty input; if `pick`, list and accept a comma-separated selection).
- Sniff one or two selected projects' `custom_fields` for a name matching `Priority`, `Status`, `Tier`, `P0`, `P1`. If found, record the field GID + its enum value names. Do NOT pre-bake a values→tier mapping — the engine maps at runtime.

Infer **lifecycle stage** using these signals:

| Stage | Signals |
|-------|---------|
| Greenfield | <10 total commits OR repo <2 weeks old |
| Alpha | version 0.x.x, <100 commits, <5 open issues, sparse docs |
| Beta | version 0.x.x or early 1.x.x, active issues, some planning docs |
| Growth | version 1.x+, >10 open issues, roadmap exists, recent velocity |
| Maintenance | <5 commits/month, stable version, issues are mostly bugs |

### For `qa`:

```bash
# Frontend framework
grep -l "react\|vue\|svelte\|angular\|next\|nuxt\|remix\|astro" package.json 2>/dev/null
```

```bash
# Test framework
ls jest.config* vitest.config* cypress.config* playwright.config* .storybook 2>/dev/null
```

```bash
# CSS framework
grep -l "tailwind\|bootstrap\|bulma\|material-ui\|chakra\|styled-components" package.json 2>/dev/null
```

```bash
# Component paths
ls -d src/components app/components components 2>/dev/null
```

```bash
# Dev server port hints
grep -E "port|PORT" .env .env.local .env.development package.json 2>/dev/null | head -5
```

```bash
# Credential files (variable names only — never log values)
# Exclude infrastructure creds (DB_, DATABASE_, POSTGRES_, REDIS_, MONGO_, S3_, AWS_)
grep -iE "^[A-Z_]*(EMAIL|PASSWORD|USERNAME|LOGIN)[A-Z_]*=" .env.local .env.development .env.test .env 2>/dev/null | grep -viE "^(DB_|DATABASE_|POSTGRES_|REDIS_|MONGO_|S3_|AWS_)" | sed 's/=.*//' | head -5
```

Also check which browser tools are available:
- Try `mcp__plugin_playwright_playwright__browser_snapshot` → Playwright MCP
- Try `mcp__claude-in-chrome__tabs_context_mcp` → Claude-in-Chrome
- Try `npx agent-browser --version` → agent-browser CLI

> **Note:** This analysis also covers the `ux` target — `qa` and `ux` are always generated together since the qa skill routes to both. When both `qa` and `ux` are targets in the same run, execute this analysis once and reuse the results for both.

### For `dcr`:

```bash
# Security/auth patterns (bounded, exclude deps)
grep -rl "auth\|permission\|role\|tenant\|org_id\|user_id" \
  --include="*.py" --include="*.ts" --include="*.js" \
  --exclude-dir=node_modules --exclude-dir=.venv --exclude-dir=vendor --exclude-dir=dist \
  2>/dev/null | head -10
```

```bash
# Multi-tenancy signals
grep -rl "tenant\|organization\|workspace" \
  --include="*.py" --include="*.ts" \
  --exclude-dir=node_modules --exclude-dir=.venv \
  2>/dev/null | head -5
```

```bash
# API patterns
ls -d routes api endpoints controllers handlers 2>/dev/null
```

```bash
# Test infrastructure
ls -d tests test __tests__ spec 2>/dev/null
```

```bash
# Guardrails / conventions
ls CLAUDE.md .claude/CLAUDE.md GUARDRAILS.md JACKED_GUARDRAILS.md CONTRIBUTING.md .editorconfig 2>/dev/null
```

```bash
# Design docs
find docs design .claude/plans \( -name "*.md" -o -name "*.html" \) -maxdepth 2 2>/dev/null | head -10
```

From these results, determine default lens weights:
- Multi-tenant signals found → **Access Control** always on
- API routes found → **Security** always on
- Pure library/CLI (no routes, no components) → **UX & Flow** usually off
- Test directory exists → **Testing** always on
- Guardrails docs found → **Guardrails** gets extra context paths
- Database/ORM/migration tool detected (alembic, django migrations, prisma, knex, etc.) → **Data Integrity & Schema Safety** always on
- Service/API project with external integrations → **Observability & Debuggability** always on
- Pure library with no I/O or persistence → **Data Integrity & Schema Safety** and **Observability & Debuggability** usually off

### For docs-sync:

```bash
# Base branch
git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||' || echo "main"

# Doc files at repo root
ls README.md CONTRIBUTING.md CHANGELOG.md LICENSE.md 2>/dev/null

# Wiki structure
ls -d _wiki 2>/dev/null
ls _wiki/*.md 2>/dev/null | head -30
ls _wiki/_Sidebar.md _wiki/_Footer.md _wiki/Home.md 2>/dev/null

# Wiki CI workflow
ls .github/workflows/update-wiki.yml .github/workflows/wiki*.yml 2>/dev/null

# CLAUDE.md sections
grep -n "^#" CLAUDE.md 2>/dev/null | head -20

# docs/ directory
find docs -name "*.md" -maxdepth 2 2>/dev/null | head -20

# Other root-level markdown
ls *.md 2>/dev/null
```

**No-wiki handling:** If no `_wiki/` directory found, note "no wiki" in the config. Do NOT offer to scaffold one during setup — that's a runtime decision.

Build the **Doc Inventory** from results — list each discovered doc file with its detected sections (grep for `^#` headings).

Build the **Change-to-Doc Map** — a table mapping change categories to the specific doc files that exist in THIS repo. Only include rows where the target doc actually exists.

## Step 4: Check for Existing Local Files

```bash
ls .claude/commands/whats-next.md .claude/commands/qa.md .claude/commands/ux.md .claude/commands/dcr.md .claude/commands/docs-sync.md 2>/dev/null
ls .claude/skills/whats-next/SKILL.md .claude/skills/qa/SKILL.md .claude/skills/ux/SKILL.md .claude/skills/dcr/SKILL.md .claude/skills/docs-sync/SKILL.md 2>/dev/null
```

Only check the **target(s) being generated** (not all files found by the `ls`). For any command file that exists, determine its format using a positive signal:
```bash
# New standalone files always have ## Repo Config; old overlays do not
grep -q '## Repo Config' .claude/commands/<target>.md 2>/dev/null && echo "STANDALONE" || echo "OLD_FORMAT"
```

**qa and ux are always a pair — handle their consent as one decision:**
- Run the format check for whichever of `qa.md` and `ux.md` exist
- If EITHER is `OLD_FORMAT` (old overlay, missing, or hand-crafted without `## Repo Config`): show ONE combined warning — "⚠️ Your existing `/qa` and/or `/ux` command files depend on jacked being installed on every developer's machine. Regenerating makes them fully standalone — repo cloners can use them without jacked." Ask: "Regenerate both?"
- If BOTH are `STANDALONE`: ask conversationally: "Both `/qa` and `/ux` already exist. Replace with fresh versions?"
- If yes → generate both; if no → skip both. Never generate one without the other.

**For non-paired targets (`whats-next`, `dcr`, `docs-sync`):** if the command **or** skill file already exists:
- **If command file exists and is `OLD_FORMAT`**: Warn — "⚠️ Your existing `/<target>` depends on jacked being installed on every developer's machine. Regenerating makes it fully standalone." Ask: "Regenerate now?"
- **If `STANDALONE` (or only skill file exists, no command file to check)**: Ask conversationally: "A `/<target>` already exists. Replace with a fresh version?"
- If yes → proceed; if no → skip that target, move to next (if doing `all`)

**Regenerating a `STANDALONE` file is a MERGE, not a clobber.** When the existing target already
has a `## Repo Config` the user may have hand-edited, do NOT blindly overwrite it:
1. Read the existing `## Repo Config` (and any sibling sections like `## Domain Wild Cards`,
   `## Default Lens Selection`, `## Framework-Specific Checks`).
2. Re-infer fresh values, then show the user a DIFF of changed inferred values (old → new).
3. **Preserve any user-added or user-corrected lines** — custom lens weights, extra wild cards,
   hand-fixed paths/ports — carrying them into the regenerated file rather than discarding them.
   When in doubt, keep the user's value and flag the inferred alternative as a comment.

## Step 5: Generate Standalone Command and Skill Files

**Confirm high-risk inferred values before writing.** After Step 3 inference, echo the key
inferred values back to the user — detected dev-server port, build/test commands (if surfaced),
browser tool, component/source paths, and lifecycle stage — and either let them correct the set
in one pass, or cheaply validate them yourself first (e.g. confirm the inferred component/path
dirs still exist using the `ls` results already gathered in Step 2). Don't bake a wrong guess
into a committed file.


Create the directories if needed:
```bash
mkdir -p .claude/commands
mkdir -p .claude/skills/<target>
```

**Before writing, get the jacked version:**
```bash
uv tool list 2>/dev/null | grep -E "^claude-jacked" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1 || \
python3 -c "from importlib.metadata import version; print(version('claude-jacked'))" 2>/dev/null || \
echo "unknown"
```
Use this as `<VERSION>` in the header below.

**Read the engine file:**
Use the Read tool to read `~/.claude/commands/<target>.md`. If the file is not found, stop and tell the user: "Engine file `~/.claude/commands/<target>.md` not found — run `jacked install` first." Do NOT proceed without reading the actual file.

**Prepare the engine body:**
From the engine file content:
1. Strip the front matter block — remove everything from line 1 through the second `---` line (inclusive), plus any immediately following blank line.
2. Anywhere in the file (typically in the preamble, before `## Config Override`), omit any `> **Note:**` block that references the old delegation model (e.g. a block starting "If `.claude/commands/<target>.md` exists in the current repo..."). This describes a flow that no longer applies in standalone files. If no such block exists, skip this step.

**Write the generated file** in this structure:

```markdown
---
description: "<standalone description — see per-target templates below>"
---
# Generated by /jacked-setup — <today's date> | Template v1 | Engine: jacked v<VERSION>
# Standalone — no dependencies. Commit this file. To update: uv tool install --upgrade claude-jacked && jacked install && /jacked-setup <target>

## Repo Config

<structured config data discovered above>

<!-- Staleness fingerprint — the engine compares these against the live repo on each run -->
- **Generated at commit**: <git rev-parse HEAD from Step 2>
- **Detected stack**: <manifest set from Step 2 tech-stack detection — e.g. `pyproject.toml, package.json`>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
<engine body, front matter and delegation Note stripped — embedded verbatim from ~/.claude/commands/<target>.md>
```

**Critical:** Use the Read tool output verbatim for the engine body. Do NOT reproduce it from memory. The engine body is injected as-is after the `<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->` marker.

**Staleness self-check (bake into every generated file).** Immediately after the `## Repo Config` block (just above the `<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->` marker), append this verbatim `## Config Staleness Check` block so the embedded engine's Config-Override path runs it on every invocation. It is **advisory, never blocking** — it surfaces a one-line nudge and then proceeds normally:

```markdown
## Config Staleness Check
Before using the config above, run a lightweight drift check (advisory only — never block on it):
1. `git rev-parse HEAD` and compare to **Generated at commit** above.
2. `ls package.json pyproject.toml go.mod Cargo.toml setup.py Gemfile pom.xml build.gradle composer.json mix.exs 2>/dev/null` and compare the manifest set to **Detected stack** above.
3. If EITHER differs, count the intervening commits (`git rev-list --count <generated-commit>..HEAD 2>/dev/null`) and surface one line, then continue:
   > ⚠️ This `/<target>` config was generated N commits ago and the stack/paths may have drifted — re-run `/jacked-setup <target>` to refresh.
   If the generated commit is unreachable (history rewritten) or the manifest set changed, surface the nudge regardless of commit count. Never stop or skip the run on a staleness mismatch.
```

**Also write a local skill file** after the command file. Use `mkdir -p .claude/skills/<target>` first. Local skills use RELATIVE paths — do NOT use `~/.claude/commands/`. Do NOT add Glob fallback checks to local skills (those are only for global skills). See per-target skill bodies below.

### whats-next standalone template:

```markdown
---
description: "Roadmap advisor — standalone (generated <date>; upgrade jacked + re-run /jacked-setup to update)"
---
# Generated by /jacked-setup — <date> | Template v1 | Engine: jacked v<VERSION>
# Standalone — no dependencies. Commit this file. To update: uv tool install --upgrade claude-jacked && jacked install && /jacked-setup whats-next

## Repo Config

- **Project**: <name>
- **Type**: <web-app|CLI|library|API|monorepo|other>
- **Stack**: <languages, frameworks>
- **Lifecycle**: <Greenfield|Alpha|Beta|Growth|Maintenance>
- **GitHub**: <authenticated|not authenticated>

## Planning Artifacts
Validate paths before reading (skip missing ones silently):
<list each discovered path>

## TODO Scan Extensions
Include: <file extensions for detected languages>

## Strategic Emphasis
Lifecycle lean: <where to weight the Step 6 decision based on lifecycle — e.g. Greenfield/Alpha: capability gaps in the core loop; Beta/Growth: cross-cutting experience levers; Maintenance: operational/debt levers. A hint for the single decision, not a ranking scheme.>

## Asana Integration

<If Access succeeded, emit this block populated:>
- **Access**: <mcp|cli|rest-pat> <(note MCP namespace or CLI binary if relevant)>
- **User GID**: <user gid> — <user name>
- **Workspaces**:
  - <workspace gid> — <workspace name>
- **Projects**: <all | list of `- <project gid> — <project name>`>
- **Priority Field**:
  - GID: <field gid>
  - Name: <field name>
  - Values: <comma-separated enum value names>
  (Omit the Priority Field block if no matching field was sniffed.)

<If Access is `none`, emit this block instead — install hint only:>
- **Access**: none
- **To enable**: install an Asana MCP plugin (recommended) — or set `ASANA_PERSONAL_ACCESS_TOKEN` from https://app.asana.com/0/my-apps — or install an `asana` CLI. Then re-run `/jacked-setup whats-next`.

<!-- Staleness fingerprint — the engine compares these against the live repo on each run -->
- **Generated at commit**: <git rev-parse HEAD from Step 2>
- **Detected stack**: <manifest set from Step 2 tech-stack detection>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/whats-next.md` embedded here — front matter and delegation Note stripped]
```

**whats-next local skill** (write to `.claude/skills/whats-next/SKILL.md`):
```markdown
---
name: whats-next
description: "Roadmap advisor — weighs a coverage-matrix read plus plans, issues, commits, and lifecycle to decide the single highest-leverage initiative and forge a ready-to-run /goal brief. (repo)"
---
Read `.claude/commands/whats-next.md` and follow it.
```

### qa standalone template:

```markdown
---
description: "Browser QA — standalone (generated <date>; upgrade jacked + re-run /jacked-setup to update)"
---
# Generated by /jacked-setup — <date> | Template v1 | Engine: jacked v<VERSION>
# Standalone — no dependencies. Commit this file. To update: uv tool install --upgrade claude-jacked && jacked install && /jacked-setup qa

## Repo Config

- **Project**: <name>
- **Stack**: <frontend framework, CSS framework>
- **Browser Tool**: <Playwright MCP|Claude-in-Chrome|agent-browser|none detected>
- **Dev Server Port**: <port if detected, or "auto-detect">
- **Component Paths**: <paths if found>

## Credential Hints
<variable names from env files, or "none found — ask user if login required">

## Framework-Specific Checks
<generated based on detected framework, e.g.:>
- React: Verify key props on list items, check useEffect cleanup, test controlled inputs
- Tailwind: Check responsive classes at mobile/tablet breakpoints
- Next.js: Test client/server component boundaries, check hydration

<!-- Staleness fingerprint — the engine compares these against the live repo on each run -->
- **Generated at commit**: <git rev-parse HEAD from Step 2>
- **Detected stack**: <manifest set from Step 2 tech-stack detection>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/qa.md` embedded here — front matter and delegation Note stripped]
```

**qa local skill** (write to `.claude/skills/qa/SKILL.md`):
```markdown
---
name: qa
description: "Browser-based QA testing — targeted single-component check (/qa) or parallel multi-aspect review (/ux). (repo)"
---
Two commands are available — read the appropriate one and follow it:
- `.claude/commands/qa.md` — Quick, focused QA pass (single agent). Best for targeted fixes or single-feature verification.
- `.claude/commands/ux.md` — Thorough parallel UX review (multiple agents). Best when changes touch layout, navigation, or multiple components.

Both are read-only detection tools — they return a detailed issue list but do NOT fix code.

Decision guide:
- Changed button styling or a single component? → `/qa`
- Changed layout, interactions, AND multiple pages? → `/ux`
```

### ux standalone template:

```markdown
---
description: "Parallel UX checks — standalone (generated <date>; upgrade jacked + re-run /jacked-setup to update)"
---
# Generated by /jacked-setup — <date> | Template v1 | Engine: jacked v<VERSION>
# Standalone — no dependencies. Commit this file. To update: uv tool install --upgrade claude-jacked && jacked install && /jacked-setup ux

## Repo Config

- **Project**: <name>
- **Stack**: <frontend framework, CSS framework>
- **Browser Tool**: <Playwright MCP|Claude-in-Chrome|agent-browser|none detected>
- **Dev Server Port**: <port if detected, or "auto-detect">
- **Component Paths**: <paths if found>

## Credential Hints
<variable names from env files, or "none found — ask user if login required">

## UX Focus Areas
<emphasis based on stack — e.g. "Tailwind: verify responsive breakpoints (375px, 768px, 1280px)" or "Next.js: test hydration boundaries, client/server component interactions" or "Nav changes: emphasize Discoverability aspect across all agents">

<!-- Staleness fingerprint — the engine compares these against the live repo on each run -->
- **Generated at commit**: <git rev-parse HEAD from Step 2>
- **Detected stack**: <manifest set from Step 2 tech-stack detection>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/ux.md` embedded here — front matter and delegation Note stripped]
```

**ux local skill** (write to `.claude/skills/ux/SKILL.md`):
```markdown
---
name: ux
description: "Parallel browser-based UX checks — spawns focused agents to test different UX aspects simultaneously. (repo)"
---
Read `.claude/commands/ux.md` and follow it.
```

### dcr standalone template:

```markdown
---
description: "Parallel recursive review — standalone (generated <date>; upgrade jacked + re-run /jacked-setup to update)"
---
# Generated by /jacked-setup — <date> | Template v1 | Engine: jacked v<VERSION>
# Standalone — no dependencies. Commit this file. To update: uv tool install --upgrade claude-jacked && jacked install && /jacked-setup dcr

## Repo Config

- **Project**: <name>
- **Type**: <type>
- **Stack**: <stack>

## Default Lens Selection
Always on: Guardrails, <lenses based on analysis>
Usually off: <lenses unlikely to be relevant>
(Still allow runtime override — if changes clearly involve an "off" lens, include it)

## Planning Phase Lenses
When reviewing a plan or design doc (PLANNING phase, no code changes yet):
Use: Guardrails, Logic & Edge Cases, Maintainability, Simplicity & Reuse
Skip: Security, Access Control, Performance, UX & Flow, Testing, Observability & Debuggability, Data Integrity & Schema Safety (can't assess without code)
Reviewers focus on: missing edge cases in the design, over-engineering, architectural soundness, plan completeness
<add any repo-specific emphasis, e.g. "Logic & Edge Cases should check tenant isolation assumptions in any multi-tenant design">

## PROJECT_CONTEXT Paths
Read these for reviewer context (validate with `ls` first, skip missing):
<list each guardrails/convention/design doc path>

## Domain Wild Cards
In addition to the standard pool, include:
<2-3 repo-specific wild card questions based on project domain>

## Domain Pre-Mortem Scenarios
In addition to the standard pool, include:
<1-2 repo-specific failure scenarios based on project type>

<!-- Staleness fingerprint — the engine compares these against the live repo on each run -->
- **Generated at commit**: <git rev-parse HEAD from Step 2>
- **Detected stack**: <manifest set from Step 2 tech-stack detection>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/dcr.md` embedded here — front matter and delegation Note stripped]
```

**dcr local skill** (write to `.claude/skills/dcr/SKILL.md`):
```markdown
---
name: dcr
description: "Parallel recursive review — selects relevant lenses, spawns focused reviewers per wave until all selected lenses pass clean. (repo)"
---
Read `.claude/commands/dcr.md` and follow it.
```

### docs-sync standalone template:

```markdown
---
description: "Docs sync — standalone (generated <date>; upgrade jacked + re-run /jacked-setup to update)"
---
# Generated by /jacked-setup — <date> | Template v1 | Engine: jacked v<VERSION>
# Standalone — no dependencies. Commit this file. To update: uv tool install --upgrade claude-jacked && jacked install && /jacked-setup docs-sync

## Repo Config

- **Project**: <name>
- **Base Branch**: <main|master|detected>
- **Stack**: <languages, frameworks>

## Doc Inventory
<list each discovered doc file with detected sections>
Examples:
- README.md (sections: Install, Usage, Features, Config)
- CLAUDE.md (sections: Architecture, Testing, Env Vars)
- _wiki/ (N pages, has/no _Sidebar.md)
- .github/workflows/update-wiki.yml (wiki CI: active/none)

## Change-to-Doc Map
| Change Category | Affected Docs |
|----------------|---------------|
| Pipeline/Architecture | <list existing docs that cover architecture> |
| Configuration | <list existing docs that cover config/env vars> |
| Commands/CLI | <list existing docs that cover usage> |
| Dependencies | <list existing docs that cover installation> |
| UI/Frontend | <list existing docs that cover features> |
| Models/Schemas | <list existing docs that cover data models> |
| Tests | <list existing docs that cover testing> |

<!-- Staleness fingerprint — the engine compares these against the live repo on each run -->
- **Generated at commit**: <git rev-parse HEAD from Step 2>
- **Detected stack**: <manifest set from Step 2 tech-stack detection>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/docs-sync.md` embedded here — front matter and delegation Note stripped]
```

**docs-sync local skill** (write to `.claude/skills/docs-sync/SKILL.md`):
```markdown
---
name: docs-sync
description: "Sync docs with code changes — diffs branch, maps to affected docs, spawns parallel update agents. (repo)"
---
Read `.claude/commands/docs-sync.md` and follow it.
```

## Step 6: Announce Results

For each generated target, announce:

```
Saved standalone `/<target>` at `.claude/commands/<target>.md` (Engine: jacked v<VERSION>).
Also saved local skill at `.claude/skills/<target>/SKILL.md`.
**Commit both `.claude/commands/` and `.claude/skills/`** — repo cloners get slash commands AND auto-triggering without jacked installed.
To pick up future engine improvements: `uv tool install --upgrade claude-jacked && jacked install` then re-run `/jacked-setup <target>`.
```

If generating `all`, list all five results together.

**After generation, run a .gitignore check:**
```bash
git check-ignore -q .claude 2>/dev/null && echo "GITIGNORED" || echo "OK"
```
If the result is `GITIGNORED`, warn the user:
> ⚠️ `.claude/` appears to be gitignored. Your teammates and repo cloners won't get these files unless you commit them explicitly. Add a `.gitignore` exception: `!.claude/` (or commit the files directly with `git add -f .claude/`).

If the repo is greenfield (<10 commits), add: "This is a young repo — re-run `/jacked-setup <target>` as your project matures to capture new planning docs and lifecycle changes."

**Confirm the staleness self-check is baked in.** Verify each generated file carries the `## Config Staleness Check` block (from Step 5) plus its two stamped fingerprint lines (**Generated at commit**, **Detected stack**). This is what lets the embedded engine's Config-Override path flag drift on future runs — an advisory nudge, never a hard stop.

**When to re-run:** the generated config is a living document — re-run `/jacked-setup <target>` when:
- The stack or framework changes (new language, new frontend/CSS framework, swapped build tool).
- New planning docs appear (roadmap, specs, design docs) that should feed the config.
- The project lifecycle shifts (Greenfield → Alpha → Beta → Growth → Maintenance).
- A major refactor moves component, route, or API paths the config points at.

## HARD RULES

- Generated files MUST be fully standalone — embed the full engine body (front matter stripped, delegation Note stripped) from `~/.claude/commands/<target>.md` after the `## Repo Config` section. Do NOT include a delegation line — the file must work without jacked installed.
- Use the Read tool to read the engine at generation time; do NOT reproduce it from memory. If the engine file is not found, stop with an error: "Engine file not found — run `jacked install` first."
- The `## Repo Config` section name is a stable contract — the embedded engine's `## Config Override` section depends on detecting this exact header. Do not rename this section without providing a migration path.
- Never log credential values — only variable names from env files.
- All `find` and `grep` commands must use `-maxdepth` or `--exclude-dir` to prevent hanging on large repos.
- If the repo passes the floor check but has minimal context, write a config with defaults. If it fails the floor check (zero manifests, zero source files, zero commits), do NOT generate — tell the user to add code first.
- Do NOT silently overwrite existing local files — always ask first (check both command and skill files).
- Each target generates TWO files: `.claude/commands/<target>.md` (standalone command) AND `.claude/skills/<target>/SKILL.md` (local skill). Both must be committed for cloners to get full functionality.
- Local skill files MUST use relative paths (`.claude/commands/<target>.md`). Do NOT use `~/.claude/commands/`. Do NOT add Glob fallback checks to local skills — that pattern belongs only in global skills.
- `qa` and `ux` are always paired — generating either one generates both (they share one analysis pass and the qa skill routes to both commands).
