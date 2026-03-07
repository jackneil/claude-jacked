---
name: qa
description: Browser-based QA testing of UI changes — returns a detailed issue list for the caller to plan fixes.
---

Two commands are available — read the appropriate one and follow it:

- `~/.claude/commands/qa.md` — Quick, focused QA pass (single agent). Visual, interactive, console, and edge case checks on specific changes. Best for targeted fixes or single-feature verification.
- `~/.claude/commands/ux.md` — Thorough parallel UX review (multiple agents). Tests 6 UX aspects across multiple pages simultaneously. Best when changes touch layout, navigation, or multiple components.

Both are **read-only detection tools** — they return a detailed issue list but do NOT fix code. After receiving findings, use `superpowers:writing-plans` to build a fix plan from the issues, let the user iterate, then execute with `/dcr` verification.

Decision guide:
- Changed button styling or a single component? → `/qa`
- Changed layout, interactions, AND multiple pages? → `/ux`
