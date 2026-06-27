---
name: demo-video
description: Use to produce a polished, narrated product DEMO / walkthrough / how-to video for documentation and education — teaching a feature or capability to end users, to embed in docs, a README, a release note, or onboarding. NOT for bug/regression evidence (that's /qa-video). Triggers include "make a demo video", "record a walkthrough", "feature walkthrough for the docs", "how-to video", "show this feature off", "onboarding video", "educational clip for new users".
---

First, check if a repo-scoped version exists in the current project:
1. If `.claude/skills/demo-video/SKILL.md` exists (Glob) → read and follow it instead of this file.
2. If `.claude/commands/demo-video.md` exists (Glob) → read and follow it instead.
3. Otherwise, read `~/.claude/commands/demo-video.md` and follow it.

`/demo-video` is `/qa-video`'s polished cousin. Both record the browser, but the intent is
opposite: `/qa-video` captures a QA session as **bug/regression evidence** for developers;
`/demo-video` produces a **clean, narrated teaching video** for end users — staged data,
deliberate pacing, captions/voiceover, and a reusable narration script committed to the repo
so the video can be regenerated whenever the UI changes. Reach for this when the goal is to
*explain a feature*, not to *prove a bug*.
