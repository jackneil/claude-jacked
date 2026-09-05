---
description: Transient API/rate-limit error — check where you actually fell off, then resume only what's needed without changing the task
---
The previous turn failed on a transient Anthropic API error or rate limit — almost never your task, plan, or anything you did. The cloud API just blipped.

**First, identify the error class — recovery differs:**
- **529 / "Overloaded" / capacity** → switch model with `/model` (capacity is per-model); don't hammer the same one.
- **Context / compaction error** → run `/compact`, then resume.
- **Account usage / session / weekly limit (a 429 that names a reset)** → NOT a transient blip; an immediate retry won't clear it. Wait for the reset or switch plan-model.

Claude Code already auto-retried ~10x with backoff before this surfaced, so a naive instant re-fire rarely helps — especially on the cases above.

Then check where you actually fell off and act on whichever is true:

1. **You had already finished the work** and were just waiting on me or something external. → Don't redo anything. Confirm it's done and say exactly what you're waiting for.

2. **Inline work** (an edit, command, or tool call) got interrupted. → Verify what actually landed. Trust on-disk / tool-result reality over your last sentence: mid-multi-step work can look finished but be partial, so reconcile what you *claimed* against what committed before declaring resume complete. Resume from the precise point it fell off — do NOT repeat steps that already succeeded.

3. **Spawned subagents or a workflow died.** → Re-launch or resume only those: resume a workflow with resumeFromRunId FIRST (completed agents are cached in the run's journal; only the dead ones re-run) and read `<transcriptDir>/journal.jsonl` before assuming anything was lost; re-dispatch only the agents that actually failed. A review wave in which any agent returned null is INCOMPLETE, never clean: re-run that wave, do not let `filter(Boolean)` convert dead agents into a pass. Near a usage limit, prefer fewer, larger lenses so a single death loses less.

**Before re-running any step with an external side effect** (git commit/push, opening or commenting on a PR, sending a message, writing to an API or DB, a destructive command), confirm it did NOT already land — duplicated side effects are the real damage, not lost compute. For pure reads/edits, just redo if unsure.

**Finish what failed IN KIND — do NOT substitute a weaker check.** Whatever specifically failed must be redone as the same thing it was. You may NOT swap a cheaper, partial, or proxy check for the real one and call it equivalent: a deterministic grep is not the LLM review it replaced, a sample is not the full set, "spot-checks" are not the complete pass, a subset that "looks consistent" does not stand in for the rest. If 30 of 50 dispatched agents died, the task needs 50 real results — not 20 plus a rationalization.

**Partial completion is NOT completion.** Never report "done" (or commit/ship the result) while any dispatched unit of the original work is still failed or unrun. Track the failed set explicitly and drive it to zero: re-run exactly those units, throttling harder (smaller concurrent batches, lower fan-out) if it's a rate limit, looping until none remain. If a unit is genuinely and permanently blocked, surface it LOUDLY by name as incomplete and let the user decide — never fold it silently into a success summary.

**Red flags — these thoughts mean you are about to skip; STOP and redo the failed work instead:**
- "the cheaper / deterministic check covers the important cases"
- "the sample was consistent, so the rest are fine"
- "that's reasonable confidence to ship"
- "I'll note it as a caveat and move on"
- "re-running the rest is just diminishing returns"

Do not change the goal, the plan, or start anything new. If you hit the same error again, back off between attempts rather than re-firing instantly (switch model for capacity errors). Then continue until the original task is genuinely complete and verified.
