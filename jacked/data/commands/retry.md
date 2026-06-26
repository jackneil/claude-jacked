---
description: Transient API/rate-limit error — check where you actually fell off, then resume only what's needed without changing the task
---
The previous turn failed because of a transient Anthropic API error or rate limit. This is almost never a problem with the task, your plan, or anything you did — usually the cloud API just blipped.

**First, identify the error class — recovery differs, so don't treat them all the same:**
- **529 / "Overloaded" / capacity** → switch model with `/model` (capacity is per-model) and keep working, rather than hammering the same one.
- **Context / compaction error** → run `/compact`, then resume.
- **Account usage / session / weekly limit (a 429 that names a reset)** → this is NOT a transient blip; an immediate retry won't clear it. Wait for the reset or switch plan-model.

Claude Code already auto-retried this ~10x with exponential backoff before it surfaced to you, so an instant naive re-fire rarely helps — especially on the cases above.

Then do a quick check of where you actually were when it cut off, and act on whichever of these is true:

1. **You had already finished the work** and were just waiting on me or on something external. → Don't redo anything. Briefly confirm it's done and tell me exactly what you're waiting for.

2. **You were doing inline work** in the main thread (an edit, command, or tool call) that got interrupted. → Verify what actually landed (did the edit apply, did the command run, did the file change). Trust the on-disk / tool-result reality over your own last sentence — a task that was mid-multi-step can look finished but be in a partial state, so reconcile what you *claimed* was done against what actually committed before declaring resume complete. Resume from the precise point it fell off — do NOT repeat steps that already succeeded.

3. **You had spawned subagents or a workflow that died** when the API dropped. → Re-launch or resume only those. Resume a workflow from where it stopped (use resumeFromRunId rather than restarting from scratch); re-dispatch only the agents that actually failed.

**Before re-running any step with an external side effect** (git commit/push, opening or commenting on a PR, sending a message, writing to an API or DB, a destructive command), first confirm it did NOT already land — duplicated side effects are the real damage, not lost compute. For pure reads/edits in the working dir, just redo if unsure.

Do not change the goal, the plan, or start anything new. If you hit the same error again, wait a few seconds and back off between attempts rather than re-firing instantly; for capacity errors prefer switching model over hammering the same one. Then continue until the original task is genuinely complete and verified.
