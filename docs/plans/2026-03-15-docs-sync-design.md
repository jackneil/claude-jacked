# docs-sync Integration Design

**Date:** 2026-03-15
**Status:** Draft
**Scope:** Add `/docs-sync` as an installable jacked skill with `/jacked-setup docs-sync` customization

## Problem

Documentation drifts from code. README sections go stale, wiki pages reference old CLI flags, CLAUDE.md architecture descriptions lag behind refactors. The fix is a skill that diffs a branch, figures out which docs are affected, and spawns parallel agents to update them.

A standalone `docs-sync` skill existed at `~/.claude/skills/docs-sync/SKILL.md` but was overfit to specific repos (hardcoded wiki page names, assumed `_wiki/` structure, hardcoded `main` as base branch). It needs to become a proper jacked installable skill with `/jacked-setup` customization so the doc mapping is repo-specific and pre-configured.

## Decisions

| Question | Answer |
|----------|--------|
| Repos without `_wiki/` | Ask user: scaffold recommended structure or just sync what exists |
| Base branch detection | Auto-detect at setup time, bake into Repo Config |
| Update aggression | Update existing docs + suggest new docs where gaps exist (don't auto-create) |
| Commit behavior | Stage changes and show summary, user decides when to commit |
| Runtime without setup | Error with "run `/jacked-setup docs-sync` first" — setup is required |
| Approach | Same pattern as dcr/qa/ux/whats-next (engine + jacked-setup target + global skill stub) |

## Deliverables

### 1. Engine File — `jacked/data/commands/docs-sync.md`

The core workflow that runs when `/docs-sync` is invoked.

**Flow:**

1. **Config gate** — Check for `## Repo Config` header. If missing: "Run `/jacked-setup docs-sync` first to configure docs-sync for this repo." Stop.

2. **Diff analysis** — Diff current branch against the configured base branch:
   ```bash
   git diff <base>...HEAD --stat
   git diff <base>...HEAD --name-only
   git log <base>..HEAD --oneline
   ```
   Categorize changes into:
   - Pipeline/architecture (new entry points, changed flows)
   - Configuration (new env vars, settings)
   - Commands/CLI (new flags, changed usage)
   - Models/schemas (new models, changed fields)
   - Tests (new test patterns, changed test commands)
   - Dependencies (new requirements)

3. **Map changes to docs** — Use the `## Change-to-Doc Map` table from Repo Config to determine which doc files are affected. Validate each target path still exists before including it.

4. **Spawn parallel agents** — Up to 3 agents based on what's stale:
   - **README agent** (`readme-maintainer` type) — if README-affecting changes detected
   - **Wiki agent** (`wiki-documentation-architect` type) — if `_wiki/` pages are affected (only if wiki exists per config)
   - **CLAUDE.md agent** — if architecture/behavioral changes detected

5. **Suggest new docs** — After agents complete, check if changes introduced things with no existing doc coverage (new env vars with no env var table, new CLI flags with no usage section). Suggest creating them. Don't auto-create.

6. **Stage and report** — Show summary of all changes made, stage them, let the user decide on commit.

**What NOT to update:**
- Internal-only refactors with no user-facing impact
- Auto-generated docs (check for generation scripts first)
- Scratch/debug scripts
- Unfinished/WIP features unless explicitly asked

### 2. Global Skill Stub — `jacked/data/skills/docs-sync/SKILL.md`

Thin delegation, same pattern as dcr/qa/ux:

```markdown
---
name: docs-sync
description: "Use when a branch has code changes that may have made docs stale. Diffs against base branch, identifies affected docs, and spawns parallel agents to update them."
---
First, check if a repo-scoped version exists:
1. If `.claude/skills/docs-sync/SKILL.md` exists in the current repo -> read and follow it instead.
2. If `.claude/commands/docs-sync.md` exists in the current repo -> read and follow it instead.
3. Otherwise, read `~/.claude/commands/docs-sync.md` and follow it.
```

### 3. jacked-setup Target — `docs-sync`

Added to `jacked/data/commands/jacked-setup.md` in three places:

**A. Argument table** — new row: `| docs-sync | Generate config for /docs-sync |`

**B. Target-specific analysis** — new section `### For docs-sync:`:

Detection checks:
```bash
# Base branch
git symbolic-ref refs/remotes/origin/HEAD 2>/dev/null | sed 's|refs/remotes/origin/||'

# Doc files at repo root
ls README.md CONTRIBUTING.md CHANGELOG.md LICENSE.md 2>/dev/null

# Wiki structure
ls -d _wiki 2>/dev/null
ls _wiki/*.md 2>/dev/null | head -30
ls _wiki/_Sidebar.md _wiki/_Footer.md _wiki/Home.md 2>/dev/null

# Wiki CI workflow
ls .github/workflows/update-wiki.yml 2>/dev/null

# CLAUDE.md sections
grep -n "^#" CLAUDE.md 2>/dev/null | head -20

# docs/ directory
find docs -name "*.md" -maxdepth 2 2>/dev/null | head -20

# Other root-level markdown
ls *.md 2>/dev/null
```

**No-wiki prompt:** If no `_wiki/` found, ask:
> "No wiki structure detected. The recommended setup is a `_wiki/` directory with a GitHub Actions workflow that auto-publishes to the GitHub Wiki on push. Want me to scaffold it?"
- Yes -> run `wiki-documentation-architect` agent, then continue
- No -> configure for just what exists

**C. Standalone template:**

```markdown
---
description: "Docs sync — standalone (generated <date>; upgrade jacked + re-run /jacked-setup to update)"
---
# Generated by /jacked-setup — <date> | Template v1 | Engine: jacked v<VERSION>
# Standalone — no dependencies. Commit this file.
# To update: uv tool install --upgrade claude-jacked && jacked install && /jacked-setup docs-sync

## Repo Config

- **Project**: <name>
- **Base Branch**: <main|master|detected>
- **Stack**: <languages, frameworks>

## Doc Inventory
<list each discovered doc file with detected sections>
Examples:
- README.md (sections: Install, Usage, Features, Config)
- CLAUDE.md (sections: Architecture, Testing, Env Vars)
- _wiki/ (14 pages, has _Sidebar.md, has _Footer.md)
- .github/workflows/update-wiki.yml (wiki CI: active)

## Change-to-Doc Map
| Change Type | Affected Docs |
|------------|---------------|
<dynamically built from what actually exists — rows only appear if target docs exist>

<!-- ENGINE — DO NOT EDIT BELOW THIS LINE -->
---
<engine body from ~/.claude/commands/docs-sync.md, front matter stripped>
```

**Local skill file** at `.claude/skills/docs-sync/SKILL.md`:
```markdown
---
name: docs-sync
description: "Sync docs with code changes — diffs branch, maps to affected docs, spawns parallel update agents. (repo)"
---
Read `.claude/commands/docs-sync.md` and follow it.
```

### 4. Install Loop Generalization — `jacked/cli.py`

**Install (replace lines ~1794-1806):**

Replace the hardcoded `jacked` skill copy with a generic loop:

```python
# Install skills (loop over all subdirectories in data/skills/)
skills_src = pkg_root / "skills"
skills_dst = home / ".claude" / "skills"
if skills_src.exists():
    skill_count = 0
    skipped = 0
    for skill_dir in sorted(skills_src.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            continue
        dst_dir = skills_dst / skill_dir.name
        dst_dir.mkdir(parents=True, exist_ok=True)
        dst_file = dst_dir / "SKILL.md"
        # Same conflict detection as commands/agents
        # (symlink check, hardlink check, content check, --force)
        ...
        _link_or_copy(skill_file, dst_file)  # or shutil.copy
        skill_count += 1
    console.print(f"[green][OK][/green] Installed {skill_count} skills ...")
```

**Uninstall (replace lines ~2119-2125):**

Replace the hardcoded `shutil.rmtree` for `jacked` with:

```python
# Remove only jacked-installed skills
skills_src = pkg_root / "skills"
skills_dst = home / ".claude" / "skills"
if skills_src.exists() and skills_dst.exists():
    skill_count = 0
    for skill_dir in skills_src.iterdir():
        if not skill_dir.is_dir():
            continue
        dst_dir = skills_dst / skill_dir.name
        if dst_dir.exists():
            shutil.rmtree(dst_dir)
            skill_count += 1
    console.print(f"[green][OK][/green] Removed {skill_count} skills")
```

Future skills = just drop a directory in `jacked/data/skills/`. No code changes.

## Non-Goals

- No runtime discovery fallback — setup is required
- No auto-commit — stage and report only
- No auto-creation of missing docs — suggest only
- No changes to existing agents (readme-maintainer, wiki-documentation-architect) — docs-sync orchestrates them as-is
