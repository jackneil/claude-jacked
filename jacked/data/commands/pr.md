---
description: Use when checking PR status, creating a PR, or managing the pull request workflow for the current branch. Conforms to the repo's own PR template, derives a Conventional-Commits title when the repo uses one, flags breaking changes, writes a release-note line, and can open a draft PR with post-create CI checks.
---

Use the **pr-workflow-checker** agent to analyze the current branch state and handle PR creation or updates. It owns pre-flight verification (stashes, worktrees, untracked files, `[gone]` branches, MEMORY.md freshness — verify before cleaning, never auto-delete), the four branch-state decision logic, the lint / `/dc` / `/docs-sync` / guardrail gates, and aggressive issue linking. Let it drive — these requirements layer ON TOP of what it produces and take precedence over its built-in description structure and tone.

## Arguments

`$ARGUMENTS` may contain `draft` — when present, create the PR as a draft (work-in-progress) instead of a ready-for-review PR.

## 1. Conform to the repo's PR template (highest signal)

Before writing any description, look for the repo's own template and FILL IT OUT instead of emitting the built-in structure. Check, in order:

- `.github/PULL_REQUEST_TEMPLATE.md` / `.github/pull_request_template.md`
- `docs/PULL_REQUEST_TEMPLATE.md` / `docs/pull_request_template.md`
- `PULL_REQUEST_TEMPLATE.md` at the repo root
- any file under a `.github/PULL_REQUEST_TEMPLATE/` directory

If one exists, the repo template is the source of truth — a reviewer recognizes a well-formed PR by it. Preserve its exact sections, checkboxes, and `<!-- HTML comment -->` guidance (fill the answers in around the comments; don't strip the comments unless the template tells you to), and answer every section honestly from the diff and commits. Only when NO template is found do you fall back to the built-in template below.

## 2. Built-in fallback template (no repo template found)

```markdown
## Summary
[2-3 sentences: what this PR does and why it matters]

## Changes
- Specific changes with file paths; explain the non-obvious ones

## Breaking Changes
[None — or list each: removed/renamed public API, env var, CLI flag, entry point,
 or config/schema change, with the migration path]

## Fixed Issues
Fixes #XX
[Be aggressive about linking. Closes/Fixes/Resolves are case-insensitive, one keyword
 per line, and auto-close only on merge to the default branch.]

## Test Plan
[How to verify it works — commands plus expected outcome]

## Release Note
- [One-line changelog entry, e.g. `Added caching for guidance files (#31)`.
   Omit this section for internal-only changes with no user-visible impact.]
```

## 3. Detect breaking changes from the diff

Inspect `git diff main...HEAD` and populate Breaking Changes honestly — never leave it blank by default. Treat as breaking: removed or renamed public functions/classes/exports, changed public signatures, removed/renamed environment variables or CLI flags, changed entry points (`pyproject.toml` / `package.json` / `setup.py`), and migration-requiring config or schema changes. An unflagged breaking change can merge and silently break downstream consumers, so write "None" only after actually checking.

## 4. Conventional-Commits title

First detect whether the repo already uses Conventional Commits — scan recent merged PR titles (`gh pr list --state merged --limit 20 --json title`) and commit subjects (`git log --oneline -30`).

- If it DOES: derive a type prefix from the commits/diff (`feat` / `fix` / `docs` / `refactor` / `chore` / `test` / `perf`) and write a clean conventional title, e.g. `feat: add caching for guidance files (#31)`. The title is commonly the squash-merge subject, so it should read as one. Append the issue number when only 1-2 issues are linked.
- If it does NOT: match the repo's existing title style. Don't force a `type:` prefix onto a project that doesn't use one.

## 5. Fast-seed, then refine

For a quick first draft, seed the title and body from the branch commits with `gh pr create --fill-first` (or `--fill`), then refine the body to satisfy sections 1-4 via `gh pr edit`. Skip the seed when you already have the full description prepared.

## 6. Draft support and post-create CI awareness

- If `draft` was requested, create with `gh pr create --draft`.
- After creating OR updating a PR, run `gh pr checks <number>` and report the result. If checks are still running, say so. If any failed, surface the failing check names and offer to investigate — do not report success on a red PR.

## 7. Tone and identity — neutral and portable by default

This command ships to many repos and many users, so write PR titles, descriptions, and commit messages in a neutral, professional voice by default — no profanity, no hardcoded GitHub handle, no personal branch-naming scheme.

- Read any tone, identity, or branch-naming preference from the project's `CLAUDE.md` (or a project settings key) and honor it if present.
- For branch names, match the repo's existing convention inferred from `git branch -a` and recent history; don't impose a personal pattern.
- The agent's built-in informal tone and any user-specific handle are overridden here unless the project config explicitly opts into them.
