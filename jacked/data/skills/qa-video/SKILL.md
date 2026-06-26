---
name: qa-video
description: Use when you want a playable video recording of a QA/test session plus a synced narrated journey — not just screenshots. For regression evidence, "it worked" proof, bug reproduction with motion, or a recorded walkthrough. Pass a URL as argument, or let it auto-detect the running app.
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/qa-video/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/qa-video.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/qa-video.md` and follow it.

`/qa-video` is `/qa`'s sibling: it drives the same kind of UI journey but produces a **playable video record** — true-motion via Playwright MCP's native video (chapter markers), or a frame-stitched MP4/GIF for any other browser tool — plus a video-synced narration doc. On Playwright it also emits an interactive **trace.zip** (per-action DOM + console + network, opened at trace.playwright.dev) as the richest QA evidence. Reach for `/qa` when you just need an issue list; reach for this when you need *evidence in motion*.
