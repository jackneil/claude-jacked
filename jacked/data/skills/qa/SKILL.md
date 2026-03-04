---
name: qa
description: Browser-based QA testing of UI changes from the current session. Pass a URL as argument, or let it auto-detect.
---

Two commands are available — read the appropriate one and follow it:

- `~/.claude/commands/qa.md` — Focused QA checklist for specific changes (visual, interactive, console, edge cases). Use when testing a targeted fix or feature.
- `~/.claude/commands/ux.md` — Parallel UX checks spawning focused agents per aspect. Use when testing broader UX impact across a page or flow.

Default to `/qa` for single-feature verification. Use `/ux` when changes touch layout, navigation, or multiple components.
