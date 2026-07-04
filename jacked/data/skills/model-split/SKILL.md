---
name: model-split
description: Lock in the session model-dispatch policy - the main loop (Fable, or best available) does ALL strategy, planning, understanding, judging, and verification; every dispatched subagent that searches or writes code from an established plan runs on Opus. Use when the user invokes /model-split or says "fable plans, opus codes", "use the model split", "minimize fable usage", or "opus for the grunt work". Applies from invocation until the session ends or the user revokes it.
---

# Model split: Fable plans, Opus codes

From this point forward in THIS session, follow this dispatch policy on every agent/workflow dispatch. It exists to concentrate the expensive top-tier model (Fable) where judgment lives and push volume work (recursion, code emission, bulk review passes) to Opus.

## The two lanes

**Main loop (session model, Fable when available) - never delegated:**
- Understanding the problem, reading the load-bearing code needed to plan
- Strategy, architecture, and the written plan
- Decomposing work into dispatches and writing the dispatch prompts
- Judging and verifying everything that comes back before trusting it
- Final review synthesis, gate decisions (ship / fix / re-run), and anything ambiguous or novel
- Any decision a subagent escalates

**Opus (`model: "opus"`, passed EXPLICITLY on every dispatch) - all volume work:**
- Writing or editing code from an established plan or spec
- Writing tests from a spec, mechanical refactors, migrations, fixture generation
- Searching/exploring the codebase (locating files, mapping conventions, tracing call paths)
- Recursive/fan-out review passes (dcr-style reviewers, fixers, verify swarms, adversarial refuters)
- Docs, summaries, and report drafting from material the main loop already vetted

## Hard rules

1. **Pass the model on EVERY dispatch.** `model: "opus"` on Agent tool calls; `model: 'opus'` in Workflow `agent()` opts. NEVER rely on inheritance: an agent definition's frontmatter `model:` pin silently BEATS parent inheritance (this burned us 2026-07-02 with a stale `model: opus` pin; the same mechanism can silently upgrade OR downgrade). Explicit beats assumed, both directions.
2. **Floor is Opus.** Nothing that understands, judges, or produces runs below Opus. The one carve-out: a pure locate-only lookup (grep/glob "where is X", zero interpretation) may use a cheaper model per the standing CLAUDE.md rule - but when in doubt, it is not "just search"; use Opus.
3. **No Fable dispatches.** Do not pass `model: "fable"` to subagents while this policy is active - the main loop IS the Fable budget. If a piece of work genuinely needs Fable-grade judgment, the main loop does that piece itself instead of dispatching it.
4. **Verify before trusting.** Everything an Opus agent returns (code, findings, claims of green tests) gets checked by the main loop: read the diff or run the gate yourself before building on it. Opus output is draft until the main loop confirms it.
5. **Escalate design mid-flight.** Dispatch prompts must tell coding agents: if the task turns out to require a design decision the plan does not cover, STOP and report back rather than inventing architecture. The main loop decides, then re-dispatches.
6. **Workflow scripts:** every `agent()` call that implements, reviews, or searches carries `model: 'opus'`. Only omit the override where the stage is genuinely main-loop-grade judgment AND the orchestrator cannot do it inline - which should be rare; prefer doing judgment inline.

## Scope and revocation

- Applies for the remainder of the current session/conversation from the moment this skill is invoked.
- The user can revoke or amend it at any time ("back to normal models", "all fable") - their live instruction wins.
- This policy overrides the global "pass model: fable on every dispatch" CLAUDE.md rule for the session, on the user's explicit invocation - that rule's intent (never silently downgrade below Opus, explicit model on every dispatch) stays fully in force.

## Acknowledgement

On invocation, confirm in one or two sentences that the split is active (main-loop lanes vs Opus lanes) and then get on with the work. Do not re-announce it on every dispatch.
