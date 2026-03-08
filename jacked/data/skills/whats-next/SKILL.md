---
name: whats-next
description: Roadmap advisor for any repo — reads plans, issues, commits, and lifecycle stage to recommend the highest-yield next work items. Use when the user asks "what should I work on", "what's next", "what are our priorities", "help me prioritize", "what should we build next", "I'm not sure what to do next", or "where should I start". Run `/jacked-setup whats-next` for faster repeat runs.
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/whats-next/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/whats-next.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/whats-next.md` and follow it.
