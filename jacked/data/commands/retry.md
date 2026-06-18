---
description: Transient API/rate-limit error — check where you actually fell off, then resume only what's needed without changing the task
---
The previous turn failed because of a transient Anthropic API error or rate limit. This is NOT a problem with the task, your plan, or anything you did — the cloud API just blipped.

Before reacting, do a quick check of where you actually were when it cut off, then act on whichever of these is true:

1. **You had already finished the work** and were just waiting on me or on something external. → Don't redo anything. Briefly confirm it's done and tell me exactly what you're waiting for.

2. **You were doing inline work** in the main thread (an edit, command, or tool call) that got interrupted. → Verify what actually landed (did the edit apply, did the command run, did the file change). Resume from the precise point it fell off — do NOT repeat steps that already succeeded.

3. **You had spawned subagents or a workflow that died** when the API dropped. → Re-launch or resume only those. Resume a workflow from where it stopped (use resumeFromRunId rather than restarting from scratch); re-dispatch only the agents that actually failed.

Do not change the goal, the plan, or start anything new. If you hit the same API/rate-limit error again, wait a moment and try again — keep retrying until it goes through. Then continue until the original task is genuinely complete and verified.
