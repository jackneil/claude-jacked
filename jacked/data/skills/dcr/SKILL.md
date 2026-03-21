---
name: dcr
description: "Parallel recursive review — selects relevant lenses, spawns focused reviewers per wave until all selected lenses pass clean. Use after implementing a feature, fixing a bug, or completing any non-trivial code change."
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/dcr/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/dcr.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/dcr.md` and follow it.
