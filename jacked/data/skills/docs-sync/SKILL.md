---
name: docs-sync
description: Use when a branch has code changes that may have made documentation stale, after completing a feature, or before creating a PR. Diffs branch against base and maps changes to affected docs.
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/docs-sync/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/docs-sync.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/docs-sync.md` and follow it.
