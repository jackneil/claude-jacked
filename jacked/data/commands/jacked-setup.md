---
description: "Analyze this repo and generate faster, customized versions of jacked commands (/whats-next, /qa, /dcr)"
---

You are a repo analyzer. Your job is to examine the current repo's structure, tech stack, and conventions, then generate fully standalone command files that embed the engine logic with repo-specific config pre-filled.

> **How it works:** Each generated file is fully standalone — it embeds the complete engine logic with repo-specific config pre-filled. No jacked installation required to USE the generated commands. Commit these files to your repo so collaborators can use them without installing jacked. To get engine updates, upgrade jacked and re-run `/jacked-setup`. The engine's `## Config Override` section detects the `## Repo Config` header and skips discovery automatically.
>
> **Note:** Generated command files are exempt from the 300/500-line code guardrail — they are markdown command documents, not code files.

## Step 1: Parse Argument

Check `$ARGUMENTS` for a target:

| Argument | Action |
|----------|--------|
| `whats-next` | Generate config for `/whats-next` |
| `qa` | Generate config for `/qa` |
| `dcr` | Generate config for `/dcr` |
| `all` | Generate all three sequentially |
| *(empty)* | Show the explanation below and ask which to generate |

**If no argument provided**, show this:
```
/jacked-setup generates repo-specific config files that make jacked commands faster.
It analyzes your repo's tech stack, planning docs, and structure once, then saves the
results so future command runs skip discovery.

Available targets:
  whats-next  — Pre-configure lifecycle, planning doc paths, tier weights
  qa          — Pre-configure browser tool, framework checks, component paths
  dcr         — Pre-configure lens selection, context paths, domain-specific checks
  all         — Generate all three

Usage: /jacked-setup <target>
```
Then ask which target to generate.

If the argument doesn't match any of the above, say: "Unknown target. Valid options: `whats-next`, `qa`, `dcr`, `all`."

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
find docs design .claude/plans -name "*.md" -maxdepth 2 2>/dev/null | head -20
```

```bash
# Version detection
grep -r "version" pyproject.toml package.json Cargo.toml go.mod setup.py 2>/dev/null | grep -iE '^\s*version\s*[=:]' | head -5
grep -r "__version__" --include="*.py" -l 2>/dev/null | head -3
```

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
find docs design .claude/plans -name "*.md" -maxdepth 2 2>/dev/null | head -10
```

From these results, determine default lens weights:
- Multi-tenant signals found → **Access Control** always on
- API routes found → **Security** always on
- Pure library/CLI (no routes, no components) → **UX & Flow** usually off
- Test directory exists → **Testing** always on
- Guardrails docs found → **Guardrails** gets extra context paths

## Step 4: Check for Existing Local File

```bash
ls .claude/commands/whats-next.md .claude/commands/qa.md .claude/commands/dcr.md 2>/dev/null
```

If the target file already exists, ask the user conversationally: "A customized `/<target>` already exists at `.claude/commands/<target>.md`. Replace it with a fresh version?"

- If yes → proceed to generation
- If no → skip that target, move to next (if doing `all`)

## Step 5: Generate Standalone Command File

Create the directory if needed:
```bash
mkdir -p .claude/commands
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

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
<engine body, front matter and delegation Note stripped — embedded verbatim from ~/.claude/commands/<target>.md>
```

**Critical:** Use the Read tool output verbatim for the engine body. Do NOT reproduce it from memory. The engine body is injected as-is after the `<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->` marker.

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

## Tier Weights
Emphasize: <tier guidance based on lifecycle>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/whats-next.md` embedded here — front matter and delegation Note stripped]
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

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/qa.md` embedded here — front matter and delegation Note stripped]
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

## PROJECT_CONTEXT Paths
Read these for reviewer context (validate with `ls` first, skip missing):
<list each guardrails/convention/design doc path>

## Domain Wild Cards
In addition to the standard pool, include:
<2-3 repo-specific wild card questions based on project domain>

## Domain Pre-Mortem Scenarios
In addition to the standard pool, include:
<1-2 repo-specific failure scenarios based on project type>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
[Engine body from `~/.claude/commands/dcr.md` embedded here — front matter and delegation Note stripped]
```

## Step 6: Announce Results

For each generated file, announce:

```
Saved standalone `/<target>` at `.claude/commands/<target>.md` (Engine: jacked v<VERSION>).
**Commit this file** — it works for anyone who clones your repo, no jacked required.
To pick up future engine improvements: `uv tool install --upgrade claude-jacked && jacked install` then re-run `/jacked-setup <target>`.
```

If generating `all`, list all three results together.

If the repo is greenfield (<10 commits), add: "This is a young repo — re-run `/jacked-setup <target>` as your project matures to capture new planning docs and lifecycle changes."

## HARD RULES

- Generated files MUST be fully standalone — embed the full engine body (front matter stripped, delegation Note stripped) from `~/.claude/commands/<target>.md` after the `## Repo Config` section. Do NOT include a delegation line — the file must work without jacked installed.
- Use the Read tool to read the engine at generation time; do NOT reproduce it from memory. If the engine file is not found, stop with an error: "Engine file not found — run `jacked install` first."
- The `## Repo Config` section name is a stable contract — the embedded engine's `## Config Override` section depends on detecting this exact header. Do not rename this section without providing a migration path.
- Never log credential values — only variable names from env files.
- All `find` and `grep` commands must use `-maxdepth` or `--exclude-dir` to prevent hanging on large repos.
- If the repo passes the floor check but has minimal context, write a config with defaults. If it fails the floor check (zero manifests, zero source files, zero commits), do NOT generate — tell the user to add code first.
- Do NOT silently overwrite existing local files — always ask first.
