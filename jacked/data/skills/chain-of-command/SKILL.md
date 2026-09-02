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

## Scope and revocation

- Applies for the remainder of the current session/conversation from the moment this skill is invoked.
- The user can revoke or amend it at any time ("back to normal models", "all fable") - their live instruction wins.
- The global CLAUDE.md model-selection rule encodes these same lanes as standing doctrine; invoking this skill makes them explicit and binding for the session even where that doctrine is absent or stale. The non-negotiables travel with it: never silently downgrade below Opus for work that understands, judges, or produces; explicit model on every dispatch.

## Acknowledgement

On invocation, confirm in one or two sentences that the split is active (main-loop lanes vs Opus lanes) and then get on with the work. Do not re-announce it on every dispatch.
