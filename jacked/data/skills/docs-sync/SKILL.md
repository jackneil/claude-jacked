---
name: docs-sync
description: "Sync docs with code changes — diffs branch against base, maps changes to affected docs, spawns parallel update agents. Use when a branch has code changes that may have made documentation stale, after completing a feature, or before creating a PR."
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/docs-sync/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/docs-sync.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/docs-sync.md` and follow it.
