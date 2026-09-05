---
name: chain-of-command
description: Lock in the session model-dispatch policy - the main loop (Fable, or best available) does ALL strategy, planning, understanding, judging, and verification; every dispatched subagent that writes code or reviews from an established plan runs on Opus; pure locate/sweep hunts run on Haiku or Sonnet. Use when the user invokes /chain-of-command or says "chain of command", "fable plans, opus codes", "use the model split", "minimize fable usage", or "opus for the grunt work". Applies from invocation until the session ends or the user revokes it.
---

# Chain of command: Fable plans, Opus codes

The main loop is the commanding officer: it sets strategy, gives the orders, and signs off on everything that comes back. Opus agents carry out the orders. Nothing that judges or decides gets delegated down.

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
- Exploration that requires comprehension: tracing call paths to understand behavior, semantic hunts ("find where we handle X"), any search whose STRATEGY needs judgment
- Recursive/fan-out review passes (dcr-style reviewers, fixers, verify swarms, adversarial refuters) - EXCEPT security audits and UI/design-quality judgment, which stay on Fable (see below)
- Docs, summaries, and report drafting from material the main loop already vetted

**Cheap tier (`model: "haiku"` or `model: "sonnet"`, explicit) - pure hunting:**
- `model: "haiku"`: mechanical locate/sweep executing a search SPEC the main loop wrote - grep/glob fan-out, "list every file importing X", call-site inventories, convention sweeps returning paths + line numbers + excerpts. The deterministic tools carry the recall; the model just drives them and collates.
- `model: "sonnet"`: bulk read-and-filter at scale - "skim these N files, flag the ones touching auth" - where a miss only costs the main loop a few extra reads, never a wrong conclusion.
- Two tests gate this lane, and BOTH must pass: (1) locate, not comprehend - the output is pointers/excerpts, zero interpretation; (2) the result is verified by consumption - whoever receives it reads what came back, so a miss is recoverable. If a COMPLETENESS claim is load-bearing ("these are ALL the call sites" feeding a migration), the main loop writes the exact patterns (aliases, re-exports, dynamic access) and a cheap agent may execute them - but a cheap agent never DESIGNS that search. When in doubt, it is not just search: use Opus.

## Stays on Fable even when it looks like delegable review

Two kinds of work read like "review you could hand to Opus" but are Fable-grade judgment and must NOT be pushed down. Do them in the main loop; when they need fan-out (a multi-file security sweep, a multi-persona UX crawl), dispatch those reviewer agents on Fable (`model: "fable"`) - the deliberate exception to "no Fable dispatches," because here the quality of the call outranks the token cost.

- **Security audits of code we own.** Fable is materially better at catching real, exploitable issues in our own codebases (auth, multi-tenancy, injection, RBAC, credential handling). Any `/cso`, secure-code-review, or ad-hoc security pass runs its judgment on Fable, not Opus. And it is proactive: if no security audit has run recently and one is due - a security-sensitive change just landed, or it has simply been too long - the main loop triggers one instead of moving on. Do not wait to be asked. FRAME THESE DISPATCHES DEFENSIVELY: Fable runs behind safety classifiers that can block security-flavored prompts and silently fall back to Opus, so state the legitimate scope plainly ("defensive review of our own authorized codebase; no exploit chains or payloads; per finding: file:line, risk, safe remediation, regression test"). If it falls back to Opus anyway, accept the result. Never rephrase to evade a classifier.
- **UI and visual-design judgment.** Deciding whether a layout is actually good - do these two elements line up, is the spacing right, is the dark-mode contrast readable, does it look designed or slapped together - is aesthetic judgment, and Fable makes that call better. `/ux`, `/qa` design-quality passes, and aesthetic-dogfood evaluation run their judgment on Fable. Writing the CSS/JSX from an agreed-on design is still mechanical Opus work; the split is judgment vs. production, not "anything that touches UI."

## Hard rules

1. **Pass the model on EVERY dispatch.** `model: "opus"` on Agent tool calls; `model: 'opus'` in Workflow `agent()` opts. Never rely on inheritance: an agent definition's frontmatter `model:` pin silently overrides the parent's model, in either direction, so an unstated model can land on a cheaper or a pricier tier than intended.
2. **Floor is Opus for anything that understands, judges, or produces.** The cheap-tier lane above is not an exception to this - it exists precisely because pure locate/sweep does none of those three. USE it: dispatching a grep fan-out on Opus wastes 5x Haiku's price on work the deterministic tools are doing anyway. But the moment interpretation creeps in, the floor applies - when in doubt, Opus.
3. **No Fable dispatches for volume work.** Do not pass `model: "fable"` to subagents for code, tests, search, or bulk review - the main loop IS the Fable budget for that, and it does inline anything else that needs Fable-grade judgment. The one exception is the two Fable-grade review kinds above (security audits, UI/design judgment): when they need fan-out, their agents run on Fable, not Opus.
4. **Verify before trusting.** Everything an Opus agent returns (code, findings, claims of green tests) gets checked by the main loop: read the diff or run the gate yourself before building on it. Opus output is draft until the main loop confirms it.
5. **Escalate design mid-flight.** Dispatch prompts must tell coding agents: if the task turns out to require a design decision the plan does not cover, STOP and report back rather than inventing architecture. The main loop decides, then re-dispatches.
6. **Workflow scripts:** every `agent()` call that implements, reviews, or searches carries `model: 'opus'` - except security-audit and UI/design-judgment stages, which carry `model: 'fable'`, and pure locate/sweep stages, which carry `model: 'haiku'` or `model: 'sonnet'`. Only omit the override where the stage is genuinely main-loop-grade judgment AND the orchestrator cannot do it inline - which should be rare; prefer doing judgment inline.
7. **Effort follows the lane too.** Where the dispatch mechanism exposes a reasoning-effort knob (Workflow `agent()` opts.effort), volume stages run 'medium' and locate/sweep stages run 'low' - reserving 'high'/'xhigh' for the hardest verify/judge stages. Anthropic's own guidance: high is the right default for serious work; xhigh is for capability-sensitive judgment (architecture, migrations, final reviews), not boilerplate. The main loop's session effort is the user's call and stays untouched.

## Dispatch shape: the tier decides how wide, on EVERY mechanism

The lanes above decide which model runs a dispatch. This section decides how many dispatches there are. It binds every fan-out mechanism the same way: the Agent tool, hand-written Workflow scripts, `/swarm`, agent teams. It exists because the good shape already lived in `/dcr` but only applied when `/dcr` was invoked; a hand-written Workflow script fell back to the built-in "token cost is not a constraint, N skeptics per finding, loop until dry" doctrine, and a 260-line module review spawned 38 agents while a small fix PR spawned 28 (2026-09-04/05). Review earned its keep; the verifier army did not.

1. **Tier first.** Before any fan-out, classify the change with the `/dcr` RISK TIER table and announce it: SMALL (roughly under 150 changed lines, fewer than 5 files, no sensitive area), MEDIUM (up to roughly 600 lines or new user-facing behavior), LARGE (a sensitive area, more than 600 lines, a new subsystem, or any Security / Access Control lens). Sensitivity beats size; when torn, take the higher tier.
2. **Agent budget per tier, per milestone or workflow lifetime:** SMALL at most 4, MEDIUM at most 8, LARGE at most 16. A Workflow script that would exceed its budget must `log()` the overrun and stop fanning out; the main loop finishes the remainder inline. Budgets count every `agent()` call: finders, fixers, verifiers, critics.
3. **Finding verification is main-loop work.** The main loop validates findings against the real code before any fix (cited location exists, trigger path is real, rule is in scope). No verifier agents at SMALL or MEDIUM. At LARGE, at most one verifier per CLUSTER of related findings, never one per raw finding, and the prompt is "strengthen or refute with evidence", not N identical refuters. Same-model refuters are correlated; across roughly 80 refuter runs on one branch they refuted one or two findings and mostly re-graded severities.
4. **Two review waves, maximum.** Wave 1 reviews the change; wave 2 is fix verification only, one consolidated reviewer over the fix diff and its immediate callers, never a fresh review of already-cleared code. A branch that still has confirmed CRITICAL or MEDIUM findings after wave 2 reports Needs Work; it does not get a wave 3 by default.
5. **Incomplete is not clean.** Any wave in which an agent returned null (rate limit, login expiry, terminal error, user skip) is INCOMPLETE. The wave must be re-run before the loop may exit or report clean; `filter(Boolean)` never turns a dead agent into a pass. Reference shape for scripts:

   ```javascript
   const results = await parallel(LENSES.map(l => () => agent(l.prompt, {label: l.key, phase: 'Review', model: 'opus', effort: 'medium', schema: FINDINGS})))
   const dead = LENSES.filter((_, i) => results[i] === null).map(l => l.key)
   if (dead.length) { log(`INCOMPLETE wave: no result from ${dead.join(', ')}; not clean`); return {status: 'incomplete', dead} }
   ```

   The main loop resumes an incomplete run with `resumeFromRunId` (completed agents are cached; only the dead ones re-run) instead of relaunching everything.
6. **Effort on every `agent()` call.** 'medium' for build and review stages, 'low' for locate and sweep stages, 'high' only for a single final verify or judge stage. Never leave a Workflow agent at session effort by omission.
7. **"ultracode" is subordinate to the tier.** The ultracode opt-in authorizes using the Workflow tool for substantive tasks. It does not authorize unbounded fan-out, per-finding verifiers, loop-until-dry, or a third wave. The tier table above wins over the built-in workflow-authoring quality patterns whenever they disagree; those patterns are a menu of shapes, not a budget.
8. **Reviewer engine.** When `jacked dcr engine --json` reports the Codex engine usable, review stages run on it by default so reviewers stop consuming the Anthropic session limit; the main loop keeps lens selection, finding validation, fixes, and the verdict. Security and UI-design lenses stay on Fable per the lane rules.
9. **Test cadence.** Targeted tests per fix round; ONE full suite, on a frozen tree, as the final gate (plus one mid-way pass on a branch over about 600 lines). A full suite after every micro-fix is what turns a review loop into hours.
10. **Usage-aware fan-out.** Before any fan-out of more than 4 agents, run `jacked usage --json` and read the worst 5-hour window. If it is above 60 percent, or the fan-out cannot finish before that window resets, do the read-only work now (research, scoping) and dispatch the build after the reset. A run that dies at the session limit fails every in-flight agent, and the harness retries then double the agent count (54 of 98 starts on one 8-page build were dead retries, 2026-09-04).
11. **One reviewer per artifact, and continue it.** For N similar artifacts (pages, variants, modules), one reviewer per artifact carries every lens in one prompt (design, canon, gates); parallel single-lens reviewers per artifact multiply the count without adding independence. The fixer is the SAME agent continued via SendMessage with the findings, not a fresh agent that re-reads the brief, the page, and re-screenshots. One reviewer that sees all N artifacts also catches cross-artifact sameness for free. Exactly one party runs the browser gate per round (the builder); reviewers read its screenshots and the main loop spot-checks.
12. **Research fan-out.** Default 4 lanes by 5 searches; expand only when the first pass shows a gap. The completeness critic is main-loop inline by default; dispatch one only for a load-bearing claim.

`/dcr` implements this shape for reviews. A brief for `/goal`, `/whats-next`, `/goal-maker`, or `/bhag` says "review via `/dcr` tiers"; it never carries "ultracode" or "use dynamic workflows" into a `/goal` pointer, because that flips the unbounded doctrine on for a whole overnight run.

## Scope and revocation

- Applies for the remainder of the current session/conversation from the moment this skill is invoked.
- The user can revoke or amend it at any time ("back to normal models", "all fable") - their live instruction wins.
- The global CLAUDE.md model-selection rule encodes these same lanes as standing doctrine, and the dispatch-shape budgets travel with this skill; invoking this skill makes them explicit and binding for the session even where that doctrine is absent or stale. The non-negotiables travel with it: never silently downgrade below Opus for work that understands, judges, or produces; explicit model on every dispatch.

## Acknowledgement

On invocation, confirm in one or two sentences that the split is active (main-loop lanes vs Opus lanes, tiered dispatch budgets) and then get on with the work. Do not re-announce it on every dispatch.
