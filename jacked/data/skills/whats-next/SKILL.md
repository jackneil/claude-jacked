---
name: whats-next
description: Use when the user asks "what should I work on", "what's next", "what are our priorities", "help me prioritize", "what should we build next", "I'm not sure what to do next", or "where should I start". Analyzes plans, issues, commits, and lifecycle stage to recommend highest-yield next work items.
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/whats-next/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/whats-next.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/whats-next.md` and follow it.
