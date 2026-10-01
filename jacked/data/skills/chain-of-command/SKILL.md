---
name: chain-of-command
description: Lock in the session model-dispatch policy - the main loop (Fable, or best available) does ALL strategy, planning, understanding, judging, and verification; every dispatched subagent that writes code or reviews from an established plan runs on Opus; pure locate/sweep hunts run on Haiku or Sonnet. Use when the user invokes /chain-of-command or says "chain of command", "fable plans, opus codes", "use the model split", "minimize fable usage", or "opus for the grunt work". Applies from invocation until the session ends or the user revokes it.
---

# Chain of command: Fable plans, Opus codes

The main loop judges and decides; agents do volume work on the cheapest capable lane, inside a per-tier budget. Binds every dispatch for the rest of the session.

## Budget first

The tier decides how many agents run, on every fan-out mechanism: the Agent tool, Workflow scripts, `/swarm`, agent teams.

1. **Tier first.** Before any fan-out, classify with the `/dcr` RISK TIER table and announce it: SMALL (under ~150 changed lines, fewer than 5 files, no sensitive area), MEDIUM (up to ~600 lines or new user-facing behavior), LARGE (a sensitive area, over 600 lines, a new subsystem, or any Security / Access Control lens). Sensitivity beats size; when torn, take the higher tier.
2. **Agent budget per tier, per milestone or workflow lifetime: SMALL at most 4, MEDIUM at most 8, LARGE at most 16.** Count every `agent()` call and Agent dispatch: finders, fixers, verifiers, critics. A script that would exceed it `log()`s the overrun and stops fanning out; the main loop finishes inline.
3. **Finding verification is main-loop work.** The main loop checks each finding against the real code before any fix (location exists, trigger is real, rule in scope). No verifier agents at SMALL or MEDIUM. At LARGE, at most one verifier per CLUSTER of related findings, never one per raw finding, prompted "strengthen or refute with evidence"; LOW findings go to the main loop unverified. Two blind refuters per finding is for CONTESTED findings only; same-model refuters are correlated.
4. **Two review waves, maximum.** Wave 1 reviews the change; wave 2 is fix verification only (one consolidated reviewer over the fix diff and its immediate callers). A review loop converges: it stops when a wave yields no branch-introduced CRITICAL or MEDIUM and no critical pre-existing one, filing the rest. Confirmed CRITICAL or MEDIUM after wave 2 reports Needs Work, no default wave 3; a third wave that still finds branch-introduced defects means the fixes are too broad, so split the PR instead of reviewing again.
5. **Incomplete is not clean.** A wave where any agent returned null (rate limit, login expiry, error, user skip) is INCOMPLETE and re-runs before the loop exits or reports clean; `filter(Boolean)` never turns a dead agent into a pass. Resume with `resumeFromRunId` (only dead agents re-run).

   ```javascript
   const results = await parallel(LENSES.map(l => () => agent(l.prompt, {label: l.key, phase: 'Review', model: 'opus', effort: 'medium', schema: FINDINGS})))
   const dead = LENSES.filter((_, i) => results[i] === null).map(l => l.key)
   if (dead.length) { log(`INCOMPLETE wave: no result from ${dead.join(', ')}; not clean`); return {status: 'incomplete', dead} }
   ```
6. **"ultracode" is subordinate to the tier.** ultracode, "token cost is not a constraint", and the workflow-authoring quality patterns (loop-until-dry, N refuters per finding, judge panels) are a menu of shapes, never a budget; this section wins whenever they disagree. ultracode authorizes the Workflow tool, not unbounded fan-out, per-finding verifiers, or a third wave. Exhaustive on the FIRST pass of any task; convergent after that.
7. **Briefs.** A `/goal`, `/whats-next`, `/goal-maker`, or `/bhag` brief says "review via `/dcr` tiers". It never carries "ultracode" or "use dynamic workflows" into a `/goal` pointer; that turns the unbounded doctrine on for a whole overnight run.

## The lanes

**Main loop (session model, Fable when available), never delegated:** understanding, strategy, architecture, the plan, dispatch prompts, judging and verifying everything that returns, gate decisions, anything ambiguous or novel, escalations.

**Opus (`model: "opus"`), volume work:** code from an established plan; tests, refactors, migrations, fixtures; exploration that needs comprehension (tracing call paths, semantic hunts like "find where we handle X", searches whose strategy needs judgment); bulk review fan-out except the Fable kinds below; docs and reports from vetted material.

**Cheap tier, pure hunting:** `model: "haiku"` for mechanical locate/sweep executing a search spec the main loop wrote (grep/glob fan-out, call-site inventories, convention sweeps); `model: "sonnet"` for bulk read-and-filter ("skim these N files, flag the ones touching auth"). Both tests must pass: (1) the output is pointers and excerpts, zero interpretation; (2) the consumer reads it, so a miss is recoverable. For a load-bearing COMPLETENESS claim, the main loop writes the exact patterns (aliases, re-exports, dynamic access); a cheap agent may execute them but never designs that search. When in doubt, it is not just search: use Opus.

**Stays on Fable** (inline, or `model: "fable"` when it needs fan-out):
- **Security audits of code we own** (`/cso`, secure-code-review, ad-hoc passes). Proactive: when a security-sensitive change landed or no audit ran recently, trigger one unasked. Frame dispatches defensively (Fable's classifiers can block security-flavored prompts and fall back to Opus): "defensive review of our own authorized codebase; no exploit chains or payloads; per finding: file:line, risk, safe remediation, regression test". If it falls back to Opus anyway, accept the result; never rephrase to evade a classifier.
- **UI and visual-design judgment** (alignment, spacing, contrast, designed vs slapped together): `/ux`, `/qa` design passes, aesthetic-dogfood. CSS/JSX from an agreed design is Opus work.

## Hard rules

1. **Explicit model on EVERY dispatch**, Agent calls and Workflow `agent()` opts alike, set to the lane's model. Never rely on inheritance: a frontmatter `model:` pin in the agent definition silently overrides the parent, either direction. Omit it only for a Workflow stage of main-loop-grade judgment the orchestrator cannot do inline (rare).
2. **Floor is Opus for anything that understands, judges, or produces.** Use the cheap lane for pure locate/sweep (Opus on a grep fan-out pays 5x for work the tools do); once interpretation creeps in, the floor applies.
3. **No Fable dispatches for volume work** (code, tests, search, bulk review); only the two Fable kinds above fan out on Fable.
4. **Effort on every `agent()` call.** 'medium' for build and review, 'low' for locate and sweep, 'high' or 'xhigh' only for a single final verify or judge stage. Never leave a Workflow agent at session effort by omission; the main loop's own effort is the user's call.
5. **Verify before trusting.** Agent output (code, findings, "tests green") is draft until the main loop reads the diff or runs the gate itself.
6. **Escalate design mid-flight.** Coding-agent prompts say: if the task needs a design decision the plan does not cover, STOP and report back. The main loop decides, then re-dispatches.
7. **Usage-aware fan-out.** Before a fan-out of more than 4 agents, run `jacked usage --json` and read the worst 5-hour window. Above 60 percent, or if the fan-out cannot finish before that window resets, do read-only work now and dispatch the build after the reset.
8. **One reviewer per artifact, and continue it.** For N similar artifacts, one reviewer per artifact carries every lens in one prompt. The fixer is the SAME agent continued via SendMessage with the findings, never a fresh one. One party runs the browser gate per round (the builder); reviewers read its screenshots and the main loop spot-checks.
9. **Research fan-out.** Default 4 lanes by 5 searches; expand only when the first pass shows a gap. The completeness critic is main-loop inline unless a claim is load-bearing.
10. **Scope and provenance on every review dispatch.** The prompt states the branch's scope in one sentence and requires `introduced_by_branch: true|false` on every finding. Branch-introduced defects are fixed in the PR, always. Pre-existing ones are fixed in the PR only when security-, data-integrity-, or billing-critical, or a one-line change; otherwise filed as an issue with the file:line evidence and linked from the PR. A user's CLAUDE.md that says otherwise wins.
11. **Reviewer engine.** When `jacked dcr engine --json` reports the Codex engine usable, review stages run on it by default; the main loop keeps lens selection, finding validation, fixes, and the verdict. Security and UI-design lenses stay on Fable.
12. **Test cadence.** Targeted tests per fix round; ONE full suite on a frozen tree as the final gate (plus one mid-way pass on a branch over ~600 lines). The full-suite gate is per PUSH, not per fix batch.

## Scope and revocation

Applies from invocation until the session ends; the user can revoke or amend it live ("back to normal models", "all fable") and their instruction wins. The global CLAUDE.md model-selection rule encodes the same lanes; this skill makes them and the budget binding even where that rule is absent or stale.

## Acknowledgement

On invocation, confirm in one or two sentences that the split and the tiered budget are active, then get on with the work. Do not re-announce it on every dispatch.
