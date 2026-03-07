---
name: qa
description: Browser-based QA testing of UI changes — detects issues, fixes them, and verifies with /dcr until clean.
---

Two commands are available — read the appropriate one and follow it:

- `~/.claude/commands/qa.md` — Quick, focused QA pass (single agent). Visual, interactive, console, and edge case checks on specific changes. Best for targeted fixes or single-feature verification.
- `~/.claude/commands/ux.md` — Thorough parallel UX review (multiple agents). Tests 6 UX aspects across multiple pages simultaneously. Best when changes touch layout, navigation, or multiple components.

Both follow the same end-to-end pattern: detect issues → compile fix plan → execute fixes → /dcr verification until clean pass.

Decision guide:
- Changed button styling or a single component? → `/qa`
- Changed layout, interactions, AND multiple pages? → `/ux`
